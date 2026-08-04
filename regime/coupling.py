"""耦合计算层（M1）：三面板收益、双速 EWMA 相关、pair 资格、全局统计量。

规格：docs/DESIGN_COUPLING_20260804.md §1-2。本模块只做**计算与 nowcast**，
状态机（M2）、显著性协议与预测层（M4）后续在此之上搭建。

核心纪律：
- 三面板分立：all247（加密+商品，24/7 小时钟）/ usrth（美股永续，仅完整
  RTH 小时且**日界重置**——每日首根含隔夜信息的收益被丢弃，防止把盘外
  粘滞与隔夜跳空混进盘中相关）/ cross（全体，共同完整 RTH 小时，同样日界重置）。
- 成对时钟 EWMA：权重只在**两腿同时有效**的 bar 上衰减与累积——缺失不
  消耗记忆，n_eff 只数联合有效样本。T_eff 不足输出 INSUFFICIENT，
  禁止缩窗冒充（半衰期 140/420 共同小时 ≈ T_eff 404/1212）。
- β_ret 与 β_spread 语义分离：本层只算 β_ret（收益暴露）；对冲比是 M2+
  价差门控的事，不在此混算。
- 相关性统计一律可转 Fisher-z（atanh）域；本层输出 ρ 与 z 并存。
- 核心矩阵只收 pool=core 品种（观察池数据照采，消费层过滤——此处即消费层）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import instruments, storage
from .calendar_nyse import bar_full_rth

H_MS = 3_600_000
HL_FAST = 140    # 快线半衰期（共同小时），T_eff=(1+λ)/(1-λ)≈404
HL_SLOW = 420    # 慢线，T_eff≈1212
MIN_EFF = 400    # pair 联合有效样本下限（不足 → INSUFFICIENT）
ELIG_ABS_RHO = 0.25   # 资格门：慢线 |ρ| 下限（低于是"不适用"，不是"脱耦"）
ELIG_SIGN_RATE = 0.80  # 参考期同号率下限
ELIG_COVERAGE = 0.95   # 联合覆盖率下限（相对面板行数）


def _hl_alpha(halflife: float) -> float:
    return 1.0 - 0.5 ** (1.0 / halflife)


def panel_members(conn, panel: str):
    """面板成员（仅核心池）。all247=加密+商品（24/7）；usrth=美股永续。

    intl_stock_perp（KRX 时段）归 all247——其永续 24/7 报价，但正股时段
    不是 NYSE，进 RTH 面板会用错时钟；cross 面板同理只收 us+crypto+commodity。
    """
    out = []
    for sym in sorted(storage.symbols(conn)):
        cfg = instruments.get(sym)
        if (cfg.get("pool") or "core") != "core":
            continue
        cls = cfg.get("class")
        if panel == "all247" and cls in ("crypto", "commodity", "intl_stock_perp"):
            out.append(sym)
        elif panel == "usrth" and cls == "us_stock_perp":
            out.append(sym)
        elif panel == "cross" and cls in ("crypto", "commodity", "us_stock_perp"):
            out.append(sym)
    return out


def panel_returns(conn, symbols, panel: str, limit: int = 6000) -> pd.DataFrame:
    """对齐的 1h log 收益宽表（index=ts_ms, cols=symbols，缺失 NaN）。

    usrth/cross：只保留完整 RTH 小时，且**只取同日相邻小时**的收益——
    每日首根有效小时的收益跨隔夜，一律置 NaN（C5：滞后在日界重置）。
    """
    closes = {}
    for sym in symbols:
        df = storage.get_ohlcv(conn, sym, "1h", limit=limit)
        if not len(df):
            continue
        ts = np.array([int(t) for t in storage.ts_to_ms(df["ts"])], dtype="int64")
        closes[sym] = pd.Series(df["close"].to_numpy(), index=ts)
    px = pd.DataFrame(closes).sort_index()
    if panel == "all247":
        r = np.log(px / px.shift(1))
        # 仅当相邻 index 恰为 1h 时收益才有效（缺根即 NaN，不跨洞差分）
        gap = np.diff(px.index.to_numpy(), prepend=px.index[0] - H_MS)
        r[gap != H_MS] = np.nan
        return r.iloc[1:]
    # RTH 类面板：先筛完整 RTH 小时，再按"同日且相邻 1h"取收益
    rth_mask = np.array([bar_full_rth(int(t), H_MS) for t in px.index])
    px = px[rth_mask]
    if not len(px):
        return pd.DataFrame()
    idx = px.index.to_numpy()
    same_day_adjacent = np.zeros(len(px), dtype=bool)
    same_day_adjacent[1:] = (np.diff(idx) == H_MS)  # RTH 小时连续 ⇒ 同日相邻
    r = np.log(px / px.shift(1))
    r[~same_day_adjacent] = np.nan
    return r.iloc[1:]


def ewma_vol_standardize(r: pd.DataFrame, halflife: float = HL_SLOW):
    """逐列 EWMA 波动标准化（因果，min_periods 保护）。返回 (标准化收益, vol)。"""
    vol = r.pow(2).ewm(halflife=halflife, min_periods=30).mean().pow(0.5).shift(1)
    z = r / vol
    return z.replace([np.inf, -np.inf], np.nan), vol


def pairwise_ewma_corr(r: pd.DataFrame, halflife: float):
    """成对时钟 EWMA 相关：权重只在两腿同时有效时衰减/累积。

    返回 dict(corr=DataFrame, cov=..., var=..., n_joint=联合有效样本数矩阵)。
    实现：S/W 递推——S = S·(1-αV) + α·(x⊗x)·V，W 同构；V=联合有效指示。
    """
    a = _hl_alpha(halflife)
    cols = list(r.columns)
    n = len(cols)
    X = r.to_numpy(dtype=float)
    S = np.zeros((n, n))
    W = np.zeros((n, n))
    N = np.zeros((n, n))
    for row in X:
        m = np.isfinite(row)
        if not m.any():
            continue
        x = np.where(m, row, 0.0)
        V = np.outer(m, m).astype(float)
        S = S * (1.0 - a * V) + a * np.outer(x, x) * V
        W = W * (1.0 - a * V) + a * V
        N += V
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = S / W
        d = np.sqrt(np.diag(cov))
        corr = cov / np.outer(d, d)
    corr = np.clip(corr, -1.0, 1.0)
    return {
        "corr": pd.DataFrame(corr, index=cols, columns=cols),
        "cov": pd.DataFrame(cov, index=cols, columns=cols),
        "var": pd.Series(np.diag(cov), index=cols),
        "n_joint": pd.DataFrame(N, index=cols, columns=cols),
    }


def rolling_sign_rate(r: pd.DataFrame, halflife: float = HL_SLOW,
                      window_frac: float = 1.0):
    """参考期同号率的简化因果实现：以慢窗内逐段短窗（半衰期/4）相关的符号
    与慢线符号一致的占比近似。返回 DataFrame[sym, sym]。"""
    seg_hl = max(20, int(halflife / 4))
    segs = []
    n = len(r)
    seg_len = seg_hl * 2
    for start in range(max(0, n - int(halflife * 3)), n - seg_len + 1, seg_len):
        seg = r.iloc[start:start + seg_len]
        if len(seg) >= seg_len:
            segs.append(seg.corr(min_periods=int(seg_len * 0.6)))
    if not segs:
        return None
    slow = pairwise_ewma_corr(r, halflife)["corr"]
    agree = sum((np.sign(s) == np.sign(slow)).astype(float).fillna(0) for s in segs)
    valid = sum(s.notna().astype(float) for s in segs)
    with np.errstate(invalid="ignore", divide="ignore"):
        return agree / valid


def pair_table(r: pd.DataFrame) -> pd.DataFrame:
    """pair 级 nowcast 表：ρ_fast/ρ_slow/z 差/c_ij/β 快慢/资格/INSUFFICIENT。"""
    z, _ = ewma_vol_standardize(r)
    fast = pairwise_ewma_corr(z, HL_FAST)
    slow = pairwise_ewma_corr(z, HL_SLOW)
    sign_rate = rolling_sign_rate(z)
    n_rows = len(r)
    valid_rows = r.notna().sum()  # 每列有效行数
    rows = []
    cols = list(r.columns)
    for i, a_ in enumerate(cols):
        for b_ in cols[i + 1:]:
            n_joint = fast["n_joint"].loc[a_, b_]
            rho_f = fast["corr"].loc[a_, b_]
            rho_s = slow["corr"].loc[a_, b_]
            # coverage 按两腿联合窗计（min 有效行），不按面板并集行数——
            # 否则错峰上市的老对会被并集长度机械压低（M1 首轮实测的精修项）
            cover = n_joint / max(1, min(valid_rows[a_], valid_rows[b_]))
            sr = (sign_rate.loc[a_, b_]
                  if sign_rate is not None and np.isfinite(sign_rate.loc[a_, b_])
                  else np.nan)
            insufficient = (n_joint < MIN_EFF) or not np.isfinite(rho_s)
            eligible = (not insufficient and abs(rho_s) >= ELIG_ABS_RHO
                        and (np.isnan(sr) or sr >= ELIG_SIGN_RATE)
                        and cover >= ELIG_COVERAGE)
            s = np.sign(rho_s) if np.isfinite(rho_s) else np.nan
            # β_ret 用标准化前收益（暴露语义）；快慢各一
            rows.append({
                "a": a_, "b": b_, "n_joint": int(n_joint),
                "coverage": round(float(cover), 3),
                "rho_fast": round(float(rho_f), 3) if np.isfinite(rho_f) else None,
                "rho_slow": round(float(rho_s), 3) if np.isfinite(rho_s) else None,
                "dz": (round(float(np.arctanh(np.clip(rho_f, -0.9999, 0.9999))
                             - np.arctanh(np.clip(rho_s, -0.9999, 0.9999))), 3)
                       if np.isfinite(rho_f) and np.isfinite(rho_s) else None),
                "c": (round(float(s * rho_f), 3)
                      if np.isfinite(rho_f) and np.isfinite(s) else None),
                "sign_rate": round(float(sr), 2) if np.isfinite(sr) else None,
                "status": ("INSUFFICIENT" if insufficient
                           else ("ELIGIBLE" if eligible else "NOT_APPLICABLE")),
            })
    return pd.DataFrame(rows)


def lw_shrink_corr(z: pd.DataFrame, window: int = 3 * HL_FAST) -> pd.DataFrame:
    """Ledoit-Wolf 线性收缩（单位阵目标）后的相关矩阵，取末端 window 完整行。

    仅用于块/全局统计（λ1/N、块能量）；pair FSM 用 EWMA 原生 ρ。
    行内任一缺失即丢整行（矩阵统计需要共同支撑），行数不足返回 None。
    """
    tail = z.dropna(how="any").tail(window)
    n, p = tail.shape
    if n < max(60, p * 2):
        return None
    X = tail.to_numpy()
    X = (X - X.mean(0)) / (X.std(0, ddof=1) + 1e-12)
    S = X.T @ X / n
    mu = np.trace(S) / p
    F = mu * np.eye(p)
    d2 = np.linalg.norm(S - F, "fro") ** 2
    b2_ = sum(np.linalg.norm(np.outer(x, x) - S, "fro") ** 2 for x in X) / n ** 2
    b2 = min(b2_, d2)
    shrink = b2 / d2 if d2 > 0 else 1.0
    Sh = shrink * F + (1 - shrink) * S
    d = np.sqrt(np.diag(Sh))
    C = Sh / np.outer(d, d)
    return pd.DataFrame(np.clip(C, -1, 1), index=tail.columns, columns=tail.columns)


def theme_blocks(symbols):
    """主题块（主标签=theme[0]；semi_levered_proxy/broad_index 不组块）。"""
    blocks: dict = {}
    for sym in symbols:
        th = (instruments.get(sym).get("theme") or ["unlabeled"])[0]
        if th in ("semi_levered_proxy", "broad_index"):
            continue
        blocks.setdefault(th, []).append(sym)
    return {k: v for k, v in blocks.items() if len(v) >= 2}


def global_stats(C: pd.DataFrame, blocks: dict) -> dict:
    """全局与块级直读量：m=λ1/N、平均相关、离散度、块内-块外 D_b、跨块能量占比。"""
    A = C.to_numpy()
    n = len(A)
    off = A[np.triu_indices(n, 1)]
    ev = np.linalg.eigvalsh(A)
    out = {
        "n": n,
        "market_mode": round(float(ev[-1] / n), 4),
        "mean_corr": round(float(off.mean()), 4),
        "dispersion": round(float(off.std()), 4),
        "blocks": {},
    }
    total_off_energy = float((off ** 2).sum())
    cross_energy = 0.0
    names = list(C.columns)
    for bname, members in blocks.items():
        ins = [names.index(s) for s in members if s in names]
        outs = [i for i in range(n) if i not in ins]
        if len(ins) < 2:
            continue
        sub = A[np.ix_(ins, ins)]
        a_b = float(sub[np.triu_indices(len(ins), 1)].mean())
        e_b = float(A[np.ix_(ins, outs)].mean()) if outs else np.nan
        cross_energy += float((A[np.ix_(ins, outs)] ** 2).sum())
        out["blocks"][bname] = {
            "n": len(ins), "intra": round(a_b, 3),
            "extra": round(e_b, 3) if np.isfinite(e_b) else None,
            "D": round(a_b - e_b, 3) if np.isfinite(e_b) else None,
        }
    out["cross_block_energy_share"] = (
        round(cross_energy / (2 * total_off_energy), 4) if total_off_energy > 0 else None)
    return out
