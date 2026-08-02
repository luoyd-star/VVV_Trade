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


def _check_symbol(symbol: str) -> str:
    """品种白名单：symbol 会被拼进 <panel> 的 system 上下文，是提示词注入向量。
    面板端点早就关死了（dashboard.py），CLI 一直漏着——审计时用
    `-s NOPE-USDT` 验退出码，结果真调了一次模型并写库两行（本文档 6.5 案例）。"""
    conn = storage.connect_ro()
    try:
        known = set(storage.symbols(conn))
    finally:
        conn.close()
    if symbol not in known:
        raise SystemExit(
            f"未知品种 {symbol!r}——库里现有：{', '.join(sorted(known))}"
        )
    return symbol


def ask(symbol: str, text: str) -> bool:
    conn = storage.connect_rw_nomigrate()
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
            return False
        storage.add_chat(conn, "user", text)
        storage.add_chat(conn, "assistant", out["reply"])
        print(out["reply"])
        return True
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="VVVhermes 终端入口（与面板共享对话）")
    ap.add_argument("question", nargs="*", help="要问的问题；留空进入交互模式")
    ap.add_argument("-s", "--symbol", default="BTC-USDT", help="注入哪个品种的面板上下文")
    args = ap.parse_args()

    cfg = load_config()
    symbol = _check_symbol(args.symbol.upper())
    conn = storage.connect_ro()
    n_hist = len(storage.get_chat(conn, limit=60))
    conn.close()
    print(
        f"VVVhermes · provider={cfg.get('provider')} · model={cfg.get('model')}"
        f" · 上下文: {symbol} · 共享历史 {n_hist} 条",
        file=sys.stderr,
    )

    if args.question and len(" ".join(args.question)) > 8000:
        raise SystemExit("消息超过 8000 字符上限（与面板 /api/agent/chat 同一边界）")
    if args.question:
        # 单问模式失败必须以非零退出——否则 cron/脚本把静默失败当成功
        if not ask(symbol, " ".join(args.question)):
            sys.exit(1)
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
            want = line.split(None, 1)[1].strip().upper()
            try:
                symbol = _check_symbol(want)
            except SystemExit as e:   # REPL 里不该退出进程，提示后继续
                print(str(e), file=sys.stderr)
                continue
            print(f"已切换到 {symbol}", file=sys.stderr)
            continue
        if line == "/clear":
            conn = storage.connect_rw_nomigrate()
            storage.clear_chat(conn)
            conn.close()
            print("共享历史已清空（面板刷新后同步）", file=sys.stderr)
            continue
        ask(symbol, line)


if __name__ == "__main__":
    main()
