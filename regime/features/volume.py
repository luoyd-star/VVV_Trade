"""成交量支柱：相对量能水平、多空量差、突破的量能确认。

成交量不给方向，它回答的是"当前价格行为有没有参与度背书"。
"""
from __future__ import annotations

import pandas as pd

from .utils import pct_rank


def volume_features(df: pd.DataFrame, rank_window: int = 250) -> dict:
    vol = df["volume"]
    chg = df["close"].diff()

    mean20 = float(vol.rolling(20).mean().iloc[-1])
    std20 = float(vol.rolling(20).std().iloc[-1])
    vol_z20 = (float(vol.iloc[-1]) - mean20) / (std20 + 1e-12)
    vol_rank = pct_rank(vol, rank_window)

    # 近 20 根内上涨 K 线与下跌 K 线的成交量差，[-1,1]：量能站在哪一边
    n = 20
    up = float(vol.tail(n)[chg.tail(n) > 0].sum())
    down = float(vol.tail(n)[chg.tail(n) < 0].sum())
    tilt = (up - down) / (up + down) if (up + down) > 0 else 0.0

    # 近 5 根内若收盘突破 60 根 Donchian 高/低点，记录突破方向和当根量分位
    breakout = None
    dc_hi = df["high"].rolling(60).max().shift(1)
    dc_lo = df["low"].rolling(60).min().shift(1)
    for i in df.tail(5).index:
        c = float(df["close"].loc[i])
        hi, lo = dc_hi.loc[i], dc_lo.loc[i]
        if pd.notna(hi) and c > float(hi):
            breakout = {"dir": "up", "vol_rank": round(pct_rank(vol.loc[:i], rank_window), 3)}
        elif pd.notna(lo) and c < float(lo):
            breakout = {"dir": "down", "vol_rank": round(pct_rank(vol.loc[:i], rank_window), 3)}

    return {
        "vol_z20": round(vol_z20, 2),
        "vol_rank": round(vol_rank, 3),
        "updown_tilt_20": round(tilt, 3),
        "breakout": breakout,
    }
