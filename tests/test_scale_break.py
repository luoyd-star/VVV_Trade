#!/usr/bin/env python3
"""价格尺度断裂检测（拆股 / 合约重定标）。

守一个真实事故（2026-08-08 由研究队发现、监工核实）：
KORU 在 2026-07-15 做 1:20 拆股，前一根收 481.11、下一根开 22.68。
既有三道健康门——有没有数、时间齐不齐、K 线是否合法——**全部通过**，
于是系统把它当成 −95% 的真实暴跌：连续 trend_down、ATR/BBW 分位 0.992、
方向分 −0.998，污染持续三周无人发现。

最难发现的一点：regime、关键位、cRSI 消费的是**同一条**坏序列，
三层给出的是「一致但全错」的解读——它们互相印证，所以没有任何一层会报警。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime.data import SCALE_BREAK_RATIO, find_scale_breaks  # noqa: E402


def _bars(closes, opens=None):
    n = len(closes)
    opens = opens if opens is not None else closes
    return pd.DataFrame({
        "ts": pd.to_datetime([1_700_000_000_000 + i * 14_400_000 for i in range(n)],
                             unit="ms", utc=True),
        "open": opens, "high": [max(o, c) * 1.01 for o, c in zip(opens, closes)],
        "low": [min(o, c) * 0.99 for o, c in zip(opens, closes)],
        "close": closes, "volume": [1.0] * n,
    })


def test_detects_the_real_koru_reverse_split():
    """KORU 的真实数字：481.11 → 22.68，比例 0.047。"""
    closes = [497.44, 481.11, 481.11, 23.36, 20.98]
    opens = [497.30, 481.11, 481.11, 22.68, 23.36]
    hits = find_scale_breaks(_bars(closes, opens))
    assert len(hits) == 1, f"应恰好检出 1 处，实得 {len(hits)}"
    _, _, pc, op, rr = hits[0]
    assert abs(pc - 481.11) < 1e-6 and abs(op - 22.68) < 1e-6
    assert abs(rr - 22.68 / 481.11) < 1e-9
    assert rr < 1 / SCALE_BREAK_RATIO


def test_forward_split_direction_also_caught():
    """反向（价格变大）同样要抓——合约重定标两个方向都可能。"""
    hits = find_scale_breaks(_bars([10.0, 10.1, 10.05], [10.0, 10.1, 402.0]))
    assert len(hits) == 1 and hits[0][4] > SCALE_BREAK_RATIO


def test_violent_but_real_move_is_not_flagged():
    """真实暴跌不该误报：单日 −40% 很惨烈，但远不到 3 倍。

    这条守的是"宁可偶尔误报也不漏报"的分寸——阈值若压到 1.5 倍，
    加密的真实行情会天天触发，告警就会被无视。
    """
    hits = find_scale_breaks(_bars([100.0, 100.0, 60.0, 55.0], [100.0, 100.0, 62.0, 60.0]))
    assert hits == [], f"−40% 的真实行情被误报: {hits}"


def test_short_and_degenerate_input_is_safe():
    assert find_scale_breaks(_bars([1.0])) == []
    assert find_scale_breaks(pd.DataFrame(
        {"ts": [], "open": [], "high": [], "low": [], "close": [], "volume": []})) == []


def test_zero_and_nan_prices_do_not_crash_or_false_positive():
    """脏数据不得让检测崩溃，也不得因除零冒出假断裂。"""
    df = _bars([100.0, 0.0, 100.0, np.nan, 100.0], [100.0, 0.0, 100.0, 100.0, 100.0])
    hits = find_scale_breaks(df)
    for _, _, pc, _, rr in hits:
        assert pc > 0 and np.isfinite(rr)


def test_ratio_is_configurable_and_symmetric():
    """阈值可调，且两个方向对称——3 倍与 1/3 倍应当同样被视为断裂。"""
    up = find_scale_breaks(_bars([10.0, 10.0, 10.0], [10.0, 10.0, 41.0]), ratio=4.0)
    dn = find_scale_breaks(_bars([10.0, 10.0, 10.0], [10.0, 10.0, 0.24]), ratio=4.0)
    assert len(up) == 1 and len(dn) == 1
    assert find_scale_breaks(_bars([10.0, 10.0, 10.0], [10.0, 10.0, 39.0]), ratio=4.0) == []
