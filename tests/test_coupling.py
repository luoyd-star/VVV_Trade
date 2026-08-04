"""M1 耦合计算层测试：EWMA 手算对照 / 缺失不衰减 / RTH 无隔夜泄漏 / 资格纪律。"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime.coupling import (  # noqa: E402
    MIN_EFF, lw_shrink_corr, pair_table, pairwise_ewma_corr,
)

H = 3_600_000


def test_ewma_corr_converges_to_true_rho():
    rng = np.random.default_rng(7)
    n = 4000
    x = rng.normal(size=n)
    y = 0.6 * x + 0.8 * rng.normal(size=n)  # 真 ρ=0.6
    r = pd.DataFrame({"A": x, "B": y})
    out = pairwise_ewma_corr(r, halflife=420)
    rho = out["corr"].loc["A", "B"]
    assert abs(rho - 0.6) < 0.08, rho
    assert out["n_joint"].loc["A", "B"] == n


def test_missing_does_not_decay_pair_memory():
    """B 腿缺席的行不得衰减 A-B 的联合记忆，n_joint 只数联合有效。"""
    rng = np.random.default_rng(3)
    n = 2000
    x = rng.normal(size=n)
    y = 0.9 * x + np.sqrt(1 - 0.81) * rng.normal(size=n)
    r = pd.DataFrame({"A": x, "B": y})
    r2 = r.copy()
    r2.loc[1000:1899, "B"] = np.nan  # 900 行单腿缺席
    full = pairwise_ewma_corr(r, 140)["corr"].loc["A", "B"]
    holed = pairwise_ewma_corr(r2, 140)
    rho2 = holed["corr"].loc["A", "B"]
    assert holed["n_joint"].loc["A", "B"] == n - 900
    # 缺席段不算证据也不清空记忆：两版相关都应贴近真值
    assert abs(rho2 - 0.9) < 0.1 and abs(full - 0.9) < 0.1, (full, rho2)


def test_rth_panel_no_overnight_return():
    """usrth 面板：每日首根有效小时的收益（含隔夜）必须为 NaN。"""
    from regime.coupling import panel_returns

    class FakeConn:  # panel_returns 只经 storage.get_ohlcv 用 conn——打桩最小化
        pass

    # 直接测收益构造逻辑：构造两天 RTH（EDT，完整小时 UTC 14..19）+ 盘外小时
    ts, close = [], []
    for day in (3, 4):  # 2026-08-03/04 均为交易日
        for h in range(13, 21):
            ts.append(int(datetime(2026, 8, day, h, tzinfo=timezone.utc)
                          .timestamp() * 1000))
            close.append(100.0 + len(close))
    import regime.coupling as cp
    import regime.storage as st
    df = pd.DataFrame({"ts": pd.to_datetime(ts, unit="ms", utc=True),
                       "close": close, "open": close, "high": close,
                       "low": close, "volume": 1.0})
    orig = st.get_ohlcv
    try:
        st.get_ohlcv = lambda conn, sym, tf, limit=0: df
        r = cp.panel_returns(None, ["X-USDT"], "usrth")
    finally:
        st.get_ohlcv = orig
    col = r["X-USDT"]
    # 每天 6 根完整 RTH 小时 → 5 个同日相邻收益；两天共 10 个有效值
    assert col.notna().sum() == 10, col
    # 第二天首根（UTC 14:00）收益必须 NaN（跨隔夜）
    d2_first = int(datetime(2026, 8, 4, 14, tzinfo=timezone.utc).timestamp() * 1000)
    assert d2_first in col.index and np.isnan(col.loc[d2_first])


def test_pair_table_insufficient_and_eligibility():
    rng = np.random.default_rng(5)
    n = 3000
    x = rng.normal(size=n)
    df = pd.DataFrame({
        "A": x,
        "B": 0.7 * x + np.sqrt(1 - 0.49) * rng.normal(size=n),   # 高相关 → ELIGIBLE
        "C": rng.normal(size=n),                                  # 零相关 → NOT_APPLICABLE
    })
    df.loc[300:, "D"] = np.nan
    df.loc[:299, "D"] = rng.normal(size=300)                      # 仅 300 联合 → INSUFFICIENT
    t = pair_table(df)
    get = lambda a, b: t[(t.a == a) & (t.b == b)].iloc[0]
    assert get("A", "B")["status"] == "ELIGIBLE"
    assert get("A", "C")["status"] == "NOT_APPLICABLE"
    assert get("A", "D")["status"] == "INSUFFICIENT"
    assert get("A", "D")["n_joint"] < MIN_EFF
    # c 分数方向固定：ELIGIBLE 对 c≈|ρ|>0
    assert get("A", "B")["c"] > 0.5


def test_lw_shrink_valid_psd():
    rng = np.random.default_rng(11)
    z = pd.DataFrame(rng.normal(size=(600, 8)))
    C = lw_shrink_corr(z)
    ev = np.linalg.eigvalsh(C.to_numpy())
    assert ev.min() > -1e-10          # PSD
    assert abs(np.diag(C.to_numpy()) - 1).max() < 1e-9
    # 零相关总体 + 收缩：非对角应明显小于样本噪声上限
    off = C.to_numpy()[np.triu_indices(8, 1)]
    assert np.abs(off).max() < 0.15
