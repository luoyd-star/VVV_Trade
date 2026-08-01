#!/usr/bin/env python3
"""数据采集器：每隔 interval 秒拉一轮行情写入 SQLite，并做 walk-forward 状态分类。

设计：
- collector 只写库，面板只读库，两个进程通过 data/market.db（WAL 模式）解耦；
- OHLCV 按 (symbol, tf, ts) 幂等 upsert，本地历史随运行时间越攒越长；
- 状态历史（regime_history）逐根 K 线 walk-forward 计算，只补库里缺的 ts，
  每根只用它之前的数据——与日后回测口径一致。

用法:
  .venv/bin/python collector.py                 # 常驻，每 5 分钟一轮
  .venv/bin/python collector.py --once          # 只跑一轮（cron/launchd 用）
  .venv/bin/python collector.py --symbols BTC-USDT,ETH-USDT --interval 300
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

from regime import instruments, storage
from regime.classify import confirm_states, rolling_states_missing
from regime.data import fetch_dvol, fetch_ohlcv
from regime.deriv import backfill as deriv_backfill
from regime.deriv import fetch_snapshot as deriv_snapshot
from regime.deriv import funding_interval_h as deriv_funding_interval
from regime.usvol import (
    VOL_INDEXES,
    backfill_index_history,
    fetch_index_quote,
    fetch_stock_iv30,
)

log = logging.getLogger("collector")

DEFAULT_SYMBOLS = (
    "BTC-USDT,ETH-USDT,SOL-USDT,"
    "NVDA-USDT,TSLA-USDT,AAPL-USDT,MU-USDT,SOXL-USDT,SPY-USDT,QQQ-USDT"
)
DEFAULT_TFS = "1d,4h,1h"
DVOL_CURRENCIES = ("BTC", "ETH")


def setup_logging() -> None:
    os.makedirs(storage.DATA_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(storage.DATA_DIR, "collector.log")),
        ],
    )


def _should(conn, key: str, interval_s: int) -> bool:
    """低频动作的节流闸（跨进程持久在 meta 表）。到期即更新时间戳并放行。"""
    last = storage.get_meta(conn, key)
    now = time.time()
    if last and now - float(last) < interval_s:
        return False
    storage.set_meta(conn, key, str(now))
    return True


def sync_vol_index(
    conn,
    name: str,
    *,
    fetch_csv=backfill_index_history,
    fetch_quote=fetch_index_quote,
    now=None,
) -> tuple:
    """同步一个 CBOE 波动率指数，返回 (日志串或 None, 错误列表)。

    权威分层：**CSV 官方收盘价 > 延迟报价**。报价只写 CSV 尚未覆盖的交易日，
    永不回写已确权的日子——否则每轮报价都会把官方值盖回成盘中值。
    两次网络调用各自 try：CSV 是 600KB/20s，报价是 500B/10s，前者失败不该拖累后者。
    fetch_* 与 now 可注入，供 tests/test_usvol_authority.py 做确定性回归。
    """
    now = time.time() if now is None else now
    errors, msg = [], None
    csv_max = int(storage.get_meta(conn, f"usvol_csv_max_{name}", 0) or 0)
    q = None
    try:
        q = fetch_quote(name)
    except Exception as e:  # noqa: BLE001
        errors.append(f"usvol quote {name}: {e}")
    try:
        # 重拉 CSV 的时机：从未拉过 / 距上次满 24h（CBOE 偶有历史修订）/
        # 报价已经进入 CSV 还没覆盖的交易日（收盘后确权，1h 重试下限防刷）。
        at_key = f"usvol_csv_at_{name}"
        at = float(storage.get_meta(conn, at_key, 0) or 0)
        since = now - at
        behind = q is not None and q["ts"] > csv_max
        if not at or since > 86_400 or (behind and since > 3_600):
            storage.set_meta(conn, at_key, str(now))  # 先记尝试，防刷
            rows = fetch_csv(name)
            if rows:
                storage.upsert_usvol(conn, name, rows)
                csv_max = max(t for t, _ in rows)
                storage.set_meta(conn, f"usvol_csv_max_{name}", str(csv_max))
                msg = (
                    f"波动率指数日线 {name}：{len(rows)} 行，官方确权至 "
                    f"{datetime.fromtimestamp(csv_max / 1000, timezone.utc).date()}"
                    f"（{'首次回填' if not at else ('补确权' if behind else '每日刷新')}）"
                )
    except Exception as e:  # noqa: BLE001
        errors.append(f"usvol csv {name}: {e}")
    if q is not None and q["ts"] > csv_max:
        storage.upsert_usvol(conn, name, [(q["ts"], q["close"])])
    return msg, errors


def cycle(conn, symbols, timeframes, source_order) -> list:
    errors = []
    for sym in symbols:
        session_aware = instruments.get(sym)["class"] == "us_stock_perp"
        for tf in timeframes:
            try:
                df_full, src = fetch_ohlcv(
                    sym, tf, sources=source_order, drop_unclosed=False
                )
                df = df_full.iloc[:-1].reset_index(drop=True)  # 已收盘部分
                storage.upsert_ohlcv(conn, sym, tf, df, src)
                # 形成中的最后一根另存 live_bars，供面板滚动预览（不入确认历史）
                live = df_full.iloc[-1]
                storage.set_live_bar(conn, sym, tf, {
                    "ts": int(live["ts"].value // 10**6),
                    "open": live["open"], "high": live["high"],
                    "low": live["low"], "close": live["close"],
                    "volume": live["volume"],
                    "fetched_at": int(time.time() * 1000),
                })
                hist = storage.get_ohlcv(conn, sym, tf, limit=1200)
                existing = storage.state_ts_set(conn, sym, tf)
                new_states = rolling_states_missing(
                    hist, tf, existing, session_aware=session_aware
                )
                if new_states:
                    storage.upsert_states(conn, sym, tf, new_states)
                # 非对称迟滞：对整条 raw 序列做确认折叠，把确认态写回 state 列
                rows_all = storage.get_states(conn, sym, tf, limit=100_000)
                confirmed, cand = confirm_states([r["raw_state"] for r in rows_all])
                fixes = [
                    (confirmed[i], rows_all[i]["ts"])
                    for i in range(len(rows_all))
                    if rows_all[i]["state"] != confirmed[i]
                ]
                if fixes:
                    storage.set_confirmed(conn, sym, tf, fixes)
                now_state = confirmed[-1] if confirmed else "?"
                log.info(
                    "%s %s src=%s bars=%d states+%d 确认=%s%s%s",
                    sym, tf, src, len(hist), len(new_states), now_state,
                    f" 原始={rows_all[-1]['raw_state']}" if rows_all and rows_all[-1]["raw_state"] != now_state else "",
                    f" 候选={cand['state']}({cand['count']}/{cand['need']})" if cand else "",
                )
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sym} {tf}: {e}")
                log.warning("%s %s 失败: %s", sym, tf, e)

    # 衍生品持仓（Binance 永续）：首轮回填历史（OI/taker 仅约 30 天保留期，先到先得），
    # 之后每轮采一次快照。失败不拖累 K 线与 DVOL。
    for sym in symbols:
        try:
            flag = f"deriv_backfilled_v2_{sym}"
            if not storage.get_meta(conn, flag):
                rows = deriv_backfill(sym)
                storage.upsert_deriv(conn, sym, rows)
                storage.set_meta(conn, flag, "1")
                log.info("衍生品回填 %s：%d 行（funding≈333天，OI/taker/premium≈20天）", sym, len(rows))
            # 结算周期每天问一次接口存 meta，面板/Hermes 只读库（不从数据反推，
            # 因为 funding 列混着快照预测行，差分中位数会被带偏）
            if _should(conn, f"funding_info_at_{sym}", 86_400):
                storage.set_meta(
                    conn, f"funding_interval_{sym}", str(deriv_funding_interval(sym))
                )
            snap = deriv_snapshot(sym)
            storage.upsert_deriv(conn, sym, [snap])
            log.info(
                "衍生品 %s OI=%.0f funding=%.5f%% premium=%.4f%% taker=%s",
                sym, snap["oi"], snap["funding"] * 100,
                (snap["premium"] or 0) * 100,
                f"{snap['taker_ratio']:.3f}" if snap.get("taker_ratio") else "—",
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"deriv {sym}: {e}")
            log.warning("衍生品 %s 失败: %s", sym, e)

    # 美股波动率（CBOE 免费延迟源；delayed_quotes 属 ToS 灰区 → 全模块限频 30 分钟）
    if _should(conn, "usvol_last_fetch", 1800):
        for name in VOL_INDEXES:
            msg, errs = sync_vol_index(conn, name)
            if msg:
                log.info("%s", msg)
            for e in errs:
                log.warning("%s", e)
            errors.extend(errs)
        iv_line = []
        for sym in symbols:
            if instruments.get(sym)["class"] != "us_stock_perp":
                continue
            try:
                iv = fetch_stock_iv30(sym.split("-")[0])
                if iv is not None:
                    storage.upsert_deriv(
                        conn, sym, [{"ts": int(time.time() * 1000), "iv30": iv}]
                    )
                    iv_line.append(f"{sym.split('-')[0]}={iv:.1f}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"iv30 {sym}: {e}")
        if iv_line:
            log.info("个股 iv30: %s", " ".join(iv_line))

    bases = {s.upper().replace("/", "-").split("-")[0] for s in symbols}
    for currency in sorted(bases & set(DVOL_CURRENCIES)):
        try:
            dv = fetch_dvol(currency)
            storage.upsert_dvol(conn, currency, dv)
            log.info("DVOL %s 最新 %.1f（%d 天）", currency, dv["dvol"].iloc[-1], len(dv))
        except Exception as e:  # noqa: BLE001
            errors.append(f"DVOL {currency}: {e}")
            log.warning("DVOL %s 失败: %s", currency, e)
    return errors


def main() -> None:
    ap = argparse.ArgumentParser(description="市场状态系统数据采集器")
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--timeframes", default=DEFAULT_TFS)
    ap.add_argument(
        "--sources",
        default="auto",
        help="数据源顺序；auto=按 instruments 注册表逐品种路由（默认）",
    )
    ap.add_argument("--interval", type=int, default=300, help="采集间隔（秒），默认 300")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    args = ap.parse_args()

    setup_logging()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    source_order = (
        None
        if args.sources.strip() == "auto"
        else [s.strip() for s in args.sources.split(",") if s.strip()]
    )
    conn = storage.connect()

    log.info(
        "采集器启动 symbols=%s tfs=%s interval=%ds once=%s",
        ",".join(symbols), ",".join(timeframes), args.interval, args.once,
    )
    try:
        while True:
            t0 = time.time()
            errors = cycle(conn, symbols, timeframes, source_order)
            elapsed = time.time() - t0
            storage.set_meta(conn, "last_run", str(int(time.time() * 1000)))
            storage.set_meta(conn, "status", json.dumps({
                "interval": args.interval,
                "cycle_sec": round(elapsed, 1),
                "errors": errors[-10:],
                "symbols": symbols,
            }, ensure_ascii=False))
            log.info("本轮完成 %.1fs 错误=%d 库=%s", elapsed, len(errors), storage.counts(conn))
            if args.once:
                break
            time.sleep(max(5.0, args.interval - elapsed))
    except KeyboardInterrupt:
        log.info("收到中断，采集器退出")


if __name__ == "__main__":
    main()
