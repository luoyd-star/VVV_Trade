"""R3：4h 主导共振、1d 冲突警示与价格图关键位标注。"""
from __future__ import annotations

from pathlib import Path

import pytest

import dashboard
from regime.agent import render_context, render_overview_context


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "z1d,z1h,score,grade",
    [
        ("超卖区", "带内", 1, "强"),
        ("带内", "带内", 0, "中"),
        ("超买区", "带内", -1, "弱"),
        ("超买区", "超买区", -2, "弱"),
    ],
)
def test_resonance_has_three_grades_and_reverse_votes_subtract(z1d, z1h, score, grade):
    result = dashboard._resonance("at_support", "超卖区", z1d, z1h)
    assert result["main_ok"] is True
    assert result["score"] == score
    assert result["grade"] == grade
    if score < 0:
        assert any("反向" in note for note in result["conflicts"])


def test_resonance_missing_timeframes_are_none_and_do_not_vote():
    result = dashboard._resonance("at_support", "超卖区", None, "带内")
    assert result == {
        "main_tf": "4h",
        "main_zone": "超卖区",
        "main_ok": True,
        "aux": {"1d": None, "1h": 0},
        "score": 0,
        "grade": "中",
        "conflicts": [],
    }


@pytest.mark.parametrize(
    "at,z4,expected",
    [
        ("at_support", "超卖区", True),
        ("at_support", "超买区", False),
        ("at_support", "带内", False),
        ("at_support", None, None),
        ("at_resistance", "超买区", True),
    ],
)
def test_resonance_main_vote_is_strictly_4h(at, z4, expected):
    result = dashboard._resonance(at, z4, "超卖区", "超卖区")
    assert result["main_ok"] is expected
    assert (result["grade"] is not None) is (expected is True)


@pytest.mark.parametrize(
    "regime_4h,regime_1d,triggered",
    [
        ("trend_up", "trend_down", True),
        ("trend_up", "range", True),
        ("range", "trend_down", True),
        ("range", "squeeze", False),
        ("trend_up", "trend_up", False),
        ("trend_up", None, False),
    ],
)
def test_regime_conflict_trigger_conditions(regime_4h, regime_1d, triggered):
    result = dashboard._regime_conflict(regime_4h, regime_1d)
    assert (result is not None) is triggered
    if result:
        assert result["severity"] == "warn"


def _overview_item(symbol: str, grade: str, score: int, *, conflict=None) -> dict:
    return {
        "symbol": symbol, "display": symbol, "regime_4h": "trend_up",
        "regime_1d": "trend_down" if conflict else "trend_up",
        "price": 100.0, "atr": 4.0, "at": "at_support", "role": "support",
        "meaning": "pullback_long_opportunity", "tradeable": True,
        "approach": "from_above", "role_flipped": False,
        "zone": {"lo": 99.0, "hi": 101.0, "kinds": ["ema55"], "touches": 2},
        "dist_atr": 0.2,
        "crsi": {"crsi": 20.0, "pos": -1.0, "zone": "超卖区"},
        "crsi_by_tf": {
            "4h": {"crsi": 20.0, "pos": -1.0, "zone": "超卖区"},
            "1d": {"crsi": 80.0, "pos": 101.0, "zone": "超买区"},
            "1h": {"crsi": 50.0, "pos": 50.0, "zone": "带内"},
        },
        "signal_ok": True, "signal_tf": "4h",
        "resonance": {
            "main_tf": "4h", "main_zone": "超卖区", "main_ok": True,
            "aux": {"1d": score, "1h": 0}, "score": score, "grade": grade,
            "conflicts": ["1d 超买区与 4h 超卖区反向"] if score < 0 else [],
        },
        "regime_conflict": conflict,
        "play": "S4 趋势回踩做多", "vol_note": None, "vol_notes": [],
        "vol_meta": None, "stop_check": None, "degraded": [], "warmup": False,
        "bar_close_ts": 1, "age_sec": 1, "_vol_notes": [],
    }


def test_armed_depends_only_on_4h_main_and_grade_controls_sorting():
    weak_conflict = {
        "note": "4h 趋势上行 vs 1d 趋势下行：这是逆大势的反弹单",
        "severity": "warn",
    }
    missing_aux = _overview_item("MISSING", "中", 0)
    missing_aux["resonance"]["aux"] = {"1d": None, "1h": 0}
    missing_aux["crsi_by_tf"]["1d"] = {"crsi": None, "pos": None, "zone": None}
    scans = [
        {"symbol": "WEAK", "item": _overview_item("WEAK", "弱", -1, conflict=weak_conflict)},
        {"symbol": "MISSING", "item": missing_aux},
        {"symbol": "STRONG", "item": _overview_item("STRONG", "强", 1)},
    ]
    result = dashboard._partition_overview(scans)
    assert [item["symbol"] for item in result["armed"]] == ["STRONG", "MISSING", "WEAK"]
    assert result["counts"]["armed"] == 3
    assert result["armed"][2]["regime_conflict"] == weak_conflict
    assert result["armed"][2]["play"] == "S4 趋势回踩做多"


def test_hermes_both_scopes_carry_resonance_and_regime_conflict():
    conflict = {
        "note": "4h 趋势上行 vs 1d 趋势下行：这是逆大势的反弹单",
        "severity": "warn",
    }
    item = _overview_item("BTC-USDT", "弱", -1, conflict=conflict)
    symbol_text = render_context({
        "symbol": "BTC-USDT", "tfs": {}, "collector": {},
        "policy": item,
    })
    overview_text = render_overview_context({
        "tf": "4h",
        "counts": {"armed": 1, "wait_signal": 0, "near": 0, "risk": 0,
                   "middle": 0, "unavailable": 0},
        "armed": [item],
    })
    for text in (symbol_text, overview_text):
        assert "main_tf=4h main_zone=超卖区 main_ok=True" in text
        assert "aux(1d=-1,1h=0) score=-1 grade=弱" in text
        assert "4h 趋势上行 vs 1d 趋势下行" in text


def test_price_zone_helper_hides_non_4h_and_filters_ineligible_zones():
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert "function policyZoneMarkAreas(tf, zones, hit)" in app
    assert "if (tf !== '4h') return [];" in app
    assert "zone.eligible === true" in app
    assert "borderType: 'dashed'" in app
    assert "data: [...markData, ...zoneMarkData]" in app
    assert "绿虚线=支撑区" in app and "红虚线=压力区" in app
