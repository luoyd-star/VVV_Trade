#!/usr/bin/env python3
"""个股 IV 回填/增量（moomoo 口径）→ stock_vol 表。

    .venv/bin/python scripts/backfill_moomoo_iv.py          # 增量（自库内最后一日起）
    .venv/bin/python scripts/backfill_moomoo_iv.py --full   # 全量（自 2023-06-26 起）

前置：本机 OpenD 已启动并登录。31 个标的全量约 3-4 分钟（限频自律 0.6s/次）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from regime import moomoo_iv, storage  # noqa: E402


def us_symbols() -> list[str]:
    cfg = json.loads((ROOT / "instruments.json").read_text())
    return sorted(
        s for s, v in cfg.items()
        if isinstance(v, dict) and v.get("class") == "us_stock_perp"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="从数据边界全量重拉")
    ap.add_argument("--symbol", help="只处理单个品种（调试用）")
    args = ap.parse_args()

    if not moomoo_iv.opend_alive():
        print(f"✗ OpenD 未在 {moomoo_iv.HOST}:{moomoo_iv.PORT} 监听——请先启动并登录。")
        return 1

    symbols = [args.symbol] if args.symbol else us_symbols()
    today = date.today()
    conn = storage.connect()
    ctx = moomoo_iv.open_ctx()
    total, failed = 0, []
    try:
        for i, sym in enumerate(symbols, 1):
            have = storage.get_stock_vol(conn, sym, moomoo_iv.SOURCE)
            if args.full or have.empty:
                begin = moomoo_iv.DATA_FLOOR
            else:
                # 自库内最后一日重叠一天起拉——覆盖当日可能的盘中未定值
                last = datetime.fromtimestamp(
                    int(have["ts"].iloc[-1]) / 1000, tz=timezone.utc
                ).date()
                begin = (last - timedelta(days=1)).isoformat()
                if last >= today:
                    print(f"  [{i:>2}/{len(symbols)}] {sym:<11} 已最新（{last}），跳过")
                    continue
            try:
                rows = moomoo_iv.fetch_history(ctx, sym, begin, today.isoformat())
            except RuntimeError as e:
                print(f"  [{i:>2}/{len(symbols)}] {sym:<11} ✗ {str(e)[:70]}")
                failed.append(sym)
                continue
            if rows:
                storage.upsert_stock_vol(conn, sym, moomoo_iv.SOURCE, rows)
                total += len(rows)
            d0 = datetime.fromtimestamp(rows[0]["ts"] / 1000, tz=timezone.utc).date() if rows else None
            d1 = datetime.fromtimestamp(rows[-1]["ts"] / 1000, tz=timezone.utc).date() if rows else None
            print(f"  [{i:>2}/{len(symbols)}] {sym:<11} {len(rows):>4} 行"
                  + (f"  {d0} → {d1}" if rows else "  （无数据）"))

        print(f"\n写入 {total} 行；失败 {len(failed)}" + (f" {failed}" if failed else ""))
        cov = storage.stock_vol_coverage(conn, moomoo_iv.SOURCE)
        if len(cov):
            cov["days"] = ((cov.t1 - cov.t0) / 86_400_000).round(0).astype(int)
            print(f"\n覆盖：{len(cov)} 个品种，中位 {int(cov.n.median())} 行，"
                  f"最长 {int(cov.days.max())} 天")
            short = cov[cov.n < 250]
            if len(short):
                print("样本不足 250 行（分位窗需自适应）：")
                for _, r in short.iterrows():
                    print(f"  {r.symbol:<11} {int(r.n):>4} 行")
        return 0 if not failed else 2
    finally:
        ctx.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
