#!/usr/bin/env python3
"""walk-forward 因果性回归：追加未来数据不得改变过去任何一根 bar 的输出。

用法: .venv/bin/python tests/test_causality.py

这是对 rolling_states_missing 那句 docstring 承诺（"没有未来函数，与日后回测
的口径一致"）的**装配层**检验——它证明 walk-forward 的窗口切片不引入未来数据，
但**测不出特征内部的 shift 方向错误**（那类 bug 会同时改变前缀与完整两条路径的
输出，两边一起错就比不出来；特征级的端点行为由 test_session_ds 等分头把守）。
关键在于未来数据**真的被传进函数**：
    前缀路径:  rolling_states_missing(df.iloc[:cut], ...)
    完整路径:  rolling_states_missing(df, ...)
两条路径在重叠 ts 上的 (state, confidence, features, rules) 必须逐字段相等。

历史教训：旧版 test_pathgeom 比较的是 full[:i].copy() 与 full.copy()[:i]——
两个输入的值完全相同，只是底层 buffer 不同，测的其实是"函数不会越界读"，
在 Python+numpy 里那是不可达的失败模式。外部审阅指出了这一点，属实。
本测试经过突变体验证：把 classify.py 窗口切片的 i 改成 i+1（引入一根未来
泄漏），本测试必红，而旧断言依然全绿。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime.classify import rolling_states_missing  # noqa: E402


def make_df(n: int, seed: int = 7, hourly: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0, 0.012, n)))
    step = 3600 if hourly else 86400
    ts = pd.to_datetime((1_700_000_000 + np.arange(n) * step) * 1000, unit="ms", utc=True)
    amp = 0.004 * (1 + np.abs(rng.standard_normal(n)))
    return pd.DataFrame({
        "ts": ts,
        "open": close, "high": close * (1 + amp), "low": close * (1 - amp),
        "close": close, "volume": rng.uniform(80, 120, n),
    })


def run(df, tf="1h", session_aware=False):
    return {
        r[0]: r for r in rolling_states_missing(df, tf, set(), session_aware=session_aware)
    }


def compare(past: dict, future: dict) -> dict:
    """重叠 ts 上逐字段比较，返回各字段的不一致计数。

    四个字段一个都不能删：泄漏通常只在每个切点的末根显形，且 state 往往
    不翻转——真正抓到差异的是 confidence（2 位小数）与 features（3 位小数）
    的逐字段比较。把它"简化"成只比 state，突变体就存活了。"""
    bad = {"state": 0, "confidence": 0, "features": 0, "rules": 0}
    shared = set(past) & set(future)
    assert len(shared) > 100, f"重叠样本太少（{len(shared)}），fixture 不成立"
    for t in shared:
        p, f = past[t], future[t]
        if p[1] != f[1]:
            bad["state"] += 1
        if p[2] != f[2]:
            bad["confidence"] += 1
        if json.loads(p[3]) != json.loads(f[3]):
            bad["features"] += 1
        if p[4] != f[4]:
            bad["rules"] += 1
    return bad


def main() -> None:
    df = make_df(800)

    # ① 核心性质：三个切点 × 全字段
    total = 0
    for cut in (300, 500, 650):
        bad = compare(run(df.iloc[:cut]), run(df))
        n_bad = sum(bad.values())
        total += n_bad
        print(f"① 切点 {cut}: 不一致 {bad}")
        assert n_bad == 0, f"追加未来数据改变了过去的输出: {bad}"

    # ② session_aware 路径（走到去季节化分支，验它的装配层无泄漏）
    bad = compare(
        run(df.iloc[:600], session_aware=True), run(df, session_aware=True)
    )
    print(f"② session_aware: 不一致 {bad}")
    assert sum(bad.values()) == 0, f"去季节化路径存在未来泄漏: {bad}"

    # ②b 1d 路径：BARS_PER_YEAR 不同、任何按 timeframe 条件化的逻辑都在此设防
    d1 = make_df(420, seed=11, hourly=False)
    bad = compare(run(d1.iloc[:300], tf="1d"), run(d1, tf="1d"))
    print(f"②b 1d 路径: 不一致 {bad}")
    assert sum(bad.values()) == 0, f"1d 路径存在未来泄漏: {bad}"

    # ③ 增量口径 = 全量口径：existing 集合跳过已算 ts 后，补出来的行
    #    必须与一次性全量算出的完全一致（collector 每轮走的就是增量路径）
    full = run(df)
    first_half = rolling_states_missing(df.iloc[:500], "1h", set())
    existing = {r[0] for r in first_half}
    increment = rolling_states_missing(df, "1h", existing)
    merged = {r[0]: r for r in first_half}
    merged.update({r[0]: r for r in increment})
    assert set(merged) == set(full), "增量合并后的 ts 集合与全量不一致"
    diff = sum(1 for t in full if merged[t][:5] != full[t][:5])
    print(f"③ 增量=全量: 合并 {len(merged)} 行，与全量不一致 {diff} 行")
    assert diff == 0, f"增量路径与全量路径分叉 {diff} 行"

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()


def test_all():
    main()
