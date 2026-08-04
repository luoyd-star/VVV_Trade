#!/usr/bin/env python3
"""calibrated-1 网格搜索：在校准平台上为 FSM 参数寻找有推导链的取值。

  .venv/bin/python scripts/run_coupling_grid.py [--fa-paths 1000] [--pw-paths 50]

网格（27 组合）：
  elig_enter ∈ {0.35, 0.40, 0.45}（exit = enter − 0.05）
  d_enter_std ∈ {1.5, 2.0, 2.5}（strong = std − 0.5）
  delta_floor_std ∈ {0.25, 0.29, 0.35}（strong = std − 0.04）
每组合：误报（fa_paths 路稳态零模型，11 资格对）+ 功效（pw_paths 路，
Δρ=0.29，代表对 BTC×ETH 强 / ETH×XAU 边缘）。
双目标：约束 max 年化误报 ≤5%，目标最大化检出率、最小化中位延迟。
**逐组合打印进度**（M2b 4 小时黑盒的教训）。产出 docs/COUPLING_CALIBRATED1_<日期>.md。
"""
from __future__ import annotations

import argparse
import itertools
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import coupling, storage  # noqa: E402
from regime.coupling_fsm import (  # noqa: E402
    FSMParams, calibrate_false_alarms, pair_rho_series, run_pair_fsm,
    stationary_bootstrap_rows,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_ELIG = (0.35, 0.40, 0.45)
GRID_DENTER = (1.5, 2.0, 2.5)
GRID_DELTA = (0.25, 0.29, 0.35)
POWER_DELTA = 0.29
REP_PAIRS_IDX = 2  # 代表对数量：1 强 + 1 边缘


def make_params(elig, denter, delta) -> FSMParams:
    return FSMParams(
        elig_enter=elig, elig_exit=round(elig - 0.05, 2),
        d_enter_std=denter, d_enter_strong=max(0.5, denter - 0.5),
        delta_floor_std=delta, delta_floor_strong=max(0.15, delta - 0.04),
    )


def power_one(z, pair, params, n_paths, seed=71):
    """单对单档功效：注入 Δρ=0.29，检出=decoupling 或 eligibility_lost。"""
    import pandas as pd
    rng = np.random.default_rng(seed)
    a, b = pair
    sub = z[[a, b]].dropna()
    base = pair_rho_series(sub[a], sub[b])
    rho0 = float(base["rho_slow"].iloc[-1])
    target = rho0 - np.sign(rho0) * POWER_DELTA
    c = np.clip(target / rho0 if abs(rho0) > 1e-6 else 0.0, -1, 1)
    det, delays = 0, []
    done = 0
    for path in stationary_bootstrap_rows(sub, 24, n_paths,
                                          seed=int(rng.integers(1e9))):
        n = len(path)
        t0 = n // 2
        pb = path[b].to_numpy().copy()
        seg = pb[t0:].copy()
        rng.shuffle(seg)
        pb[t0:] = c * pb[t0:] + np.sqrt(max(0.0, 1 - c * c)) * seg
        ser = pair_rho_series(path[a], pd.Series(pb, index=path.index))
        _, events = run_pair_fsm(ser, params)
        pos = list(ser.index)
        t0_ts = int(path.index[t0])
        hits = [e for e in events
                if e["to_state"] in ("decoupling", "NOT_APPLICABLE")
                and e["ts"] >= t0_ts]
        done += 1
        if hits:
            det += 1
            k0 = min(range(len(pos)), key=lambda k: abs(pos[k] - t0_ts))
            delays.append(pos.index(hits[0]["ts"]) - k0)
    return {"detect": det / max(1, done),
            "med_delay": int(np.median(delays)) if delays else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fa-paths", type=int, default=1000)
    ap.add_argument("--pw-paths", type=int, default=50)
    args = ap.parse_args()
    t0 = time.time()

    conn = storage.connect_ro()
    try:
        syms = coupling.panel_members(conn, "all247")
        r = coupling.panel_returns(conn, syms, "all247")
        z, _ = coupling.ewma_vol_standardize(r)
        t = coupling.pair_table(r)
        elig = t[t.status == "ELIGIBLE"]
        pairs = [(x.a, x.b) for _, x in elig.iterrows()]
    finally:
        conn.close()
    years = len(z) / 8760.0
    strong = next(((a, b) for a, b in pairs if "BTC" in a and "ETH" in b), pairs[0])
    edge = next(((a, b) for a, b in pairs if "XAU" in (a + b) and "ETH" in (a + b)),
                pairs[-1])
    reps = [strong, edge]

    rows = []
    combos = list(itertools.product(GRID_ELIG, GRID_DENTER, GRID_DELTA))
    for idx, (eg, de, dl) in enumerate(combos, 1):
        tc = time.time()
        params = make_params(eg, de, dl)
        cal = calibrate_false_alarms(z, pairs, n_paths=args.fa_paths,
                                     params=params)
        worst = max((v["p_false_decoupled"] for k, v in cal.items()
                     if k != "_meta"), default=0.0)
        worst_yr = 1 - (1 - worst) ** (1 / max(years, 1e-6))
        pw = {name: power_one(z, pr, params, args.pw_paths, seed=97 + idx)
              for name, pr in (("strong", strong), ("edge", edge))}
        rows.append({
            "elig": eg, "d_enter": de, "delta": dl,
            "worst_fa_yr": round(worst_yr, 4),
            "pass_budget": worst_yr <= 0.05,
            "det_strong": round(pw["strong"]["detect"], 3),
            "delay_strong": pw["strong"]["med_delay"],
            "det_edge": round(pw["edge"]["detect"], 3),
            "delay_edge": pw["edge"]["med_delay"],
        })
        print(f"[{idx}/{len(combos)}] elig={eg} d={de} δ={dl} → "
              f"最坏年化误报 {worst_yr:.3f} | 强对检出 {pw['strong']['detect']:.2f}"
              f"@{pw['strong']['med_delay']} | 边缘检出 {pw['edge']['detect']:.2f}"
              f" | 累计 {(time.time()-t0)/60:.0f}min", flush=True)

    ok = [x for x in rows if x["pass_budget"]]
    ok.sort(key=lambda x: (-(x["det_strong"] + x["det_edge"]),
                           (x["delay_strong"] or 9999)))
    L = [f"# calibrated-1 网格搜索 · {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC",
         "", f"误报 {args.fa_paths} 路/组合 · 功效 {args.pw_paths} 路 Δρ={POWER_DELTA}"
         f" · 代表对 强={strong[0].split('-')[0]}×{strong[1].split('-')[0]}"
         f" 边缘={edge[0].split('-')[0]}×{edge[1].split('-')[0]}"
         f" · 耗时 {(time.time()-t0)/60:.0f} 分钟", "",
         "| elig | d_enter | δ地板 | 最坏年化误报 | 预算 | 强对检出@延迟 | 边缘检出@延迟 |",
         "|---|---|---|---|---|---|---|"]
    for x in sorted(rows, key=lambda v: v["worst_fa_yr"]):
        L.append(f"| {x['elig']} | {x['d_enter']} | {x['delta']} | {x['worst_fa_yr']} "
                 f"| {'✓' if x['pass_budget'] else '✗'} "
                 f"| {x['det_strong']}@{x['delay_strong']} "
                 f"| {x['det_edge']}@{x['delay_edge']} |")
    L += ["", "## 预算内按检出排序（calibrated-1 候选）", ""]
    if ok:
        for x in ok[:5]:
            L.append(f"- elig={x['elig']} d_enter={x['d_enter']} δ={x['delta']}"
                     f" → 误报 {x['worst_fa_yr']}，强 {x['det_strong']}@{x['delay_strong']}，"
                     f"边缘 {x['det_edge']}@{x['delay_edge']}")
        L += ["", f"**推荐 calibrated-1**：{ok[0]}（人工复核后升版）"]
    else:
        L.append("- 无组合满足预算——需扩网格或改入场锚定设计（见 prior-2 已知的入场选择效应）")
    L += ["", "---", "*每组合结果可由本脚本参数复现；升版须人工复核 + 满额（20k 路）终验。*"]
    out = os.path.join(ROOT, "docs",
                       f"COUPLING_CALIBRATED1_{time.strftime('%Y%m%d', time.gmtime())}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("报告:", out)


if __name__ == "__main__":
    main()
