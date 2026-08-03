"""滚动 VWAP 偏离（币安量源，显示/上下文层——不进规则树、不进审计快照）。

设计（2026-08-03 决策）：
- 量源统一取币安 fapi 1h 量流（quote_vol/volume = 该小时精确 VWAP），与各
  序列的 OHLCV 主源解耦——价格被套利钉死可跨源，量是场馆局部量必须单源；
  币安是全市场量最大的场馆。
- 任意周期任意日界锚（Deribit 1d 的 08:00 / 币安的 00:00）都从同一条 1h
  流按**时间窗**聚合：bar 的窗 = (close_ts - W, close_ts]，与 K 线网格相位无关。
- VWAP 对量的水平是零次齐次——场馆量的绝对大小不影响值，只有组成结构进入。
- 宁缺毋滥：窗内 1h 覆盖率不足 COVER_MIN 时返回 NaN，不塌成部分窗的假值。

证据等级：VWAP 偏离回归属从业者经验（预研未覆盖、无严格学术证据），
故按影子纪律只做展示与 Hermes 上下文；若未来要入库快照须升 AUDIT_VERSION。
"""
from __future__ import annotations

import numpy as np

from .utils import pct_rank

# 滚动窗（小时）：1h≈日锚、4h≈周锚、1d≈月锚（20 交易日惯例）
WIN_HOURS = {"1h": 24, "4h": 168, "1d": 480}
COVER_MIN = 0.8   # 窗内 1h bar 覆盖率下限
RANK_WIN = 250    # 偏离分位参照窗（与全系统分位纪律同口径）
RANK_MIN = 60     # 分位最小参照样本；不足返回 None（不许用 0.5 伪装中性）
_H_MS = 3_600_000


def rolling_vwap(bar_close_ms, vol_ts_ms, volume, quote_vol, window_hours: int):
    """对每根 bar 的收线时刻取 (close-W, close] 内 Σquote/Σvol。

    bar_close_ms: 目标 K 线的**收线**时刻数组（开盘 ts + tf）；
    vol_ts_ms: 1h 量流的开盘 ts（升序）；一根 1h 量 bar 属于窗内当且仅当
    其整根落在窗内：ts >= close-W 且 ts+1h <= close。
    覆盖率 < COVER_MIN 或 Σvol=0 时该 bar 为 NaN。
    """
    bars = np.asarray(bar_close_ms, dtype="int64")
    vts = np.asarray(vol_ts_ms, dtype="int64")
    v = np.asarray(volume, dtype=float)
    q = np.asarray(quote_vol, dtype=float)
    w_ms = window_hours * _H_MS
    # 前缀和 + 二分：每根 bar O(log n)
    cv = np.concatenate([[0.0], np.cumsum(v)])
    cq = np.concatenate([[0.0], np.cumsum(q)])
    out = np.full(len(bars), np.nan)
    for i, close in enumerate(bars):
        lo = np.searchsorted(vts, close - w_ms, side="left")
        hi = np.searchsorted(vts, close - _H_MS, side="right")  # ts+1h<=close
        n = hi - lo
        if n < window_hours * COVER_MIN:
            continue
        sv = cv[hi] - cv[lo]
        if sv <= 0:
            continue
        out[i] = (cq[hi] - cq[lo]) / sv
    return out


def vwap_payload(close, atr14, vwap):
    """偏离序列（ATR 单位）+ 末值 + 分位。close/atr14/vwap 等长对齐。"""
    close = np.asarray(close, dtype=float)
    atr = np.asarray(atr14, dtype=float)
    with np.errstate(invalid="ignore", divide="ignore"):
        dev = np.where(atr > 0, (close - vwap) / atr, np.nan)
    fin = np.isfinite(dev)
    last = float(dev[-1]) if fin[-1] else None
    rank = None
    if fin.sum() >= RANK_MIN and fin[-1]:
        import pandas as pd
        rank = round(pct_rank(pd.Series(dev), RANK_WIN), 3)
    return dev, last, rank
