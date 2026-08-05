"""policy 批 1：regime-aware 位置语义与来向门槛。"""
from __future__ import annotations

import math

import pytest

from regime.policy.location import infer_approach, locate


def _zone(kind: str, role: str, lo: float = 99.0, hi: float = 101.0) -> dict:
    return {
        "lo": lo,
        "hi": hi,
        "mid": (lo + hi) / 2,
        "kinds": [kind],
        "touches": 1,
        "last_touch_bars": 0,
        "origin_role": role,
        "width_atr": (hi - lo) / 4,
    }


def test_approach_three_states():
    zone = _zone("ema21", "support")
    assert infer_approach([105.0, 103.0, 100.5], zone) == "from_above"
    assert infer_approach([95.0, 97.0, 99.5], zone) == "from_below"
    assert infer_approach([102.0, 104.0, 103.0], zone) is None
    assert infer_approach([100.0, 100.5], zone) is None
    assert infer_approach([95.0, math.nan, 99.0], zone) is None


def test_role_flip_above_old_resistance_and_below_old_support():
    old_high = _zone("pivot_high", "resistance")
    up = locate(101.4, 4.0, [old_high], "trend_up", "from_above")
    assert up["at"] == "at_support"
    assert up["role"] == "support" and up["role_flipped"] is True
    assert up["dist_atr"] == pytest.approx(0.1)

    old_low = _zone("pivot_low", "support")
    down = locate(98.6, 4.0, [old_low], "trend_down", "from_below")
    assert down["at"] == "at_resistance"
    assert down["role"] == "resistance" and down["role_flipped"] is True
    assert down["tradeable"] is True


@pytest.mark.parametrize(
    "regime,kind,origin_role,approach,expected_at,expected_meaning,tradeable",
    [
        ("trend_up", "ema21", "support", "from_above", "at_support", "pullback_long_opportunity", True),
        ("trend_up", "pivot_high", "resistance", None, "at_resistance", "reduce_long_not_open_short", False),
        ("trend_down", "ema200", "support", None, "at_support", "deep_long_requires_second_confirmation", True),
        ("trend_down", "ema55", "resistance", "from_below", "at_resistance", "bounce_short_opportunity", True),
        ("range", "range_lo", "support", None, "at_support", "range_edge_opportunity", True),
        ("range", "range_hi", "resistance", None, "at_resistance", "range_edge_opportunity", True),
        ("squeeze", "range_lo", "support", None, "at_support", "wait_for_confirmed_breakout", False),
        ("squeeze", "range_hi", "resistance", None, "at_resistance", "wait_for_confirmed_breakout", False),
        ("high_vol_chop", "range_lo", "support", None, "at_support", "high_vol_reduce_frequency", False),
        ("high_vol_chop", "range_hi", "resistance", None, "at_resistance", "high_vol_reduce_frequency", False),
    ],
)
def test_every_regime_location_semantics_cell(
    regime, kind, origin_role, approach, expected_at, expected_meaning, tradeable,
):
    result = locate(100.0, 4.0, [_zone(kind, origin_role)], regime, approach)
    assert result["at"] == expected_at
    assert result["meaning"] == expected_meaning
    assert result["tradeable"] is tradeable
    assert result["reason"] is None


def test_trend_down_prior_low_is_not_deep_long_opportunity():
    result = locate(100.0, 4.0, [_zone("pivot_low", "support")], "trend_down", None)
    assert result["at"] == "at_support"
    assert result["meaning"] == "prior_low_support_not_tradeable"
    assert result["tradeable"] is False


@pytest.mark.parametrize(
    "regime,kind,role,wrong",
    [
        ("trend_up", "ema21", "support", "from_below"),
        ("trend_up", "ema55", "support", None),
        ("trend_down", "ema21", "resistance", "from_above"),
        ("trend_down", "ema55", "resistance", None),
    ],
)
def test_wrong_approach_vetoes_trend_opportunity(regime, kind, role, wrong):
    result = locate(100.0, 4.0, [_zone(kind, role)], regime, wrong)
    assert result["tradeable"] is False
    assert result["reason"] == "wrong_approach"


@pytest.mark.parametrize("regime", ["trend_up", "trend_down", "range", "squeeze", "high_vol_chop"])
def test_middle_zone_is_never_tradeable_in_any_regime(regime):
    # POC 是可测量的 level，但用户语义表没有把它列为任何 regime 的机会来源。
    result = locate(100.0, 4.0, [_zone("poc", "support")], regime, None)
    assert result["at"] == "middle_zone"
    assert result["zone"] is None
    assert result["dist_atr"] is None
    assert result["tradeable"] is False
    assert result["meaning"] == "middle_zone_veto"


def test_same_price_is_regime_dependent():
    ema_pullback = _zone("ema21", "support")
    up = locate(100.0, 4.0, [ema_pullback], "trend_up", "from_above")
    sideways = locate(100.0, 4.0, [ema_pullback], "range", "from_above")
    assert up["at"] == "at_support" and up["tradeable"] is True
    assert sideways["at"] == "middle_zone" and sideways["tradeable"] is False


def test_near_zone_distance_and_outside_threshold():
    zone = _zone("range_lo", "support")
    near = locate(102.0, 4.0, [zone], "range", None)
    assert near["at"] == "at_support" and near["dist_atr"] == pytest.approx(0.25)
    far = locate(104.0, 4.0, [zone], "range", None)
    assert far["at"] == "middle_zone" and far["dist_atr"] is None


@pytest.mark.parametrize(
    "price,atr,zones,reason",
    [
        (100.0, 0.0, [_zone("range_lo", "support")], "no_atr"),
        (100.0, math.nan, [_zone("range_lo", "support")], "no_atr"),
        (math.nan, 4.0, [_zone("range_lo", "support")], "no_price"),
        (100.0, 4.0, None, "no_zones"),
        (100.0, 4.0, [], "no_zones"),
    ],
)
def test_unavailable_inputs_propagate_none(price, atr, zones, reason):
    result = locate(price, atr, zones, "range", None)
    assert result["at"] is None
    assert result["zone"] is None
    assert result["dist_atr"] is None
    assert result["role"] is None
    assert result["meaning"] is None
    assert result["tradeable"] is False
    assert result["degraded"] == [reason]

