#!/usr/bin/env python3
"""Phase 0 探测脚本：枚举币安合约区与 Hyperliquid 上的美股永续，验证接口形状。

用法: .venv/bin/python scripts/probe_stock_perps.py [SYMBOL]
（SYMBOL 默认 NVDA；用于品种上新/下架时对账，探测结果决定 instruments 注册表内容。）
"""
from __future__ import annotations

import sys
import time

import requests

FAPI = "https://fapi.binance.com"
HL = "https://api.hyperliquid.xyz/info"


def binance_equity_symbols() -> list:
    info = requests.get(f"{FAPI}/fapi/v1/exchangeInfo", timeout=15).json()
    return sorted(
        s["symbol"]
        for s in info["symbols"]
        if s.get("underlyingType") == "EQUITY" and s["status"] == "TRADING"
    )


def hl(body: dict):
    r = requests.post(HL, json=body, timeout=15)
    r.raise_for_status()
    return r.json()


def hl_stock_universe() -> dict:
    out = {}
    for d in hl({"type": "perpDexs"}):
        if not d:
            continue
        name = d["name"]
        try:
            uni = hl({"type": "meta", "dex": name}).get("universe", [])
            out[name] = [a["name"] for a in uni]
        except Exception as e:  # noqa: BLE001
            out[name] = [f"<查询失败: {e}>"]
    return out


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "NVDA").upper()

    syms = binance_equity_symbols()
    print(f"== Binance 合约区 EQUITY 标的: {len(syms)} 个 ==")
    print(" ".join(syms))
    bsym = f"{ticker}USDT"
    if bsym in syms:
        t24 = requests.get(
            f"{FAPI}/fapi/v1/ticker/24hr", params={"symbol": bsym}, timeout=15
        ).json()
        print(f"{bsym}: 24h额 {float(t24['quoteVolume'])/1e6:.1f}M USDT")

    print("\n== Hyperliquid builder dex 股票盘 ==")
    for dex, names in hl_stock_universe().items():
        hits = [n for n in names if ticker in n.upper()]
        print(f"{dex}: {len(names)} 个资产" + (f" | {ticker} 命中: {hits}" if hits else ""))

    coin = f"xyz:{ticker}"
    try:
        mac = hl({"type": "metaAndAssetCtxs", "dex": "xyz"})
        uni, ctxs = mac[0]["universe"], mac[1]
        idx = next(i for i, a in enumerate(uni) if a["name"] == coin)
        c = ctxs[idx]
        print(
            f"\n{coin}: mark={c['markPx']} oracle={c['oraclePx']} funding={c['funding']}"
            f" OI={float(c['openInterest']):,.0f} 24h额={float(c['dayNtlVlm'])/1e6:.1f}M"
        )
        now = int(time.time() * 1000)
        fh = hl({"type": "fundingHistory", "coin": coin, "startTime": now - 86_400_000})
        if len(fh) >= 2:
            gap = (fh[-1]["time"] - fh[-2]["time"]) / 3_600_000
            print(f"funding 结算间隔 ≈ {gap:.1f}h（{len(fh)} 条/24h）")
    except StopIteration:
        print(f"\nxyz dex 中无 {coin}")


if __name__ == "__main__":
    main()
