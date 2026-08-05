"""policy 批 2：波动率与事件标注只在可计算时输出。"""
from __future__ import annotations

import pytest

from regime.policy.volnote import (
    VOLNOTE_VERSION, interpolate_iv_to_tenor, vol_notes,
)


def test_expected_holding_period_move_uses_iv3_formula():
    notes = vol_notes("BTC-USDT", 50_000.0, iv3=44.4)
    assert notes == ["3日预期波动 ±4.0%（IV3 44.4）"]


def test_extreme_environment_and_term_inversion_notes():
    notes = vol_notes(
        "ETH-USDT", 3_000.0, iv30=55.0, iv30_rank=0.91,
        term_inverted=True,
    )
    assert "隐含波动历史高位（分位 0.9），policy §6.5 建议降频降仓" in notes
    assert "3d>30d 倒挂：近端定价高于制度层，短期有事" in notes

    # 严格大于 0.85；边界本身不冒充历史高位。
    assert vol_notes("ETH-USDT", 3_000.0, iv30_rank=0.85) == []


def test_earnings_conflict_and_nearby_windows():
    assert vol_notes("NVDA-USDT", 180.0, earnings_days=0) == [
        "⚠ 持仓窗内有财报（还有 0 日），非技术面风险"
    ]
    assert vol_notes("NVDA-USDT", 180.0, earnings_days=3) == [
        "⚠ 持仓窗内有财报（还有 3 日），非技术面风险"
    ]
    assert vol_notes("NVDA-USDT", 180.0, earnings_days=4) == ["财报临近（4 日）"]
    assert vol_notes("NVDA-USDT", 180.0, earnings_days=10) == ["财报临近（10 日）"]
    assert vol_notes("NVDA-USDT", 180.0, earnings_days=11) == []
    assert vol_notes("NVDA-USDT", 180.0, earnings_days=-1) == []


def test_missing_or_invalid_inputs_are_skipped_without_placeholders():
    assert vol_notes("BTC-USDT", None) == []
    assert vol_notes(
        "BTC-USDT", float("nan"), iv3=float("nan"), iv30_rank=None,
        earnings_days=None, term_inverted=None,
    ) == []
    assert vol_notes("BTC-USDT", 0.0, iv3=40.0) == []
    assert vol_notes("BTC-USDT", 50_000.0, iv3=0.0) == []


def test_nearest_iv_uses_actual_tenor_and_exposes_proxy_metadata():
    notes = vol_notes(
        "DOGE-USDT", 1.0, iv3=50.0,
        tenor_days=1.67, method="nearest", n_expiries=1,
    )
    expected = 50.0 * (1.67 / 365.0) ** 0.5
    assert f"预期波动 ±{expected:.1f}%" in notes[0]
    assert "按实际期限 ~1.7d 计" in notes[0]
    assert "近端IV ~1.7d·单点（3d 代理）" in notes[0]
    assert "可用到期数1" in notes[0]


def test_total_variance_interpolation_and_version():
    result = interpolate_iv_to_tenor(40.0, 2.0, 30.0, 9.0, 3.0)
    expected = ((6 / 7 * 40.0**2 * 2 + 1 / 7 * 30.0**2 * 9) / 3) ** 0.5
    assert result == pytest.approx(expected)
    assert interpolate_iv_to_tenor(40.0, 1.0, 30.0, 2.0, 3.0) is None
    assert VOLNOTE_VERSION == "vol1"


def test_unknown_same_day_earnings_timing_is_marked_uncertain():
    notes = vol_notes(
        "NVDA-USDT", 180.0, earnings_days=0, earnings_uncertain=True,
    )
    assert notes == [
        "⚠ 持仓窗内有财报（还有 0 日，发布时间 uncertain），非技术面风险"
    ]
