#!/usr/bin/env python3
"""M2 判定层首轮：pair FSM（资格对全历史）+ 块级三票 + 稳态零模型校准初跑。

  .venv/bin/python scripts/run_coupling_m2.py [--paths 300]

事件 append-only 写入 data/backtest_ledger.sqlite3 · coupling_events；
报告 docs/COUPLING_M2_<UTC日期>.md。当前阈值代 threshold_version=calibrated-1；
20k 路满额终验仍未执行（见 GAPS E6），本脚本给出平台实测底数。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import coupling, storage  # noqa: E402
from regime.coupling_fsm import (  # noqa: E402
    THRESHOLD_VERSION, block_votes, calibrate_false_alarms, pair_rho_series,
    run_pair_fsm,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ledger():
    conn = sqlite3.connect(os.path.join(storage.DATA_DIR, "backtest_ledger.sqlite3"),
                           timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS coupling_events("
        " panel TEXT, a TEXT, b TEXT, ts INTEGER, from_state TEXT, to_state TEXT,"
        " x REAL, d REAL, rho_fast REAL, rho_slow REAL, reason TEXT,"
        " threshold_version TEXT,"
        " PRIMARY KEY(panel, a, b, ts, to_state))")
    return conn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=300, help="校准路径数（正式验收 ≥20000）")
    args = ap.parse_args()

    conn = storage.connect_ro()
    L = [f"# 耦合判定层 M2 首轮 · {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC",
         "", f"阈值代 `{THRESHOLD_VERSION}`（先验）。事件已入账本 coupling_events；"
         "正式效力待满额校准（≥20k 路 + 注入式功效）。", ""]
    led = _ledger()
    try:
        panel = "all247"
        syms = coupling.panel_members(conn, panel)
        r = coupling.panel_returns(conn, syms, panel)
        z, _ = coupling.ewma_vol_standardize(r)
        t = coupling.pair_table(r)
        elig = t[t.status == "ELIGIBLE"]
        L += [f"## 面板 {panel}：资格对 {len(elig)} / {len(t)}", "",
              "| pair | 当前状态 | ρ_fast | ρ_slow | 全史事件数 | 最近事件 |",
              "|---|---|---|---|---|---|"]
        pair_states = {}
        n_events_total = 0
        for _, row in elig.iterrows():
            ser = pair_rho_series(z[row.a], z[row.b])
            states, events = run_pair_fsm(ser)
            cur = states.iloc[-1]
            pair_states[tuple(sorted((row.a, row.b)))] = cur
            with led:
                led.executemany(
                    "INSERT OR IGNORE INTO coupling_events VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?)",
                    [(panel, row.a, row.b, e["ts"], e["from_state"], e["to_state"],
                      e["x"], e["d"], e["rho_fast"], e["rho_slow"], e["reason"],
                      e["threshold_version"]) for e in events])
            n_events_total += len(events)
            last = events[-1] if events else None
            L.append(
                f"| {row.a.split('-')[0]}×{row.b.split('-')[0]} | **{cur}** "
                f"| {row.rho_fast} | {row.rho_slow} | {len(events)} "
                f"| {(last['reason'] + '→' + last['to_state']) if last else '—'} |")
        L += ["", f"事件入账本共 {n_events_total} 条（append-only，含全史）。", ""]

        blocks = coupling.theme_blocks(list(r.columns))
        bv = block_votes(z, blocks, pair_states)
        if len(bv):
            L += ["## 块级三票（nowcast 诊断）", "",
                  "| 块对 | n对 | 中位ρ_slow | 指数ρ_fast | coupled占比 | 票数 | 状态 |",
                  "|---|---|---|---|---|---|---|"]
            for _, x in bv.iterrows():
                L.append(f"| {x.block_a}×{x.block_b} | {x.n_pairs} "
                         f"| {x.median_rho_slow} | {x.index_rho_fast} "
                         f"| {x.coupled_share} | {x.votes}/3 | {x.state} |")
            L.append("")

        L += [f"## 稳态零模型校准（{args.paths} 路初跑）", "",
              "结构恒定的整行平稳 bootstrap 下，FSM 误入脱耦进程的路径占比"
              "（每 pair 全史一次机会口径）：", ""]
        pairs = [(x.a, x.b) for _, x in elig.iterrows()]
        cal = calibrate_false_alarms(z, pairs, n_paths=args.paths)
        L += ["| pair | P(误入 decoupling) | P(误入 decoupled) |", "|---|---|---|"]
        worst = 0.0
        for k, v in cal.items():
            if k == "_meta":
                continue
            worst = max(worst, v["p_false_decoupled"])
            a, b = k.split("|")
            L.append(f"| {a.split('-')[0]}×{b.split('-')[0]} "
                     f"| {v['p_false_decoupling']} | {v['p_false_decoupled']} |")
        L += ["", f"最坏 P(误入 decoupled)={worst}（C6 预算：全史口径下应≤5%/panel-年；"
              "本口径为全史 5000+ 根≈7 个 panel-月……换算与满额校准见 M2b）。", ""]
    finally:
        conn.close()
        led.close()
    L += ["---", "*M2 判定层首轮。状态只作诊断输出；进入五状态机决策路径须待"
          "≥30 个独立 inference block 且锁箱 ΔCRPS 为正（设计 §5-6）。*"]
    out = os.path.join(ROOT, "docs",
                       f"COUPLING_M2_{time.strftime('%Y%m%d', time.gmtime())}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("报告:", out)


if __name__ == "__main__":
    main()
