#!/usr/bin/env python3
"""一次性运维：向前回填 OHLCV 历史（沿各序列当前源，绝不换源）。

  .venv/bin/python scripts/backfill_history.py            # 全系列
  .venv/bin/python scripts/backfill_history.py --dry-run  # 只取数不落库
  .venv/bin/python scripts/backfill_history.py -s BTC-USDT

原理：storage._invalidate_if_revised 把"新 ts < 现有 max"判为结构性修订，
删掉该序列全部状态行——回填落库后跑一轮 `collector.py --once` 即自动
walk-forward 重算全历史，无需任何手工迁移。

纪律：
- 每序列**沿用库内当前源**（加密 1d 锚 Deribit 08:00 UTC 日界，不可换源）；
- 整段在本地拼好并通过网格/缝合校验后才 upsert（单次写入 = 单次失效）；
- 拼段内部 diff==step、末根 + step == 库内最早 ts，任一不满足即放弃该序列；
- 运行前必须停 collector 守护（两个写者会打架），跑完 --once 后再拉起。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import storage  # noqa: E402
from regime.data import (  # noqa: E402
    DERIBIT_BASE, _BAR_MS, _BINANCE_INTERVALS, _DERIBIT_RES,
    _binance_symbol, _deribit_instrument, _normalize, _resample_4h,
)

# 目标深度（根）。1h≈208 天、4h≈333 天、1d≈4 年；美股永续受上市日自然封顶。
TARGET_BARS = {"1h": 5000, "4h": 2000, "1d": 1500}
SLEEP = 0.35  # 请求间隔，敬畏限频


def _fetch_deribit_range(symbol, tf, start_ms, end_ms):
    """Deribit tradingview 端点按范围取数；4h 走 1h+重采样（与生产同路径）。"""
    fetch_tf = "1h" if tf == "4h" else tf
    r = requests.get(
        f"{DERIBIT_BASE}/api/v2/public/get_tradingview_chart_data",
        params={
            "instrument_name": _deribit_instrument(symbol),
            "resolution": _DERIBIT_RES[tf],
            "start_timestamp": int(start_ms),
            "end_timestamp": int(end_ms),
        },
        timeout=15,
    )
    r.raise_for_status()
    res = (r.json().get("result") or {})
    if res.get("status") != "ok" or not res.get("ticks"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "ts": res["ticks"], "open": res["open"], "high": res["high"],
        "low": res["low"], "close": res["close"], "volume": res["volume"],
    })
    df = _normalize(df)
    if tf == "4h":
        df = _resample_4h(df)
    _ = fetch_tf
    return df


def _fetch_binance_futures_range(symbol, tf, end_ms, limit=1000):
    """fapi endTime 语义：返回 openTime <= endTime 的最近 limit 根。"""
    r = requests.get(
        "https://fapi.binance.com/fapi/v1/klines",
        params={
            "symbol": _binance_symbol(symbol),
            "interval": _BINANCE_INTERVALS[tf],
            "endTime": int(end_ms),
            "limit": limit,
        },
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([row[:6] for row in rows],
                      columns=["ts", "open", "high", "low", "close", "volume"])
    return _normalize(df)


def backfill_series(conn, sym, tf, dry_run=False):
    step = _BAR_MS[tf]
    row = conn.execute(
        "SELECT MIN(ts), COUNT(*) FROM ohlcv WHERE symbol=? AND tf=?", (sym, tf)
    ).fetchone()
    earliest, n_have = row[0], row[1]
    if earliest is None:
        return f"{sym} {tf}: 库内无数据，跳过（回填不负责冷启动）"
    src = storage.last_source(conn, sym, tf)
    need = TARGET_BARS[tf] - n_have
    if need <= 0:
        return f"{sym} {tf}: 已有 {n_have} 根 >= 目标，跳过"

    chunks, E = [], earliest
    while need > 0:
        if src == "deribit":
            span = min(need, 750 if tf == "4h" else 3000)
            fetch_step = _BAR_MS["1h"] if tf == "4h" else step
            end = E - fetch_step  # tick 含 end：最后一根开盘于 E-step，正好衔接
            start = E - span * step
            if tf == "4h":
                start = (start // step) * step  # 对齐 4h 边界，桶必完整
            df = _fetch_deribit_range(sym, tf, start, end)
        elif src == "binance_futures":
            df = _fetch_binance_futures_range(sym, tf, E - 1, limit=min(need, 1000))
        else:
            return f"{sym} {tf}: 源 {src} 未支持回填，跳过"
        if not len(df):
            break  # 源已到头（上市日/存续起点）——先判空再过滤，空表没有 ts 列
        df = df[df["ts"].astype("int64") // 10**6 < E].reset_index(drop=True)
        if not len(df):
            break
        chunks.insert(0, df)
        got = len(df)
        need -= got
        new_E = int(df["ts"].iloc[0].value // 10**6)
        if new_E >= E:
            return f"{sym} {tf}: 翻页未推进（E={E}），中止"
        E = new_E
        if got < 50:  # 尾水：再翻也没多少了
            break
        time.sleep(SLEEP)

    if not chunks:
        return f"{sym} {tf}: 源无更早数据（已到存续起点）"
    full = pd.concat(chunks, ignore_index=True).drop_duplicates("ts")
    full = full.sort_values("ts").reset_index(drop=True)
    ts = full["ts"].astype("int64") // 10**6
    gaps = int(((ts.diff().dropna()) != step).sum())
    seam_ok = int(ts.iloc[-1]) + step == earliest
    if gaps or not seam_ok:
        return (f"{sym} {tf}: 拒绝落库——段内缺口 {gaps} 处, "
                f"缝合 {'OK' if seam_ok else 'FAIL'}（宁缺毋假）")
    if dry_run:
        return (f"{sym} {tf}: [dry-run] 可回填 {len(full)} 根 "
                f"({pd.Timestamp(ts.iloc[0], unit='ms', tz='UTC'):%Y-%m-%d} 起，源 {src}）")
    storage.upsert_ohlcv(conn, sym, tf, full, src)
    return (f"{sym} {tf}: 回填 {len(full)} 根（{src}，"
            f"{pd.Timestamp(ts.iloc[0], unit='ms', tz='UTC'):%Y-%m-%d} 起）"
            f"——状态行已按结构性修订自动失效，跑 collector --once 重算")


def main() -> None:
    ap = argparse.ArgumentParser(description="OHLCV 历史回填（一次性运维）")
    ap.add_argument("-s", "--symbols", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run:
        import subprocess
        chk = subprocess.run(["pgrep", "-f", "collector.py"], capture_output=True)
        if chk.returncode == 0:
            others = [p for p in chk.stdout.decode().split()
                      if p and int(p) != os.getpid()]
            if others:
                sys.exit("collector 守护仍在运行（先停掉再回填，避免双写打架）")

    conn = storage.connect() if not args.dry_run else storage.connect_ro()
    try:
        syms = (sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
                if args.symbols else sorted(storage.symbols(conn)))
        for sym in syms:
            for tf in ("1h", "4h", "1d"):
                try:
                    print(backfill_series(conn, sym, tf, dry_run=args.dry_run), flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"{sym} {tf}: 失败 {e}", flush=True)
                time.sleep(SLEEP)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
