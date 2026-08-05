"""波动率与事件风险标注。

本模块只把调用方已经装配好的价格、IV 与事件输入翻译成展示文案；它不读取
数据、不改变 policy 动作，也不拿缺失值生成占位提示。
"""
from __future__ import annotations

import math
from typing import Optional


HOLDING_DAYS = 3                 # 起步值，待校准；与 1-3 天持仓前端口径对齐
DAYS_PER_YEAR = 365              # 起步值，待校准；IV 年化换算沿用自然日口径
IV30_HIGH_RANK = 0.85            # 起步值，待校准；policy §6.5 的极端环境提示门槛
EARNINGS_WINDOW_DAYS = 3         # 起步值，待校准；财报落入计划持仓窗的边界
EARNINGS_NEAR_DAYS = 10          # 起步值，待校准；财报临近的最远提示边界


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def vol_notes(symbol, price, iv3=None, iv30=None, iv30_rank=None,
              rv3=None, earnings_days=None, term_inverted=None) -> list[str]:
    """返回波动率与事件风险标注；缺失或非法的单项直接跳过。

    ``symbol``、``iv30`` 与 ``rv3`` 保留在接口中，便于消费方按同一口径装配完整
    波动快照；本批三类文案不单独使用它们。
    """
    del symbol, iv30, rv3
    notes: list[str] = []

    price_f = _finite(price)
    iv3_f = _finite(iv3)
    if price_f is not None and price_f > 0 and iv3_f is not None and iv3_f >= 0:
        expected_abs = price_f * iv3_f / 100.0 * math.sqrt(HOLDING_DAYS / DAYS_PER_YEAR)
        expected_pct = expected_abs / price_f * 100.0
        notes.append(f"3日预期波动 ±{expected_pct:.1f}%（IV3 {iv3_f:.1f}）")

    rank_f = _finite(iv30_rank)
    if rank_f is not None and rank_f > IV30_HIGH_RANK:
        notes.append(
            f"隐含波动历史高位（分位 {rank_f:.1f}），policy §6.5 建议降频降仓"
        )

    if term_inverted is True:
        notes.append("3d>30d 倒挂：近端定价高于制度层，短期有事")

    earnings_f = _finite(earnings_days)
    if earnings_f is not None:
        days_text = f"{earnings_f:g}"
        if 0 <= earnings_f <= EARNINGS_WINDOW_DAYS:
            notes.append(f"⚠ 持仓窗内有财报（还有 {days_text} 日），非技术面风险")
        elif EARNINGS_WINDOW_DAYS < earnings_f <= EARNINGS_NEAR_DAYS:
            notes.append(f"财报临近（{days_text} 日）")

    return notes
