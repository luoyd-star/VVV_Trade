#!/usr/bin/env python3
"""一次性修复：时间对齐批次留下的存量坏数据（2026-08-02）。

用法: .venv/bin/python scripts/repair_time_alignment.py [--dry-run]

修两处**代码改完也不会自愈**的存量损坏：

1. **4h 残桶**（BTC/ETH/SOL 各 8 根，每天再坏 6 根）。_resample_4h 原先不丢首桶，
   而取数窗口起点没对齐 4h 边界，首桶只装 1~3 根 1h K 线；upsert 按 ts 覆盖，
   这根残值会在滑出取数窗口的那一刻被定格、永久留库。修法是用 Deribit 1h 宽窗
   重新完整重采样，只取 n==4 的完整桶覆盖回去。
2. **premium 末根未收线**（10 个品种各 1 行）。backfill 原先不裁末根，而
   deriv_backfilled_* 是一次性开关，那行永不刷新。重拉一次 premiumIndexKlines
   覆盖（新代码已带 [:-1]）。

两处修完后必须让 collector 跑一轮触发 regime_audit_v5 重算——4h 的 ohlcv 值变了，
库里的审计快照对不上了（实测 BTC 4h 90/221 行 atr_rank 不符）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import storage  # noqa: E402
from regime.data import DERIBIT_BASE, _deribit_instrument, _normalize  # noqa: E402
from regime.deriv import backfill as deriv_backfill  # noqa: E402

CRYPTO = ("BTC-USDT", "ETH-USDT", "SOL-USDT")


def rebuild_4h(symbol: str, hours: int = 3000) -> pd.DataFrame:
    """用 Deribit 1h 宽窗重采样出**完整**的 4h（只保留 n==4 的桶）。"""
    end = int(time.time() * 1000)
    r = requests.get(
        f"{DERIBIT_BASE}/api/v2/public/get_tradingview_chart_data",
        params={
            "instrument_name": _deribit_instrument(symbol),
            "resolution": "60",
            "start_timestamp": end - hours * 3_600_000,
            "end_timestamp": end,
        },
        timeout=30,
    )
    r.raise_for_status()
    res = r.json()["result"]
    h = _normalize(pd.DataFrame({
        "ts": res["ticks"], "open": res["open"], "high": res["high"],
        "low": res["low"], "close": res["close"], "volume": res["volume"],
    }))
    bucket = (h["ts"].astype("int64") // 10**9 // 14_400) * 14_400
    g = h.groupby(bucket).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), volume=("volume", "sum"), n=("close", "size"),
    )
    g = g[g["n"] == 4].drop(columns=["n"])
    g.insert(0, "ts", pd.to_datetime(g.index, unit="s", utc=True))
    return g.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告差异，不写库")
    args = ap.parse_args()
    conn = storage.connect()
    total_fixed = 0

    print("=== 1. 4h 残桶修复（Deribit 1h 宽窗重采样） ===")
    for sym in CRYPTO:
        truth = rebuild_4h(sym)
        tmap = {int(t.value // 10**6): row for t, row in
                zip(truth["ts"], truth.to_dict("records"))}
        db = storage.get_ohlcv(conn, sym, "4h", limit=5000)
        bad = []
        for _, row in db.iterrows():
            t = int(row["ts"].value // 10**6)
            tv = tmap.get(t)
            if tv is None:
                continue
            if abs(row["volume"] - tv["volume"]) > 1e-6 * max(1.0, tv["volume"]):
                bad.append(t)
        print(f"  {sym}: 库内 {len(db)} 根，可比 {sum(1 for _, r in db.iterrows() if int(r['ts'].value//10**6) in tmap)} 根，"
              f"需修 {len(bad)} 根")
        if bad and not args.dry_run:
            fix = truth[truth["ts"].isin([pd.Timestamp(t, unit="ms", tz="UTC") for t in bad])]
            storage.upsert_ohlcv(conn, sym, "4h", fix, "deribit")
            total_fixed += len(fix)
            print(f"    → 已覆盖 {len(fix)} 根")

    print("\n=== 2. premium 未收线坏行修复（重拉 premiumIndexKlines） ===")
    syms = storage.symbols(conn)
    for sym in syms:
        try:
            rows = [r for r in deriv_backfill(sym) if "premium" in r]
            if not args.dry_run and rows:
                storage.upsert_deriv(conn, sym, rows)
            print(f"  {sym}: 重写 {len(rows)} 行 premium（已裁未收线末根）")
            total_fixed += len(rows) if not args.dry_run else 0
        except Exception as e:  # noqa: BLE001
            print(f"  {sym}: 失败 {e}")

    conn.close()
    if args.dry_run:
        print("\n[dry-run] 未写库。")
    else:
        print(f"\n完成，共写回 {total_fixed} 行。")
        print("下一步：跑一轮 collector 触发 regime_audit_v5 全量重算。")


if __name__ == "__main__":
    main()
