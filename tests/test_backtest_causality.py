"""P0 回测框架的因果与正确性测试。

钉死四件事：
1. CRPS 实现与暴力双循环一致（O(n log n) 前缀和没写错）；
2. 前缀不变性：t 之后的数据被污染，t 及之前的评分记录逐字段不变——
   条件池/基线池确实只用 u <= t-H 的已实现结果（无未来泄漏）；
3. 已知答案：状态真的携带未来波动信息时技能显著为正，
   状态被打乱后技能塌向 0——评分方向没有接反；
4. 锁箱两个字面量（日期串与毫秒数）互相咬合，防止改一处忘另一处。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime.backtest import (  # noqa: E402
    LOCKBOX_START, LOCKBOX_START_MS, conditional_skill, crps_empirical,
    direction_pnl, future_rv, skill_summary, surrogate_band,
)


def _crps_brute(sample, y):
    x = np.asarray(sample, dtype=float)
    n = len(x)
    return float(np.abs(x - y).mean()
                 - 0.5 * np.abs(x[:, None] - x[None, :]).sum() / (n * n))


def test_crps_matches_bruteforce():
    rng = np.random.default_rng(3)
    for n in (1, 2, 7, 50):
        x = rng.normal(size=n)
        for y in (-1.3, 0.0, 0.4, 2.2):
            fast, slow = crps_empirical(x, y), _crps_brute(x, y)
            assert abs(fast - slow) < 1e-12, (n, y, fast, slow)


def test_future_rv_alignment():
    close = pd.Series([100.0, 110.0, 105.0, 105.0, 121.0])
    y = future_rv(close, 2)
    # y_0 = sqrt(mean(r1^2, r2^2))，r1=log(110/100)，r2=log(105/110)
    r1, r2 = np.log(110 / 100), np.log(105 / 110)
    assert abs(y[0] - np.sqrt((r1 * r1 + r2 * r2) / 2)) < 1e-12
    # 末端 H 根没有完整未来窗，必须 NaN
    assert np.isnan(y[-1]) and np.isnan(y[-2])


def _synthetic(n=2000, hi=3.0, seed=11):
    """随机块长两态序列：state 决定当根波动，块内状态预测未来窗波动。

    块长必须随机（50~150）：固定块长的序列是周期性的，环移会把状态
    重新对齐回真值，把零假设污染成"换个相位的真信号"。
    """
    rng = np.random.default_rng(seed)
    states, vols = [], []
    flag = False
    while len(states) < n:
        b = int(rng.integers(50, 151))
        s = "high_vol_chop" if flag else "range"
        states += [s] * b
        vols += [hi if flag else 1.0] * b
        flag = not flag
    states, vols = states[:n], vols[:n]
    r = rng.normal(0, 1, n) * np.array(vols) * 0.01
    close = pd.Series(100 * np.exp(np.cumsum(r)))
    return states, close


def test_prefix_invariance_no_future_leak():
    states, close = _synthetic()
    H = 6
    y = future_rv(close, H)
    t0 = 900
    y_poison = y.copy()
    y_poison[t0 + 1:] = 99.9  # 污染严格 t0 之后的目标
    recs_a, _ = conditional_skill(states, y, H)
    recs_b, _ = conditional_skill(states, y_poison, H)
    a = {r["t"]: r for r in recs_a if r["t"] <= t0}
    b = {r["t"]: r for r in recs_b if r["t"] <= t0}
    assert a and a.keys() == b.keys()
    for t in a:
        assert a[t] == b[t], f"t={t} 的评分被未来数据改变——存在泄漏"


def test_skill_positive_on_informative_states_zero_on_shuffled():
    states, close = _synthetic()
    H = 6
    y = future_rv(close, H)
    recs, _ = conditional_skill(states, y, H)
    s = skill_summary(recs, block=2 * H)
    assert s["skill"] > 0.15 and s["ci95"][0] > 0, s  # 真信息 → 显著正技能

    rng = np.random.default_rng(5)
    shuffled = list(states)
    rng.shuffle(shuffled)
    recs2, _ = conditional_skill(shuffled, y, H)
    s2 = skill_summary(recs2, block=2 * H)
    # 打乱后条件≈无条件：技能塌向 0 且远低于真信息（评分方向没接反）
    assert abs(s2["skill"]) < 0.05 and s2["skill"] < s["skill"] - 0.10, (s, s2)


def test_surrogate_band_separates_signal_from_pool_size_bias():
    """surrogate 带（描述性）：真信息的实际技能出带（above），打乱后落回带内。

    注意这只验证机制方向；协议 p3 明确带不构成显著性检验（状态回看窗与
    目标窗在环移后重叠、无多重检验校正）。
    """
    states, close = _synthetic()
    H = 6
    y = future_rv(close, H)
    recs, _ = conditional_skill(states, y, H)
    s = skill_summary(recs, block=2 * H)
    band = surrogate_band(states, y, H)
    assert band is not None and s["skill"] > band["hi"], (s, band)

    rng = np.random.default_rng(5)
    shuffled = list(states)
    rng.shuffle(shuffled)
    recs2, _ = conditional_skill(shuffled, y, H)
    s2 = skill_summary(recs2, block=2 * H)
    band2 = surrogate_band(shuffled, y, H)
    assert band2["lo"] <= s2["skill"] <= band2["hi"], (s2, band2)


def test_direction_pnl_fee_accounting():
    states = ["range", "trend_up", "trend_up", "trend_down", "range"]
    close = [100.0, 100.0, 110.0, 121.0, 110.0]
    ts = [i * 3_600_000 for i in range(5)]
    d = direction_pnl(states, close, ts, 3_600_000, fee_bps=10.0)
    # 持仓路径 held=pos[:-1]=[0,+1,+1,-1]：三根有收益腿各吃 +log(1.1)。
    # 费用口径（p3）：建仓 |pos[0]|=0 + 路径内换仓 |1-0|+|1-1|+|-1-1| = 3 边，
    # 末根信号（range）引发的平仓不计费——它没有收益腿。
    assert abs(d["gross_sum"] - 3 * np.log(1.1)) < 2e-5
    assert abs(d["fee_sum"] - 3 * 10.0 / 10_000) < 2e-5
    assert abs(d["net_after_fee"] - (d["gross_sum"] - d["fee_sum"])) < 2e-5
    assert d["n_position_bars"] == 3  # 按实际持仓路径计，末根信号不算
    # 边界：n<2 是"样本不足"，不是零收益
    assert direction_pnl(["trend_up"], [100.0], [0], 3_600_000) == {"insufficient": 1}


def test_direction_pnl_funding_attribution():
    """funding 归属口径：bar i 期间持有的是 pos[i-1]，与 gross=pos[i-1]·r[i] 同口径。

    结算落在 bar 3（span [3h,4h)）：期间持仓 = pos[2] = +1（trend_up），
    此刻 pos[3] 已翻成 -1——记错对象会把成本符号整个反过来。
    ts 直接用毫秒整数（评审实证：deriv ts 已是毫秒，再转会变 1970 年）。
    """
    states = ["range", "trend_up", "trend_up", "trend_down", "range"]
    close = [100.0, 100.0, 110.0, 121.0, 110.0]
    ts = [i * 3_600_000 for i in range(5)]
    funding = [(3 * 3_600_000 + 1_800_000, 0.01)]  # bar 3 中点结算，费率 +1%
    d = direction_pnl(states, close, ts, 3_600_000, fee_bps=0.0, funding=funding)
    assert abs(d["funding_sum"] - 0.01) < 2e-5, d  # 多头付正费率：成本 +0.01
    assert abs(d["net_after_fee_funding"] - (d["net_after_fee"] - 0.01)) < 2e-5


def test_lockbox_literals_agree():
    ms = int(pd.Timestamp(LOCKBOX_START, tz="UTC").timestamp() * 1000)
    assert ms == LOCKBOX_START_MS, (ms, LOCKBOX_START_MS)


def test_skill_summary_degenerate_and_circular_blocks():
    from regime.backtest import skill_summary
    # 退化基线（恒定价格 → CRPS 全 0）：必须返回 None，不许输出 NaN
    recs = [{"t": i, "ep": 0, "state": "range", "crps_c": 0.0, "crps_u": 0.0,
             "n_cond": 30} for i in range(40)]
    assert skill_summary(recs, block=4) is None
    # 评审 p3 反例：n=2, c=[0,2], u=[1,1]，真技能 0。旧实现边界起点抽出
    # 截断短块（如只含 [1]）→ 伪造 [-1,0] 的 CI；环块修复后 CI 必须钉在 0
    recs2 = [
        {"t": 0, "ep": 0, "state": "a", "crps_c": 0.0, "crps_u": 1.0, "n_cond": 30},
        {"t": 1, "ep": 0, "state": "a", "crps_c": 2.0, "crps_u": 1.0, "n_cond": 30},
    ]
    s = skill_summary(recs2, block=2)
    assert s["skill"] == 0.0 and s["ci95"] == [0.0, 0.0], s


def test_surrogate_band_deterministic():
    from regime.backtest import surrogate_band
    states, close = _synthetic(n=600)
    y = future_rv(close, 6)
    b1 = surrogate_band(states, y, 6)
    b2 = surrogate_band(states, y, 6)
    assert b1 == b2 and b1["n_shifts"] >= 10, (b1, b2)  # 全枚举/等距抽，无 RNG


def test_load_series_rejects_version_holes():
    """版本空洞会把不相邻 K 线压成相邻（评审双方独立发现）——必须报错拒绝。"""
    import sqlite3 as sq
    from regime import backtest, storage

    conn = sq.connect(":memory:")
    conn.row_factory = sq.Row
    conn.executescript(storage._SCHEMA)
    storage._migrate(conn)
    step = 3_600_000
    base = 486_112 * step  # 2025-06 附近的整点网格，远在锁箱之前
    for i in range(10):
        conn.execute("INSERT INTO ohlcv VALUES ('X-USDT','1h',?,?,?,?,?,1,'t')",
                     (base + i * step, 100.0, 101.0, 99.0, 100.0 + i))
    from regime.classify import AUDIT_VERSION, RULES_VERSION
    for i in range(2, 10):
        conn.execute(
            "INSERT INTO regime_history(symbol,tf,ts,state,raw_state,confidence,"
            "features,rules,version,audit_version) VALUES "
            "('X-USDT','1h',?,?,?,0.5,'{}','{}',?,?)",
            (base + i * step, "range", "range", RULES_VERSION, AUDIT_VERSION))
    df, rows, idx, _ = backtest._load_series(conn, "X-USDT", "1h")
    assert idx == 2 and len(rows) == 8  # 干净尾段：正常通过

    conn.execute("UPDATE regime_history SET version='v0' WHERE ts=?",
                 (base + 5 * step,))  # 中间一行变旧版本 → 过滤后出洞
    try:
        backtest._load_series(conn, "X-USDT", "1h")
        raise AssertionError("版本空洞未被拒绝")
    except RuntimeError as e:
        assert "空洞" in str(e) or "缺根" in str(e)


def test_ledger_conflict_raises():
    """同 id 不同 metrics 必须报错——OR IGNORE 静默保留旧值会让报告与账本各说各话。"""
    import tempfile
    from regime import backtest

    old = backtest.LEDGER_PATH
    with tempfile.TemporaryDirectory() as td:
        backtest.LEDGER_PATH = td + "/ledger.sqlite3"
        try:
            payload = {"meta": {"protocol": 3, "data_cutoff_ms": 1},
                       "results": [{"a": 1}]}
            eid = backtest.record_experiment(payload, "codeX")
            # 同 meta 同代码（同 id）但结果不同 → 异常，而不是静默忽略
            payload2 = {"meta": {"protocol": 3, "data_cutoff_ms": 1},
                        "results": [{"a": 2}]}
            try:
                backtest.record_experiment(payload2, "codeX")
                raise AssertionError("id 冲突未被发现")
            except RuntimeError:
                pass
            # 逐字重跑 → 幂等，同 id
            assert backtest.record_experiment(payload, "codeX") == eid
        finally:
            backtest.LEDGER_PATH = old
