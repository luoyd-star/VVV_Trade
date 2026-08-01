"""cRSI（Cyclic Smoothed RSI，von Thienen 派生）——由用户提供的 Pine v6 脚本本地化。

与 Pine 原版对齐的语义细节：
- Pine v6 中整数除法按浮点计算，below/cyclicMemory >= aperc 是真正的百分比逻辑；
- lmax/lmin 沿用原版的 else-if 扫描（从最新一根往回数），保留"先更新极大值
  则该根跳过极小值判断"的原始行为；
- 背离采用枢轴确认，天然滞后 piv_len 根——无未来函数：事件在确认根成立，
  标记画在枢轴根（对应 Pine 的 offset=-pivLen）；
- ta.rma 以 SMA 为种子；ta.change 首根为 na，故 RMA 从第 2 根开始积累。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _rma(x: np.ndarray, length: int) -> np.ndarray:
    """Wilder RMA，SMA 种子，允许前导 NaN。"""
    out = np.full(len(x), np.nan)
    valid = np.where(~np.isnan(x))[0]
    if len(valid) == 0:
        return out
    start = valid[0]
    seed_end = start + length
    if seed_end > len(x):
        return out
    out[seed_end - 1] = np.nanmean(x[start:seed_end])
    for i in range(seed_end, len(x)):
        xi = x[i] if not np.isnan(x[i]) else 0.0
        out[i] = (out[i - 1] * (length - 1) + xi) / length
    return out


def crsi_features(
    df: pd.DataFrame,
    dom_cycle: int = 20,
    vibration: int = 10,
    leveling: float = 10.0,
    piv_len: int = 5,
    src_col: str = "close",
) -> dict:
    """计算 cRSI 主线、自适应分位带、带内位置与枢轴确认背离。

    返回:
      series: {crsi, db, ub}  与 df 等长的 ndarray（前段为 NaN 暖机）
      last:   {crsi, db, ub, pos, zone}  末根读数（pos: 0=下带 100=上带，可超界）
      divergences: [{i, kind}]  kind 为 bull/bear，i 是枢轴根索引
      last_divergence: {kind, i, bars_ago} 或 None
    """
    src = df[src_col].to_numpy(dtype=float)
    n = len(src)
    cycle_len = max(1, dom_cycle // 2)
    mem = dom_cycle * 2
    torque = 2.0 / (vibration + 1.0)
    lag = max(0, int((vibration - 1) / 2.0))
    aperc = leveling / 100.0

    # ── cRSI 主线 ──
    delta = np.diff(src, prepend=np.nan)
    up_in = np.where(delta > 0, delta, 0.0)
    dn_in = np.where(delta < 0, -delta, 0.0)
    up_in[0] = np.nan  # ta.change 首根为 na
    dn_in[0] = np.nan
    up = _rma(up_in, cycle_len)
    dn = _rma(dn_in, cycle_len)

    rsi = np.full(n, np.nan)
    for i in range(n):
        u, d = up[i], dn[i]
        if np.isnan(u) or np.isnan(d):
            continue
        if d == 0.0:
            rsi[i] = 100.0
        elif u == 0.0:
            rsi[i] = 0.0
        else:
            rsi[i] = 100.0 - 100.0 / (1.0 + u / d)

    crsi = np.full(n, np.nan)
    for i in range(n):
        if np.isnan(rsi[i]) or i - lag < 0 or np.isnan(rsi[i - lag]):
            continue
        prev = crsi[i - 1] if i > 0 and np.isfinite(crsi[i - 1]) else 0.0  # nz(crsi[1])
        crsi[i] = torque * (2.0 * rsi[i] - rsi[i - lag]) + (1.0 - torque) * prev

    # ── 自适应分位带（每根用它之前 mem 根，含当根；无未来函数）──
    db = np.full(n, np.nan)
    ub = np.full(n, np.nan)
    valid_idx = np.where(~np.isnan(crsi))[0]
    if len(valid_idx):
        for t in range(valid_idx[0] + mem - 1, n):
            win = crsi[t - mem + 1 : t + 1][::-1]  # 最新在前 = Pine 的 crsi[0..mem-1]
            if np.isnan(win).any():
                continue
            lmax, lmin = -999999.0, 999999.0
            for v in win:
                if v > lmax:
                    lmax = v
                elif v < lmin:
                    lmin = v
            mstep = (lmax - lmin) / 100.0
            db_t = 0.0
            for steps in range(101):
                tv = lmin + mstep * steps
                if (win < tv).sum() / mem >= aperc:
                    db_t = tv
                    break
            ub_t = 0.0
            for steps in range(101):
                tv = lmax - mstep * steps
                if (win >= tv).sum() / mem >= aperc:
                    ub_t = tv
                    break
            db[t], ub[t] = db_t, ub_t

    # ── 末根读数 ──
    c_last = crsi[-1] if n else np.nan
    db_last = db[-1] if n else np.nan
    ub_last = ub[-1] if n else np.nan
    pos = None
    zone = None
    if np.isfinite(c_last) and np.isfinite(db_last) and np.isfinite(ub_last):
        pos = (c_last - db_last) / (ub_last - db_last) * 100.0 if ub_last != db_last else 50.0
        zone = "超买区" if c_last >= ub_last else ("超卖区" if c_last <= db_last else "带内")

    # ── 背离（枢轴左右各 piv_len 根严格确认）──
    divergences = []
    pl_prev = None  # (crsi 枢轴低, 当根价格低点)
    ph_prev = None
    lows = df["low"].to_numpy(dtype=float)
    highs = df["high"].to_numpy(dtype=float)
    for i in range(piv_len, n - piv_len):
        c = crsi[i]
        if np.isnan(c):
            continue
        left = crsi[i - piv_len : i]
        right = crsi[i + 1 : i + piv_len + 1]
        if np.isnan(left).any() or np.isnan(right).any():
            continue
        if (c < left).all() and (c < right).all():  # 枢轴低点
            if pl_prev is not None and c > pl_prev[0] and lows[i] < pl_prev[1]:
                divergences.append({"i": i, "kind": "bull"})
            pl_prev = (c, lows[i])
        if (c > left).all() and (c > right).all():  # 枢轴高点
            if ph_prev is not None and c < ph_prev[0] and highs[i] > ph_prev[1]:
                divergences.append({"i": i, "kind": "bear"})
            ph_prev = (c, highs[i])

    last_div = None
    if divergences:
        d0 = divergences[-1]
        last_div = {"kind": d0["kind"], "i": d0["i"], "bars_ago": (n - 1) - d0["i"]}

    def _r(v):
        return round(float(v), 2) if v is not None and np.isfinite(v) else None

    return {
        "series": {"crsi": crsi, "db": db, "ub": ub},
        "last": {"crsi": _r(c_last), "db": _r(db_last), "ub": _r(ub_last),
                 "pos": round(float(pos), 1) if pos is not None else None, "zone": zone},
        "divergences": divergences,
        "last_divergence": last_div,
    }
