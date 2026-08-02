"""波动率支柱：ATR 及其历史分位、已实现波动率、布林带宽与挤压识别。

核心思路：波动率的绝对值意义不大，"当前处于自身历史的什么分位"才可比、可用。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .utils import pct_rank


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    """SMA-ATR(14)——**不是** Wilder RMA。

    Wilder 的 alpha=1/14 等价 span≈27，记忆约为 SMA(14) 的两倍、尖峰后衰减
    慢得多；实测 BTC 1h（358 根）末根差 9.9%、中位 9.3%、P90 24.0%（早期快照记的 39%~57%
    是特定时点的极值，已核伪）；经分位吸收后仍会翻转部分 squeeze/high_vol 布尔。迁移阈值到 TradingView 等 Wilder 实现时必须换算。
    是否切换 Wilder 留给回测校准裁决（那是 RULES_VERSION 升版级别的变更）；
    在此之前，本实现的名字必须诚实：它是 SMA 变体。
    """
    return true_range(df).rolling(n).mean()


def realized_vol(close: pd.Series, n: int = 30, bars_per_year: float = 365) -> pd.Series:
    """close-to-close 已实现波动率（年化）。"""
    ret = np.log(close / close.shift(1))
    return ret.rolling(n).std() * np.sqrt(bars_per_year)


def bb_width(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return (2 * k * sd) / mid


def session_bucket(ts: pd.Series) -> pd.Series:
    """时段桶键 = ET 小时 + 24×(ET 是否周末)，共 48 桶。

    **必须按 ET 而不是 UTC**：时段效应锚在交易所本地时间上，夏令时会让 ET 时段
    相对 UTC 整体平移一小时。同一个 ET 09:00（开盘小时）在 EDT 是 UTC 13:00、
    在 EST 是 UTC 14:00——UTC 分桶会把开盘小时和开盘前一小时混进同一个桶，
    实测这两个时段的 TR% 差近 3 倍。周末标志同理：ET 周五 20:00-24:00 在 UTC
    已是周六，用 UTC 会被错当周末（实测 5.08% 的 bar 归错）。
    """
    et = ts.dt.tz_convert("America/New_York")
    return et.dt.hour + 24 * (et.dt.dayofweek >= 5).astype(int)


def _deseasonalized_atr_rank(df: pd.DataFrame, rank_window: int):
    """hour-of-day × 周末 去季节化的 ATR 分位（影子字段，美股永续 1h/4h 用）。

    动机（实测）：美股永续 TR% 盘中/盘外 2.2-2.7 倍、周末塌陷 2-5 倍，
    atr_rank 在盘外主要在识别"现在是夜里"而非真实波动状态。
    做法：每根 bar 的 TR% 除以其 (**ET 小时**, 是否周末) 桶的**此前** 30 个同桶观测均值
    （rolling+shift(1)，walk-forward 无泄漏），再乘全局滚动均值还原量纲，
    然后照常 ATR(14) + 分位。因子样本不足（新上市/周末桶不满 8 个观测）时返回 None
    ——宁缺毋滥，不塌成未去季节化的值。

    **必须按 ET 而不是 UTC 分桶**：时段效应锚在交易所本地时间上，夏令时切换会让
    ET 时段相对 UTC 整体平移一小时。实测 TSLA 1h 跨 2026-03-08 DST：按 UTC 分桶时
    开盘小时桶的 EDT/EST 均值比达 2.98（因子错配 3 倍），按 ET 分桶则全部 24 个桶
    落在 0.86~1.43 无系统性台阶；周末标志同理，UTC 口径有 5.08% 的 bar 归错。
    因子 rolling(30) 在工作日桶每天仅 1 个观测，错配后要 6 周才冲刷干净。
    """
    trp = true_range(df) / df["close"]
    bucket = session_bucket(df["ts"])
    fac = trp.groupby(bucket).transform(
        lambda s: s.rolling(30, min_periods=8).mean().shift(1)
    )
    overall = trp.rolling(240, min_periods=48).mean().shift(1)
    tr_ds = trp / fac * overall
    atr_ds = tr_ds.rolling(14).mean()
    if not np.isfinite(atr_ds.iloc[-1]):
        return None
    return round(pct_rank(atr_ds, rank_window), 3)


def volatility_features(
    df: pd.DataFrame,
    bars_per_year: float = 365,
    rank_window: int = 250,
    session_aware: bool = False,
) -> dict:
    close = df["close"]
    atr_s = atr(df)
    atr_pct_price = atr_s / close  # ATR 相对价格，跨价格水平可比
    bbw = bb_width(close)
    rv = realized_vol(close, 30, bars_per_year)

    atr_rank = pct_rank(atr_pct_price, rank_window)
    bbw_rank = pct_rank(bbw, rank_window)

    # 方向性波动率特征（启发自 crypto-monitor）：
    # 加速度 = 快 RV / 慢 RV（>1 扩张中，比分位更早）；下行方差占比给波动率装上方向。
    ret = np.log(close / close.shift(1))
    accel_series = ret.rolling(12).std() / ret.rolling(72).std()
    vol_accel = float(accel_series.iloc[-1])
    accel_rank = pct_rank(accel_series, rank_window)
    ret2 = ret**2
    down2 = ret.where(ret < 0, 0.0) ** 2
    dshare_series = down2.rolling(48).sum() / ret2.rolling(48).sum()
    downside_share = float(dshare_series.iloc[-1])

    atr_rank_ds = _deseasonalized_atr_rank(df, rank_window) if session_aware else None

    return {
        "atr_rank_ds": atr_rank_ds,
        "vol_accel": round(vol_accel, 3) if np.isfinite(vol_accel) else None,
        "vol_accel_rank": round(accel_rank, 3),
        "downside_share": round(downside_share, 3) if np.isfinite(downside_share) else None,
        "atr": float(atr_s.iloc[-1]),
        "atr_pct_of_price": round(float(atr_pct_price.iloc[-1]) * 100, 3),
        "atr_rank": round(atr_rank, 3),
        "bbw_rank": round(bbw_rank, 3),
        "rv30_annual_pct": round(float(rv.iloc[-1]) * 100, 1),
        "squeeze": bool(bbw_rank < 0.15 and atr_rank < 0.30),
        "high_vol": bool(atr_rank > 0.85),
    }
