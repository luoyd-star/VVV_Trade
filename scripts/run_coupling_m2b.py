#!/usr/bin/env python3
"""M2b 满额校准：20k 路稳态误报 + 注入式功效（设计 §5 完整协议）。

  .venv/bin/python scripts/run_coupling_m2b.py [--paths 20000] [--power-paths 500]

产出 docs/COUPLING_M2B_<UTC日期>.md：
- 误报：结构恒定零模型下逐 pair P(误入 decoupling/decoupled)，按 panel-年换算；
- 功效：在路径中段注入 Δρ∈{0.15,0.29,0.40} 的相关衰减（混洗稀释法，保边际
  分布），测检出率与中位延迟（C6 预算：可检档 P(延迟≤0.25·T_F)≥80%）。
"""
from __future__ import annotations

import argparse
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
T_F = 404  # 快线有效样本数


def power_test(z, pairs, deltas=(0.15, 0.29, 0.40), n_paths=500,
               mean_block=24, seed=29):
    """注入式功效：路径中段把 b 腿与自身混洗版线性混合，稀释相关 Δρ。

    b' = c·b + √(1-c²)·b_shuffled，ρ_new ≈ c·ρ_old（边际分布不变）。
    记录：检出率（t0 后进入 decoupling）、中位延迟（bar）、
    延迟≤0.25·T_F 的占比。
    """
    rng = np.random.default_rng(seed)
    out = {}
    for a, b in pairs:
        sub = z[[a, b]].dropna()
        base = pair_rho_series(sub[a], sub[b])
        rho0 = float(base["rho_slow"].iloc[-1])
        for delta in deltas:
            target = rho0 - np.sign(rho0) * delta
            c = np.clip(target / rho0 if abs(rho0) > 1e-6 else 0.0, -1, 1)
            det, delays, fast_ok = 0, [], 0
            done = 0
            for path in stationary_bootstrap_rows(sub, mean_block, n_paths,
                                                  seed=int(rng.integers(1e9))):
                n = len(path)
                t0 = n // 2
                pb = path[b].to_numpy().copy()
                seg = pb[t0:].copy()
                rng.shuffle(seg)
                pb[t0:] = c * pb[t0:] + np.sqrt(max(0.0, 1 - c * c)) * seg
                import pandas as pd
                pa = path[a]
                ser = pair_rho_series(pa, pd.Series(pb, index=path.index))
                _, events = run_pair_fsm(ser)
                # 检出 = 走任一扇门：decoupling（关系变弱）或 eligibility_lost
                # （关系死亡）——M2b 首轮只数了前者，中强度对的断裂多走后者
                hits = [e for e in events
                        if e["to_state"] in ("decoupling", "NOT_APPLICABLE")
                        and e["reason"] != "init_na"
                        and e["ts"] >= int(path.index[t0])]
                done += 1
                if hits:
                    det += 1
                    delay = (hits[0]["ts"] - int(path.index[t0])) // 3_600_000 \
                        if path.index[t0] > 1e12 else \
                        list(ser.index).index(hits[0]["ts"]) - t0
                    # 统一按 bar 数：用序列位置差
                    pos = list(ser.index)
                    delay = pos.index(hits[0]["ts"]) - min(
                        range(len(pos)), key=lambda k: abs(pos[k] - int(path.index[t0])))
                    delays.append(delay)
                    if delay <= 0.25 * T_F:
                        fast_ok += 1
            out[f"{a.split('-')[0]}×{b.split('-')[0]}|Δρ={delta}"] = {
                "rho0": round(rho0, 3),
                "detect_rate": round(det / max(1, done), 3),
                "median_delay_bars": (int(np.median(delays)) if delays else None),
                "p_delay_le_0.25TF": round(fast_ok / max(1, done), 3),
            }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", type=int, default=20000)
    ap.add_argument("--power-paths", type=int, default=500)
    args = ap.parse_args()
    t_start = time.time()

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

    # panel-年换算：全史 bar 数 → 年数（24/7 小时钟 ≈ 8760 bar/年）
    years = len(z) / 8760.0
    L = [f"# 耦合校准 M2b（满额） · {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC",
         "", f"面板 all247 · {len(pairs)} 资格对 · 全史 {len(z)} bar ≈ {years:.2f} panel-年",
         f"误报路径 {args.paths} · 功效路径 {args.power_paths}/档", ""]

    cal = calibrate_false_alarms(z, pairs, n_paths=args.paths)
    L += ["## 误报（结构恒定零模型）", "",
          "| pair | P(误入 decoupling)/全史 | P(误入 decoupled)/全史 | 换算/panel-年 | 预算≤5% |",
          "|---|---|---|---|---|"]
    for k, v in cal.items():
        if k == "_meta":
            continue
        a, b = k.split("|")
        per_year = 1 - (1 - v["p_false_decoupled"]) ** (1 / max(years, 1e-6))
        ok = "✓" if per_year <= 0.05 else "**✗**"
        L.append(f"| {a.split('-')[0]}×{b.split('-')[0]} | {v['p_false_decoupling']} "
                 f"| {v['p_false_decoupled']} | {per_year:.3f} | {ok} |")

    reps = pairs[:2] + [p for p in pairs if "XAU" in p[0] or "XAU" in p[1]][:2]
    reps = list(dict.fromkeys(reps))
    L += ["", f"## 功效（注入式，代表对 {len(reps)} 个）", "",
          "| pair|Δρ | ρ0 | 检出率 | 中位延迟(bar) | P(延迟≤101bar) |",
          "|---|---|---|---|---|"]
    pw = power_test(z, reps, n_paths=args.power_paths)
    for k, v in pw.items():
        L.append(f"| {k} | {v['rho0']} | {v['detect_rate']} "
                 f"| {v['median_delay_bars']} | {v['p_delay_le_0.25TF']} |")

    L += ["", f"耗时 {(time.time()-t_start)/60:.0f} 分钟。",
          "", "## 结论模板（人工復核后定稿）",
          "- 误报超预算的 pair → prior-2 收紧（提高该 pair δ*/确认根数或资格线）；",
          "- 可检档（Δρ≥0.29）检出率与延迟达标情况 → 决定块级报警是否解锁。", "---"]
    out = os.path.join(ROOT, "docs",
                       f"COUPLING_M2B_{time.strftime('%Y%m%d', time.gmtime())}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("报告:", out)


if __name__ == "__main__":
    main()
