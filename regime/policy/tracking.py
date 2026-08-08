"""把稀疏的关键位快照重建并聚合成有生命周期的机会。

setup_history 只保存价格处在 regime 认可区间内的 4h bar；middle 不落库。
本模块一半负责严格 as-of 的逐根装配，另一半把这些稀疏行折叠成 run。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Callable

import numpy as np
import pandas as pd

from regime.classify import RULES_VERSION
from regime.policy import assemble


TRACKING_VERSION = "trk1"
SETUP_GAP_BARS = 1       # 起步值，待校准；允许短暂离区一根而不切断同一机会
GAP_TREND_WINDOW = 4     # 起步值，待校准；约 16h，压低单根 cRSI 抖动的影响
GAP_SLOPE_EPS = 1.0      # 起步值，待校准；每根 4h 至少变化 1 带位点才认趋势
BAR_4H_MS = 4 * 3_600_000
VOL1H_LOOKBACK_HOURS = 260  # 数据上下文；镜像 dashboard 既有 POC 240h + 20h 余量

# schema 契约把 regime 判定代际记作 rules_version；装配层另有独立 asm 代际。
RULES_GENERATION = RULES_VERSION

_VALID_AT = {"at_support", "at_resistance"}
_TF_MS = dict(assemble.TF_SEC)


def _ts_ms(values: pd.Series) -> np.ndarray:
    if pd.api.types.is_numeric_dtype(values):
        raw = pd.to_numeric(values, errors="raise").to_numpy(dtype="int64")
        return raw if not len(raw) or np.max(np.abs(raw)) < 10**15 else raw // 10**6
    return (pd.to_datetime(values, utc=True).astype("int64") // 10**6).to_numpy()


def _closed_prefix(frame: pd.DataFrame | None, tf: str, asof_ms: int) -> pd.DataFrame:
    """先按收线时刻截断，再保留与 dashboard 相同的 400 根计算上下文。"""
    if frame is None or not len(frame):
        return pd.DataFrame() if frame is None else frame.iloc[:0].copy()
    times = _ts_ms(frame["ts"])
    closed = times + _TF_MS[tf] * 1000 <= int(asof_ms)
    return (
        frame.loc[closed]
        .tail(assemble.POLICY_SIGNAL_OHLCV_LIMIT)
        .reset_index(drop=True)
    )


def _vol_prefix(vol1h: pd.DataFrame | None, target_ts: int,
                asof_ms: int) -> pd.DataFrame | None:
    if vol1h is None:
        return None
    if not len(vol1h):
        return vol1h.copy()
    times = _ts_ms(vol1h["ts"])
    # dashboard 当前口径从 4h 开盘向前取 260h；这里照同一范围，而不是给历史
    # 装配更多上下文。260=POC 240h + 20h 余量，不是新的交易门槛。
    start = int(target_ts) - VOL1H_LOOKBACK_HOURS * 3_600_000
    keep = (times >= start) & (times < int(asof_ms))
    return vol1h.loc[keep].reset_index(drop=True)


def _timeline_arrays(timeline) -> tuple[np.ndarray, list]:
    if timeline is None:
        return np.asarray([], dtype="int64"), []
    if isinstance(timeline, pd.DataFrame):
        if not len(timeline):
            return np.asarray([], dtype="int64"), []
        values = timeline["state"].tolist()
        times = _ts_ms(timeline["ts"])
    else:
        rows = list(timeline)
        if not rows:
            return np.asarray([], dtype="int64"), []
        times = np.asarray([int(row["ts"]) for row in rows], dtype="int64")
        values = [row.get("state") for row in rows]
    order = np.argsort(times, kind="stable")
    return times[order], [values[int(i)] for i in order]


def _state_asof(times: np.ndarray, values: list, tf: str, asof_ms: int):
    if not len(times):
        return None
    # regime_history.ts 也是开盘时刻；只有该 bar 已收线才可供本时点使用。
    eligible = times + _TF_MS[tf] * 1000 <= int(asof_ms)
    indexes = np.flatnonzero(eligible)
    return values[int(indexes[-1])] if len(indexes) else None


def _setup_row(symbol: str, ts: int, payload: Mapping[str, Any],
               rules_version: str, assemble_version: str) -> dict | None:
    location = payload.get("location") or {}
    if location.get("at") not in _VALID_AT:
        return None
    zone = location.get("zone") or {}
    by_tf = payload.get("crsi_by_tf") or {}
    resonance = payload.get("resonance") or {}

    def crsi(tf: str, key: str):
        value = (by_tf.get(tf) or {}).get(key)
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if np.isfinite(number) else None

    kinds = zone.get("kinds") or []
    if isinstance(kinds, str):
        kinds = [kinds]
    return {
        "symbol": symbol,
        "ts": int(ts),
        "regime_4h": payload.get("regime_4h"),
        "regime_1d": payload.get("regime_1d"),
        "at": location.get("at"),
        "zone_lo": zone.get("lo"),
        "zone_hi": zone.get("hi"),
        "zone_kinds": list(kinds),
        "dist_atr": location.get("dist_atr"),
        "approach": location.get("approach"),
        "tradeable": location.get("tradeable"),
        "meaning": location.get("meaning"),
        "role_flipped": location.get("role_flipped"),
        "crsi_4h": crsi("4h", "crsi"),
        "crsi_1d": crsi("1d", "crsi"),
        "crsi_1h": crsi("1h", "crsi"),
        "crsi_pos_4h": crsi("4h", "pos"),
        "crsi_pos_1d": crsi("1d", "pos"),
        "crsi_pos_1h": crsi("1h", "pos"),
        "main_ok": resonance.get("main_ok", payload.get("signal_ok")),
        "score": resonance.get("score"),
        "grade": resonance.get("grade"),
        "play": payload.get("play"),
        "price": payload.get("price"),
        "atr": payload.get("atr"),
        "rules_version": rules_version,
        "assemble_version": assemble_version,
    }


def walk_forward(
    symbol: str,
    *,
    ohlcv_by_tf: Mapping[str, pd.DataFrame | None],
    regime_by_tf: Mapping[str, Sequence[dict] | pd.DataFrame | None],
    instrument: Mapping[str, Any] | None,
    target_ts: Sequence[int] | None = None,
    existing_ts: set[int] | None = None,
    vol1h: pd.DataFrame | None = None,
    rules_version: str = RULES_GENERATION,
    assemble_version: str = assemble.ASSEMBLE_VERSION,
    assemble_fn: Callable | None = None,
) -> tuple[list[int], list[dict]]:
    """严格 walk-forward 装配目标 4h bar，返回 ``(已处理 ts, setup 行)``。

    每一次调用 build 前，OHLCV、regime 与量流都先截到该 bar 收线时刻；把
    未来数据传进本函数不会改变过去结果。``existing_ts`` 只跳过同一代际已经
    落库的稀疏机会行，middle 的“已处理”由调用方扫描水位负责。
    """
    frame_4h = ohlcv_by_tf.get("4h")
    if frame_4h is None or not len(frame_4h):
        return [], []
    all_4h_ts = _ts_ms(frame_4h["ts"])
    available = set(int(value) for value in all_4h_ts)
    targets = (
        sorted(available)
        if target_ts is None
        else sorted({int(value) for value in target_ts if int(value) in available})
    )
    existing = set(existing_ts or ())
    timelines = {
        tf: _timeline_arrays(regime_by_tf.get(tf))
        for tf in assemble.POLICY_RESONANCE_TFS
    }
    build_fn = assemble_fn or assemble.build
    processed, out = [], []
    for ts in targets:
        if ts in existing:
            continue
        processed.append(ts)
        asof_ms = ts + BAR_4H_MS
        # 因果边界必须在本层完成：不能依赖下游 build “碰巧也会截断”。这样
        # 换装配实现时，历史重建仍不会把未来 frame 交给它。
        frames = {
            tf: _closed_prefix(ohlcv_by_tf.get(tf), tf, asof_ms)
            for tf in assemble.POLICY_RESONANCE_TFS
        }
        regimes = {
            tf: _state_asof(*timelines[tf], tf, asof_ms)
            for tf in assemble.POLICY_RESONANCE_TFS
        }
        payload = build_fn(
            symbol,
            ohlcv_by_tf=frames,
            regime_by_tf=regimes,
            instrument=instrument,
            asof_ms=asof_ms,
            vol1h=_vol_prefix(vol1h, ts, asof_ms),
            vol_input=None,
        )
        actual_assemble = (
            (payload.get("versions") or {}).get("assemble") or assemble_version
        )
        row = _setup_row(
            symbol, ts, payload, rules_version, str(actual_assemble),
        )
        if row is not None:
            out.append(row)
    return processed, out


def _finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _gap(row: Mapping[str, Any]):
    pos = _finite(row.get("crsi_pos_4h"))
    if pos is None:
        # 兼容轻量测试/旧调用方；正式落库始终有独立 crsi_pos_4h。
        pos = _finite(row.get("crsi_4h"))
    if pos is None:
        return None
    if row.get("at") == "at_support":
        return max(0.0, pos)
    if row.get("at") == "at_resistance":
        return max(0.0, 100.0 - pos)
    return None


def _slope(points: list[tuple[float, float]]):
    if len(points) < 2:
        return 0.0
    x = np.asarray([point[0] for point in points], dtype=float)
    y = np.asarray([point[1] for point in points], dtype=float)
    if np.allclose(x, x[0]):
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def _bar_offset(ts: int, start: int) -> float:
    return (int(ts) - int(start)) / BAR_4H_MS


def _summarize(part: list[dict], expired: bool) -> dict:
    first, last = part[0], part[-1]
    start, end = int(first["ts"]), int(last["ts"])
    raw_crsi = [
        (_bar_offset(row["ts"], start), value)
        for row in part if (value := _finite(row.get("crsi_4h"))) is not None
    ]
    gaps = [
        (_bar_offset(row["ts"], start), value)
        for row in part if (value := _gap(row)) is not None
    ]
    gap_slope = _slope(gaps[-GAP_TREND_WINDOW:])
    if gap_slope <= -GAP_SLOPE_EPS:
        gap_trend = "收敛中"
    elif gap_slope >= GAP_SLOPE_EPS:
        gap_trend = "发散中"
    else:
        gap_trend = "横盘"

    ever = any(row.get("main_ok") is True for row in part)
    if expired:
        status = "expired"
    elif last.get("main_ok") is True:
        status = "ripe"
    elif ever:
        status = "matured"
    else:
        status = "brewing"
    return {
        "tracking_version": TRACKING_VERSION,
        "symbol": first.get("symbol"),
        "at": first.get("at"),
        "regime_4h": first.get("regime_4h"),
        "started_at": start,
        "last_at": end,
        # bars 是日历持续根数（含允许的一根中断）；setup_bars 是实际命中行数。
        "bars": int(round((end - start) / BAR_4H_MS)) + 1,
        "setup_bars": len(part),
        "crsi_first": raw_crsi[0][1] if raw_crsi else None,
        "crsi_last": raw_crsi[-1][1] if raw_crsi else None,
        "crsi_slope": round(_slope(raw_crsi), 3) if raw_crsi else None,
        "gap_now": round(gaps[-1][1], 2) if gaps else None,
        "gap_slope": round(gap_slope, 3) if gaps else None,
        "gap_trend": gap_trend,
        "ever_main_ok": ever,
        "status": status,
        "zone_lo": last.get("zone_lo"),
        "zone_hi": last.get("zone_hi"),
        "zone_kinds": last.get("zone_kinds") or [],
        "play": last.get("play"),
        "last_play": last.get("play"),
    }


def runs(rows: list[dict]) -> list[dict]:
    """按 ``symbol + (at, regime_4h) + 时间连续性`` 聚合 setup 快照。"""
    if not rows:
        return []
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("symbol") is not None and row.get("ts") is not None:
            by_symbol[str(row["symbol"])].append(dict(row))

    result = []
    for symbol in sorted(by_symbol):
        ordered = sorted(by_symbol[symbol], key=lambda row: int(row["ts"]))
        parts: list[list[dict]] = []
        for row in ordered:
            if not parts:
                parts.append([row])
                continue
            prev = parts[-1][-1]
            delta = int(row["ts"]) - int(prev["ts"])
            exact_grid = delta > 0 and delta % BAR_4H_MS == 0
            missing = delta // BAR_4H_MS - 1 if exact_grid else SETUP_GAP_BARS + 1
            same_key = (
                row.get("at") == prev.get("at")
                and row.get("regime_4h") == prev.get("regime_4h")
            )
            if same_key and missing <= SETUP_GAP_BARS:
                parts[-1].append(row)
            else:
                parts.append([row])

        observed_values = [
            int(row["observed_through_ts"])
            for row in ordered if row.get("observed_through_ts") is not None
        ]
        observed_through = max(observed_values) if observed_values else int(ordered[-1]["ts"])
        for index, part in enumerate(parts):
            is_old_part = index < len(parts) - 1
            newer_bars = (observed_through - int(part[-1]["ts"])) // BAR_4H_MS
            expired = is_old_part or newer_bars > SETUP_GAP_BARS
            result.append(_summarize(part, expired))
    return sorted(result, key=lambda row: (row["symbol"], row["started_at"]))
