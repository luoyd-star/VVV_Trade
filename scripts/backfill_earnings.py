#!/usr/bin/env python3
"""财报日历回填 → earnings 表。

用途不是预测财报，而是给 IV 分位标注事件邻近度——实测 NVDA 财报事前峰→事后谷
崩塌 38%、占分位分母 9.9%（docs/EARNINGS_IV_CONTAMINATION_20260804.md）。

    .venv/bin/python scripts/backfill_earnings.py               # 与 IV 同区间 + 未来 90 天
    .venv/bin/python scripts/backfill_earnings.py --forward     # 只补未来（日常增量）

接口单次窗口 ≤7 天，全区间约 165 段、每段 ~0.6s，全量约 2 分钟。
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
    return sorted(s for s, v in cfg.items()
                  if isinstance(v, dict) and v.get("class") == "us_stock_perp")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forward", action="store_true", help="只补未来 90 天（日常增量）")
    args = ap.parse_args()

    if not moomoo_iv.opend_alive():
        print(f"✗ OpenD 未在 {moomoo_iv.HOST}:{moomoo_iv.PORT} 监听")
        return 1

    today = date.today()
    begin = today.isoformat() if args.forward else moomoo_iv.DATA_FLOOR
    end = (today + timedelta(days=90)).isoformat()
    symbols = us_symbols()
    print(f"财报日历 {begin} → {end}，{len(symbols)} 品种（7 天/段）")

    ctx = moomoo_iv.open_ctx()
    conn = storage.connect()
    try:
        rows = moomoo_iv.fetch_earnings(ctx, symbols, begin, end)
        if rows:
            storage.upsert_earnings(conn, moomoo_iv.SOURCE, rows)
        print(f"\n写入 {len(rows)} 条")
        from collections import Counter
        c = Counter(r["symbol"] for r in rows)
        if c:
            lo = min(c.values())
            print(f"每品种 {lo}~{max(c.values())} 条，中位 "
                  f"{sorted(c.values())[len(c)//2]}")
            miss = [s for s in symbols if s not in c]
            if miss:
                print(f"无财报记录（ETF 或新股属正常）：{[m.split('-')[0] for m in miss]}")
        # 邻近度抽查
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        near = []
        for s in symbols:
            p = storage.earnings_proximity(conn, s, now)
            if p:
                near.append((s.split("-")[0], p["days"]))
        if near:
            print("\n未来/刚过 10 日内有财报：")
            for sym, d in sorted(near, key=lambda x: abs(x[1])):
                print(f"  {sym:<6} {'还有' if d > 0 else '已过'} {abs(d)} 天")
        return 0
    finally:
        ctx.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
