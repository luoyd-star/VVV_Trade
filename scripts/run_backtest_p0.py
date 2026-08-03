#!/usr/bin/env python3
"""P0 验收实验入口：跑状态条件技能评估，写 ledger + Markdown 报告。

  .venv/bin/python scripts/run_backtest_p0.py            # 全品种全周期
  .venv/bin/python scripts/run_backtest_p0.py -s BTC-USDT -o /tmp/r.md

只读连接，绝不写 market.db；结果进 data/backtest_ledger.sqlite3（追加）
与 docs/BACKTEST_P0_<UTC日期>_<exp_id>.md（含 id，避免同日实验互相覆盖）。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import backtest, storage  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code_digest() -> str:
    """HEAD + 实际工作区内容摘要。同一 HEAD 上不同的未提交改动必须得到
    不同摘要——否则 ledger 会把 B 版结果配上 A 版代码的 id（评审 p3）。"""
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True, cwd=ROOT).strip()
        diff = subprocess.check_output(
            ["git", "diff", "HEAD"], text=True, cwd=ROOT)
        untracked = subprocess.check_output(
            ["git", "ls-files", "--others", "--exclude-standard"],
            text=True, cwd=ROOT)
        extra = diff + untracked
        if extra.strip():
            return f"{head}+dirty.{hashlib.sha256(extra.encode()).hexdigest()[:8]}"
        return head
    except Exception:  # noqa: BLE001
        return "unknown"


def _fmt_skill(s) -> str:
    if s is None:
        return "不可估"
    if s["n_eval"] < backtest.MIN_EVAL:
        return f"{s['skill']:+.3f} n={s['n_eval']}（只描述）"
    ci = s.get("ci95")
    ci_txt = f" [{ci[0]:+.3f},{ci[1]:+.3f}]" if ci else ""
    return f"{s['skill']:+.3f}{ci_txt} n={s['n_eval']}/ep{s['n_episodes_eval']}"


def _fmt_band(h) -> str:
    b, vs = h.get("surrogate"), h.get("vs_band")
    if b is None:
        return "—"
    tag = {"above": " ↑出带", "below": " ↓出带", "inside": ""}.get(vs, "")
    return f"[{b['lo']:+.3f},{b['hi']:+.3f}]×{b['n_shifts']}{tag}"


def render_md(payload, exp_id, code_digest) -> str:
    m = payload["meta"]
    lines = [
        f"# P0 验收实验报告 · {time.strftime('%Y-%m-%d', time.gmtime())}",
        "",
        f"experiment_id `{exp_id}` · code `{code_digest}` · 协议 p{m['protocol']}"
        f" · 版本桶 ({m['rules_version']},{m['audit_version']})"
        f" · 锁箱 {m['lockbox_start']} 起（前向累积）",
        "",
        "**问题**：t 收线时的确认态对 t+1..t+H 的已实现波动是否有超越无条件基线的"
        "信息（CRPS 技能，>0 = 优于无条件）。",
        "",
        "**两个独立的轴，禁止互译**（协议 p3）：技能符号 = 是否优于无条件基线；"
        "surrogate 带位置 = 相对环移参照的高低。带是**描述性参照**——状态由 ~250 根"
        "回看窗生成而序列仅 ~300 根，环移后的状态见过未来，构不成干净零假设，"
        "且未做多重检验校正。因此**出带只是探索性观察，不是显著性结论**。",
        "",
        "| symbol | tf | 行数(warmup) | H | 全部行技能 | surrogate带 | 成熟行技能 | ep |",
        "|---|---|---|---|---|---|---|---|",
    ]
    watch, insuff = [], []
    for r in payload["results"]:
        if "error" in r:
            insuff.append(f"- {r['symbol']} {r['tf']}: 错误 {r['error']}")
            continue
        if "insufficient" in r:
            insuff.append(f"- {r['symbol']} {r['tf']}: 仅 {r['insufficient']} 行，跳过")
            continue
        ep = r["episodes"]["n_episodes"]
        for hk, h in r["horizons"].items():
            lines.append(
                f"| {r['symbol']} | {r['tf']} | {r['n_rows']}({r['n_warmup']}) | {hk} "
                f"| {_fmt_skill(h['strata']['all'])} | {_fmt_band(h)} "
                f"| {_fmt_skill(h['strata']['mature'])} | {ep} |")
            s = h["strata"]["all"]
            if s and h.get("vs_band") in ("above", "below") \
                    and s["n_eval"] >= backtest.MIN_EVAL \
                    and s["n_episodes_eval"] >= backtest.MIN_EPISODES:
                watch.append((r["symbol"], r["tf"], hk, s["skill"], h["vs_band"],
                              s["n_episodes_eval"]))
    lines += ["", "## 探索性观察清单（n≥30、评估覆盖 ep≥10、出 surrogate 带；"
              "**非结论**，供后续数据验证）", ""]
    if watch:
        for sym, tf, hk, sk, vs, nep in sorted(watch, key=lambda x: -abs(x[3])):
            axis1 = "优于" if sk > 0 else "劣于"
            axis2 = "高于" if vs == "above" else "低于"
            lines.append(f"- {sym} {tf} {hk}: 技能 {sk:+.3f}（{axis1}无条件基线；"
                         f"{axis2} surrogate 带；评估覆盖 {nep} 个 episode）")
    else:
        lines.append("- 无。当前样本量下没有任何格子满足观察门槛——如实记录。")
    lines += ["", "## 方向对照（次级，含费用与已结算 funding；无 funding 数据则该列为—）",
              "", "| symbol | tf | 持仓根数 | 毛收益 | 费净 | 费+funding净 |",
              "|---|---|---|---|---|---|"]
    for r in payload["results"]:
        d = r.get("direction")
        if not d or "gross_sum" not in d:
            continue
        ff = d.get("net_after_fee_funding")
        lines.append(
            f"| {r['symbol']} | {r['tf']} | {d['n_position_bars']}/{d['n_bars']} "
            f"| {d['gross_sum']:+.4f} | {d['net_after_fee']:+.4f} "
            f"| {('%+.4f' % ff) if ff is not None else '—'} |")
    if insuff:
        lines += ["", "## 样本不足/错误", ""] + insuff
    lines += ["", "---", "*协议：docs/PRERESEARCH_VOL_20260803.md 第 3 节 + 评审修正"
              "（p3）。锁箱内数据（≥2026-09-01 收线）对一切校准不可见，开箱是一次性"
              "正式动作。方向对照无多重检验保护，禁止据此调仓。*"]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="P0 验收实验")
    ap.add_argument("-s", "--symbols", default=None, help="逗号分隔，缺省=库内全部")
    ap.add_argument("-o", "--out", default=None, help="报告输出路径")
    ap.add_argument("--note", default="", help="ledger 备注")
    args = ap.parse_args()

    conn = storage.connect_ro()
    try:
        known = sorted(storage.symbols(conn))
        if args.symbols:
            syms = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
            bad = [s for s in syms if s not in known]
            if bad:
                ap.error(f"未知品种 {bad}；库内现有 {known}")
        else:
            syms = known
        payload = backtest.run_experiment(conn, syms, note=args.note)
    finally:
        conn.close()

    ok = [r for r in payload["results"] if "horizons" in r]
    digest = _code_digest()
    exp_id = backtest.record_experiment(payload, digest, note=args.note)
    out = args.out or os.path.join(
        ROOT, "docs",
        f"BACKTEST_P0_{time.strftime('%Y%m%d', time.gmtime())}_{exp_id}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(render_md(payload, exp_id, digest))
    print(f"experiment_id={exp_id} 成功序列={len(ok)} 报告: {out}")
    if not ok:
        sys.exit(1)  # 全部序列失败：报告与账本已留痕，但退出码必须非零


if __name__ == "__main__":
    main()
