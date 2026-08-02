#!/usr/bin/env python3
"""非对称迟滞 confirm_states 的全分支回归。

用法: .venv/bin/python tests/test_confirm_states.py

这是系统里唯一的状态机，collector 与 dashboard 双路径依赖，任何 off-by-one
都会静默改变每一个对外信号。确认根数：一般态 2 根、恢复 range 3 根、
high_vol_chop 立即（1 根）。语义基准：**"连续 N 根新状态"指 raw 序列里
出现 N 根，确认发生在第 N 根上**（含它自己）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime.classify import _confirm_need, confirm_states  # noqa: E402

T, R, C, S = "trend_up", "range", "high_vol_chop", "squeeze"


def main() -> None:
    # ① 确认根数常量本身
    assert _confirm_need(C) == 1 and _confirm_need(R) == 3 and _confirm_need(T) == 2
    print("① 确认根数    chop=1 / range=3 / 其余=2 ✓")

    # ② 一般态 2 根确认：第 2 根 raw=T 时确认切换（不是第 3 根）
    conf, cand = confirm_states([R, R, T, T, T])
    assert conf == [R, R, R, T, T], f"2 根确认边界错: {conf}"
    assert cand is None
    print("② 两根确认    [R,R,T,T,T] → [R,R,R,T,T] ✓")

    # ③ 恢复 range 3 根：第 3 根 raw=R 才确认；2 根时仍是旧态且报候选
    conf, cand = confirm_states([T, T, R, R])
    assert conf == [T, T, T, T], f"range 2 根就切了: {conf}"
    assert cand == {"state": R, "count": 2, "need": 3}, f"候选计数错: {cand}"
    conf, _ = confirm_states([T, T, R, R, R])
    assert conf[-1] == R and conf[-2] == T, f"range 第 3 根应确认: {conf}"
    print("③ 恢复震荡    2 根不切且报候选 (2/3)，第 3 根切 ✓")

    # ④ 冲击立即：单根 chop 直接确认
    conf, cand = confirm_states([R, R, C])
    assert conf == [R, R, C] and cand is None, f"chop 未立即确认: {conf}"
    print("④ 冲击立即    [R,R,C] → [R,R,C] ✓")

    # ⑤ 抖动重置：A/B 来回跳时计数必须清零，永不凑数确认
    conf, cand = confirm_states([R, T, S, T, S, T, S])
    assert all(x == R for x in conf), f"抖动序列不该确认任何切换: {conf}"
    assert cand == {"state": S, "count": 1, "need": 2}, f"末端候选错: {cand}"
    print("⑤ 抖动重置    T/S 交替 6 根零确认，候选停在 1/2 ✓")

    # ⑥ 回到当前态清空酝酿：T,T,R,R 后一根 T，候选必须归零
    conf, cand = confirm_states([T, T, R, R, T])
    assert conf == [T] * 5 and cand is None, f"回本态未清候选: {conf}, {cand}"
    print("⑥ 回本态清零  [T,T,R,R,T] → 全 T 且无候选 ✓")

    # ⑦ 首根即当前态；chop 之后进 range 同样要 3 根
    conf, cand = confirm_states([C, R, R])
    assert conf == [C, C, C] and cand == {"state": R, "count": 2, "need": 3}
    print("⑦ 冲击后恢复  chop→range 同样吃 3 根迟滞 ✓")

    # ⑧ 输出与输入等长、空输入安全
    assert confirm_states([]) == ([], None)
    seq = [R, T, T, C, R, R, R, S, S]
    conf, _ = confirm_states(seq)
    assert len(conf) == len(seq)
    assert conf == [R, R, T, C, C, C, R, R, S], f"综合序列错: {conf}"
    print("⑧ 综合序列    进 T(2)→冲击 C(1)→恢复 R(3)→进 S(2) 全对 ✓")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()


def test_all():
    main()
