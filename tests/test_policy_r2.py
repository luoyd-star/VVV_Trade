"""R2 + P4：口径元数据、Hermes 边界与分期文档回归。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import dashboard
from regime import agent


ROOT = Path(__file__).resolve().parents[1]


def _event_ts(day: date) -> int:
    return int(datetime.combine(day, dtime(), tzinfo=timezone.utc).timestamp() * 1000)


def _et_ms(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime.combine(
        day, dtime(hour, minute), tzinfo=ZoneInfo("America/New_York"),
    ).timestamp() * 1000)


def test_policy_earnings_pub_type_corrects_same_day_boundary():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE earnings(symbol TEXT, ts INTEGER, source TEXT, pub_type TEXT)"
    )
    day = date(2026, 8, 20)
    conn.executemany(
        "INSERT INTO earnings VALUES(?,?,?,?)",
        [
            ("BEFORE-USDT", _event_ts(day), "moomoo", "BEFORE"),
            ("AFTER-USDT", _event_ts(day), "moomoo", "AFTER"),
            ("UNKNOWN-USDT", _event_ts(day), "moomoo", "REGULAR"),
        ],
    )
    assert dashboard._policy_earnings_proximity(
        conn, "BEFORE-USDT", _et_ms(day, 8),
    )["days"] == 0
    assert dashboard._policy_earnings_proximity(
        conn, "BEFORE-USDT", _et_ms(day, 10),
    )["days"] == -1
    assert dashboard._policy_earnings_proximity(
        conn, "AFTER-USDT", _et_ms(day, 15),
    )["days"] == 0
    assert dashboard._policy_earnings_proximity(
        conn, "AFTER-USDT", _et_ms(day, 17),
    )["days"] == -1
    unknown = dashboard._policy_earnings_proximity(
        conn, "UNKNOWN-USDT", _et_ms(day, 10),
    )
    assert unknown["days"] == 0 and unknown["uncertain"] is True
    conn.close()


def test_stock_front_iv_is_interpolated_to_three_days_with_metadata(monkeypatch):
    now = 1_785_000_000.0
    term = pd.DataFrame([{
        "ts": int(now * 1000),
        "iv3": 40.0, "t3": 2.0,
        "iv9": 30.0, "t9": 9.0,
        "iv30": 28.0, "t30": 30.0,
    }])
    monkeypatch.setattr(dashboard.time, "time", lambda: now)
    monkeypatch.setattr(
        dashboard.instruments, "get", lambda symbol: {"class": "us_stock_perp"},
    )
    monkeypatch.setattr(dashboard.storage, "get_stock_iv_term", lambda *args, **kwargs: term)
    monkeypatch.setattr(dashboard, "_stock_iv_block", lambda *args: None)
    monkeypatch.setattr(dashboard, "_policy_earnings_proximity", lambda *args, **kwargs: None)

    out = dashboard._overview_vol_inputs(object(), "TEST-USDT")
    expected = ((6 / 7 * 40.0**2 * 2 + 1 / 7 * 30.0**2 * 9) / 3) ** 0.5
    assert out["iv3"] == pytest.approx(expected)
    assert out["tenor_days"] == 3.0
    assert out["method"] == "interp" and out["n_expiries"] == 2


def test_overview_context_injects_near_unavailable_timestamps_and_heartbeat():
    payload = {
        "tf": "4h", "asof": 1_785_000_000_000, "updated_at": 1_785_000_060_000,
        "counts": {
            "armed": 0, "wait_signal": 0, "near": 1, "risk": 0,
            "middle": 0, "unavailable": 1,
        },
        "near": [{
            "symbol": "BTC-USDT", "regime_4h": "trend_up", "at": "at_support",
            "meaning": "pullback_long_opportunity", "dist_atr": 0.4,
            "crsi": {"zone": "带内"},
        }],
        "unavailable": [{"symbol": "BAD-USDT", "reason": "poc_stale"}],
        "heartbeat": [{
            "key": "近端IV", "state": "ok", "age_min": 5.0, "note": "30分频",
        }],
    }
    text = agent.render_overview_context(payload)
    assert "数据截至(asof):" in text and "横截面更新(updated_at):" in text
    assert "采集心跳: 近端IV=ok/5.0分·30分频" in text
    assert "BTC-USDT | 4h=trend_up" in text and "cRSI=带内" in text
    assert "BAD-USDT: poc_stale" in text


def test_policy_guard_is_injected_after_legend_and_before_panel(monkeypatch):
    captured = {}
    monkeypatch.setattr(agent, "load_config", lambda: {"provider": "openai", "model": "test"})
    monkeypatch.setattr(agent, "load_system", lambda: "给出最优行动，不要等待。")
    monkeypatch.setattr(agent, "system_brief", lambda: "")
    monkeypatch.setattr(agent, "overview_brief", lambda: "")

    def fake_openai(cfg, system, messages):
        captured["system"] = system
        return "ok"

    monkeypatch.setattr(agent, "_openai", fake_openai)
    result = agent.chat(
        {"symbol": "TEST", "tfs": {}, "collector": {}},
        [{"role": "user", "content": "现在做什么？"}],
    )
    assert result["reply"] == "ok"
    system = captured["system"]
    assert system.index("<panel_legend>") < system.index("<policy_guard") < system.index("\n<panel>\n")
    assert "WAIT 是本系统的正常输出" in system
    assert "禁止命令式开仓、平仓或仓位指令" in system
    assert "消息与叙事永不构成开仓依据（P20）" in system
    assert "满仓或一把梭一律否决" in system


def test_frontend_wording_versions_and_scope_document_are_locked():
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    scope = (ROOT / "docs/POLICY_SCOPE_20260806.md").read_text(encoding="utf-8")
    assert "先验 ${versions.levels}/${versions.location}/${versions.stopcheck}/${versions.volnote} · 未校准" in app
    assert "距最近已收线 4h 收盘价" in app and "距现价" not in app
    assert "距最近已收线 4h 收盘价 ATR" in html and "触碰次数" not in html
    assert "S13 深跌逆势做多（需二次确认·仓位为顺势单的 1/3-1/2）" in dashboard._policy_play(
        "trend_down", "at_support", True, zone={"kinds": ["ema100"]}, tradeable=True,
    )
    for section in range(10):
        assert f"§{section}" in scope
    for script in range(1, 17):
        assert f"S{script} " in scope
