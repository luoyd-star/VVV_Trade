"""OHLCV 多源健康度偏好与短历史入库回归。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from regime import data


def _ohlcv(n: int, *, gap_after: int | None = None) -> pd.DataFrame:
    ts = pd.date_range("2020-01-01", periods=n, freq="D", tz="UTC")
    if gap_after is not None:
        ts = ts[:gap_after].append(ts[gap_after:] + pd.Timedelta(days=1))
    values = np.arange(n, dtype=float) + 100.0
    return pd.DataFrame({
        "ts": ts,
        "open": values,
        "high": values + 1.0,
        "low": values - 1.0,
        "close": values + 0.5,
        "volume": values * 10.0,
    })


def _install_sources(monkeypatch, **frames: pd.DataFrame) -> None:
    for name, frame in frames.items():
        monkeypatch.setitem(
            data.SOURCES,
            name,
            lambda symbol, timeframe, limit, frame=frame: frame.copy(),
        )


def test_healthy_source_beats_all_short_sources(monkeypatch):
    """不能让先返回的 5 根或较长的 89 根抢走后面的健康源。"""
    _install_sources(
        monkeypatch,
        short_first=_ohlcv(5),
        short_longer=_ohlcv(89),
        healthy=_ohlcv(data.HEALTHY_BARS),
    )

    frame, source = data.fetch_ohlcv(
        "TEST-USDT", "1d", sources=["short_first", "short_longer", "healthy"]
    )

    assert source == "healthy"
    assert len(frame) == data.HEALTHY_BARS


def test_only_short_sources_returns_the_longest(monkeypatch):
    _install_sources(
        monkeypatch,
        short_first=_ohlcv(5),
        short_longest=_ohlcv(83),
        short_last=_ohlcv(40),
    )

    frame, source = data.fetch_ohlcv(
        "TEST-USDT", "1d", sources=["short_first", "short_longest", "short_last"]
    )

    assert source == "short_longest"
    assert len(frame) == 83


@pytest.mark.parametrize(
    ("first_n", "second_n", "expected_source"),
    [
        (89, 90, "second"),
        (90, 91, "first"),
        (91, 90, "first"),
    ],
)
def test_healthy_boundary_is_90(
    monkeypatch, first_n: int, second_n: int, expected_source: str
):
    """89 是降级候选；90/91 都是健康源，并维持 sources 配置顺序。"""
    assert data.HEALTHY_BARS == 90
    _install_sources(
        monkeypatch,
        first=_ohlcv(first_n),
        second=_ohlcv(second_n),
    )

    frame, source = data.fetch_ohlcv(
        "TEST-USDT", "1d", sources=["first", "second"]
    )

    assert source == expected_source
    assert len(frame) == (first_n if expected_source == "first" else second_n)


def test_83_closed_daily_bars_are_returned(monkeypatch):
    _install_sources(monkeypatch, recent_listing=_ohlcv(83))

    frame, source = data.fetch_ohlcv(
        "ORCL-USDT", "1d", sources=["recent_listing"]
    )

    assert source == "recent_listing"
    assert len(frame) == 83


def test_all_sources_with_zero_closed_bars_still_raise(monkeypatch):
    _install_sources(monkeypatch, empty_a=_ohlcv(0), empty_b=_ohlcv(0))

    with pytest.raises(RuntimeError, match="无已收盘 K 线"):
        data.fetch_ohlcv("TEST-USDT", "1d", sources=["empty_a", "empty_b"])


def test_gap_source_is_rejected_for_gapless_fallback(monkeypatch):
    _install_sources(
        monkeypatch,
        gappy=_ohlcv(91, gap_after=45),
        gapless=_ohlcv(83),
    )

    frame, source = data.fetch_ohlcv(
        "TEST-USDT", "1d", sources=["gappy", "gapless"]
    )

    assert source == "gapless"
    assert len(frame) == 83


def test_all_sources_with_gaps_still_raise(monkeypatch):
    _install_sources(
        monkeypatch,
        gappy_a=_ohlcv(91, gap_after=45),
        gappy_b=_ohlcv(83, gap_after=40),
    )

    with pytest.raises(RuntimeError, match="序列有 1 处缺口"):
        data.fetch_ohlcv("TEST-USDT", "1d", sources=["gappy_a", "gappy_b"])
