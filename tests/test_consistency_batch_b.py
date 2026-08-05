"""批 B 口径统一回归：实验分位、收线判据、CLI 特征窗与 funding 未知态。"""
from __future__ import annotations

import sqlite3
import sys

import numpy as np
import pandas as pd

import dashboard
import main as cli
from regime import data, storage
from regime.classify import FEATURE_WINDOW
from regime.experiments import _rank_series
from regime.features.utils import rolling_pct_rank


def _ohlcv(n: int = 100) -> pd.DataFrame:
    ts = pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC")
    x = np.arange(n, dtype=float) + 100.0
    return pd.DataFrame({
        "ts": ts,
        "open": x,
        "high": x + 1,
        "low": x - 1,
        "close": x + 0.5,
        "volume": x * 10,
    })


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)
    return conn


def test_experiments_rank_matches_production_pointwise_with_ties_and_nan():
    """E1/E2 适配路径必须逐点等于生产实现，包括并列值与 NaN 窗口。"""
    s = pd.Series([1.0, np.nan, 1.0, 2.0, 2.0, np.nan, 0.0, 2.0, 2.0])
    actual = _rank_series(s, win=4).to_numpy()
    expected = rolling_pct_rank(s, window=4)

    np.testing.assert_allclose(actual, expected, equal_nan=True)
    assert actual[2] == 0.0, "并列值必须按严格 < 计为 0，而不是 <= 的 1"
    assert np.isnan(actual[1]), "当前点为 NaN 时必须保持 NaN"


def test_fetch_ohlcv_drops_only_a_truly_unclosed_last_bar(monkeypatch):
    """2026-08-05 起 drop_unclosed 按收线时刻判断，不再恒删末行。"""
    frame = _ohlcv()
    monkeypatch.setitem(data.SOURCES, "stub", lambda symbol, tf, limit: frame.copy())
    last_open = int(frame["ts"].iloc[-1].value // 10**6)
    close_ms = last_open + 3_600_000

    closed, source = data.fetch_ohlcv(
        "TEST-USDT", "1h", sources=["stub"], now_ms=close_ms
    )
    assert source == "stub" and len(closed) == len(frame), \
        "理论收线时刻已到，休市/历史末根必须保留"

    forming, _ = data.fetch_ohlcv(
        "TEST-USDT", "1h", sources=["stub"], now_ms=close_ms - 1
    )
    assert len(forming) == len(frame) - 1, "理论收线时刻未到才删除末根"


def test_ohlcv_upsert_is_idempotent_when_closed_last_bar_reappears():
    """休市期间重复返回同一已收线末根，只覆盖同主键，不产生重复行。"""
    conn = _mem_conn()
    frame = _ohlcv()

    storage.upsert_ohlcv(conn, "TEST-USDT", "1h", frame, "stub")
    storage.upsert_ohlcv(conn, "TEST-USDT", "1h", frame, "stub")

    count = conn.execute(
        "SELECT count(*) FROM ohlcv WHERE symbol='TEST-USDT' AND tf='1h'"
    ).fetchone()[0]
    assert count == len(frame)


def test_cli_requests_and_analyzes_the_shared_feature_window(monkeypatch):
    calls = {}
    frame = _ohlcv(FEATURE_WINDOW + 1)

    def fake_fetch(symbol, tf, limit, sources):
        calls["limit"] = limit
        return frame.copy(), "stub"

    def fake_analyze(df, tf):
        calls["analyzed"] = len(df)
        calls["first_ts"] = df["ts"].iloc[0]
        return object()

    monkeypatch.setattr(cli, "fetch_ohlcv", fake_fetch)
    monkeypatch.setattr(cli, "analyze_timeframe", fake_analyze)
    monkeypatch.setattr(cli, "_fetch_iv", lambda symbol: None)
    monkeypatch.setattr(cli, "render", lambda *args, **kwargs: "ok")
    monkeypatch.setattr(sys, "argv", ["main.py", "--symbols", "TEST-USDT", "--timeframes", "1h"])

    cli.main()

    assert calls["limit"] == FEATURE_WINDOW + 1
    assert calls["analyzed"] == FEATURE_WINDOW
    assert calls["first_ts"] == frame["ts"].iloc[1]


def test_dashboard_does_not_annualize_unknown_funding_interval():
    conn = _mem_conn()
    storage.upsert_deriv(conn, "BTC-USDT", [{
        "ts": 1_785_000_000_000,
        "funding": 0.0001,
        "kind": "pred",
    }])

    payload = dashboard._deriv_payload(conn, "BTC-USDT")

    assert payload["funding_pct"] == 0.01
    assert payload["funding_interval_h"] is None
    assert payload["funding_annual_pct"] is None
