"""价格结构支柱：摆动高低点序列（HH/HL vs LH/LL）、均线坡度、Donchian 位置。

所有子分数都压缩到 [-1, +1] 后加权合成一个方向分 direction。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .volatility import atr


def swing_pivots(df: pd.DataFrame, k: int = 4) -> pd.DataFrame:
    """滚动分形摆动点。一个 pivot 需要左右各 k 根 K 线确认，
    因此最近 k 根内的极值不会被标记——这是刻意的，避免未来函数。
    """
    h, l = df["high"].to_numpy(), df["low"].to_numpy()
    out = []
    for i in range(k, len(df) - k):
        window_h = h[i - k : i + k + 1]
        window_l = l[i - k : i + k + 1]
        if h[i] == window_h.max() and h[i] > h[i - k : i].max():
            out.append((i, df["ts"].iloc[i], h[i], "H"))
        if l[i] == window_l.min() and l[i] < l[i - k : i].min():
            out.append((i, df["ts"].iloc[i], l[i], "L"))
    return pd.DataFrame(out, columns=["idx", "ts", "price", "kind"])


def structure_direction(pivots: pd.DataFrame, n_pairs: int = 3) -> float:
    """比较最近几个同类摆动点的抬升/压低：HH/HL 计 +1，LH/LL 计 -1，得 [-1, 1]。"""
    score, count = 0, 0
    for kind in ("H", "L"):
        seq = pivots[pivots["kind"] == kind]["price"].tail(n_pairs + 1).to_numpy()
        for a, b in zip(seq[:-1], seq[1:]):
            score += 1 if b > a else -1
            count += 1
    return score / count if count else 0.0


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def efficiency_ratio_series(close: pd.Series, n: int = 30) -> pd.Series:
    """Kaufman 效率比：|净位移| / 路径长度，[0,1]，衡量趋势效率（替代 ADX）。"""
    net = (close - close.shift(n)).abs()
    path = close.diff().abs().rolling(n).sum()
    return net / path


def structure_features(df: pd.DataFrame, pivot_k: int = 4) -> dict:
    close = df["close"]
    piv = swing_pivots(df, k=pivot_k)
    pivot_dir = structure_direction(piv)

    # EMA50 近 10 根的坡度，用 ATR 归一化后经 tanh 压缩到 [-1,1]
    ema50 = ema(close, 50)
    atr_last = float(atr(df).iloc[-1])
    if np.isfinite(atr_last) and atr_last > 0 and len(df) > 11:
        slope = (float(ema50.iloc[-1]) - float(ema50.iloc[-11])) / (atr_last * 10)
        slope_score = float(np.tanh(2.0 * slope))
    else:
        slope_score = 0.0

    dc_win = min(100, len(df) - 1)
    dc_high = float(df["high"].rolling(dc_win).max().iloc[-1])
    dc_low = float(df["low"].rolling(dc_win).min().iloc[-1])
    dc_pos = (float(close.iloc[-1]) - dc_low) / max(dc_high - dc_low, 1e-12)
    dc_score = 2 * dc_pos - 1

    # 缺席的子分必须把权重让给在场的（重归一），而不是按 0 计入——
    # 否则历史 <200 根时 |direction| 的上限悄悄从 1.00 掉到 0.90，
    # 却仍去比同一个 0.30 阈值：同样的结构，短历史品种更难被判成趋势。
    # 截至 2026-08-05，现库 win<200 的入库行占 7.62%（1d 23.09% / 4h 16.51% /
    # 1h 4.47%）；v1→v2 时的历史比例见 git blame，不作为当前覆盖率引用。
    parts = [(0.45, pivot_dir), (0.25, slope_score), (0.20, dc_score)]
    if len(df) >= 200:
        above_ema200 = 1.0 if float(close.iloc[-1]) > float(ema(close, 200).iloc[-1]) else -1.0
        parts.append((0.10, above_ema200))
    else:
        above_ema200 = 0.0
    wsum = sum(w for w, _ in parts)
    direction = sum(w * v for w, v in parts) / wsum

    highs = piv[piv["kind"] == "H"]["price"]
    lows = piv[piv["kind"] == "L"]["price"]
    return {
        "pivot_dir": round(pivot_dir, 3),
        "ema_slope": round(slope_score, 3),
        "donchian_pos": round(dc_pos, 3),
        "direction": round(float(direction), 3),
        "swing_high": float(highs.iloc[-1]) if len(highs) else None,
        "swing_low": float(lows.iloc[-1]) if len(lows) else None,
    }
