"""policy 批 3：结构止损与 IV3 宽度校验。"""
from __future__ import annotations

import math

import pytest

from regime.policy.stopcheck import check_stop_vs_iv, find_structural_stop


def _zone(lo=99.0, hi=101.0):
    return {
        "lo": lo, "hi": hi, "mid": (lo + hi) / 2,
        "kinds": ["ema21"], "touches": 2, "last_touch_bars": 0,
        "origin_role": "support", "width_atr": (hi - lo) / 4,
    }


@pytest.mark.parametrize(
    "ratio,verdict,note",
    [
        (0.8, "too_tight", "正常波动就会打掉"),
        (1.2, "tight", "止损偏紧"),
        (1.5, "ok", "止损宽度相对预期波动充足"),
    ],
)
def test_stop_check_three_verdict_bands(ratio, verdict, note):
    iv3 = 50.0
    expected = iv3 * math.sqrt(3.0 / 365.0)
    result = check_stop_vs_iv(expected * ratio, iv3)
    assert result is not None
    assert result["expected_move_pct"] == pytest.approx(expected)
    assert result["ratio"] == pytest.approx(ratio)
    assert result["verdict"] == verdict
    assert note in result["note"]


def test_find_structural_stop_and_missing_hit():
    zone = _zone()
    location = {"at": "at_support", "role": "support", "zone": zone}
    result = find_structural_stop(location, [zone], 100.0, 4.0, "long")
    assert result == {"side": "long", "stop_price": 98.0, "stop_dist_pct": 2.0}

    assert find_structural_stop(
        {"at": "middle_zone", "role": None, "zone": None}, [zone],
        100.0, 4.0, "long",
    ) is None
    assert find_structural_stop(location, [], 100.0, 4.0, "long") is None


def test_structural_stop_rejects_wrong_side_and_unreasonable_distance():
    support = _zone(lo=70.0, hi=71.0)
    location = {"at": "at_support", "role": "support", "zone": support}
    assert find_structural_stop(location, [support], 100.0, 4.0, "long") is None
    assert find_structural_stop(location, [support], 100.0, 4.0, "short") is None


def test_iv3_missing_or_invalid_returns_none():
    assert check_stop_vs_iv(2.0, None) is None
    assert check_stop_vs_iv(2.0, float("nan")) is None
    assert check_stop_vs_iv(2.0, 0.0) is None
    assert check_stop_vs_iv(0.0, 50.0) is None

