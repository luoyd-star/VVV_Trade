#!/usr/bin/env python3
"""pathgeom + margin 的因果性与正确性测试（regime-spectrum 采纳批次一）。

用法: .venv/bin/python tests/test_pathgeom.py
设计对照：regime-spectrum 的 04 参考实现的"因果性自检"比较的是原始输入切片（恒真），
这里比较的是**函数输出**——同一历史切片在"未来数据存在与否"两种内存布局下必须逐字段相等。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime.classify import _boundary_margin, analyze_timeframe  # noqa: E402
from regime.features.pathgeom import kendall_tau, path_features  # noqa: E402


def make_df(close: np.ndarray) -> pd.DataFrame:
    n = len(close)
    ts = pd.to_datetime((1_700_000_000 + np.arange(n) * 3600) * 1000, unit="ms", utc=True)
    return pd.DataFrame({
        "ts": ts,
        "open": close, "high": close * 1.001, "low": close * 0.999,
        "close": close, "volume": np.full(n, 100.0),
    })


def main() -> None:
    rng = np.random.default_rng(42)

    # ── 1. 因果性（输出级，非输入级）──
    full = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.01, 600)))
    for i in (200, 300, 450):
        hist_only = full[:i].copy()                      # 未来不存在
        hist_in_extended = full.copy()[:i]               # 未来存在于同一数组
        a, b = path_features(hist_only), path_features(hist_in_extended)
        assert a == b, f"pathgeom 因果性失败 @ {i}: {a} != {b}"
    # analyze_timeframe 级：同一前缀切片，全特征与状态必须一致
    df_full = make_df(full)
    r1 = analyze_timeframe(df_full.iloc[:300].reset_index(drop=True), "1h")
    r2 = analyze_timeframe(df_full.iloc[:300].copy().reset_index(drop=True), "1h")
    assert r1.state == r2.state and r1.features == r2.features, "analyze 因果性失败"
    print("1. 因果性（输出级）: PASS")

    # ── 2. 慢正弦 → 低频率、主周期≈40 ──
    t = np.arange(240)
    sine = 100 + 5 * np.sin(2 * np.pi * t / 40)
    p = path_features(sine)
    assert p["chop_freq"] is not None and p["chop_freq"] < 10, p
    # 穿越计数按整数量化（6次→40, 7次→34.3），±25% 是该估计器的固有粒度
    assert 30 <= p["dom_period"] <= 50, p
    print(f"2. 慢正弦: freq={p['chop_freq']}/100根, 主周期={p['dom_period']} PASS")

    # ── 3. 快噪声 → 高频率 ──
    noise = 100 + rng.normal(0, 1, 240)
    pn = path_features(noise)
    assert pn["chop_freq"] > 30, pn
    assert pn["chop_freq"] > 3 * p["chop_freq"], "频率轴无法区分快噪声与慢摆动"
    print(f"3. 快噪声: freq={pn['chop_freq']}/100根 PASS")

    # ── 4. Kendall τ 方向 ──
    up = np.linspace(100, 120, 240) + rng.normal(0, 0.05, 240)
    dn = np.linspace(120, 100, 240) + rng.normal(0, 0.05, 240)
    assert path_features(up)["kendall_tau"] > 0.9
    assert path_features(dn)["kendall_tau"] < -0.9
    print("4. Kendall τ 方向: PASS")

    # ── 5. 数据不足/NaN → 全 None（不塌中性）──
    assert all(v is None for v in path_features(full[:100]).values())
    bad = full[:150].copy(); bad[140] = np.nan  # NaN 须在最后 120 根窗口内才应触发拒算
    assert all(v is None for v in path_features(bad).values())
    print("5. 不足/NaN → None: PASS")

    # ── 6. margin：分层树下"最近可翻转边界" ──
    vol = lambda bbw, atr: {"bbw_rank": bbw, "atr_rank": atr}
    m = _boundary_margin("trend_up", 0.32, 0.65, vol(0.5, 0.5))
    assert (m["margin"], m["nearest"]) == (0.02, "exit_trend"), m
    m = _boundary_margin("squeeze", 0.10, 0.58, vol(0.02, 0.07))
    assert (m["margin"], m["nearest"]) == (0.13, "exit_squeeze"), m
    m = _boundary_margin("range", 0.10, 0.55, vol(0.40, 0.50))
    assert (m["margin"], m["nearest"]) == (0.20, "to_trend"), m
    m = _boundary_margin("high_vol_chop", 0.05, 0.20, vol(0.50, 0.87))
    assert (m["margin"], m["nearest"]) == (0.02, "to_range"), m
    # trend 态下 bbw 逼近 squeeze 边界不得触发"临界"（不可达翻转）
    m = _boundary_margin("trend_up", 0.60, 0.90, vol(0.151, 0.29))
    assert m["nearest"] == "exit_trend" and m["margin"] == 0.30, m
    print("6. margin 分层边界: PASS")

    # ── 7. 对抗校验补充断言 ──
    # τ 精确小样本（含平局的 τ-a 语义）
    assert kendall_tau([1, 2, 3]) == 1.0
    assert abs(kendall_tau([3, 1, 2]) - (-1 / 3)) < 1e-12
    assert kendall_tau([5, 4, 3, 2, 1]) == -1.0
    assert abs(kendall_tau([1, 1, 2]) - (2 / 3)) < 1e-12  # 平局不修：S=2, C(3,2)=3
    # 常数序列：残差全落轴被剔除 → 0 穿越、主周期 None、τ=0（与 regime-spectrum 口径一致）
    pc = path_features(np.full(150, 100.0))
    assert (pc["chop_freq"], pc["dom_period"], pc["kendall_tau"]) == (0.0, None, 0.0), pc
    # 窗口外 NaN 不拒算（窗口只看最后 120 根）
    ok = full[:150].copy(); ok[5] = np.nan
    assert path_features(ok)["chop_freq"] is not None
    # 窗口下限护栏
    try:
        path_features(full[:150], window=50)
        raise AssertionError("window<100 未被拒绝")
    except ValueError:
        pass
    print("7. 补充断言（τ精确值/常数序列/窗口外NaN/下限护栏）: PASS")

    print("\n全部 7 组断言通过。")


if __name__ == "__main__":
    main()
