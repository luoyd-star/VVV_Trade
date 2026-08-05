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
from regime.features.volatility import atr


LEVELS_VERSION = "lv1"

ATR_PERIOD = 14                 # 起步值，待校准；与现有 volatility.atr 的默认口径一致
PIVOT_K = 4                     # 起步值，待校准；与现有 swing_pivots 默认确认窗一致
PIVOTS_PER_SIDE = 4             # 起步值，待校准；每侧保留最近四个枢轴
EMA_PERIODS = (21, 55, 100, 200)  # 起步值，待校准；周期取自 policy 原文
RANGE_WINDOW = 60               # 起步值，待校准；区间边界观察窗
RANGE_HIGH_Q = 0.90             # 起步值，待校准；区间上沿分位
RANGE_LOW_Q = 0.10              # 起步值，待校准；区间下沿分位
POC_WINDOW_1H = 240             # 起步值，待校准；成交密集区观察窗（小时）
POC_BUCKET_ATR = 0.25           # 起步值，待校准；价格分桶宽度（ATR 倍数）
POC_MIN_COVERAGE = 0.80         # 起步值，待校准；低于此覆盖率不输出部分窗口假值
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


def _zone(point: float, kind: str, origin_role: str, atr_value: float,
          lo: Optional[float] = None, hi: Optional[float] = None) -> dict:
    """单点至少扩成可交易区间；枢轴还可把真实 K 线高低传进来。

    **单体也受 MAX_ZONE_ATR 约束**（2026-08-05 全宇宙试算：不约束时枢轴 zone
    最宽到 9.47 ATR、34% 超限——一根大振幅 K 线会造出一个"价格在里面晃荡都算到位"
    的伪关键位）。超限时以 point 为锚居中截断，保住关键位的锚点语义。
    """
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
        "last_touch_bars": None,
        "origin_role": origin_role,
        "width_atr": float((hi_value - lo_value) / atr_value),
    }


def _touch_stats(df: Optional[pd.DataFrame], lo: float, hi: float) -> tuple[int, Optional[int]]:
    """连续停留在区间内只算一次触碰，避免横盘把强度虚增成 bar 数。"""
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


def _with_touches(zone: dict, df: Optional[pd.DataFrame]) -> dict:
    out = dict(zone)
    touches, last = _touch_stats(df, out["lo"], out["hi"])
    out["touches"] = touches
    out["last_touch_bars"] = last
    return out


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
        "touches": 0,
        "last_touch_bars": None,
        "origin_role": origin_role,
        "width_atr": float((hi - lo) / atr_value),
    }
    if price_df is not None:
        return _with_touches(out, price_df)
    # 独立测试/复用 merge_zones 时没有价格序列，宁可保留最保守的可审计统计，
    # 也不能把共振成员的 touches 相加（同一次触碰会被重复计数）。
    out["touches"] = max((int(z.get("touches", 0)) for z in cluster), default=0)
    lasts = [z.get("last_touch_bars") for z in cluster if z.get("last_touch_bars") is not None]
    out["last_touch_bars"] = min(lasts) if lasts else None
    return out


def merge_zones(zones: list[dict], atr_value: float,
                price_df: Optional[pd.DataFrame] = None) -> Optional[list[dict]]:
    """按 ATR 距离聚类，并在每次合并前执行最终宽度上限。

    相邻成员逐个入簇，因而允许真正的链式共振；``MAX_ZONE_ATR`` 是防止该链条
    无限制扩张的硬护栏。输入不会被修改。
    """
    atr_f = _finite_float(atr_value)
    if atr_f is None or atr_f <= 0:
        return None
    clean = []
    for raw in zones or []:
        lo = _finite_float(raw.get("lo"))
        hi = _finite_float(raw.get("hi"))
        mid = _finite_float(raw.get("mid"))
        if lo is None or hi is None or mid is None or lo > hi:
            continue
        z = dict(raw)
        z.update({"lo": lo, "hi": hi, "mid": mid})
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
    return [_merged_cluster(c, atr_f, price_df) for c in clusters]


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
    return _with_touches(hi, df), _with_touches(lo, df)


def _poc_level(vol1h: Optional[pd.DataFrame], atr_value: float,
               current_price: float) -> tuple[Optional[dict], Optional[str]]:
    if vol1h is None or len(vol1h) == 0:
        return None, "poc_missing"
    required = {"ts", "volume", "quote_vol"}
    if not required.issubset(vol1h.columns):
        return None, "poc_columns_missing"

    ts = _timestamps_utc(vol1h["ts"])
    volume = pd.to_numeric(vol1h["volume"], errors="coerce").to_numpy(dtype=float)
    quote = pd.to_numeric(vol1h["quote_vol"], errors="coerce").to_numpy(dtype=float)
    frame = pd.DataFrame({"ts": ts, "volume": volume, "quote": quote})
    frame = frame.dropna(subset=["ts"]).sort_values("ts").drop_duplicates("ts", keep="last")
    valid = (
        np.isfinite(frame["volume"]) & np.isfinite(frame["quote"])
        & (frame["volume"] > 0) & (frame["quote"] > 0)
    )
    frame = frame.loc[valid]
    if len(frame) == 0:
        return None, "poc_invalid_volume"

    end = frame["ts"].iloc[-1].floor("h")
    start = end - pd.Timedelta(hours=POC_WINDOW_1H - 1)
    frame = frame[(frame["ts"] >= start) & (frame["ts"] <= end)]
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
                   atr_value: Optional[float] = None) -> dict:
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
    required = {"ts", "high", "low", "close"}
    if not isinstance(df, pd.DataFrame) or len(df) == 0:
        degraded.append("ohlcv_missing")
        return base
    if not required.issubset(df.columns):
        degraded.append("ohlcv_columns_missing")
        return base

    if atr_value is None:
        try:
            atr_value = atr(df, ATR_PERIOD).iloc[-1]
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

    # 枢轴必须保留原 K 线区间；NaN 会破坏局部极值比较，因此整类拒算而非删行续算。
    ohl = df[["high", "low"]].apply(pd.to_numeric, errors="coerce")
    if len(df) < 2 * PIVOT_K + 1:
        degraded.append("pivot_history_insufficient")
    elif not np.isfinite(ohl.to_numpy(dtype=float)).all():
        degraded.append("pivot_invalid_ohlc")
    else:
        pivots = swing_pivots(df, k=PIVOT_K)
        for pivot_kind, source_name, role in (
            ("H", "pivot_high", "resistance"),
            ("L", "pivot_low", "support"),
        ):
            selected = pivots[pivots["kind"] == pivot_kind].tail(PIVOTS_PER_SIDE)
            built = []
            for row in selected.itertuples(index=False):
                i = int(row.idx)
                point = float(row.price)
                zone = _zone(
                    point, source_name, role, atr_f,
                    float(df["low"].iloc[i]), float(df["high"].iloc[i]),
                )
                built.append(_with_touches(zone, df))
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
        zone = _with_touches(_zone(point, name, role, atr_f), df)
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
            range_hi = _with_touches(
                _zone(float(highs.quantile(RANGE_HIGH_Q)), "range_hi", "resistance", atr_f), df
            )
            range_lo = _with_touches(
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

    poc, poc_reason = _poc_level(vol1h, atr_f, current_price)
    if poc is None:
        degraded.append(poc_reason or "poc_unavailable")
    else:
        poc = _with_touches(poc, df)
        sources["poc"] = poc
        candidates.append(poc)

    merged = merge_zones(candidates, atr_f, price_df=df)
    base["zones"] = merged if merged else None
    if not merged:
        degraded.append("no_levels")
    base["degraded"] = _dedupe(degraded)
    return base


def key_levels(df: pd.DataFrame, vol1h: Optional[pd.DataFrame] = None,
               atr_value: Optional[float] = None) -> Optional[list[dict]]:
    """只取 zone 列表的轻量入口；需要降级审计时使用 ``extract_levels``。"""
    return extract_levels(df, vol1h=vol1h, atr_value=atr_value)["zones"]


# 兼容调用方偏好的动词命名；两者返回完全相同的审计 payload。
build_levels = extract_levels
