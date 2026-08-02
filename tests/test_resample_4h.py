#!/usr/bin/env python3
"""Deribit 1h→4h 重采样的桶边界回归。

用法: .venv/bin/python tests/test_resample_4h.py

守的是一个已发生的事故：取数窗口起点未对齐 4h 边界时，首桶只装 1~3 根 1h，
聚合出残值；而 upsert 按 ts 覆盖，残值会在滑出取数窗口那一刻被定格、永久留库
——BTC/ETH/SOL 各坏过 8 根、每天再坏 6 根，最终靠宽窗重拉 + 全量重算才清掉
（scripts/repair_time_alignment.py）。规则：**首桶不完整必须丢，末桶必须留**
（末桶是"形成中"的当前 4h，collector 拿它写 live_bars 做未收线预览）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime.data import _resample_4h  # noqa: E402


def hourly(start_hour_utc: int, n: int) -> pd.DataFrame:
    """从某个 UTC 整点小时开始的 n 根 1h K 线，o/h/l/c/v 全部可逐根追溯。"""
    base = 1_700_000_000 - (1_700_000_000 % 86_400) + start_hour_utc * 3600
    ts = pd.to_datetime((base + np.arange(n) * 3600) * 1000, unit="ms", utc=True)
    i = np.arange(n, dtype=float)
    return pd.DataFrame({
        "ts": ts,
        "open": 100 + i, "high": 200 + i, "low": 50 - i,
        "close": 150 + i, "volume": np.full(n, 10.0),
    })


def main() -> None:
    # ① 起点未对齐（01:00 开始）：首桶只有 3 根 → 必须被丢弃
    df = hourly(1, 12)                     # 01:00..12:00，首桶 01-03 共 3 根
    g = _resample_4h(df)
    first_ts = int(g["ts"].iloc[0].value // 10**9) % 86_400 // 3600
    print(f"① 未对齐起点  首桶起始小时={first_ts}（应为 4，01-03 的残桶被丢）")
    assert first_ts == 4, f"残缺首桶未被丢弃，首桶落在 {first_ts} 点"
    # 完整桶的聚合语义逐字段验证（open=首根开、high=max、low=min、close=末根收、vol=和）
    b = g.iloc[0]
    assert b["open"] == 103 and b["close"] == 156, f"open/close 语义错: {b['open']}/{b['close']}"
    assert b["high"] == 206 and b["low"] == 44, f"high/low 语义错: {b['high']}/{b['low']}"
    assert b["volume"] == 40, f"volume 应为 4 根之和 40，实测 {b['volume']}"

    # ② 末桶不完整：必须保留（live_bars 预览依赖它）
    df2 = hourly(0, 10)                    # 00:00..09:00，末桶 08-09 只有 2 根
    g2 = _resample_4h(df2)
    last_n_src = 2
    print(f"② 残缺末桶    桶数={len(g2)}（应为 3：两个完整 + 一个形成中的末桶）")
    assert len(g2) == 3, f"末桶被误丢或多出：{len(g2)} 个桶"
    assert g2["volume"].iloc[-1] == 10.0 * last_n_src, "末桶应只聚合已有的 2 根"

    # ③ 起点恰好对齐：一根都不丢
    df3 = hourly(0, 12)
    g3 = _resample_4h(df3)
    print(f"③ 对齐起点    桶数={len(g3)}（应为 3，零浪费）")
    assert len(g3) == 3 and g3["volume"].iloc[0] == 40

    # ④ 幂等性：对同一数据重采样两次结果一致（upsert 反复写入的前提）
    assert g3.equals(_resample_4h(df3)), "重采样不幂等"
    print("④ 幂等性      两次重采样逐值相等 ✓")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()


def test_all():
    main()
