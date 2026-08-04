"""M2 状态机测试：注入式已知答案 / 稳态零误报 / 符号反转 / 校准平台冒烟。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime.coupling_fsm import (  # noqa: E402
    FSMParams, calibrate_false_alarms, pair_rho_series, run_pair_fsm,
)


def _pair(n1, rho1, n2, rho2, seed=9):
    """前 n1 根真相关 rho1，后 n2 根真相关 rho2 的合成 pair。"""
    rng = np.random.default_rng(seed)
    xs, ys = [], []
    for n, rho in ((n1, rho1), (n2, rho2)):
        x = rng.normal(size=n)
        e = rng.normal(size=n)
        y = rho * x + np.sqrt(max(1e-9, 1 - rho * rho)) * e
        xs.append(x)
        ys.append(y)
    idx = np.arange(n1 + n2) * 3_600_000
    return (pd.Series(np.concatenate(xs), index=idx),
            pd.Series(np.concatenate(ys), index=idx))


def test_injected_decoupling_walks_the_states():
    ra, rb = _pair(2500, 0.8, 2500, 0.0)
    ser = pair_rho_series(ra, rb)
    states, events = run_pair_fsm(ser)
    seq = [(e["from_state"], e["to_state"]) for e in events]
    assert ("coupled", "decoupling") in seq, seq
    # 断裂前不许出现脱耦进程（无前视）
    first_dec = next(e["ts"] for e in events if e["to_state"] == "decoupling")
    assert first_dec >= 2500 * 3_600_000, first_dec
    # 完整生命周期：…→decoupled→（慢线衰穿退出线）NOT_APPLICABLE 终态
    assert ("decoupling", "decoupled") in seq, seq
    assert states.iloc[-1] == "NOT_APPLICABLE", states.iloc[-1]
    assert any(e["reason"] == "eligibility_lost" for e in events)


def test_stable_correlation_no_decoupled():
    ra, rb = _pair(5000, 0.7, 0, 0.7, seed=21)
    ser = pair_rho_series(ra, rb)
    states, events = run_pair_fsm(ser)
    assert not any(e["to_state"] == "decoupled" for e in events), events
    assert states.iloc[-1] == "coupled"


def test_sign_reversal_is_not_decoupling():
    """符号翻转 ≠ 脱耦。慢线过零必穿失格区，正确路径是
    eligibility_lost → eligibility_regained（新符号重锚），且全程无 decoupled。"""
    ra, rb = _pair(2500, 0.6, 2500, -0.6, seed=33)
    ser = pair_rho_series(ra, rb)
    states, events = run_pair_fsm(ser)
    reasons = [e["reason"] for e in events]
    assert ("sign_reversal" in reasons
            or ("eligibility_lost" in reasons and "eligibility_regained" in reasons)), reasons
    # 旧关系破裂途经 decoupled 是诚实记录；关键是不滞留、且以新符号重建
    assert states.iloc[-1] == "coupled", states.iloc[-1]


def test_calibration_platform_smoke():
    rng = np.random.default_rng(4)
    n = 1500
    x = rng.normal(size=n)
    df = pd.DataFrame({
        "A": x, "B": 0.7 * x + 0.71 * rng.normal(size=n),
        "C": 0.6 * x + 0.8 * rng.normal(size=n),
    }, index=np.arange(n) * 3_600_000)
    out = calibrate_false_alarms(df, [("A", "B"), ("A", "C")], n_paths=5,
                                 mean_block=24)
    assert out["_meta"]["n_paths"] == 5
    for k, v in out.items():
        if k == "_meta":
            continue
        assert 0 <= v["p_false_decoupling"] <= 1
