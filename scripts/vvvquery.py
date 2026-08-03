#!/usr/bin/env python3
"""只读查询接口：终端与 VVVhermes（只读沙箱）共用的数据查询入口。

  .venv/bin/python scripts/vvvquery.py overview            # 全品种横截面
  .venv/bin/python scripts/vvvquery.py panel QQQ-USDT      # 任意品种全量面板文本
  .venv/bin/python scripts/vvvquery.py states BTC-USDT 4h 20   # 最近 N 行状态+审计
  .venv/bin/python scripts/vvvquery.py sql "SELECT tf,COUNT(*) FROM ohlcv GROUP BY tf"

纪律：
- 全程 connect_ro（sqlite mode=ro），结构上不可能写库；
- sql 子命令只接受单条 SELECT（防御性拦截，双保险）；
- panel 复用 build_dashboard + render_context——与 Hermes 注入的 <panel>
  同一条代码路径，口径永远一致，不另造第二套语义。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import storage  # noqa: E402


def cmd_overview() -> int:
    from regime.agent import overview_brief
    out = overview_brief()
    print(out or "（无数据）")
    return 0


def _check_symbol(symbol: str) -> str:
    conn = storage.connect_ro()
    try:
        known = set(storage.symbols(conn))
    finally:
        conn.close()
    if symbol not in known:
        print(f"未知品种 {symbol!r}——库内现有：{', '.join(sorted(known))}",
              file=sys.stderr)
        sys.exit(2)
    return symbol


def cmd_panel(symbol: str) -> int:
    from dashboard import build_dashboard
    from regime.agent import render_context
    payload = build_dashboard(_check_symbol(symbol.upper()))
    print(render_context(payload))
    return 0


def cmd_states(symbol: str, tf: str, n: int) -> int:
    sym = _check_symbol(symbol.upper())
    if tf not in ("1h", "4h", "1d"):
        print("tf 必须是 1h/4h/1d", file=sys.stderr)
        return 2
    conn = storage.connect_ro()
    try:
        rows = conn.execute(
            "SELECT ts, state, raw_state, confidence, features FROM regime_history"
            " WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?", (sym, tf, n),
        ).fetchall()
    finally:
        conn.close()
    print("ts(UTC)\tstate\traw\tconf\tdir\ter%\tatr%\tbbw%\twarmup")
    for ts, st, raw, conf, feats in reversed(rows):
        try:
            f = json.loads(feats) if feats else {}
        except Exception:  # noqa: BLE001
            f = {}
        t = time.strftime("%m-%d %H:%M", time.gmtime(ts / 1000))
        print(f"{t}\t{st}\t{raw or st}\t{conf}\t{f.get('dir')}\t{f.get('er_rank')}"
              f"\t{f.get('atr_rank')}\t{f.get('bbw_rank')}\t{f.get('warmup')}")
    return 0


def cmd_sql(query: str) -> int:
    q = query.strip().rstrip(";").strip()
    if ";" in q or not q.lower().startswith("select"):
        print("只允许单条 SELECT（连接本身也是 mode=ro，写入结构上不可能）",
              file=sys.stderr)
        return 2
    conn = storage.connect_ro()
    try:
        cur = conn.execute(q)
        cols = [c[0] for c in cur.description or []]
        print("\t".join(cols))
        for i, row in enumerate(cur):
            if i >= 500:
                print(f"…（截断于 500 行）", file=sys.stderr)
                break
            print("\t".join(str(v) for v in row))
    except Exception as e:  # noqa: BLE001
        print(f"SQL 错误: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="VVV 只读查询接口")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("overview")
    p = sub.add_parser("panel"); p.add_argument("symbol")
    p = sub.add_parser("states"); p.add_argument("symbol"); p.add_argument("tf")
    p.add_argument("n", nargs="?", type=int, default=20)
    p = sub.add_parser("sql"); p.add_argument("query")
    a = ap.parse_args()
    if a.cmd == "overview":
        sys.exit(cmd_overview())
    if a.cmd == "panel":
        sys.exit(cmd_panel(a.symbol))
    if a.cmd == "states":
        sys.exit(cmd_states(a.symbol, a.tf, a.n))
    if a.cmd == "sql":
        sys.exit(cmd_sql(a.query))


if __name__ == "__main__":
    main()
