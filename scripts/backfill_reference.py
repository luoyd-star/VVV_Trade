#!/usr/bin/env python3
"""底层参考历史回填（2020-2026）：耦合研究的制度覆盖层（DESIGN_COUPLING §7 决策项1）。

  .venv/bin/python scripts/backfill_reference.py

数据源（2026-08-04 实测选定）：
- 价格层主源 Yahoo chart API（免费无 key，period1/period2 显式日线——
  range=max 会被降采样成月线，勿用）：NDX/QQQ/MSTR/COIN/GLD/GC/CL/VIX
- BTC 用币安现货 BTCUSDT 1d（2020-01-01 起，与系统既有币安管线同构）
- 宏观协变量 FRED fredgraph CSV：当前网络实测持续超时，逐条尝试、
  失败跳过并明示——价格层不受影响，FRED 待网络可达或换 API key 方案

覆盖 episode：2020 疫情、2022 紧缩去杠杆、2024 ETF 事件脱耦、2025 再耦合。
幂等：INSERT OR REPLACE，重跑只补新。
"""
from __future__ import annotations

import io
import csv
import os
import sys
import time

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import storage  # noqa: E402
from regime.data import (  # noqa: E402
    fetch_binance_spot_daily, fetch_yahoo_daily,
)

UA = {"User-Agent": "Mozilla/5.0"}
P1_2020 = 1_577_836_800  # 2020-01-01 UTC 秒

YAHOO_SERIES = {
    "NDX": "^NDX", "QQQ": "QQQ", "MSTR": "MSTR", "COIN": "COIN",
    "GLD": "GLD", "GC": "GC=F", "CL": "CL=F", "VIX": "^VIX",
}
FRED_SERIES = ("DFII10", "T10YIE", "DTWEXBGS", "BAMLH0A0HYM2", "NFCI",
               "VIXCLS", "DCOILWTICO", "WALCL")


def fetch_fred_csv(series: str):
    r = requests.get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}",
                     headers=UA, timeout=60)
    r.raise_for_status()
    rows = list(csv.reader(io.StringIO(r.text)))
    out = []
    for d, v in rows[1:]:
        if v in (".", ""):
            continue
        t = int(time.mktime(time.strptime(d, "%Y-%m-%d"))) - time.timezone
        out.append(((t // 86400) * 86400 * 1000, float(v)))
    return [(t, v) for t, v in out if t >= P1_2020 * 1000]


def main() -> None:
    conn = storage.connect()  # 写者连接（建表）；与 collector 短暂并存无碍（WAL）
    try:
        print("== BTC（币安现货 1d）==")
        rows = fetch_binance_spot_daily()
        print(f"  BTC: {storage.upsert_ref_daily(conn, 'BTC', rows, 'binance_spot')} 行")
        print("== 价格层（Yahoo 日线）==")
        for name, tk in YAHOO_SERIES.items():
            try:
                rows = fetch_yahoo_daily(tk)
                n = storage.upsert_ref_daily(conn, name, rows, "yahoo")
                print(f"  {name:5s}: {n} 行 ({time.strftime('%Y-%m-%d', time.gmtime(rows[0][0]/1000))} 起)")
            except Exception as e:  # noqa: BLE001
                print(f"  {name:5s}: 失败 {e}", file=sys.stderr)
            time.sleep(0.5)
        print("== 宏观协变量（FRED，可能超时跳过）==")
        for s in FRED_SERIES:
            try:
                rows = fetch_fred_csv(s)
                n = storage.upsert_ref_daily(conn, s, rows, "fred")
                print(f"  {s:12s}: {n} 行")
            except Exception as e:  # noqa: BLE001
                print(f"  {s:12s}: 跳过（{type(e).__name__}）——价格层不受影响", file=sys.stderr)
            time.sleep(0.5)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
