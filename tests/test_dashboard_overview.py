"""policy 批 2：主页分层、剧本矩阵、cRSI 共振与缓存。"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

import dashboard


ROOT = Path(__file__).resolve().parents[1]


def _item(symbol: str, *, dist=0.2, tradeable=True, at="at_support",
          notes=None, regime="trend_up", signal_ok=True,
          bar_close_ts=80_000, warmup=False) -> dict:
    zone = {
        "lo": 99.0, "hi": 101.0, "kinds": ["ema21"],
        "touches": 2, "width_atr": 0.5,
    } if at != "middle_zone" else None
    return {
        "symbol": symbol,
        "display": f"{symbol}名称",
        "regime_4h": regime,
        "regime_1d": "range",
        "price": 100.0,
        "atr": 4.0,
        "at": at,
        "role": "support" if at == "at_support" else (
            "resistance" if at == "at_resistance" else None
        ),
        "meaning": "middle_zone_veto" if at == "middle_zone" else "pullback_long_opportunity",
        "tradeable": tradeable,
        "approach": "from_above",
        "role_flipped": False,
        "zone": zone,
        "dist_atr": None if at == "middle_zone" else dist,
        "crsi": {"crsi": 20.0, "pos": -2.0, "zone": "超卖区"},
        "signal_ok": signal_ok if at in {"at_support", "at_resistance"} else None,
        "signal_tf": "1h",
        "play": "S4 趋势回踩做多" if tradeable else None,
        "vol_note": "；".join(notes or []) or None,
        "vol_notes": list(notes or []),
        "stop_check": {
            "verdict": "ok", "ratio": 1.2, "note": "宽度覆盖持仓期预期波动",
        },
        "degraded": [],
        "warmup": warmup,
        "bar_close_ts": bar_close_ts,
        "age_sec": 20,
        "_vol_notes": list(notes or []),
    }


@pytest.mark.parametrize(
    "regime,at,flipped,kinds,expected",
    [
        ("trend_up", "at_support", False, ["ema21"], "S4 趋势回踩做多"),
        ("trend_up", "at_support", True, ["pivot_high"], "S5 突破回踩加仓"),
        ("trend_down", "at_resistance", False, ["ema55"], "S1 反弹至压力做空"),
        (
            "trend_down", "at_support", False, ["ema100"],
            "S13 深跌逆势做多（需二次确认·仓位为顺势单的 1/3-1/2）",
        ),
        ("range", "at_support", False, ["range_lo"], "S3 区间下沿做多"),
        ("range", "at_resistance", False, ["range_hi"], "S3 区间上沿做空"),
    ],
)
def test_every_policy_play_cell(regime, at, flipped, kinds, expected):
    assert dashboard._policy_play(
        regime, at, True, role_flipped=flipped, zone={"kinds": kinds},
        tradeable=True, reason=None,
    ) == expected


def test_play_stays_visible_when_position_arrives_before_signal():
    play = dashboard._policy_play(
        "range", "at_resistance", False, zone={"kinds": ["range_hi"]},
        tradeable=True, reason=None,
    )
    assert play == "S3 区间上沿做空（位置到了信号没到）"
    assert dashboard._policy_play(
        "trend_down", "at_support", True, zone={"kinds": ["pivot_low"]},
        tradeable=False, reason=None,
    ) is None


def test_play_downgrades_to_wait_reference_when_approach_gate_fails():
    play = dashboard._policy_play(
        "trend_up", "at_support", False, role_flipped=True,
        zone={"kinds": ["pivot_high"]}, tradeable=False,
        reason="wrong_approach",
    )
    assert play == "WAIT · 路径未确认（P09） · 参考剧本：S5 突破回踩加仓"


@pytest.mark.parametrize(
    "at,zone,expected",
    [
        ("at_support", "超卖区", True),
        ("at_support", "超买区", False),
        ("at_support", "带内", False),
        ("at_resistance", "超买区", True),
        ("at_resistance", "超卖区", False),
        ("at_resistance", "带内", False),
        ("middle_zone", "超卖区", None),
        ("at_support", None, None),
    ],
)
def test_signal_ok_direction(at, zone, expected):
    assert dashboard._signal_ok(at, zone) is expected


def test_overview_partition_splits_armed_and_wait_signal_and_risk_can_overlap():
    scans = [
        {"symbol": "FAR", "item": _item("FAR", dist=0.4, signal_ok=False)},
        {"symbol": "CLOSE", "item": _item("CLOSE", dist=0.1, notes=["风险A"])},
        {"symbol": "NEAR", "item": _item("NEAR", dist=0.2, tradeable=False)},
        {
            "symbol": "MID",
            "item": _item("MID", tradeable=False, at="middle_zone", notes=["风险B"]),
        },
        {"symbol": "MISS", "reason": "crsi_unavailable"},
    ]
    result = dashboard._partition_overview(scans)

    assert result["counts"] == {
        "armed": 1, "wait_signal": 1, "near": 1, "risk": 2,
        "middle": 1, "unavailable": 1,
    }
    assert [row["symbol"] for row in result["armed"]] == ["CLOSE"]
    assert [row["symbol"] for row in result["wait_signal"]] == ["FAR"]
    assert result["armed"][0]["dist_atr"] == 0.1
    assert result["armed"][0]["stop_check"]["verdict"] == "ok"
    assert "_vol_notes" not in result["armed"][0]
    assert [row["symbol"] for row in result["risk"]] == ["CLOSE", "MID"]
    assert result["near"][0]["crsi"] == {"zone": "超卖区"}
    assert result["near"][0]["stop_check"]["verdict"] == "ok"
    assert result["near"][0]["signal_tf"] == "1h"
    assert result["unavailable"] == [{"symbol": "MISS", "reason": "crsi_unavailable"}]


def test_overview_payload_uses_60_second_cache(monkeypatch):
    class Conn:
        def close(self):
            pass

    clock = {"now": 100.0}
    calls = []
    monkeypatch.setattr(dashboard, "_OVERVIEW_CACHE", {"at": 0.0, "payload": None})
    monkeypatch.setattr(dashboard.time, "time", lambda: clock["now"])
    monkeypatch.setattr(dashboard.storage, "connect_ro", Conn)
    monkeypatch.setattr(dashboard.storage, "symbols", lambda conn: ["A", "B"])
    monkeypatch.setattr(dashboard, "_heartbeat", lambda conn: [{"key": "测试", "state": "ok"}])

    def scan(conn, symbol):
        calls.append(symbol)
        return {"symbol": symbol, "item": _item(symbol, dist=0.1 if symbol == "B" else 0.2)}

    monkeypatch.setattr(dashboard, "_scan_overview_symbol", scan)
    first = dashboard.overview_payload()
    clock["now"] = 159.0
    second = dashboard.overview_payload()
    assert second is first
    assert calls == ["A", "B"]

    clock["now"] = 160.0
    third = dashboard.overview_payload()
    assert third is not first
    assert calls == ["A", "B", "A", "B"]
    assert third["tf"] == "4h"
    assert third["updated_at"] == 160_000
    assert third["asof"] == 80_000
    assert [row["symbol"] for row in third["armed"]] == ["B", "A"]


def test_signal_uses_only_closed_1h_bars_at_4h_asof_and_never_falls_back(monkeypatch):
    frame = pd.DataFrame({
        "ts": pd.date_range("2026-08-06T08:00:00Z", periods=5, freq="1h"),
        "close": [1, 2, 3, 4, 5],
    })
    seen = {}
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: frame)

    def fake_crsi(closed):
        seen["last_ts"] = closed["ts"].iloc[-1]
        return {"last": {"crsi": 20.0, "pos": -1.0, "zone": "超卖区"}}

    monkeypatch.setattr(dashboard, "crsi_features", fake_crsi)
    asof = int(pd.Timestamp("2026-08-06T12:00:00Z").timestamp() * 1000)
    result = dashboard._policy_signal_snapshot(None, "TEST", "at_support", asof)

    assert seen["last_ts"] == pd.Timestamp("2026-08-06T11:00:00Z")
    assert result["signal_tf"] == "1h"
    assert result["signal_ok"] is True
    assert result["degraded"] == []

    monkeypatch.setattr(
        dashboard.storage, "get_ohlcv", lambda *args, **kwargs: pd.DataFrame(),
    )
    missing = dashboard._policy_signal_snapshot(None, "TEST", "at_support", asof)
    assert missing["signal_tf"] == "1h"
    assert missing["signal_ok"] is None
    assert missing["crsi"] == {"crsi": None, "pos": None, "zone": None}
    assert missing["degraded"] == ["crsi_1h_missing"]


def test_overview_stale_gate_preserves_bar_timing(monkeypatch):
    frame = pd.DataFrame({
        "ts": [pd.Timestamp("1970-01-01T00:00:00Z")],
        "close": [100.0],
    })
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: frame)
    monkeypatch.setattr(
        dashboard.time, "time",
        lambda: dashboard.TF_SEC["4h"] + dashboard.STALE_TF_MULTIPLIER
        * dashboard.TF_SEC["4h"] + 1,
    )
    result = dashboard._scan_overview_symbol(None, "TEST")
    assert result == {
        "symbol": "TEST",
        "reason": "stale_4h",
        "bar_close_ts": dashboard.TF_SEC["4h"] * 1000,
        "age_sec": int(dashboard.STALE_TF_MULTIPLIER * dashboard.TF_SEC["4h"] + 1),
    }


def test_latest_state_warmup_comes_from_same_audit_row():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE regime_history(symbol TEXT, tf TEXT, ts INTEGER, state TEXT, features TEXT)"
    )
    conn.executemany(
        "INSERT INTO regime_history VALUES(?,?,?,?,?)",
        [
            ("TEST", "4h", 1, "range", json.dumps({"warmup": False})),
            ("TEST", "4h", 2, "trend_up", json.dumps({"warmup": True})),
        ],
    )
    assert dashboard._latest_state_snapshot(conn, "TEST", "4h") == {
        "ts": 2, "state": "trend_up", "warmup": True,
    }
    conn.close()


def test_scan_propagates_stop_signal_timing_and_warmup(monkeypatch):
    opens = pd.date_range("2026-08-06T00:00:00Z", periods=3, freq="4h")
    frame_4h = pd.DataFrame({
        "ts": opens,
        "open": [105.0, 103.0, 100.0],
        "high": [106.0, 104.0, 101.0],
        "low": [104.0, 102.0, 99.0],
        "close": [105.0, 103.0, 100.0],
        "volume": [10.0, 10.0, 10.0],
    })
    frame_1h = pd.DataFrame({
        "ts": pd.date_range("2026-08-05T00:00:00Z", periods=36, freq="1h"),
        "close": list(range(36)),
    })
    zone = {
        "lo": 99.0, "hi": 101.0, "mid": 100.0, "kinds": ["ema21"],
        "touches": 2, "last_touch_bars": 0, "origin_role": "support",
        "width_atr": 0.5,
    }

    def get_ohlcv(conn, symbol, tf, limit):
        return frame_1h if tf == "1h" else frame_4h

    asof = int(pd.Timestamp("2026-08-06T12:00:00Z").timestamp())
    monkeypatch.setattr(dashboard.time, "time", lambda: asof + 60)
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", get_ohlcv)
    monkeypatch.setattr(
        dashboard.storage, "get_vol1h",
        lambda *args, **kwargs: pd.DataFrame(columns=["ts", "volume", "quote_vol"]),
    )
    monkeypatch.setattr(
        dashboard, "_latest_state_snapshot",
        lambda *args: {"ts": None, "state": "trend_up", "warmup": True},
    )
    monkeypatch.setattr(dashboard, "_latest_state", lambda *args: "range")
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
        lambda *args: {
            "iv3": 50.0, "iv30": None, "iv30_rank": None, "rv3": None,
            "earnings_days": None, "term_inverted": None,
        },
    )
    monkeypatch.setattr(dashboard, "vol_notes", lambda *args, **kwargs: ["风险注记"])
    monkeypatch.setattr(
        dashboard.instruments, "get", lambda symbol: {"display": "测试品种"},
    )

    result = dashboard._scan_overview_symbol(None, "TEST")
    item = result["item"]
    assert item["signal_tf"] == "1h" and item["signal_ok"] is True
    assert item["warmup"] is True
    assert item["bar_close_ts"] == asof * 1000 and item["age_sec"] == 60
    assert item["stop_check"]["verdict"] == "too_tight"
    assert item["vol_notes"] == ["风险注记"]


def test_frontend_uses_backend_path_gate_and_validated_deep_link():
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    overview = (ROOT / "web/overview.js").read_text(encoding="utf-8")
    html = (ROOT / "web/overview.html").read_text(encoding="utf-8")

    assert "new URLSearchParams(window.location.search).get('symbol')" in app
    assert "URL 中的品种不存在" in app
    assert "window.history.replaceState" in app
    assert "let required = null" not in app
    assert "location.reason === 'wrong_approach'" in app
    assert "data.armed || []" in overview and "data.wait_signal || []" in overview
    assert "item.warmup === true" in overview
    assert "id=\"asof\"" in html and "▸ 位置候选" in html
