#!/usr/bin/env python3
"""因果回填 setup_history；默认且显式 ``--dry-run`` 都只读。

示例：
  .venv/bin/python scripts/backfill_setups.py --days 30 --dry-run
  .venv/bin/python scripts/backfill_setups.py --days 90 --symbols BTC-USDT ETH-USDT
  .venv/bin/python scripts/backfill_setups.py --days 30 --write  # 审核后由监工执行

每个 4h bar 都交给 regime.policy.tracking.walk_forward；该函数在调用
assemble.build 前先把 4h/1d/1h/vol1h/regime 全部截到该 bar 的收线时点。
"""
from __future__ import annotations

import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import instruments, storage  # noqa: E402
from regime.policy import assemble  # noqa: E402
from regime.policy import tracking  # noqa: E402


DAY_MS = 86_400_000


def _symbol_args(values) -> list[str] | None:
    if not values:
        return None
    out = []
    for value in values:
        out.extend(part.strip().upper() for part in value.split(",") if part.strip())
    return sorted(set(out))


def _limits(days: int) -> dict[str, int]:
    # 最早目标点之前仍要带足 assemble 的 400 根上下文；否则同一历史 bar 会
    # 因本次回填窗口长短不同而得到不同 EMA/cRSI/关键位。
    return {
        "4h": days * 6 + assemble.POLICY_SIGNAL_OHLCV_LIMIT,
        "1h": days * 24 + assemble.POLICY_SIGNAL_OHLCV_LIMIT,
        "1d": days + assemble.POLICY_SIGNAL_OHLCV_LIMIT,
    }


def rebuild_symbol(conn, symbol: str, days: int) -> tuple[list[int], list[dict]]:
    """只计算一个品种最近 N 天；本函数本身不写库。"""
    limits = _limits(days)
    frames = {
        tf: storage.get_ohlcv(conn, symbol, tf, limit=limit)
        for tf, limit in limits.items()
    }
    frame_4h = frames["4h"]
    if not len(frame_4h):
        return [], []
    all_ts = [int(value) for value in storage.ts_to_ms(frame_4h["ts"])]
    latest = max(all_ts)
    cutoff = latest - int(days) * DAY_MS
    targets = [value for value in all_ts if value >= cutoff]
    if not targets:
        return [], []
    timelines = {
        tf: storage.get_states(conn, symbol, tf, limit=100_000)
        for tf in assemble.POLICY_RESONANCE_TFS
    }
    vol1h = storage.get_vol1h(
        conn,
        symbol,
        since_ms=min(targets) - tracking.VOL1H_LOOKBACK_HOURS * 3_600_000,
    )
    return tracking.walk_forward(
        symbol,
        ohlcv_by_tf=frames,
        regime_by_tf=timelines,
        instrument=instruments.get(symbol),
        target_ts=targets,
        vol1h=vol1h,
        rules_version=tracking.RULES_GENERATION,
        assemble_version=assemble.ASSEMBLE_VERSION,
    )


def _fmt_ts(value: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%MZ")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="setup_history 因果回填（默认 dry-run）")
    parser.add_argument("--days", type=int, default=30, help="回填最近 N 天（默认 30）")
    parser.add_argument(
        "--symbols", nargs="*", default=None,
        help="空格或逗号分隔；默认数据库全部品种",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="只计算不写库（默认）")
    mode.add_argument("--write", action="store_true", help="审核后显式落库")
    args = parser.parse_args(argv)
    if args.days <= 0 or not math.isfinite(args.days):
        parser.error("--days 必须是正整数")
    dry_run = not args.write

    conn = storage.connect_ro() if dry_run else storage.connect()
    all_rows: list[dict] = []
    processed_total = 0
    observed_by_symbol: dict[str, int] = {}
    failed = []
    try:
        selected = _symbol_args(args.symbols) or storage.symbols(conn)
        print(
            f"mode={'DRY-RUN' if dry_run else 'WRITE'} days={args.days} "
            f"symbols={len(selected)} rules={tracking.RULES_GENERATION} "
            f"assemble={assemble.ASSEMBLE_VERSION}",
            flush=True,
        )
        for symbol in selected:
            try:
                processed, rows = rebuild_symbol(conn, symbol, args.days)
                processed_total += len(processed)
                all_rows.extend(rows)
                if processed:
                    observed_by_symbol[symbol] = max(processed)
                if not dry_run and processed:
                    storage.replace_setup_scan(
                        conn,
                        symbol,
                        processed,
                        rows,
                        rules_version=tracking.RULES_GENERATION,
                        assemble_version=assemble.ASSEMBLE_VERSION,
                    )
                print(
                    f"{symbol}: scanned={len(processed)} setup_rows={len(rows)}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001  单品种失败不拖累整次回填
                failed.append(f"{symbol}:{type(exc).__name__}:{exc}")
                print(f"{symbol}: FAILED {type(exc).__name__}: {exc}", flush=True)
    finally:
        conn.close()

    covered = len({row["symbol"] for row in all_rows})
    # 回填输入本身知道各品种已观察到的末根；显式附上后，最后一个旧 run 也能
    # 正确显示 expired，而不是因 setup 稀疏表没有 exit 行而永久 brewing。
    run_input = [
        {**row, "observed_through_ts": observed_by_symbol.get(row["symbol"], row["ts"])}
        for row in all_rows
    ]
    run_rows = tracking.runs(run_input)
    print(
        f"SUMMARY would_write={len(all_rows) if dry_run else 0} "
        f"written={0 if dry_run else len(all_rows)} covered_symbols={covered} "
        f"scanned_bars={processed_total} runs={len(run_rows)} failed={len(failed)}",
        flush=True,
    )
    if run_rows:
        current = [row for row in run_rows if row["status"] != "expired"]
        sample = max(current or run_rows, key=lambda row: (row["bars"], row["last_at"]))
        print(
            "SAMPLE_RUN "
            f"symbol={sample['symbol']} status={sample['status']} "
            f"start={_fmt_ts(sample['started_at'])} last={_fmt_ts(sample['last_at'])} "
            f"bars={sample['bars']} setup_bars={sample['setup_bars']} "
            f"crsi={sample['crsi_first']}->{sample['crsi_last']} "
            f"crsi_slope={sample['crsi_slope']} gap_now={sample['gap_now']} "
            f"gap_trend={sample['gap_trend']}",
            flush=True,
        )
    if failed:
        print("FAILURES " + " | ".join(failed), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
