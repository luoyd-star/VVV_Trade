"""policy 批 1：关键位区间提取的边界与降级纪律。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from regime.policy.levels import (
    MAX_ZONE_ATR,
    PIVOTS_PER_SIDE,
    extract_levels,
    merge_zones,
)


def _ohlcv(n: int = 240, freq: str = "4h") -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq=freq, tz="UTC")
    i = np.arange(n, dtype=float)
    close = 100.0 + 5.0 * np.sin(i / 4.0) + i * 0.01
    return pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": np.full(n, 100.0),
    })


def _raw_zone(mid: float, half: float = 0.1, kind: str = "pivot_low") -> dict:
    return {
        "lo": mid - half,
        "hi": mid + half,
        "mid": mid,
        "kinds": [kind],
        "touches": 1,
        "last_touch_bars": 2,
        "origin_role": "support",
        "width_atr": 2 * half,
    }


def _vol1h(end: pd.Timestamp, prices: np.ndarray, volume: np.ndarray) -> pd.DataFrame:
    ts = pd.date_range(end - pd.Timedelta(hours=len(prices) - 1), periods=len(prices), freq="h")
    return pd.DataFrame({"ts": ts, "volume": volume, "quote_vol": prices * volume})


def test_merge_respects_max_width_and_keeps_chain_split():
    # 相邻中心均小于 0.5 ATR，若只看邻距会把四段链式并完；
    # 第四段使总宽 1.6 ATR，必须拒并。
    raw = [_raw_zone(mid, half=0.2) for mid in (100.0, 100.4, 100.8, 101.2)]
    merged = merge_zones(raw, atr_value=1.0)
    assert merged is not None and len(merged) == 2
    assert all(z["width_atr"] <= MAX_ZONE_ATR for z in merged)
    assert merged[0]["kinds"] == ["pivot_low"]
    # 原输入不能被聚类函数原地改写。
    assert [z["mid"] for z in raw] == [100.0, 100.4, 100.8, 101.2]


def test_all_sources_and_pivot_retention_are_auditable():
    df = _ohlcv()
    prices = np.full(240, 100.2)
    volume = np.ones(240)
    volume[-40:] = 20.0
    prices[-40:] = 110.2
    vol = _vol1h(df["ts"].iloc[-1], prices, volume)

    payload = extract_levels(df, vol1h=vol)
    assert payload["version"] == "lv1"
    assert payload["zones"] is not None
    assert payload["degraded"] == []
    assert all(payload["sources"][f"ema{n}"] is not None for n in (21, 55, 100, 200))
    assert payload["sources"]["range_hi"] is not None
    assert payload["sources"]["range_lo"] is not None
    assert payload["sources"]["prev_day_hi"] is not None
    assert payload["sources"]["prev_week_lo"] is not None
    assert payload["sources"]["poc"] is not None
    assert 1 <= len(payload["sources"]["pivot_high"]) <= PIVOTS_PER_SIDE
    assert 1 <= len(payload["sources"]["pivot_low"]) <= PIVOTS_PER_SIDE

    for zone in payload["zones"]:
        assert zone["lo"] <= zone["mid"] <= zone["hi"]
        assert zone["width_atr"] <= MAX_ZONE_ATR or len(zone["kinds"]) == 1
        assert zone["touches"] >= 0
        assert zone["last_touch_bars"] is None or zone["last_touch_bars"] >= 0


def test_poc_uses_volume_bucket_and_rejects_short_coverage():
    df = _ohlcv()
    # bucket 宽=1（atr=4）；高价桶行数少但成交量大，应胜过低价桶。
    prices = np.r_[np.full(180, 100.2), np.full(60, 110.2)]
    volume = np.r_[np.ones(180), np.full(60, 10.0)]
    full = _vol1h(df["ts"].iloc[-1], prices, volume)
    payload = extract_levels(df, vol1h=full, atr_value=4.0)
    poc = payload["sources"]["poc"]
    assert poc is not None and 109.0 < poc["mid"] < 112.0

    short = full.tail(100).reset_index(drop=True)
    degraded = extract_levels(df, vol1h=short, atr_value=4.0)
    assert degraded["sources"]["poc"] is None
    assert "poc_coverage_insufficient" in degraded["degraded"]
    # POC 缺失不能用均价或 0 填进共振 kinds。
    assert all("poc" not in z["kinds"] for z in degraded["zones"])


def test_previous_day_and_week_use_utc_calendar_buckets():
    ts = pd.date_range("2026-01-05", periods=15 * 6, freq="4h", tz="UTC")  # 周一开始
    close = np.full(len(ts), 100.0)
    df = pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": np.ones(len(ts)),
    })
    latest_day = ts[-1].floor("D")
    prev_day = latest_day - pd.Timedelta(days=1)
    df.loc[df["ts"].dt.floor("D") == prev_day, "high"] = 123.0
    prev_week_start = latest_day - pd.Timedelta(days=latest_day.dayofweek + 7)
    df.loc[
        (df["ts"] >= prev_week_start) & (df["ts"] < prev_week_start + pd.Timedelta(days=7)),
        "low",
    ] = 77.0

    payload = extract_levels(df, atr_value=4.0)
    assert payload["sources"]["prev_day_hi"]["mid"] == 123.0
    assert payload["sources"]["prev_week_lo"]["mid"] == 77.0


def test_touch_count_counts_entries_not_resident_bars():
    df = _ohlcv(20, "1h")
    df[["open", "high", "low", "close"]] = 110.0
    # 三根连续处于 [99,101] 是一次触碰；离开后再进入才是第二次。
    df.loc[5:7, ["open", "high", "low", "close"]] = 100.0
    df.loc[12, ["open", "high", "low", "close"]] = 100.0
    merged = merge_zones([_raw_zone(100.0, half=1.0)], atr_value=4.0, price_df=df)
    assert merged is not None and merged[0]["touches"] == 2
    assert merged[0]["last_touch_bars"] == 7


def test_none_propagation_for_no_atr_and_short_history():
    too_short = _ohlcv(10)
    no_atr = extract_levels(too_short)
    assert no_atr["atr"] is None
    assert no_atr["zones"] is None
    assert all(value is None for value in no_atr["sources"].values())
    assert no_atr["degraded"] == ["no_atr"]

    partial = extract_levels(too_short, atr_value=2.0)
    assert partial["sources"]["ema200"] is None
    assert partial["sources"]["range_hi"] is None
    assert partial["sources"]["poc"] is None
    assert "ema200_history_insufficient" in partial["degraded"]
    assert "range_history_insufficient" in partial["degraded"]
    assert "poc_missing" in partial["degraded"]
