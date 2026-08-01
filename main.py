#!/usr/bin/env python3
"""市场状态系统 v1 —— 命令行入口。

用法:
  python main.py                            # 默认 BTC-USDT, ETH-USDT · 1d/4h/1h
  python main.py --symbols SOL-USDT
  python main.py --timeframes 1d,4h
  python main.py --demo                     # 无网络时用合成数据自检管线
"""
from __future__ import annotations

import argparse

from regime.classify import analyze_timeframe
from regime.data import demo_ohlcv, fetch_dvol, fetch_ohlcv
from regime.features.utils import pct_rank
from regime.report import render


def _fetch_iv(symbol: str):
    """BTC/ETH 拉取 Deribit DVOL（隐含波动率指数）；其余品种或失败时返回 None。"""
    base = symbol.upper().replace("/", "-").split("-")[0]
    if base not in ("BTC", "ETH"):
        return None
    try:
        dv = fetch_dvol(base)
    except Exception:  # noqa: BLE001
        return None
    return {
        "dvol": round(float(dv["dvol"].iloc[-1]), 1),
        "dvol_rank": round(pct_rank(dv["dvol"], 365), 3),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="市场状态系统：价格结构 × 成交量 × 波动率")
    ap.add_argument("--symbols", default="BTC-USDT,ETH-USDT")
    ap.add_argument("--timeframes", default="1d,4h,1h", help="可选 1d/4h/1h，逗号分隔")
    ap.add_argument(
        "--sources",
        default="auto",
        help="数据源顺序；auto=按 instruments 注册表逐品种路由",
    )
    ap.add_argument("--demo", action="store_true", help="使用合成数据（自检用）")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    source_order = (
        None
        if args.sources.strip() == "auto"
        else [s.strip() for s in args.sources.split(",") if s.strip()]
    )

    for sym in symbols:
        regimes, dfs, sources = {}, {}, {}
        try:
            for tf in timeframes:
                if args.demo:
                    df, sources[tf] = demo_ohlcv(tf), "demo"
                else:
                    df, sources[tf] = fetch_ohlcv(sym, tf, sources=source_order)
                dfs[tf] = df
                regimes[tf] = analyze_timeframe(df, tf)
        except Exception as e:  # noqa: BLE001
            print(f"[{sym}] 数据获取/计算失败: {e}\n")
            continue
        iv = None if args.demo else _fetch_iv(sym)
        print(render(sym, sources, regimes, dfs, iv=iv))


if __name__ == "__main__":
    main()
