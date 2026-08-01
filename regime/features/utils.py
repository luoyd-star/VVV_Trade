"""特征层共用的小工具。"""
from __future__ import annotations

import numpy as np
import pandas as pd


def pct_rank(series: pd.Series, window: int = 250) -> float:
    """当前值在最近 window 个有效值中的分位（0..1，越高代表当前值越极端偏大）。"""
    s = series.dropna().tail(window)
    if len(s) < 2:
        return 0.5
    return float((s < s.iloc[-1]).mean())


def rolling_pct_rank(series: pd.Series, window: int = 250) -> np.ndarray:
    """整条序列逐点计算 pct_rank（每点只看它自己及之前的 window 个值）。

    与 pct_rank 同口径，用于面板画分位时间线。返回含 NaN 的 ndarray。
    """
    vals = series.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    for i in range(len(vals)):
        if np.isnan(vals[i]):
            continue
        w = vals[max(0, i - window + 1): i + 1]
        w = w[~np.isnan(w)]
        if len(w) >= 2:
            out[i] = float((w < w[-1]).mean())
    return out
