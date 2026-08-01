#!/usr/bin/env python3
"""在终端把 VVVhermes 喊出来。（与面板共享同一份对话——历史在 SQLite chat 表）

  .venv/bin/python vvvhermes.py                      # 交互模式（REPL）
  .venv/bin/python vvvhermes.py "当前 1d 什么状态？"   # 单问单答
  .venv/bin/python vvvhermes.py -s ETH-USDT           # 指定注入哪个品种的面板上下文

共享与热读：
  data/market.db · chat  对话历史（面板与终端同一份；/clear 双端一起清空）
  agent.json             provider / 模型（provider=codex 时走本机官方 Codex CLI）
  hermes_system.md       系统提示词（用户掌控）
不需要面板服务在运行，但需要 collector 采过数据。

alias（已写入 ~/.zshrc）：VVVhermes / vvvhermes
"""
from __future__ import annotations

import argparse
import sys

from dashboard import build_dashboard
from regime import storage
from regime.agent import chat, load_config


def ask(symbol: str, text: str) -> None:
    conn = storage.connect()
    try:
        msgs = [
            {"role": r["role"], "content": r["content"]}
            for r in storage.get_chat(conn, limit=20)
        ]
        msgs.append({"role": "user", "content": text})
        payload = build_dashboard(symbol)
        out = chat(payload, msgs)
        if out.get("error"):
            print(f"[错误] {out['error']}", file=sys.stderr)
            return
        storage.add_chat(conn, "user", text)
        storage.add_chat(conn, "assistant", out["reply"])
        print(out["reply"])
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="VVVhermes 终端入口（与面板共享对话）")
    ap.add_argument("question", nargs="*", help="要问的问题；留空进入交互模式")
    ap.add_argument("-s", "--symbol", default="BTC-USDT", help="注入哪个品种的面板上下文")
    args = ap.parse_args()

    cfg = load_config()
    symbol = args.symbol.upper()
    conn = storage.connect()
    n_hist = len(storage.get_chat(conn, limit=60))
    conn.close()
    print(
        f"VVVhermes · provider={cfg.get('provider')} · model={cfg.get('model')}"
        f" · 上下文: {symbol} · 共享历史 {n_hist} 条",
        file=sys.stderr,
    )

    if args.question:
        ask(symbol, " ".join(args.question))
        return

    print("交互模式：直接提问；/symbol ETH-USDT 切品种；/clear 清空共享历史；/q 退出", file=sys.stderr)
    while True:
        try:
            line = input(f"[{symbol}] > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/q", "/quit", "exit"):
            break
        if line.startswith("/symbol "):
            symbol = line.split(None, 1)[1].strip().upper()
            print(f"已切换到 {symbol}", file=sys.stderr)
            continue
        if line == "/clear":
            conn = storage.connect()
            storage.clear_chat(conn)
            conn.close()
            print("共享历史已清空（面板刷新后同步）", file=sys.stderr)
            continue
        ask(symbol, line)


if __name__ == "__main__":
    main()
