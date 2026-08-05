"""policy 批 3：详情 payload、总览上下文与 chat scope。"""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

import dashboard
from regime.agent import render_overview_context


def _zone():
    return {
        "lo": 99.0, "hi": 101.0, "mid": 100.0,
        "kinds": ["ema21"], "touches": 2, "last_touch_bars": 0,
        "origin_role": "support", "width_atr": 0.5,
    }


def test_policy_block_has_full_location_all_zones_and_stop_check(monkeypatch):
    df = pd.DataFrame({
        "ts": pd.date_range("2026-08-01", periods=3, freq="4h", tz="UTC"),
        "open": [105.0, 103.0, 100.0],
        "high": [106.0, 104.0, 101.0],
        "low": [104.0, 102.0, 99.0],
        "close": [105.0, 103.0, 100.0],
        "volume": [10.0, 10.0, 10.0],
    })
    zone = _zone()
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: df)
    monkeypatch.setattr(
        dashboard.storage, "ts_to_ms",
        lambda values: np.array([int(pd.Timestamp(value).timestamp() * 1000) for value in values]),
    )
    monkeypatch.setattr(
        dashboard.storage, "get_vol1h",
        lambda *args, **kwargs: pd.DataFrame(columns=["ts", "volume", "quote_vol"]),
    )
    monkeypatch.setattr(
        dashboard, "extract_levels",
        lambda *args, **kwargs: {"atr": 4.0, "zones": [zone], "degraded": []},
    )
    monkeypatch.setattr(
        dashboard, "crsi_features",
        lambda frame: {"last": {"crsi": 20.0, "pos": -2.0, "zone": "超卖区"}},
    )
    monkeypatch.setattr(
        dashboard, "_overview_vol_inputs",
        lambda *args, **kwargs: {
            "iv3": 50.0, "iv30": None, "iv30_rank": None, "rv3": None,
            "earnings_days": None, "term_inverted": None,
        },
    )

    policy = dashboard._build_policy_payload(
        object(), "TEST-USDT",
        {"4h": {"state": "trend_up"}, "1d": {"state": "range"}},
    )
    assert set(policy) == {
        "tf", "regime_4h", "regime_1d", "price", "atr", "location", "crsi",
        "signal_ok", "play", "zones", "vol_notes", "stop_check", "degraded",
    }
    assert policy["location"]["at"] == "at_support"
    assert policy["location"]["approach"] == "from_above"
    assert policy["signal_ok"] is True
    assert policy["play"] == "S4 趋势回踩做多"
    assert policy["zones"] == [{
        **zone, "dist_atr": 0.0, "role_now": "support",
        "role_flipped": False, "eligible": True,
    }]
    assert policy["stop_check"]["side"] == "long"
    assert policy["stop_check"]["stop_price"] == 98.0
    assert policy["stop_check"]["verdict"] == "too_tight"
    assert policy["degraded"] == []


def test_overview_context_renders_cross_section_without_listing_wait_rows():
    payload = {
        "tf": "4h",
        "counts": {"opportunity": 1, "near": 2, "risk": 1, "middle": 7, "unavailable": 1},
        "opportunity": [{
            "symbol": "BTC-USDT", "regime_4h": "trend_up", "regime_1d": "range",
            "at": "at_support", "meaning": "pullback_long_opportunity",
            "dist_atr": 0.2, "approach": "from_above",
            "crsi": {"crsi": 20.0, "zone": "超卖区"}, "signal_ok": True,
            "play": "S4 趋势回踩做多", "vol_note": "3日预期波动 ±4.0%",
        }],
        "risk": [{"symbol": "ETH-USDT", "notes": ["3d>30d 倒挂"]}],
        "middle": [{"symbol": "WAIT-SHOULD-NOT-APPEAR"}],
    }
    text = render_overview_context(payload)
    assert text.startswith("当前时刻:")
    assert "机会 1 个 · 接近 2 个 · 风险 1 个 · 观望 7 个" in text
    assert "BTC-USDT" in text and "位置+信号共振" in text
    assert "S4 趋势回踩做多" in text and "3日预期波动" in text
    assert "ETH-USDT: 3d>30d 倒挂" in text
    assert "观望区: 7 个" in text
    assert "WAIT-SHOULD-NOT-APPEAR" not in text


def test_chat_scope_migration_keeps_one_shared_stream():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE chat(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "role TEXT NOT NULL, content TEXT NOT NULL)"
    )
    conn.execute("INSERT INTO chat(ts, role, content) VALUES(1, 'user', '旧消息')")
    dashboard._add_chat_message(conn, "user", "总览问题", "overview")
    dashboard._add_chat_message(conn, "assistant", "总览回答", "overview")

    messages = dashboard._get_chat_messages(conn, 10)
    assert [message["content"] for message in messages] == ["旧消息", "总览问题", "总览回答"]
    assert [message["scope"] for message in messages] == ["symbol", "overview", "overview"]
    conn.close()
