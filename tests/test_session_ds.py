#!/usr/bin/env python3
"""去季节化 ATR 分位（atr_rank_ds）的正确性测试（L0 影子字段）。

用法: .venv/bin/python tests/test_session_ds.py

三条性质：
1. 纯时段效应下（波动只随小时变化、无真实状态变化），raw atr_rank 在
   盘中/盘外端点间有系统性大差距，ds 版应把这个差距压掉大半；
2. 真实的全时段波动抬升（跨所有小时 ×3）ds 版必须仍然报高分位——
   去季节化去的是"时钟"，不能把真实信号一起去掉；
3. 因子样本不足（同桶不满 8 个观测）时返回 None，不塌成未去季节化的值。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime.features.volatility import (  # noqa: E402
    _deseasonalized_atr_rank,
    session_bucket,
    volatility_features,
)


def make_hourly_df(n: int, seed: int = 7, boost_last: int = 0, start_ms: int | None = None) -> pd.DataFrame:
    """小时线合成数据：**ET 9:00-16:00** 的振幅是其余时段的 4 倍（模拟美股盘中）。

    盘中时段按 ET 生成——这才是真实的锚：美股 RTH 是 9:30-16:00 ET，
    对应 UTC 13-20（夏令时）或 14-21（冬令时），随 DST 平移。
    boost_last>0 时把最后 boost_last 根的振幅再 ×3（跨所有小时的真实抬升）。
    """
    rng = np.random.default_rng(seed)
    base = 1_700_000_000 if start_ms is None else start_ms // 1000
    ts = pd.to_datetime((base + np.arange(n) * 3600) * 1000, unit="ms", utc=True)
    et_hours = ts.tz_convert("America/New_York").hour.to_numpy()
    sigma = np.where((et_hours >= 9) & (et_hours < 16), 4.0, 1.0).astype(float)
    if boost_last:
        sigma[-boost_last:] *= 3.0
    amp = sigma * (0.4 + 0.6 * np.abs(rng.standard_normal(n)))
    close = 100.0 + np.cumsum(rng.standard_normal(n) * 0.05)
    return pd.DataFrame({
        "ts": ts,
        "open": close,
        "high": close + amp,
        "low": close - amp,
        "close": close,
        "volume": np.full(n, 100.0),
    })


def ranks_at_hour(df: pd.DataFrame, hour: int, k: int = 25):
    """取最近 k 个 ET 整点为 hour 的端点，逐个算 (raw_rank, ds_rank)。"""
    idxs = np.where(df["ts"].dt.tz_convert("America/New_York").dt.hour.to_numpy() == hour)[0]
    idxs = idxs[idxs >= 800][-k:]
    raw, ds = [], []
    for i in idxs:
        v = volatility_features(df.iloc[: i + 1], bars_per_year=24 * 365, session_aware=True)
        raw.append(v["atr_rank"])
        ds.append(v["atr_rank_ds"])
    assert all(x is not None for x in ds), "样本充足时 ds 不应为 None"
    return np.array(raw, dtype=float), np.array(ds, dtype=float)


def dst_bucket_step(bucket_tz: str) -> tuple:
    """跨 2026-11-01 DST 的分桶质量，返回 (错配桶个数, 最大台阶)。

    每个桶算 DST 前后的 TR% 均值比（取 ≥1 的方向），比值远离 1 = 该分桶口径把
    DST 前后两个不同时段混进了同一个桶。ET 时段在 DST 后相对 UTC 平移一小时，
    所以 UTC 分桶的损害**集中在 session 边界的少数几个桶**（开盘/收盘那一小时，
    那里 sigma 有 4 倍跳变），整个在盘内或盘外的桶比值本来就≈1。
    因此用「比值 > 2 的桶个数」而不是中位数——中位数会被大量无辜桶稀释掉。
    """
    # 覆盖 2026-10-01 → 2026-11-30，跨 11-01 02:00 ET 的 DST 结束
    start = int(pd.Timestamp("2026-10-01", tz="UTC").value // 10**6)
    df = make_hourly_df(24 * 60, seed=11, start_ms=start)
    trp = (df["high"] - df["low"]) / df["close"]
    ts = df["ts"] if bucket_tz == "UTC" else df["ts"].dt.tz_convert("America/New_York")
    b = ts.dt.hour + 24 * (ts.dt.dayofweek >= 5).astype(int)
    et = df["ts"].dt.tz_convert("America/New_York")
    is_dst = et.dt.strftime("%Z") == "EDT"
    weekday = et.dt.dayofweek < 5
    # 取 DST 前后样本都足够的桶，算每桶的 EDT/EST 均值比，返回最极端的那个
    ratios = []
    for bucket in sorted(b[weekday].unique()):
        m = weekday & (b == bucket)
        a, c = trp[m & is_dst], trp[m & ~is_dst]
        if len(a) >= 8 and len(c) >= 8 and c.mean() > 0:
            r = a.mean() / c.mean()
            ratios.append(max(r, 1 / r))
    if not ratios:
        return 0, 1.0
    return sum(1 for r in ratios if r > 2.0), float(max(ratios))


def main() -> None:
    df = make_hourly_df(2400)

    # ① 纯时段效应：ET 15 点端点（ATR14 覆盖整个盘中）vs ET 3 点端点（纯盘外）
    raw_busy, ds_busy = ranks_at_hour(df, 15)
    raw_quiet, ds_quiet = ranks_at_hour(df, 3)
    gap_raw = float(raw_busy.mean() - raw_quiet.mean())
    gap_ds = float(ds_busy.mean() - ds_quiet.mean())
    print(f"① 时段假信号  raw 盘中-盘外分位差={gap_raw:+.3f}  ds 差={gap_ds:+.3f}")
    assert gap_raw > 0.30, f"合成数据应让 raw rank 出现大时段差距，实测 {gap_raw:.3f}"
    assert abs(gap_ds) < gap_raw / 2, (
        f"ds 应至少压掉一半时段差距: raw={gap_raw:.3f} ds={gap_ds:.3f}"
    )

    # ② 真实抬升不被去掉：最后 72 根全时段 ×3
    df2 = make_hourly_df(2400, boost_last=72)
    v2 = volatility_features(df2, bars_per_year=24 * 365, session_aware=True)
    print(f"② 真实抬升    raw={v2['atr_rank']:.3f}  ds={v2['atr_rank_ds']:.3f}")
    assert v2["atr_rank_ds"] > 0.7, f"真实全时段抬升 ds 分位应高，实测 {v2['atr_rank_ds']}"

    # ③ 样本不足返回 None（150 根 ≈ 每桶 6 个观测 < min_periods 8）
    short = make_hourly_df(150)
    r3 = _deseasonalized_atr_rank(short, 250)
    print(f"③ 样本不足    返回={r3}")
    assert r3 is None, f"因子样本不足应返回 None，实测 {r3}"

    # ④ 默认关闭：session_aware=False 时字段为 None（加密品种零改变）
    v4 = volatility_features(df, bars_per_year=24 * 365)
    assert v4["atr_rank_ds"] is None, "默认（加密）路径不应计算 ds"
    print("④ 默认关闭    加密路径 atr_rank_ds=None ✓")

    # ⑤ 生产分桶函数必须按 ET（直接钉 session_bucket，不是测试自己重算一遍）
    def b(iso):
        return int(session_bucket(pd.Series([pd.Timestamp(iso, tz="UTC")])).iloc[0])

    # 两者同为 ET 09:30 的**工作日**（10-15 周四 EDT / 11-17 周二 EST），只差 DST
    edt_open, est_open = b("2026-10-15 13:30"), b("2026-11-17 14:30")
    print(f"⑤ 生产分桶    ET 09:30 → EDT 桶 {edt_open} / EST 桶 {est_open}（必须相同）")
    assert edt_open == est_open == 9, (
        f"DST 前后同一个 ET 时段必须落进同一桶：{edt_open} vs {est_open}"
        "（若为 13/14 说明仍在按 UTC 小时分桶）"
    )
    # ET 周五 21:00 在 UTC 已是周六——周末标志必须按 ET 判
    fri_et = b("2026-10-17 01:00")   # = 2026-10-16 21:00 ET（周五）
    sat_et = b("2026-10-17 16:00")   # = 2026-10-17 12:00 ET（周六）
    print(f"⑥ 周末标志    ET 周五夜 → 桶 {fri_et}（工作日区 <24）· ET 周六 → 桶 {sat_et}（周末区 ≥24）")
    assert fri_et < 24, f"ET 周五 21:00 被错判成周末（桶 {fri_et}）——周末标志用了 UTC"
    assert sat_et >= 24, f"ET 周六应落进周末区，实测桶 {sat_et}"

    # ⑦ 概念对照：跨 2026-11-01 DST 时两种分桶口径的错配桶数量
    utc_bad, utc_max = dst_bucket_step("UTC")
    et_bad, et_max = dst_bucket_step("ET")
    print(f"⑦ DST 损害量  跨 11-01：UTC 分桶错配 {utc_bad} 桶（最大 {utc_max:.2f}×）"
          f" · ET 分桶错配 {et_bad} 桶（最大 {et_max:.2f}×）")
    assert utc_bad >= 2, f"UTC 分桶本应有多个错配桶（实测 {utc_bad}），fixture 可能没跨 DST"
    assert et_bad == 0, f"ET 分桶不应有任何错配桶，实测 {et_bad} 个（最大 {et_max:.2f}×）"

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
