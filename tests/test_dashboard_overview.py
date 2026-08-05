"""policy 批 2：主页分层、剧本矩阵、cRSI 共振与缓存。"""
from __future__ import annotations

import pytest

import dashboard


def _item(symbol: str, *, dist=0.2, tradeable=True, at="at_support",
          notes=None, regime="trend_up") -> dict:
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
        "signal_ok": True if at == "at_support" else None,
        "play": "S4 趋势回踩做多" if tradeable else None,
        "vol_note": "；".join(notes or []) or None,
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
            "S13 深跌逆势做多（需二次确认·半仓）",
        ),
        ("range", "at_support", False, ["range_lo"], "S3 区间下沿做多"),
        ("range", "at_resistance", False, ["range_hi"], "S3 区间上沿做空"),
    ],
)
def test_every_policy_play_cell(regime, at, flipped, kinds, expected):
    assert dashboard._policy_play(
        regime, at, True, role_flipped=flipped, zone={"kinds": kinds}
    ) == expected


def test_play_stays_visible_when_position_arrives_before_signal():
    play = dashboard._policy_play(
        "range", "at_resistance", False, zone={"kinds": ["range_hi"]}
    )
    assert play == "S3 区间上沿做空（位置到了信号没到）"
    assert dashboard._policy_play(
        "trend_down", "at_support", True, zone={"kinds": ["pivot_low"]}
    ) is None


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


def test_overview_partition_is_opportunity_first_and_risk_can_overlap():
    scans = [
        {"symbol": "FAR", "item": _item("FAR", dist=0.4)},
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
        "opportunity": 2, "near": 1, "risk": 2, "middle": 1, "unavailable": 1,
    }
    assert [row["symbol"] for row in result["opportunity"]] == ["CLOSE", "FAR"]
    assert result["opportunity"][0]["dist_atr"] == 0.1
    assert "_vol_notes" not in result["opportunity"][0]
    assert [row["symbol"] for row in result["risk"]] == ["CLOSE", "MID"]
    assert result["near"][0]["crsi"] == {"zone": "超卖区"}
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
    assert [row["symbol"] for row in third["opportunity"]] == ["B", "A"]

