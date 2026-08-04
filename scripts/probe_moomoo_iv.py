#!/usr/bin/env python3
"""moomoo OpenAPI 标的级 IV 探测：验权限 + 量历史深度 + 逐品种最早可用日。

回答调研留下的**唯一承重未知**：get_option_underlying_his_volatility（协议 3304，
2026-06-25 才上线）到底能回溯多少年。若 ≥3 年，moomoo 成为零成本主路线；
若只有 52 周，退回 ORATS 单月回填。

前置：本机已启动 OpenD 并登录（凭据只存在 OpenD 侧，本脚本只连 127.0.0.1:11111）。

    .venv/bin/python scripts/probe_moomoo_iv.py

限频 60 次/30 秒——脚本按 0.6s/次自律，全程约 3-5 分钟。
"""
from __future__ import annotations

import json
import socket
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from moomoo import OpenQuoteContext, RET_OK
except ImportError:
    sys.exit("缺少 SDK：.venv/bin/pip install moomoo-api")

HOST, PORT = "127.0.0.1", 11111
PACE = 0.6  # 秒/次，限频 60 次/30 秒的一半速率
PROBE_FLOOR = 2015  # 往前探到此年为止
# 深探样本：老牌大盘 / ETF / 3x 杠杆 ETF / 2023 新股 / 2025 新股——覆盖各类历史边界
DEEP = ["AAPL", "NVDA", "QQQ", "SOXL", "ARM", "CRCL"]


def us_symbols() -> list[str]:
    """instruments.json 里的美股永续 → moomoo 代码（US.NVDA）。"""
    cfg = json.loads((ROOT / "instruments.json").read_text())
    out = []
    for sym, v in cfg.items():
        if isinstance(v, dict) and v.get("class") == "us_stock_perp":
            out.append(sym.split("-")[0])
    return sorted(out)


def year_rows(ctx, code: str, yr: int) -> int:
    """某标的某年的 IV 行数（0 = 该年无数据）。单次跨度上限 364 天，一年一请求。"""
    total, key = 0, None
    while True:
        ret, data, key = ctx.get_option_underlying_his_volatility(
            f"US.{code}",
            begin_time=f"{yr}-01-01",
            end_time=f"{yr}-12-30",  # 364 天上限：01-01→12-30 恰好 363 天
            page_req_key=key,
        )
        time.sleep(PACE)
        if ret != RET_OK:
            msg = str(data)
            if "无权限" in msg or "permission" in msg.lower() or "quota" in msg.lower():
                raise PermissionError(msg)
            return -1  # 其它错误：记为不可用
        total += len(data)
        if key is None:
            return total


def opend_alive() -> bool:
    """TCP 预检。SDK 连不上时会无限重试而不抛错，无人值守下会静默挂死——必须先探端口。"""
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex((HOST, PORT)) == 0


def main() -> int:
    codes = us_symbols()
    print(f"探测 {len(codes)} 个美股标的 · OpenD {HOST}:{PORT}\n")

    if not opend_alive():
        print(f"✗ {HOST}:{PORT} 无监听——OpenD 未启动或未登录。\n"
              "  启动后重跑本脚本（凭据只存在 OpenD 侧，本脚本不接触账号密码）。")
        return 1
    ctx = OpenQuoteContext(host=HOST, port=PORT)

    try:
        # ① 权限与当前值：一次拿全 31 个标的
        print("① 当前值 get_option_underlying_overview（协议 3303）")
        ret, ov = ctx.get_option_underlying_overview([f"US.{c}" for c in codes])
        if ret != RET_OK:
            print(f"  ✗ 失败：{ov}")
            print("  → 若提示权限，检查 moomoo 港美股总资产是否 >0（免费 OPRA LV1 门槛）")
            return 2
        got = set(ov["code"].str.replace("US.", "", regex=False))
        miss = [c for c in codes if c not in got]
        print(f"  ✓ 返回 {len(ov)}/{len(codes)} 个标的" + (f"，缺 {miss}" if miss else "，全覆盖"))
        cols = [c for c in ("code", "iv", "iv_rank", "iv_percentile", "hv_30d") if c in ov.columns]
        print(ov[cols].head(8).to_string(index=False))

        # ② 历史深度：深探样本逐年回退，连续两年空即判定见底
        print(f"\n② 历史深度 get_option_underlying_his_volatility（协议 3304）· 探至 {PROBE_FLOOR}")
        this_year = date.today().year
        depth = {}
        for c in DEEP:
            if c not in codes:
                continue
            earliest, blanks = None, 0
            for yr in range(this_year, PROBE_FLOOR - 1, -1):
                n = year_rows(ctx, c, yr)
                if n > 0:
                    earliest, blanks = yr, 0
                else:
                    blanks += 1
                    if earliest is not None and blanks >= 2:
                        break
            depth[c] = earliest
            span = f"{this_year - earliest + 1} 年（{earliest} 起）" if earliest else "无数据"
            print(f"  {c:<6} {span}")

        # ③ 全品种：用深探得到的最早年份抽样确认覆盖面
        base = min([y for y in depth.values() if y] or [this_year])
        print(f"\n③ 全品种 {base} 年数据可得性")
        for c in codes:
            n = year_rows(ctx, c, base)
            flag = "✓" if n > 0 else ("—" if n == 0 else "✗")
            print(f"  {flag} {c:<6} {base} 年 {max(n, 0):>4} 行")

        # 判决
        best = max([y for y in depth.values() if y] or [this_year])
        yrs = this_year - min([y for y in depth.values() if y] or [this_year]) + 1
        print(f"\n{'='*60}\n判决：最深回溯 {yrs} 年")
        print("→ ≥3 年：moomoo 可作零成本主路线，进入回填实施" if yrs >= 3
              else "→ <3 年：历史不足，ORATS $49 单月回填 + moomoo 免费增量")
        return 0
    except PermissionError as e:
        print(f"\n✗ 权限不足：{e}\n  → 检查 moomoo 账户港美股总资产 >0 / 有美股持仓（免费 OPRA LV1 门槛）")
        return 3
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001, S110
            pass


if __name__ == "__main__":
    raise SystemExit(main())
