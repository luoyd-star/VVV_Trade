"""policy 批 2：波动率与事件标注只在可计算时输出。"""
from __future__ import annotations

from regime.policy.volnote import vol_notes


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

