#!/usr/bin/env python3
"""梯队一离线实验（E1-E4）入口：跑全品种、聚合渲染、写 trial ledger。

  .venv/bin/python scripts/run_tier1_experiments.py

只读连接；结果进 data/backtest_ledger.sqlite3 与
docs/EXPERIMENTS_TIER1_<UTC日期>_<exp_id>.md。
"""
from __future__ import annotations

import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import backtest, experiments, storage  # noqa: E402
from run_backtest_p0 import _code_digest  # noqa: E402  同一份代码摘要逻辑

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 4) if vals else None


def render(payload, exp_id, digest) -> str:
    m = payload["meta"]
    ok = [r for r in payload["results"] if "n_bars" in r]
    L = [
        f"# 梯队一离线实验报告（E1-E4） · {time.strftime('%Y-%m-%d', time.gmtime())}",
        "",
        f"experiment_id `{exp_id}` · code `{digest}` · 版本桶 "
        f"({m['rules_version']},{m['audit_version']}) · 锁箱前数据 · "
        f"成功序列 {len(ok)}/{len(payload['results'])}",
        "",
        "全部离线只读；生产规则一行未改。**这些是筛选实验，不是裁决**——"
        "任何「进规则」决定都要走 P0 框架的 proper score + 升版流程。",
        "",
        "## E1 估计器赛马（vs 现行 SMA-ATR14 的 250 根分位）",
        "",
        "闸门（预研主题1）：Spearman ≈1 且阈值翻转率极低 ⇒ 换代无意义。",
        "",
        "| tf | 估计器 | Spearman中位 | squeeze门翻转中位 | high_vol门翻转中位 |",
        "|---|---|---|---|---|",
    ]
    for tf in ("1h", "4h", "1d"):
        rows = [r for r in ok if r["tf"] == tf and "e1" in r]
        if not rows:
            continue
        for est in ("rma_atr", "parkinson", "gk", "rs"):
            L.append(
                f"| {tf} | {est} "
                f"| {_med([r['e1'].get(est, {}).get('spearman') for r in rows])} "
                f"| {_med([r['e1'].get(est, {}).get('flip_squeeze_gate') for r in rows])} "
                f"| {_med([r['e1'].get(est, {}).get('flip_highvol_gate') for r in rows])} |")
    L += ["", "## E2 squeeze 事件研究（阈值网格 → 未来波幅扩张倍数中位，跨序列中位）",
          "", "expansion_vs_base >1 = 压缩事件后的波幅扩张超过无条件基线；"
          "dir_up 偏离 0.5 的程度 = 方向可测性（预研预期：≈0.5 不可测）。", ""]
    for tf in ("1h", "4h", "1d"):
        rows = [r for r in ok if r["tf"] == tf and "e2" in r]
        if not rows:
            continue
        gates = sorted({k for r in rows for k in r["e2"]["grid"]})
        L += [f"### {tf}", "", "| 门槛 | 有效序列 | 事件数中位 | 扩张/基线中位 | dir_up中位 |",
              "|---|---|---|---|---|"]
        for g in gates:
            cells = [r["e2"]["grid"][g] for r in rows if g in r["e2"]["grid"]]
            good = [c for c in cells if not c.get("insufficient")]
            L.append(
                f"| {g} | {len(good)}/{len(cells)} "
                f"| {_med([c['n_events'] for c in good])} "
                f"| {_med([c.get('expansion_vs_base') for c in good])} "
                f"| {_med([c.get('dir_up_share') for c in good])} |")
        L.append("")
    L += ["## E3 去季节化稳健性（美股永续 1h：48 桶中位数离散度 CV，低=干净）", "",
          "| symbol | 未去季节化 | 现行均值因子 | 中位数因子 |", "|---|---|---|---|"]
    for r in ok:
        if "e3" not in r:
            continue
        e = r["e3"]
        L.append(f"| {r['symbol']} | {e['raw_cv']} | {e['mean_factor_cv']} "
                 f"| {e['median_factor_cv']} |")
    L += ["", "## E4 迟滞网格（raw_state 离线重放；跟踪偏差为描述性代理，"
          "正式裁决走 P0 CRPS）", ""]
    for tf in ("1h", "4h", "1d"):
        rows = [r for r in ok if r["tf"] == tf and "e4" in r]
        if not rows:
            continue
        variants = list(rows[0]["e4"].keys())
        L += [f"### {tf}（{len(rows)} 序列中位）", "",
              "| 方案 | episodes | 平均驻留(根) | 跟踪偏差 |", "|---|---|---|---|"]
        for v in variants:
            L.append(
                f"| {v} | {_med([r['e4'][v]['episodes'] for r in rows])} "
                f"| {_med([r['e4'][v]['avg_stay_bars'] for r in rows])} "
                f"| {_med([r['e4'][v]['tracking_dev'] for r in rows])} |")
        L.append("")
    bad = [r for r in payload["results"] if "n_bars" not in r]
    if bad:
        L += ["## 跳过/错误", ""] + [
            f"- {r['symbol']} {r['tf']}: {r.get('error') or r.get('insufficient')}"
            for r in bad]
    L += ["", "---", "*协议：docs/PRERESEARCH_VOL_20260803.md 梯队一（E1-E4）。"
          "锁箱（≥2026-09-01 收线）数据不可见。*"]
    return "\n".join(L) + "\n"


def main() -> None:
    conn = storage.connect_ro()
    try:
        syms = sorted(storage.symbols(conn))
        payload = experiments.run_tier1(conn, syms)
    finally:
        conn.close()
    payload["meta"]["experiment_family"] = "tier1_e1e4"
    payload["meta"]["data_cutoff_ms"] = max(
        (v[2] for v in payload["meta"]["data_manifest"].values()), default=0)
    digest = _code_digest()
    exp_id = backtest.record_experiment(payload, digest, note="梯队一 E1-E4 首轮")
    out = os.path.join(
        ROOT, "docs",
        f"EXPERIMENTS_TIER1_{time.strftime('%Y%m%d', time.gmtime())}_{exp_id}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(payload, exp_id, digest))
    ok = [r for r in payload["results"] if "n_bars" in r]
    print(f"experiment_id={exp_id} 成功序列={len(ok)} 报告: {out}")
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
