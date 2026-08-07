"""Policy 测量块的纯装配层。

调用方负责取得 OHLCV、regime、量流与波动输入；本模块只消费这些数据，不持有
SQLite 连接，也不自行补猜缺失值。这样 dashboard 与 collector 可以共享同一套
位置、信号、剧本和止损口径，而不会因各自取数方式不同复制判定实现。
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import numpy as np
import pandas as pd

from regime.features.crsi import crsi_features
from regime.policy.levels import LEVELS_VERSION, extract_levels
from regime.policy.location import (
    APPROACH_BARS,
    LOCATION_VERSION,
    _eligible,
    _zone_state,
    locate,
)
from regime.policy.stopcheck import (
    STOPCHECK_VERSION,
    check_stop_vs_iv,
    find_structural_stop,
)
from regime.policy.volnote import VOLNOTE_VERSION, vol_notes


ASSEMBLE_VERSION = "asm1"

POLICY_SIGNAL_TF = "4h"
POLICY_RESONANCE_TFS = ("4h", "1d", "1h")
POLICY_SIGNAL_OHLCV_LIMIT = 400  # 起步值，待校准；覆盖 cRSI 自适应带观察窗
POLICY_1D_MIN_BARS = 90          # 起步值，待校准；与详情页既有 1d 可用性门槛一致
RESONANCE_STRONG_MIN = 1         # 起步值，待校准；辅助净分至少 +1 为强
RESONANCE_WEAK_MAX = -1          # 起步值，待校准；辅助净分至多 -1 为弱
TF_SEC = {"1h": 3600, "4h": 14_400, "1d": 86_400}

POLICY_VERSIONS = {
    "levels": LEVELS_VERSION,
    "location": LOCATION_VERSION,
    "stopcheck": STOPCHECK_VERSION,
    "volnote": VOLNOTE_VERSION,
    "assemble": ASSEMBLE_VERSION,
}

_EMPTY_CRSI = {"crsi": None, "pos": None, "zone": None}


def signal_ok(at, crsi_zone):
    """主导 cRSI 极值区是否与关键位方向一致。"""
    if crsi_zone is None:
        return None
    if at == "at_support":
        return crsi_zone == "超卖区"
    if at == "at_resistance":
        return crsi_zone == "超买区"
    return None


def resonance(at: str | None, z4: str | None, z1d: str | None,
              z1h: str | None) -> dict:
    """4h 主导 + 1d/1h 分级辅助；辅助冲突只降级，不否决主票。

    实证依据（监工实测）：4h cRSI 超卖→超买循环中位 6.0 天、1h 1.4 天，
    用户持仓 1-3 天；4h 极值仅 15/71 品种，若硬要求辅助同向只剩 2 个，
    因此 armed 只看 4h 主票，辅助只形成强/中/弱分级。
    """
    main_ok = signal_ok(at, z4)
    expected = {
        "at_support": "超卖区",
        "at_resistance": "超买区",
    }.get(at)
    opposite = {"超卖区": "超买区", "超买区": "超卖区"}.get(expected)

    aux = {}
    conflicts = []
    for tf, zone in (("1d", z1d), ("1h", z1h)):
        if zone is None or expected is None:
            vote = None
        elif zone == expected:
            vote = 1
        elif zone == opposite:
            vote = -1
            if main_ok is True:
                conflicts.append(f"{tf} {zone}与 4h {z4}反向")
        else:
            vote = 0
        aux[tf] = vote

    score = sum(vote for vote in aux.values() if vote is not None)
    grade = None
    if main_ok is True:
        if score >= RESONANCE_STRONG_MIN:
            grade = "强"
        elif score <= RESONANCE_WEAK_MAX:
            grade = "弱"
        else:
            grade = "中"
    return {
        "main_tf": "4h",
        "main_zone": z4,
        "main_ok": main_ok,
        "aux": aux,
        "score": score,
        "grade": grade,
        "conflicts": conflicts,
    }


def regime_conflict(regime_4h: str | None,
                    regime_1d: str | None) -> dict | None:
    """不同周期且至少一方为趋势态时只输出警示，不修改 4h play。"""
    if regime_4h is None or regime_1d is None or regime_4h == regime_1d:
        return None
    trends = {"trend_up", "trend_down"}
    if regime_4h not in trends and regime_1d not in trends:
        return None
    labels = {
        "trend_up": "趋势上行", "trend_down": "趋势下行", "range": "震荡",
        "squeeze": "低波动挤压", "high_vol_chop": "高波动非趋势",
    }
    pair = (
        f"4h {labels.get(regime_4h, regime_4h)} vs "
        f"1d {labels.get(regime_1d, regime_1d)}"
    )
    if regime_4h == "trend_up" and regime_1d == "trend_down":
        note = f"{pair}：这是逆大势的反弹单"
    elif regime_4h == "trend_down" and regime_1d == "trend_up":
        note = f"{pair}：这是逆大势的回落单"
    else:
        note = f"{pair}：周期状态冲突，仅作背景警示"
    return {"note": note, "severity": "warn"}


def _timestamps_ms(values: pd.Series) -> np.ndarray:
    """沿用 storage 的 datetime→毫秒口径，但不把存储层带进纯模块。"""
    if pd.api.types.is_numeric_dtype(values):
        raw = pd.to_numeric(values, errors="raise").to_numpy(dtype="int64")
        # collector 可直接传库内毫秒整数；datetime64 转出的则是纳秒。
        return raw if not len(raw) or np.max(np.abs(raw)) < 10**15 else raw // 10**6
    return (values.astype("int64") // 10**6).to_numpy(dtype="int64")


def _closed_ohlcv(ohlcv: pd.DataFrame, tf: str, asof_ms) -> pd.DataFrame:
    """截到 asof 已收线数据；先截再 tail，历史重建不会被未来 400 根挤掉。"""
    if asof_ms is None:
        return ohlcv
    ts_ms = _timestamps_ms(ohlcv["ts"])
    closed = ts_ms + TF_SEC[tf] * 1000 <= int(asof_ms)
    return ohlcv.loc[closed].reset_index(drop=True)


def crsi_snapshot(ohlcv: pd.DataFrame | None, tf: str, asof_ms,
                  *, feature_fn: Callable = crsi_features) -> dict:
    """从调用方给定序列取截至决策时点的最后已收线 cRSI。"""
    empty = dict(_EMPTY_CRSI)
    if asof_ms is None:
        return {"crsi": empty, "degraded": ["signal_asof_unavailable"]}
    if ohlcv is None or not len(ohlcv):
        return {"crsi": empty, "degraded": [f"crsi_{tf}_missing"]}
    if tf == "1d" and len(ohlcv) < POLICY_1D_MIN_BARS:
        # 先保留既有降级优先级；样本够时仍会在下方按 asof 截断后复核数量。
        return {
            "crsi": empty,
            "degraded": ["crsi_1d_history_insufficient"],
        }
    try:
        # OHLCV ts 是开盘时刻；所有周期只消费收线时间不晚于统一 asof 的 bar。
        frame = _closed_ohlcv(ohlcv, tf, asof_ms)
    except (KeyError, TypeError, ValueError, OverflowError):
        return {"crsi": empty, "degraded": [f"crsi_{tf}_unavailable"]}
    if tf == "1d" and len(frame) < POLICY_1D_MIN_BARS:
        return {
            "crsi": empty,
            "degraded": ["crsi_1d_history_insufficient"],
        }
    if not len(frame):
        return {"crsi": empty, "degraded": [f"crsi_{tf}_missing_at_asof"]}
    frame = frame.tail(POLICY_SIGNAL_OHLCV_LIMIT).reset_index(drop=True)
    try:
        crsi_last = feature_fn(frame).get("last") or {}
    except Exception as exc:  # noqa: BLE001  单周期缺失只降级，不拖垮位置测量
        return {
            "crsi": empty,
            "degraded": [f"crsi_{tf}_unavailable:{type(exc).__name__}"],
        }
    value = {key: crsi_last.get(key) for key in ("crsi", "pos", "zone")}
    if any(value[key] is None for key in ("crsi", "pos", "zone")):
        return {"crsi": empty, "degraded": [f"crsi_{tf}_unavailable"]}
    return {"crsi": value, "degraded": []}


def signal_snapshot(ohlcv_by_tf: Mapping[str, pd.DataFrame | None], at,
                    asof_ms, *, feature_fn: Callable = crsi_features) -> dict:
    """用同一 asof 装配 4h 主票和 1d/1h 辅助票。"""
    snapshots = {
        tf: crsi_snapshot(ohlcv_by_tf.get(tf), tf, asof_ms, feature_fn=feature_fn)
        for tf in POLICY_RESONANCE_TFS
    }
    by_tf = {tf: snapshots[tf]["crsi"] for tf in POLICY_RESONANCE_TFS}
    result = resonance(
        at,
        by_tf["4h"].get("zone"),
        by_tf["1d"].get("zone"),
        by_tf["1h"].get("zone"),
    )
    degraded = []
    for tf in POLICY_RESONANCE_TFS:
        degraded.extend(snapshots[tf]["degraded"])
    return {
        "signal_tf": POLICY_SIGNAL_TF,
        "crsi": by_tf["4h"],
        "crsi_by_tf": by_tf,
        "signal_ok": result["main_ok"],
        "resonance": result,
        "degraded": list(dict.fromkeys(degraded)),
    }


def policy_play(regime: str | None, at, signal_is_ok, role_flipped=False,
                zone=None, tradeable=None, reason=None):
    """把明确列出的 regime × location 单元格映射成剧本展示名。"""
    kinds = set()
    if isinstance(zone, dict):
        raw_kinds = zone.get("kinds") or []
        kinds = {raw_kinds} if isinstance(raw_kinds, str) else set(raw_kinds)

    play = None
    if regime == "trend_up" and at == "at_support":
        play = "S5 突破回踩加仓" if role_flipped else "S4 趋势回踩做多"
    elif regime == "trend_down" and at == "at_resistance":
        play = "S1 反弹至压力做空"
    elif (regime == "trend_down" and at == "at_support"
          and kinds & {"ema100", "ema200"}):
        play = "S13 深跌逆势做多（需二次确认·仓位为顺势单的 1/3-1/2）"
    elif regime == "range" and at == "at_support":
        play = "S3 区间下沿做多"
    elif regime == "range" and at == "at_resistance":
        play = "S3 区间上沿做空"

    if play is not None and tradeable is not True:
        # 路径门槛未成立时仍显示参考剧本，让等待原因可审计。
        reason_note = "" if reason in {None, "wrong_approach"} else f"；{reason}"
        return f"WAIT · 路径未确认（P09{reason_note}） · 参考剧本：{play}"
    if play is not None and signal_is_ok is False:
        return f"{play}（位置到了信号没到）"
    return play if signal_is_ok is not None else None


def policy_zone_payload(zone: dict, price: float, atr_value: float,
                        regime: str | None) -> dict:
    """给完整 zone 列表补当前角色、距离与 regime 资格。"""
    raw_kinds = zone.get("kinds") or []
    if isinstance(raw_kinds, str):
        raw_kinds = [raw_kinds]
    state = _zone_state(price, atr_value, zone)
    eligible = False
    if state is not None and zone.get("chain_overflow") is not True:
        eligible = _eligible(
            regime, state["role"], {str(kind) for kind in raw_kinds},
            state["role_flipped"],
        )
    return {
        "lo": zone.get("lo"),
        "hi": zone.get("hi"),
        "mid": zone.get("mid"),
        "kinds": list(raw_kinds),
        "touches": zone.get("touches"),
        "established_ts": zone.get("established_ts"),
        "last_touch_bars": zone.get("last_touch_bars"),
        "overlap_count": zone.get("overlap_count"),
        "last_overlap_bars": zone.get("last_overlap_bars"),
        "origin_role": zone.get("origin_role"),
        "width_atr": zone.get("width_atr"),
        "chain_overflow": zone.get("chain_overflow") is True,
        "dist_atr": state.get("dist_atr") if state else None,
        "role_now": state.get("role") if state else None,
        "role_flipped": state.get("role_flipped") if state else None,
        "eligible": bool(eligible),
    }


def policy_side(regime: str | None, location: dict) -> str | None:
    """只为已有矩阵单元格标方向；未知单元格不猜。"""
    at = location.get("at")
    zone = location.get("zone") or {}
    raw_kinds = zone.get("kinds") or []
    kinds = {raw_kinds} if isinstance(raw_kinds, str) else set(raw_kinds)
    if at == "at_support":
        if regime in {"trend_up", "range"}:
            return "long"
        if regime == "trend_down" and kinds & {"ema100", "ema200"}:
            return "long"
    if at == "at_resistance" and regime in {"trend_down", "range"}:
        return "short"
    return None


def _empty_payload(regime_4h, regime_1d, degraded: list[str]) -> dict:
    return {
        "versions": dict(POLICY_VERSIONS),
        "tf": "4h",
        "regime_4h": regime_4h,
        "regime_1d": regime_1d,
        "price": None,
        "atr": None,
        "location": None,
        "crsi": dict(_EMPTY_CRSI),
        "crsi_by_tf": {tf: dict(_EMPTY_CRSI) for tf in POLICY_RESONANCE_TFS},
        "signal_ok": None,
        "signal_tf": POLICY_SIGNAL_TF,
        "resonance": resonance(None, None, None, None),
        "regime_conflict": regime_conflict(regime_4h, regime_1d),
        "play": None,
        "zones": [],
        "vol_notes": [],
        "vol_meta": None,
        "stop_check": None,
        "degraded": degraded,
    }


def build(
    symbol: str,
    *,
    ohlcv_by_tf: Mapping[str, pd.DataFrame | None],
    regime_by_tf: Mapping[str, str | None],
    instrument: Mapping[str, Any] | None,
    asof_ms,
    vol1h: pd.DataFrame | None = None,
    vol_input: Mapping[str, Any] | None = None,
    vol1h_error: str | None = None,
    vol_input_error: str | None = None,
    _levels_fn: Callable = extract_levels,
    _crsi_fn: Callable = crsi_features,
    _vol_notes_fn: Callable = vol_notes,
) -> dict:
    """从调用方给定数据装配详情 policy；任何缺口显式进入 ``degraded``。

    ``instrument`` 为 collector/dashboard 的统一接口保留；asm1 尚无按品种类别分叉，
    因而缺失时不猜默认类别，也不改变当前 payload。三个私有函数参数只用于测试与
    旧 dashboard 内部 monkeypatch 兼容，默认值均为不碰 IO 的纯计算函数。
    """
    del instrument
    regime_4h = regime_by_tf.get("4h")
    regime_1d = regime_by_tf.get("1d")
    degraded: list[str] = []
    base = _empty_payload(regime_4h, regime_1d, degraded)
    if regime_4h is None:
        degraded.append("regime_4h_missing")
    if regime_1d is None:
        degraded.append("regime_1d_missing")

    raw_df = ohlcv_by_tf.get("4h")
    if raw_df is None or not len(raw_df):
        degraded.append("4h_ohlcv_missing")
        return base
    try:
        df = _closed_ohlcv(raw_df, "4h", asof_ms)
    except (KeyError, TypeError, ValueError, OverflowError):
        degraded.append("4h_ohlcv_unavailable")
        return base
    if not len(df):
        degraded.append("4h_ohlcv_missing_at_asof")
        return base
    try:
        price = float(df["close"].iloc[-1])
    except (KeyError, TypeError, ValueError, IndexError):
        degraded.append("price_unavailable")
        return base
    if not math.isfinite(price) or price <= 0:
        degraded.append("price_unavailable")
        return base
    base["price"] = price

    usable_vol1h = vol1h
    if vol1h_error:
        degraded.append(vol1h_error)
    elif vol1h is None:
        degraded.append("vol1h_unavailable")
    elif len(vol1h) and asof_ms is not None:
        try:
            usable_vol1h = vol1h[vol1h["ts"] < int(asof_ms)].reset_index(drop=True)
        except (KeyError, TypeError, ValueError, OverflowError):
            # 旧装配在筛选失败时仍把原始对象交给 levels；保留该降级行为，避免重构
            # 顺手把下游对坏输入的既有处理改掉。
            usable_vol1h = vol1h
            degraded.append("vol1h_unavailable")

    levels = _levels_fn(df, vol1h=usable_vol1h, expected_end_ts=asof_ms)
    degraded.extend(levels.get("degraded") or [])
    atr_value = levels.get("atr")
    zones = levels.get("zones")
    base["atr"] = atr_value
    if atr_value is None or not zones:
        if not zones:
            degraded.append("zones_unavailable")
        base["degraded"] = list(dict.fromkeys(degraded))
        return base

    location = locate(
        price, atr_value, zones, regime_4h,
        prices=df["close"].tail(APPROACH_BARS).to_numpy(),
    )
    base["location"] = location
    degraded.extend(location.get("degraded") or [])

    enriched = [
        policy_zone_payload(zone, price, atr_value, regime_4h)
        for zone in zones if isinstance(zone, dict)
    ]
    enriched.sort(key=lambda zone: (
        float("inf") if zone.get("dist_atr") is None else zone["dist_atr"],
        float("inf") if zone.get("mid") is None else zone["mid"],
    ))
    base["zones"] = enriched

    signal = signal_snapshot(
        ohlcv_by_tf, location.get("at"), asof_ms, feature_fn=_crsi_fn,
    )
    base["crsi"] = signal["crsi"]
    base["crsi_by_tf"] = signal["crsi_by_tf"]
    base["signal_ok"] = signal["signal_ok"]
    base["signal_tf"] = signal["signal_tf"]
    base["resonance"] = signal["resonance"]
    degraded.extend(signal["degraded"])
    base["play"] = policy_play(
        regime_4h, location.get("at"), signal["signal_ok"],
        role_flipped=location.get("role_flipped"), zone=location.get("zone"),
        tradeable=location.get("tradeable"), reason=location.get("reason"),
    )

    vol_values = dict(vol_input) if vol_input is not None else None
    if vol_input_error:
        degraded.append(vol_input_error)
    elif vol_values is None:
        degraded.append("vol_inputs_unavailable")
    else:
        try:
            base["vol_notes"] = _vol_notes_fn(symbol, price, **vol_values)
            if vol_values.get("iv3") is not None:
                base["vol_meta"] = {
                    "iv": vol_values.get("iv3"),
                    "tenor_days": vol_values.get("tenor_days"),
                    "method": vol_values.get("method"),
                    "n_expiries": vol_values.get("n_expiries"),
                }
        except Exception as exc:  # noqa: BLE001  风险补充失败不拖垮结构测量
            degraded.append(f"vol_inputs_unavailable:{type(exc).__name__}")

    side = policy_side(regime_4h, location)
    if side is not None:
        structural = find_structural_stop(location, enriched, price, atr_value, side)
        if structural is None:
            degraded.append("structural_stop_unavailable")
        else:
            iv3 = vol_values.get("iv3") if vol_values else None
            tenor_days = vol_values.get("tenor_days") if vol_values else None
            width = (
                check_stop_vs_iv(
                    structural["stop_dist_pct"], iv3,
                    holding_days=tenor_days,
                )
                if tenor_days is not None
                else check_stop_vs_iv(structural["stop_dist_pct"], iv3)
            )
            if width is None:
                degraded.append("iv3_missing_for_stop_check")
            else:
                base["stop_check"] = {**structural, **width}

    base["degraded"] = list(dict.fromkeys(degraded))
    return base
