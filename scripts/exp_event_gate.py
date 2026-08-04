#!/usr/bin/env python3
"""A1 事件门槛的预登记复评（每周复跑挂载）。

复算 docs/A1_EVENT_GATE_20260805.md 的核心诊断：事件窗内 vs 窗外的
squeeze→趋势确认转换 10 根失败率。**预登记判据**：窗内样本 ≥100 时，
若失败率差（窗内−窗外）≤ 0，v4 应撤销门槛；显著为正则维持。

口径与 v3.1 一致：事件窗 = ET 日历日差 ∈ [0,10]。本脚本只读、纯诊断，
不受锁箱约束（它评估确认层行为，不是预测技能）。
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import storage  # noqa: E402


def main() -> int:
    conn = storage.connect_ro()
    cfg = json.load(open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "instruments.json")))
    us = [s for s, v in cfg.items()
          if isinstance(v, dict) and v.get("class") == "us_stock_perp"]
    rows = []
    for tf in ("1h", "4h", "1d"):
        for sym in us:
            df = pd.read_sql_query(
                "SELECT ts,state FROM regime_history WHERE symbol=? AND tf=? ORDER BY ts",
                conn, params=(sym, tf))
            if len(df) < 60:
                continue
            win = storage.earnings_event_windows(conn, sym, df["ts"].tolist())
            st = df["state"].values
            for i in range(1, len(df)):
                if st[i - 1] == "squeeze" and st[i] in ("trend_up", "trend_down"):
                    fut = st[i + 1:i + 11]
                    if len(fut) < 10:
                        continue
                    rows.append((tf, bool(win[i]), bool((fut != st[i]).any())))
    conn.close()
    r = pd.DataFrame(rows, columns=["tf", "win", "fail"])
    print("A1 事件门槛复评（v3.1 口径：ET 日差 ∈ [0,10]）")
    print(f"{'tf':<5}{'组':<8}{'n':>5}{'10根内失败率':>12}")
    verdicts = []
    for tf in ("1h", "4h", "1d"):
        g = r[r.tf == tf]
        for w in (True, False):
            gg = g[g.win == w]
            if len(gg):
                print(f"{tf:<5}{'窗内' if w else '窗外':<8}{len(gg):>5}{gg.fail.mean():>12.1%}")
        gi, go = g[g.win], g[~g.win]
        if len(gi) and len(go):
            verdicts.append((tf, len(gi), gi.fail.mean() - go.fail.mean()))
    n_in = int(r[r.win].shape[0])
    print(f"\n窗内总样本 {n_in}（预登记判据阈值 100）")
    for tf, n, d in verdicts:
        print(f"  {tf}: 失败率差（窗内−窗外）= {d:+.1%}  n_窗内={n}")
    if n_in >= 100:
        overall = (r[r.win].fail.mean() - r[~r.win].fail.mean())
        print(f"\n★ 样本已达阈值。总失败率差 {overall:+.1%} — "
              + ("维持门槛" if overall > 0 else "**建议 v4 撤销门槛**"))
    else:
        print(f"\n样本未达阈值（{n_in}/100），继续积累。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
