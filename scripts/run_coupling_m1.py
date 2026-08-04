#!/usr/bin/env python3
"""M1 耦合计算层首轮 nowcast：三面板 pair 表 + 全局/块统计。

  .venv/bin/python scripts/run_coupling_m1.py

只读；输出 docs/COUPLING_M1_<UTC日期>.md。这是**测量报告**（nowcast），
不含状态机判定与显著性声明——那是 M2 校准协议之后的事。
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import coupling, storage  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    conn = storage.connect_ro()
    L = [f"# 耦合计算层 M1 首轮 nowcast · {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC",
         "", "测量报告（无状态机判定/无显著性声明——M2 校准协议后才有）。",
         f"纪律：成对时钟 EWMA 半衰期 {coupling.HL_FAST}/{coupling.HL_SLOW}，"
         f"联合样本 <{coupling.MIN_EFF} 记 INSUFFICIENT；|ρ_slow|<{coupling.ELIG_ABS_RHO} "
         "记 NOT_APPLICABLE（弱相关是不适用，不是脱耦）。", ""]
    try:
        for panel in ("all247", "usrth", "cross"):
            syms = coupling.panel_members(conn, panel)
            r = coupling.panel_returns(conn, syms, panel)
            if r.empty or len(r.columns) < 2:
                L += [f"## 面板 {panel}", "", "（数据不足）", ""]
                continue
            t = coupling.pair_table(r)
            counts = t["status"].value_counts().to_dict()
            L += [f"## 面板 {panel}（{len(r.columns)} 品种 × {len(r)} 行）", "",
                  f"pair 状态计数: {counts}", ""]
            elig = t[t.status == "ELIGIBLE"].copy()
            if len(elig):
                elig["absdz"] = elig["dz"].abs()
                top = elig.sort_values("absdz", ascending=False).head(10)
                L += ["|快慢背离 Top（|Δz|）| ρ_fast | ρ_slow | Δz | c | n |",
                      "|---|---|---|---|---|---|"]
                for _, x in top.iterrows():
                    L.append(f"| {x.a.split('-')[0]}×{x.b.split('-')[0]} "
                             f"| {x.rho_fast} | {x.rho_slow} | {x.dz} | {x.c} "
                             f"| {x.n_joint} |")
                L.append("")
            z, _ = coupling.ewma_vol_standardize(r)
            C = coupling.lw_shrink_corr(z)
            if C is not None:
                blocks = coupling.theme_blocks(list(C.columns))
                g = coupling.global_stats(C, blocks)
                L += [f"全局：市场模式 λ1/N={g['market_mode']} · 平均相关 {g['mean_corr']}"
                      f" · 离散度 {g['dispersion']} · 跨块能量占比 {g['cross_block_energy_share']}", ""]
                if g["blocks"]:
                    L += ["| 主题块 | n | 块内 | 块外 | D=内−外 |", "|---|---|---|---|---|"]
                    for b, v in sorted(g["blocks"].items()):
                        L.append(f"| {b} | {v['n']} | {v['intra']} | {v['extra']} | {v['D']} |")
                    L.append("")
            else:
                L += ["（LW 矩阵：共同支撑行数不足，跳过全局统计——如实 INSUFFICIENT）", ""]
    finally:
        conn.close()
    L += ["---", "*M1 测量层。ELIGIBLE 对的快慢背离仅是观察线索；"
          "任何'耦合/脱耦'判定须待 M2 状态机 + 20k 路 bootstrap 校准。*"]
    out = os.path.join(ROOT, "docs",
                       f"COUPLING_M1_{time.strftime('%Y%m%d', time.gmtime())}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("报告:", out)


if __name__ == "__main__":
    main()
