"""耦合判定层（M2）：pair 四态状态机 + 块级三票 + 稳态结构零模型校准。

规格：docs/DESIGN_COUPLING_20260804.md §2/§5（源自 C6 参数表）。全部阈值是
**先验**（threshold_version 随行入账本），正式效力以 §5 校准协议为准。

语义硬边界（写进代码的承诺）：
- 只有资格对（|ρ_slow|≥0.25 等）才有耦合状态；弱相关是"不适用"。
- 只有**下降**才是脱耦事件（单侧）；慢线符号翻转是"关系反转"，单列事件、
  不占用脱耦语义。
- REBASE_PENDING 不自动洗成 coupled——重锚是显式事件。
- 本层输出状态与事件，不输出方向、不输出交易信号。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .coupling import _hl_alpha, HL_FAST, HL_SLOW

THRESHOLD_VERSION = "prior-2"   # 参数代际：prior-1 → prior-2（M2b 止血）→ calibrated-1（网格搜索）


@dataclass
class FSMParams:
    """C6 参数表（pair 级先验，prior-2 修订）。

    prior-2 变更（依据 M2b 满额校准，docs/COUPLING_M2B_20260804.md）：
    - FSM 资格线 0.25→0.35（退出 0.30）：定罪的误报对全落在 ρ∈0.27-0.38；
    - 按锚定时 |ρ_slow| 分层：强对（≥0.6，实测误报为零有余量）降门提速；
    - δ* 锚定冻结逐对化：max(层地板, 该对自身快慢背离的因果 q95)——
      对高背离噪声的对（如 ETH×XAU）自动抬门。
    数字血统：方向全部数据导出，具体数值仍是圆整先验——calibrated-1 由
    网格搜索给出推导链。
    """
    q_low: float = 0.10          # L = 因果分位 q10%(x)
    q_high: float = 0.30         # H = q30%(x)
    delta_floor_std: float = 0.29    # 标准层效应门地板（实测 P90|Δρ|）
    delta_floor_strong: float = 0.25  # 强层（|ρ_s|≥0.6）地板
    tier_strong: float = 0.60
    d_enter_std: float = 2.0     # 标准层进入 decoupling 的标准化幅度
    d_enter_strong: float = 1.5  # 强层（M2b：强对误报为零，有提速余量）
    d_retrace: float = 0.75      # recoupling 需自峰值回落比例
    k_enter: int = 2             # coupled→decoupling 连续确认
    k_decouple: int = 3          # decoupling→decoupled 连续确认
    k_recouple: int = 2          # decoupled→recoupling 连续确认
    k_back: int = 3              # recoupling→coupled 连续确认
    dwell_decoupled: int = 5     # decoupled 最短驻留
    dwell_other: int = 2
    quant_win: int = 3 * HL_FAST  # 因果分位/σ_d 的滚动窗
    # C6：活动期超过 2·T_F 挂起。T_F 是**有效样本数**（半衰期 140 → T_eff≈404），
    # 不是半衰期本身——错用 140 会让慢速衰减的真实脱耦被提前打成技术态
    rebase_age: int = 808
    # 运行时资格（prior-2：0.20/0.25 → 0.30/0.35）：|ρ_slow| 跌破退出线 →
    # NOT_APPLICABLE（关系死亡的诚实终态）；回到入场线以上才重新入场。
    # 测量层展示口径仍是 coupling.ELIG_ABS_RHO=0.25——只有状态机收紧。
    elig_exit: float = 0.30
    elig_enter: float = 0.35


def pair_rho_series(ra: pd.Series, rb: pd.Series,
                    hl_fast: float = HL_FAST, hl_slow: float = HL_SLOW):
    """pair 自身时钟上的双速 EWMA 相关路径。

    只在两腿同时有效的 bar 上推进（缺失不衰减、不产出）；返回
    DataFrame(index=联合 ts, cols=[rho_fast, rho_slow])。
    """
    df = pd.concat({"a": ra, "b": rb}, axis=1).dropna()
    if not len(df):
        return pd.DataFrame(columns=["rho_fast", "rho_slow"])
    x, y = df["a"].to_numpy(), df["b"].to_numpy()
    out = {}
    for name, hl in (("rho_fast", hl_fast), ("rho_slow", hl_slow)):
        a = _hl_alpha(hl)
        sxx = syy = sxy = w = 0.0
        rho = np.full(len(x), np.nan)
        for i in range(len(x)):
            sxx = (1 - a) * sxx + a * x[i] * x[i]
            syy = (1 - a) * syy + a * y[i] * y[i]
            sxy = (1 - a) * sxy + a * x[i] * y[i]
            w = (1 - a) * w + a
            if w > 1e-12 and sxx > 0 and syy > 0:
                rho[i] = sxy / np.sqrt(sxx * syy)
        out[name] = np.clip(rho, -1, 1)
    return pd.DataFrame(out, index=df.index)


def run_pair_fsm(rho: pd.DataFrame, p: FSMParams = FSMParams()):
    """在 pair 时钟上运行四态状态机。

    输入 pair_rho_series 的输出；返回 (states: Series, events: list[dict])。
    x = s·atanh(ρ_fast)，s = sign(ρ_slow)；L/H/σ_d 全部因果（shift(1) 滚动）。
    """
    rf = rho["rho_fast"].to_numpy()
    rs = rho["rho_slow"].to_numpy()
    n = len(rf)
    z = np.arctanh(np.clip(rf, -0.9999, 0.9999))
    s_series = np.sign(rs)
    x_raw = pd.Series(s_series * z, index=rho.index)
    L = x_raw.rolling(p.quant_win, min_periods=100).quantile(p.q_low).shift(1)
    H = x_raw.rolling(p.quant_win, min_periods=100).quantile(p.q_high).shift(1)
    sd = x_raw.rolling(p.quant_win, min_periods=100).std().shift(1)
    # 该对自身快慢背离的因果 q95（δ* 逐对化的原料；锚定时冻结防自我消音）
    div95 = pd.Series(np.abs(rf - rs), index=rho.index).rolling(
        p.quant_win, min_periods=100).quantile(0.95).shift(1)

    states = np.array(["WARMUP"] * n, dtype=object)
    events = []
    st = "WARMUP"
    anchor_z = anchor_sd = np.nan
    ep_d_enter = p.d_enter_std
    ep_delta_gate = p.delta_floor_std
    ep_sign = 0.0
    ep_peak_d = 0.0
    ep_age = 0          # 当前非 coupled episode 已持续根数
    dwell = 0           # 当前状态驻留
    streak = 0          # 待确认转移的连续计数
    pending = None      # 待确认的目标状态

    def emit(i, frm, to, reason, d_val):
        events.append({
            "ts": int(rho.index[i]), "from_state": frm, "to_state": to,
            "x": round(float(x_raw.iloc[i]), 4), "d": round(float(d_val), 3),
            "rho_fast": round(float(rf[i]), 4), "rho_slow": round(float(rs[i]), 4),
            "reason": reason, "threshold_version": THRESHOLD_VERSION,
        })

    for i in range(n):
        xi, Li, Hi = x_raw.iloc[i], L.iloc[i], H.iloc[i]
        if not (np.isfinite(xi) and np.isfinite(Li) and np.isfinite(Hi)
                and np.isfinite(sd.iloc[i])):
            states[i] = st if st != "WARMUP" else "WARMUP"
            continue
        if st == "WARMUP":
            if not (np.isfinite(rs[i]) and abs(rs[i]) >= p.elig_enter):
                st = "NOT_APPLICABLE"   # prior-2：初始资格检查（此前缺失）
                states[i] = st
                continue
            st = "coupled"
            anchor_z, anchor_sd, ep_sign = xi, max(sd.iloc[i], 1e-6), s_series[i]
            ep_tier_strong = abs(rs[i]) >= p.tier_strong
            ep_d_enter = p.d_enter_strong if ep_tier_strong else p.d_enter_std
            floor = p.delta_floor_strong if ep_tier_strong else p.delta_floor_std
            dv = div95.iloc[i]
            ep_delta_gate = max(floor, float(dv)) if np.isfinite(dv) else floor
            emit(i, "WARMUP", "coupled", "init_anchor", 0.0)
        # 运行时资格：慢线跌破退出线 → NOT_APPLICABLE（关系死亡终态）；
        # 回到入场线以上重新初始化。资格检查先于一切转移逻辑。
        if st == "NOT_APPLICABLE":
            if np.isfinite(rs[i]) and abs(rs[i]) >= p.elig_enter:
                st = "coupled"
                anchor_z, anchor_sd, ep_sign = xi, max(sd.iloc[i], 1e-6), s_series[i]
                ep_peak_d, ep_age, dwell, pending, streak = 0.0, 0, 0, None, 0
                ep_tier_strong = abs(rs[i]) >= p.tier_strong
                ep_d_enter = p.d_enter_strong if ep_tier_strong else p.d_enter_std
                floor = p.delta_floor_strong if ep_tier_strong else p.delta_floor_std
                dv = div95.iloc[i]
                ep_delta_gate = max(floor, float(dv)) if np.isfinite(dv) else floor
                emit(i, "NOT_APPLICABLE", "coupled", "eligibility_regained", 0.0)
            states[i] = st
            continue
        if np.isfinite(rs[i]) and abs(rs[i]) < p.elig_exit:
            emit(i, st, "NOT_APPLICABLE", "eligibility_lost", 0.0)
            st, pending, streak, dwell = "NOT_APPLICABLE", None, 0, 0
            states[i] = st
            continue
        # 慢线符号翻转：关系反转事件，重锚回 coupled（不占脱耦语义）
        if ep_sign != 0 and s_series[i] != 0 and s_series[i] != ep_sign:
            emit(i, st, "coupled", "sign_reversal", 0.0)
            st, pending, streak, dwell = "coupled", None, 0, 0
            anchor_z, anchor_sd, ep_sign = xi, max(sd.iloc[i], 1e-6), s_series[i]
            ep_peak_d, ep_age = 0.0, 0
            ep_tier_strong = abs(rs[i]) >= p.tier_strong
            ep_d_enter = p.d_enter_strong if ep_tier_strong else p.d_enter_std
            floor = p.delta_floor_strong if ep_tier_strong else p.delta_floor_std
            dv = div95.iloc[i]
            ep_delta_gate = max(floor, float(dv)) if np.isfinite(dv) else floor
            states[i] = st
            continue

        d = (anchor_z - xi) / anchor_sd if np.isfinite(anchor_z) else 0.0
        drho = abs(np.tanh(anchor_z) - rf[i] * (1 if ep_sign >= 0 else -1))
        dwell += 1
        if st != "coupled":
            ep_age += 1
            ep_peak_d = max(ep_peak_d, d)
        # 技术超时：episode 拖过 2T_F，挂 REBASE_PENDING（不许自动洗白）
        if st in ("decoupling", "decoupled", "recoupling") and ep_age > p.rebase_age:
            emit(i, st, "REBASE_PENDING", "episode_timeout", d)
            st, pending, streak, dwell = "REBASE_PENDING", None, 0, 0
            states[i] = st
            continue

        want = None
        if st == "coupled":
            if xi < Hi and d >= ep_d_enter and drho >= ep_delta_gate:
                want = ("decoupling", p.k_enter)
        elif st == "decoupling":
            if xi <= Li:
                want = ("decoupled", p.k_decouple)
            elif xi >= Hi:
                want = ("coupled", p.k_enter)   # 迟滞收回（宽松出口）
        elif st == "decoupled":
            if xi > Li and ep_peak_d > 0 and d <= (1 - p.d_retrace) * ep_peak_d:
                want = ("recoupling", p.k_recouple)
        elif st == "recoupling":
            if xi >= Hi and d <= 0.75:
                want = ("coupled", p.k_back)
            elif xi <= Li:
                want = ("decoupled", p.k_decouple)
        elif st == "REBASE_PENDING":
            if xi >= Hi:
                want = ("coupled", p.k_back)    # 显式重锚

        if want is not None and pending == want[0]:
            streak += 1
        elif want is not None:
            pending, streak = want[0], 1
        else:
            pending, streak = None, 0

        min_dwell = p.dwell_decoupled if st == "decoupled" else p.dwell_other
        if (pending is not None and streak >= dict(
                decoupling=p.k_enter, decoupled=p.k_decouple,
                recoupling=p.k_recouple, coupled=p.k_back)[pending]
                and dwell >= min_dwell):
            reason = "rebase_anchor" if st == "REBASE_PENDING" else "confirmed"
            emit(i, st, pending, reason, d)
            st, dwell = pending, 0
            if st == "coupled":
                anchor_z = xi
                anchor_sd = max(sd.iloc[i], 1e-6)
                ep_sign = s_series[i]
                ep_peak_d, ep_age = 0.0, 0
                ep_tier_strong = abs(rs[i]) >= p.tier_strong
                ep_d_enter = p.d_enter_strong if ep_tier_strong else p.d_enter_std
                floor = p.delta_floor_strong if ep_tier_strong else p.delta_floor_std
                dv = div95.iloc[i]
                ep_delta_gate = max(floor, float(dv)) if np.isfinite(dv) else floor

            pending, streak = None, 0
        states[i] = st
    return pd.Series(states, index=rho.index), events


# ---------------------------------------------------------------- 块级三票

def block_votes(r: pd.DataFrame, blocks: dict, pair_states: dict):
    """块对三票 nowcast：跨块 ρ 中位（票1）/ 等权指数 EWMA 相关（票2，PC1 代理）
    / 跨块 pair coupled 占比（票3）。≥2 票 → coupled。诊断输出，无状态历史。"""
    names = list(blocks)
    rows = []
    for i, A in enumerate(names):
        for B in names[i + 1:]:
            cross = [(a, b) for a in blocks[A] for b in blocks[B]
                     if a in r.columns and b in r.columns]
            if not cross:
                continue
            rhos, coupled = [], 0
            for a, b in cross:
                ser = pair_rho_series(r[a], r[b])
                if len(ser) and np.isfinite(ser["rho_slow"].iloc[-1]):
                    rhos.append(float(ser["rho_slow"].iloc[-1]))
                key = tuple(sorted((a, b)))
                if pair_states.get(key) == "coupled":
                    coupled += 1
            if not rhos:
                continue
            med = float(np.median(rhos))
            ia = r[blocks[A]].mean(axis=1)
            ib = r[blocks[B]].mean(axis=1)
            idx_ser = pair_rho_series(ia, ib)
            idx_rho = (float(idx_ser["rho_fast"].iloc[-1])
                       if len(idx_ser) else np.nan)
            v1 = abs(med) >= 0.25
            v2 = np.isfinite(idx_rho) and abs(idx_rho) >= 0.35
            v3 = coupled / max(1, len(cross)) >= 0.5
            rows.append({
                "block_a": A, "block_b": B, "n_pairs": len(cross),
                "median_rho_slow": round(med, 3),
                "index_rho_fast": round(idx_rho, 3) if np.isfinite(idx_rho) else None,
                "coupled_share": round(coupled / max(1, len(cross)), 2),
                "votes": int(v1) + int(v2) + int(v3),
                "state": "coupled" if int(v1) + int(v2) + int(v3) >= 2 else "not_coupled",
            })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 校准平台

def stationary_bootstrap_rows(r: pd.DataFrame, mean_block: int, n_paths: int,
                              seed: int = 17):
    """整行平稳 bootstrap（保横截面相关与波动簇；几何块长）。生成器。"""
    rng = np.random.default_rng(seed)
    X = r.to_numpy()
    n = len(X)
    p_geo = 1.0 / mean_block
    for _ in range(n_paths):
        idx = np.empty(n, dtype=int)
        pos = 0
        while pos < n:
            start = rng.integers(0, n)
            length = 1 + rng.geometric(p_geo)
            for k in range(min(length, n - pos)):
                idx[pos + k] = (start + k) % n
            pos += length
        yield pd.DataFrame(X[idx], columns=r.columns,
                           index=r.index[:n])


def calibrate_false_alarms(r: pd.DataFrame, pairs, n_paths: int = 300,
                           mean_block: int = 24, params: FSMParams = FSMParams(),
                           seed: int = 17):
    """稳态结构零模型下的 FSM 误报率：结构恒定（bootstrap 保持平均相关），
    统计每条路径每个 pair 是否误入 decoupling/decoupled。

    返回 {pair: {p_decoupling, p_decoupled}} 与总体行；这是 §5 校准协议的
    平台底座（正式验收要 ≥20k 路径 + 注入式功效，此函数即引擎）。
    """
    counts = {pr: {"decoupling": 0, "decoupled": 0} for pr in pairs}
    done = 0
    for path in stationary_bootstrap_rows(r, mean_block, n_paths, seed):
        for a, b in pairs:
            ser = pair_rho_series(path[a], path[b])
            if not len(ser):
                continue
            _, events = run_pair_fsm(ser, params)
            tos = {e["to_state"] for e in events}
            if "decoupling" in tos:
                counts[(a, b)]["decoupling"] += 1
            if "decoupled" in tos:
                counts[(a, b)]["decoupled"] += 1
        done += 1
    out = {
        f"{a}|{b}": {
            "p_false_decoupling": round(c["decoupling"] / max(1, done), 4),
            "p_false_decoupled": round(c["decoupled"] / max(1, done), 4),
        }
        for (a, b), c in counts.items()
    }
    out["_meta"] = {"n_paths": done, "mean_block": mean_block,
                    "threshold_version": THRESHOLD_VERSION}
    return out
