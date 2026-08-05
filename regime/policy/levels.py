"""关键位区间提取。

本模块只把调用方已经装配好的 OHLCV 与币安 1h 量流变成可审计的价格区间；
不读取数据库，也不猜测缺失数据。regime 过滤刻意留给 ``location``，避免测量
结果与判定版本互相耦合。
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from regime.features.structure import ema, swing_pivots
LEVELS_VERSION = "lv1"

ATR_PERIOD = 14                 # 起步值，待校准；与现有 volatility.atr 的默认口径一致
PIVOT_K = 4                     # 起步值，待校准；与现有 swing_pivots 默认确认窗一致
PIVOTS_PER_SIDE = 4             # 起步值，待校准；每侧保留最近四个枢轴
PIVOT_CLUSTER_ATR = 0.35        # 起步值，待校准；同一反转价簇允许的最大价格散布
EMA_PERIODS = (21, 55, 100, 200)  # 起步值，待校准；周期取自 policy 原文
RANGE_WINDOW = 60               # 起步值，待校准；区间边界观察窗
RANGE_HIGH_Q = 0.90             # 起步值，待校准；区间上沿分位
RANGE_LOW_Q = 0.10              # 起步值，待校准；区间下沿分位
POC_WINDOW_1H = 240             # 起步值，待校准；成交密集区观察窗（小时）
POC_BUCKET_ATR = 0.25           # 起步值，待校准；价格分桶宽度（ATR 倍数）
POC_MIN_COVERAGE = 0.80         # 起步值，待校准；低于此覆盖率不输出部分窗口假值
POC_MAX_LAG_HOURS = 2           # 起步值，待校准；量流末槽相对决策时点的最大容忍滞后
ZONE_HALF_ATR = 0.25            # 起步值，待校准；单点位向两侧扩展的 ATR 倍数
MERGE_ATR = 0.50                # 起步值，待校准；区间聚类的中心距离上限
MAX_ZONE_ATR = 1.50             # 起步值，待校准；阻止链式聚类把区间越并越宽

_SOURCE_NAMES = (
    "pivot_high", "pivot_low",
    "ema21", "ema55", "ema100", "ema200",
    "range_hi", "range_lo", "poc",
    "prev_day_hi", "prev_day_lo", "prev_week_hi", "prev_week_lo",
)


def _finite_float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _timestamps_utc(values: pd.Series) -> pd.Series:
    """兼容仓库的毫秒整数 ts 与测试/调用方传入的 datetime。"""
    if pd.api.types.is_numeric_dtype(values):
        raw = pd.to_numeric(values, errors="coerce")
        finite = raw[np.isfinite(raw)]
        diffs = finite.sort_values().diff().dropna().abs()
        # 小整数测试时间轴也可能是毫秒；步长比纪元绝对值更能说明单位。
        looks_ms = bool(
            (len(finite) and finite.abs().max() >= 100_000_000_000)
            or (len(diffs) and diffs.median() >= 100_000)
        )
        return pd.to_datetime(raw, unit="ms" if looks_ms else "s", utc=True, errors="coerce")
    return pd.to_datetime(values, utc=True, errors="coerce")


def _dedupe(items) -> list:
    return list(dict.fromkeys(items))


def _strict_atr(df: pd.DataFrame, n: int) -> pd.Series:
    """与现有 SMA-ATR 同口径，但横向 max 明确禁止跳过 NaN。"""
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    prev_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1, skipna=False)
    return true_range.rolling(n).mean()


def _valid_atr_window(df: pd.DataFrame) -> bool:
    """ATR 只在末 ``ATR_PERIOD+1`` 根 OHLC 完整且满足蜡烛约束时成立。"""
    if len(df) < ATR_PERIOD + 1:
        return False
    recent = df.tail(ATR_PERIOD + 1)[["open", "high", "low", "close"]].apply(
        pd.to_numeric, errors="coerce",
    )
    values = recent.to_numpy(dtype=float)
    if not np.isfinite(values).all():
        return False
    high = recent["high"].to_numpy(dtype=float)
    low = recent["low"].to_numpy(dtype=float)
    body_max = recent[["open", "close", "low"]].max(axis=1).to_numpy(dtype=float)
    body_min = recent[["open", "close", "high"]].min(axis=1).to_numpy(dtype=float)
    return bool((high >= body_max).all() and (low <= body_min).all())


def _zone(point: float, kind: str, origin_role: str, atr_value: float,
          lo: Optional[float] = None, hi: Optional[float] = None) -> dict:
    """把 EMA/边界/POC 等单点扩成固定最小宽度，不读取枢轴 K 线振幅。"""
    floor_lo = point - ZONE_HALF_ATR * atr_value
    floor_hi = point + ZONE_HALF_ATR * atr_value
    lo_value = min(floor_lo, point if lo is None else lo)
    hi_value = max(floor_hi, point if hi is None else hi)
    max_w = MAX_ZONE_ATR * atr_value
    if hi_value - lo_value > max_w:
        half = max_w / 2.0
        lo_value, hi_value = point - half, point + half
    return {
        "lo": float(lo_value),
        "hi": float(hi_value),
        "mid": float((lo_value + hi_value) / 2.0),
        "kinds": [kind],
        "touches": 0,
        "established_ts": None,
        "last_touch_bars": None,
        "overlap_count": 0,
        "last_overlap_bars": None,
        "origin_role": origin_role,
        "width_atr": float((hi_value - lo_value) / atr_value),
        "chain_overflow": False,
        "_pivot_members": [],
    }


def _overlap_stats(df: Optional[pd.DataFrame], lo: float,
                   hi: float) -> tuple[int, Optional[int]]:
    """静态价格带的历史重叠次数（非因果），不得冒充反转验证次数。"""
    if df is None or len(df) == 0 or not {"high", "low"}.issubset(df.columns):
        return 0, None
    high = pd.to_numeric(df["high"], errors="coerce").to_numpy(dtype=float)
    low = pd.to_numeric(df["low"], errors="coerce").to_numpy(dtype=float)
    hit = np.isfinite(high) & np.isfinite(low) & (high >= lo) & (low <= hi)
    if not hit.any():
        return 0, None
    entries = hit & ~np.r_[False, hit[:-1]]
    last_i = int(np.flatnonzero(hit)[-1])
    return int(entries.sum()), int(len(hit) - 1 - last_i)


def _with_overlap(zone: dict, df: Optional[pd.DataFrame]) -> dict:
    out = dict(zone)
    count, last = _overlap_stats(df, out["lo"], out["hi"])
    out["overlap_count"] = count
    out["last_overlap_bars"] = last
    return out


def _timestamp_ms(value) -> Optional[int]:
    parsed = _timestamps_utc(pd.Series([value])).iloc[0]
    return None if pd.isna(parsed) else int(parsed.value // 1_000_000)


def _pivot_clusters(members: list[dict], atr_value: float) -> list[list[dict]]:
    """按价格聚类枢轴；限制簇直径，阻断单链接的链式吞并。"""
    if not members:
        return []
    ordered = sorted(members, key=lambda item: (item["price"], item["idx"]))
    max_span = PIVOT_CLUSTER_ATR * atr_value
    clusters: list[list[dict]] = [[ordered[0]]]
    for member in ordered[1:]:
        cluster = clusters[-1]
        lo = min(cluster[0]["price"], member["price"])
        hi = max(cluster[-1]["price"], member["price"])
        if hi - lo <= max_span:
            cluster.append(member)
        else:
            clusters.append([member])
    return clusters


def _pivot_zone(members: list[dict], kind: str, origin_role: str,
                atr_value: float, n_bars: int) -> dict:
    """枢轴带宽只由反转价位散布决定；单成员退化为固定 ±ZONE_HALF_ATR。"""
    prices = [float(member["price"]) for member in members]
    raw_lo, raw_hi = min(prices), max(prices)
    min_width = 2.0 * ZONE_HALF_ATR * atr_value
    width = min(max(raw_hi - raw_lo, min_width), MAX_ZONE_ATR * atr_value)
    padding = max(0.0, width - (raw_hi - raw_lo)) / 2.0
    lo, hi = raw_lo - padding, raw_hi + padding
    earliest = min(members, key=lambda member: member["idx"])
    latest_idx = max(int(member["idx"]) for member in members)
    return {
        "lo": float(lo),
        "hi": float(hi),
        "mid": float((lo + hi) / 2.0),
        "kinds": [kind],
        "touches": len(members),
        "established_ts": _timestamp_ms(earliest.get("ts")),
        "last_touch_bars": int(n_bars - 1 - latest_idx),
        "overlap_count": 0,
        "last_overlap_bars": None,
        "origin_role": origin_role,
        "width_atr": float((hi - lo) / atr_value),
        "chain_overflow": False,
        "_pivot_members": [
            {
                "price": float(member["price"]),
                "ts": _timestamp_ms(member.get("ts")),
                "last_touch_bars": int(n_bars - 1 - int(member["idx"])),
            }
            for member in members
        ],
    }


def _merged_cluster(cluster: list[dict], atr_value: float,
                    price_df: Optional[pd.DataFrame]) -> dict:
    lo = min(z["lo"] for z in cluster)
    hi = max(z["hi"] for z in cluster)
    kinds = _dedupe(kind for z in cluster for kind in z.get("kinds", []))
    roles = [z.get("origin_role") for z in cluster]
    # 共振来源角色冲突时用多数票；平票保留价格较低成员的角色，保证结果确定。
    support_n = roles.count("support")
    resistance_n = roles.count("resistance")
    if support_n == resistance_n:
        origin_role = min(cluster, key=lambda z: z["mid"]).get("origin_role", "support")
    else:
        origin_role = "support" if support_n > resistance_n else "resistance"
    out = {
        "lo": float(lo),
        "hi": float(hi),
        "mid": float((lo + hi) / 2.0),
        "kinds": kinds,
        "touches": sum(max(0, int(z.get("touches") or 0)) for z in cluster),
        "established_ts": None,
        "last_touch_bars": None,
        "overlap_count": 0,
        "last_overlap_bars": None,
        "origin_role": origin_role,
        "width_atr": float((hi - lo) / atr_value),
        "chain_overflow": any(z.get("chain_overflow") is True for z in cluster),
    }
    private_members = [
        member for z in cluster for member in (z.get("_pivot_members") or [])
    ]
    if any("_pivot_members" in z for z in cluster):
        out["_pivot_members"] = private_members
    established = [int(z["established_ts"]) for z in cluster
                   if z.get("established_ts") is not None]
    out["established_ts"] = min(established) if established else None
    lasts = [z.get("last_touch_bars") for z in cluster if z.get("last_touch_bars") is not None]
    out["last_touch_bars"] = min(lasts) if lasts else None
    if price_df is not None:
        return _with_overlap(out, price_df)
    overlaps = [int(z.get("overlap_count") or 0) for z in cluster]
    out["overlap_count"] = max(overlaps, default=0)
    overlap_lasts = [z.get("last_overlap_bars") for z in cluster
                     if z.get("last_overlap_bars") is not None]
    out["last_overlap_bars"] = min(overlap_lasts) if overlap_lasts else None
    return out


def _finalize_pivot_stats(zone: dict) -> dict:
    """按终态边界重算枢轴成员统计，并移除内部聚类明细。"""
    out = dict(zone)
    if "_pivot_members" not in out:
        return out
    members = [
        member for member in (out.pop("_pivot_members") or [])
        if out["lo"] <= member["price"] <= out["hi"]
    ]
    out["touches"] = len(members)
    times = [int(member["ts"]) for member in members if member.get("ts") is not None]
    out["established_ts"] = min(times) if times else None
    lasts = [int(member["last_touch_bars"]) for member in members]
    out["last_touch_bars"] = min(lasts) if lasts else None
    return out


def _resolve_overlaps(zones: list[dict], atr_value: float,
                      price_df: Optional[pd.DataFrame]) -> list[dict]:
    """把拒并后仍重叠的分片按相邻中心切成闭区间也不相交的终态。"""
    if not zones:
        return []
    ordered = sorted(zones, key=lambda z: (z["lo"], z["mid"], z["hi"]))
    components: list[list[dict]] = []
    component = [ordered[0]]
    component_hi = ordered[0]["hi"]
    for zone in ordered[1:]:
        if zone["lo"] <= component_hi:
            component.append(zone)
            component_hi = max(component_hi, zone["hi"])
        else:
            components.append(component)
            component = [zone]
            component_hi = zone["hi"]
    components.append(component)

    out: list[dict] = []
    for component in components:
        if len(component) == 1:
            zone = dict(component[0])
            if zone["width_atr"] > MAX_ZONE_ATR:
                zone["chain_overflow"] = True
            out.append(_finalize_pivot_stats(zone))
            continue
        component.sort(key=lambda z: (z["mid"], z["lo"], z["hi"]))
        mids = [z["mid"] for z in component]
        cuts = [(left + right) / 2.0 for left, right in zip(mids, mids[1:])]
        split: list[dict] = []
        possible = all(right > left for left, right in zip(mids, mids[1:]))
        for i, raw in enumerate(component):
            zone = dict(raw)
            lo = zone["lo"] if i == 0 else max(zone["lo"], np.nextafter(cuts[i - 1], np.inf))
            hi = zone["hi"] if i == len(component) - 1 else min(zone["hi"], cuts[i])
            if lo > hi:
                possible = False
                break
            zone.update(
                lo=float(lo), hi=float(hi),
                width_atr=float((hi - lo) / atr_value),
            )
            if price_df is not None:
                zone = _with_overlap(zone, price_df)
            split.append(zone)
        min_width_atr = 2.0 * ZONE_HALF_ATR
        if possible and all(
            min_width_atr - 1e-12 <= z["width_atr"] <= MAX_ZONE_ATR
            for z in split
        ):
            out.extend(_finalize_pivot_stats(z) for z in split)
        else:
            overflow = _merged_cluster(component, atr_value, price_df)
            overflow["chain_overflow"] = True
            out.append(_finalize_pivot_stats(overflow))

    out.sort(key=lambda z: (z["lo"], z["hi"]))
    if any(left["hi"] >= right["lo"] for left, right in zip(out, out[1:])):
        raise AssertionError("policy zone 终态必须互不重叠")
    return out


def merge_zones(zones: list[dict], atr_value: float,
                price_df: Optional[pd.DataFrame] = None) -> Optional[list[dict]]:
    """合并共振来源、限制单区宽度，并把输出正规化为互斥区间。"""
    atr_f = _finite_float(atr_value)
    if atr_f is None or atr_f <= 0:
        return None
    clean = []
    for raw in zones or []:
        if not isinstance(raw, dict):
            continue
        lo = _finite_float(raw.get("lo"))
        hi = _finite_float(raw.get("hi"))
        mid = _finite_float(raw.get("mid"))
        if lo is None or hi is None or mid is None or lo > hi:
            continue
        z = dict(raw)
        z.update({"lo": lo, "hi": hi, "mid": mid})
        z["width_atr"] = float((hi - lo) / atr_f)
        z.setdefault("touches", 0)
        z.setdefault("established_ts", None)
        z.setdefault("last_touch_bars", None)
        z.setdefault("overlap_count", 0)
        z.setdefault("last_overlap_bars", None)
        z.setdefault("chain_overflow", False)
        clean.append(z)
    if not clean:
        return []

    clean.sort(key=lambda z: z["mid"])
    clusters: list[list[dict]] = [[clean[0]]]
    for zone in clean[1:]:
        cluster = clusters[-1]
        previous_mid = cluster[-1]["mid"]
        prospective_lo = min(min(z["lo"] for z in cluster), zone["lo"])
        prospective_hi = max(max(z["hi"] for z in cluster), zone["hi"])
        near = abs(zone["mid"] - previous_mid) / atr_f < MERGE_ATR
        within_cap = (prospective_hi - prospective_lo) / atr_f <= MAX_ZONE_ATR
        if near and within_cap:
            cluster.append(zone)
        else:
            clusters.append([zone])
    merged = [_merged_cluster(c, atr_f, price_df) for c in clusters]
    return _resolve_overlaps(merged, atr_f, price_df)


def _previous_period_levels(df: pd.DataFrame, atr_value: float,
                            period: str) -> tuple[Optional[dict], Optional[dict]]:
    if "ts" not in df.columns or len(df) == 0:
        return None, None
    ts = _timestamps_utc(df["ts"])
    if ts.isna().all():
        return None, None
    latest = ts.iloc[-1]
    if period == "day":
        current_key = latest.floor("D")
        target_key = current_key - pd.Timedelta(days=1)
        keys = ts.dt.floor("D")
        hi_kind, lo_kind = "prev_day_hi", "prev_day_lo"
    else:
        current_key = latest.floor("D") - pd.Timedelta(days=int(latest.dayofweek))
        target_key = current_key - pd.Timedelta(days=7)
        keys = ts.dt.floor("D") - pd.to_timedelta(ts.dt.dayofweek, unit="D")
        hi_kind, lo_kind = "prev_week_hi", "prev_week_lo"
    mask = keys == target_key
    highs = pd.to_numeric(df.loc[mask, "high"], errors="coerce").dropna()
    lows = pd.to_numeric(df.loc[mask, "low"], errors="coerce").dropna()
    if len(highs) == 0 or len(lows) == 0:
        return None, None
    hi = _zone(float(highs.max()), hi_kind, "resistance", atr_value)
    lo = _zone(float(lows.min()), lo_kind, "support", atr_value)
    return _with_overlap(hi, df), _with_overlap(lo, df)


def _poc_level(vol1h: Optional[pd.DataFrame], atr_value: float,
               current_price: float,
               expected_end_ts=None) -> tuple[Optional[dict], Optional[str]]:
    if vol1h is None or len(vol1h) == 0:
        return None, "poc_missing"
    required = {"ts", "volume", "quote_vol"}
    if not required.issubset(vol1h.columns):
        return None, "poc_columns_missing"
    if expected_end_ts is None:
        return None, "poc_expected_end_missing"
    expected_end = _timestamps_utc(pd.Series([expected_end_ts])).iloc[0]
    if pd.isna(expected_end):
        return None, "poc_expected_end_invalid"

    ts = _timestamps_utc(vol1h["ts"])
    volume = pd.to_numeric(vol1h["volume"], errors="coerce").to_numpy(dtype=float)
    quote = pd.to_numeric(vol1h["quote_vol"], errors="coerce").to_numpy(dtype=float)
    frame = pd.DataFrame({"ts": ts, "volume": volume, "quote": quote})
    frame = frame.dropna(subset=["ts"])
    frame["slot"] = frame["ts"].dt.floor("h")
    frame = frame.sort_values("ts").drop_duplicates("slot", keep="last")
    valid = (
        np.isfinite(frame["volume"]) & np.isfinite(frame["quote"])
        & (frame["volume"] > 0) & (frame["quote"] > 0)
    )
    frame = frame.loc[valid]
    if len(frame) == 0:
        return None, "poc_invalid_volume"

    # vol1h.ts 是小时槽开端；4h 决策收线时刻之前的最后完整槽为 end-1h。
    end = expected_end.floor("h") - pd.Timedelta(hours=1)
    latest = frame["slot"].max()
    if end - latest > pd.Timedelta(hours=POC_MAX_LAG_HOURS):
        return None, "poc_stale"
    start = end - pd.Timedelta(hours=POC_WINDOW_1H - 1)
    frame = frame[(frame["slot"] >= start) & (frame["slot"] <= end)]
    coverage = len(frame) / POC_WINDOW_1H
    if coverage < POC_MIN_COVERAGE:
        return None, "poc_coverage_insufficient"

    prices = frame["quote"].to_numpy(dtype=float) / frame["volume"].to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        return None, "poc_invalid_price"
    bucket_width = POC_BUCKET_ATR * atr_value
    if not np.isfinite(bucket_width) or bucket_width <= 0:
        return None, "poc_no_atr"
    bucket = np.floor(prices / bucket_width).astype(np.int64)
    grouped = pd.DataFrame({"bucket": bucket, "volume": frame["volume"].to_numpy(dtype=float)})
    totals = grouped.groupby("bucket", sort=True)["volume"].sum()
    if len(totals) == 0 or not np.isfinite(totals.max()) or totals.max() <= 0:
        return None, "poc_invalid_volume"
    # idxmax 在并列时取价格较低的桶；固定 tie-break 防止行顺序改变结果。
    winning = int(totals.idxmax())
    bucket_lo = winning * bucket_width
    bucket_hi = bucket_lo + bucket_width
    point = (bucket_lo + bucket_hi) / 2.0
    role = "support" if current_price >= point else "resistance"
    return _zone(point, "poc", role, atr_value, bucket_lo, bucket_hi), None


def extract_levels(df: pd.DataFrame, vol1h: Optional[pd.DataFrame] = None,
                   atr_value: Optional[float] = None,
                   expected_end_ts=None) -> dict:
    """提取全部关键位并聚类，返回 zones、逐来源结果与降级原因。

    ``atr_value`` 允许调用方复用已经算好的同口径 ATR；不传则调用仓库现有
    ``volatility.atr``。任何无法成立的来源在 ``sources`` 中保留为 ``None``。
    """
    sources = {name: None for name in _SOURCE_NAMES}
    degraded: list[str] = []
    base = {
        "version": LEVELS_VERSION,
        "atr": None,
        "zones": None,
        "sources": sources,
        "degraded": degraded,
    }
    required = {"ts", "open", "high", "low", "close"}
    if not isinstance(df, pd.DataFrame) or len(df) == 0:
        degraded.append("ohlcv_missing")
        return base
    if not required.issubset(df.columns):
        degraded.append("ohlcv_columns_missing")
        return base

    # R2-1：先验 OHLC 校验必须发生在 ATR 之前；传入复用 ATR 也不能绕过数据质量门。
    if not _valid_atr_window(df):
        degraded.append("no_atr")
        return base
    if atr_value is None:
        try:
            atr_value = _strict_atr(df.tail(ATR_PERIOD + 1), ATR_PERIOD).iloc[-1]
        except (KeyError, TypeError, ValueError, IndexError):
            atr_value = None
    atr_f = _finite_float(atr_value)
    if atr_f is None or atr_f <= 0:
        degraded.append("no_atr")
        return base
    base["atr"] = atr_f

    current_price = _finite_float(df["close"].iloc[-1])
    if current_price is None or current_price <= 0:
        degraded.append("no_price")
        return base

    candidates: list[dict] = []

    # 枢轴价位先按同类反转聚类；zone 不再读取枢轴 K 线本身的 high-low 振幅。
    ohl = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if len(df) < 2 * PIVOT_K + 1:
        degraded.append("pivot_history_insufficient")
    elif (not np.isfinite(ohl.to_numpy(dtype=float)).all()
          or not (ohl["high"] >= ohl[["open", "close", "low"]].max(axis=1)).all()
          or not (ohl["low"] <= ohl[["open", "close", "high"]].min(axis=1)).all()):
        degraded.append("pivot_invalid_ohlc")
    else:
        pivots = swing_pivots(df, k=PIVOT_K)
        for pivot_kind, source_name, role in (
            ("H", "pivot_high", "resistance"),
            ("L", "pivot_low", "support"),
        ):
            selected = pivots[pivots["kind"] == pivot_kind]
            members = [
                {
                    "idx": int(row.idx),
                    "price": float(row.price),
                    "ts": df["ts"].iloc[int(row.idx)],
                }
                for row in selected.itertuples(index=False)
            ]
            clusters = _pivot_clusters(members, atr_f)
            clusters.sort(key=lambda cluster: max(member["idx"] for member in cluster))
            clusters = clusters[-PIVOTS_PER_SIDE:]
            built = [
                _with_overlap(
                    _pivot_zone(cluster, source_name, role, atr_f, len(df)), df,
                )
                for cluster in clusters
            ]
            if built:
                sources[source_name] = built
                candidates.extend(built)
            else:
                degraded.append(f"{source_name}_unavailable")

    close = pd.to_numeric(df["close"], errors="coerce")
    for period in EMA_PERIODS:
        name = f"ema{period}"
        if len(close) < period or not np.isfinite(close.tail(period).to_numpy(dtype=float)).all():
            degraded.append(f"{name}_history_insufficient")
            continue
        point = _finite_float(ema(close, period).iloc[-1])
        if point is None:
            degraded.append(f"{name}_unavailable")
            continue
        role = "support" if current_price >= point else "resistance"
        zone = _with_overlap(_zone(point, name, role, atr_f), df)
        sources[name] = zone
        candidates.append(zone)

    if len(df) < RANGE_WINDOW:
        degraded.append("range_history_insufficient")
    else:
        recent = df.tail(RANGE_WINDOW)
        highs = pd.to_numeric(recent["high"], errors="coerce")
        lows = pd.to_numeric(recent["low"], errors="coerce")
        if not np.isfinite(highs.to_numpy(dtype=float)).all() or not np.isfinite(lows.to_numpy(dtype=float)).all():
            degraded.append("range_invalid_ohlc")
        else:
            range_hi = _with_overlap(
                _zone(float(highs.quantile(RANGE_HIGH_Q)), "range_hi", "resistance", atr_f), df
            )
            range_lo = _with_overlap(
                _zone(float(lows.quantile(RANGE_LOW_Q)), "range_lo", "support", atr_f), df
            )
            sources["range_hi"], sources["range_lo"] = range_hi, range_lo
            candidates.extend([range_hi, range_lo])

    for period in ("day", "week"):
        hi_zone, lo_zone = _previous_period_levels(df, atr_f, period)
        hi_name, lo_name = f"prev_{period}_hi", f"prev_{period}_lo"
        if hi_zone is None or lo_zone is None:
            degraded.append(f"prev_{period}_history_insufficient")
        else:
            sources[hi_name], sources[lo_name] = hi_zone, lo_zone
            candidates.extend([hi_zone, lo_zone])

    poc, poc_reason = _poc_level(
        vol1h, atr_f, current_price, expected_end_ts=expected_end_ts,
    )
    if poc is None:
        degraded.append(poc_reason or "poc_unavailable")
    else:
        poc = _with_overlap(poc, df)
        sources["poc"] = poc
        candidates.append(poc)

    merged = merge_zones(candidates, atr_f, price_df=df)
    for value in sources.values():
        if isinstance(value, dict):
            value.pop("_pivot_members", None)
        elif isinstance(value, list):
            for zone in value:
                if isinstance(zone, dict):
                    zone.pop("_pivot_members", None)
    base["zones"] = merged if merged else None
    if not merged:
        degraded.append("no_levels")
    base["degraded"] = _dedupe(degraded)
    return base


def key_levels(df: pd.DataFrame, vol1h: Optional[pd.DataFrame] = None,
               atr_value: Optional[float] = None,
               expected_end_ts=None) -> Optional[list[dict]]:
    """只取 zone 列表的轻量入口；需要降级审计时使用 ``extract_levels``。"""
    return extract_levels(
        df, vol1h=vol1h, atr_value=atr_value,
        expected_end_ts=expected_end_ts,
    )["zones"]


# 兼容调用方偏好的动词命名；两者返回完全相同的审计 payload。
build_levels = extract_levels
