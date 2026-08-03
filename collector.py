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
from regime.classify import (
    AUDIT_VERSION,
    FEATURE_WINDOW,
    RULES_VERSION,
    confirm_states,
    rolling_states_missing,
)
from regime.data import fetch_binance_vol1h, fetch_dvol, fetch_ohlcv
from regime.deriv import backfill as deriv_backfill
from regime.deriv import fetch_snapshot as deriv_snapshot
from regime.deriv import fetch_funding_history as deriv_funding_history
from regime.deriv import funding_interval_h as deriv_funding_interval
from regime.usvol import (
    VOL_INDEXES,
    backfill_index_history,
    fetch_index_quote,
    fetch_stock_iv30,
)

log = logging.getLogger("collector")

DEFAULT_SYMBOLS = (
    "BTC-USDT,ETH-USDT,SOL-USDT,HYPE-USDT,"
    "NVDA-USDT,TSLA-USDT,AAPL-USDT,MU-USDT,SOXL-USDT,SPY-USDT,QQQ-USDT,"
    "AMD-USDT,MSFT-USDT"
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
        # 两个时间戳分工：at=上次**尝试**（防刷），ok=上次**成功**（24h 刷新周期）。
        # 合成一个的话，一次失败会被当成"刚刷过"，真正的刷新推迟满 24h。
        at_key, ok_key = f"usvol_csv_at_{name}", f"usvol_csv_ok_{name}"
        at = float(storage.get_meta(conn, at_key, 0) or 0)
        ok = float(storage.get_meta(conn, ok_key, 0) or 0)
        behind = q is not None and q["ts"] > csv_max
        need = (
            not ok                              # 从未成功过
            or now - ok > 86_400                # 距上次成功满 24h（CBOE 偶有历史修订）
            or (behind and now - at > 3_600)    # 报价进入未确权交易日（1h 重试下限）
            or (not behind and now - at > 3_600 and now - ok > 86_400)
        )
        if need:
            storage.set_meta(conn, at_key, str(now))  # 记尝试，防刷
            rows = fetch_csv(name)
            if rows:
                storage.upsert_usvol(conn, name, rows)
                # 水位必须单调：CSV 尾部偶发缺失会让 fetched_max 回退，
                # 一旦水位降下去，同轮 quote 就获准覆盖已确权的官方收盘价
                fetched_max = max(t for t, _ in rows)
                if fetched_max < csv_max:
                    errors.append(
                        f"usvol csv {name}: 确权水位回退（{fetched_max} < {csv_max}），"
                        "保持原水位，本轮不下调"
                    )
                csv_max = max(csv_max, fetched_max)
                storage.set_meta(conn, f"usvol_csv_max_{name}", str(csv_max))
                storage.set_meta(conn, ok_key, str(now))  # 只有真拿到行才算成功
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


# VWAP 量流深度：480h（1d 窗）+ 250×24h（1d 偏离的分位参照期）≈ 6480，取整。
# 上市不足该深度的品种自然到头（分页返回空即止）。
VOL1H_TARGET = 7000


def sync_vol1h(conn, symbol: str) -> int:
    """币安 1h 量流同步：冷启动向后分页回填至目标深度，日常按水位增量。"""
    wm = storage.vol1h_watermark(conn, symbol)
    total = 0
    if wm is None:
        end = None
        need = VOL1H_TARGET
        while need > 0:
            rows = fetch_binance_vol1h(symbol, end_ms=end, limit=min(need, 1000))
            if not rows:
                break  # 上市日到头
            total += storage.upsert_vol1h(conn, symbol, rows)
            need -= len(rows)
            end = rows[0][0] - 1  # 继续向更早翻页
            if len(rows) < 50:
                break
            time.sleep(0.25)
        return total
    gap_h = max(0, int((time.time() * 1000 - wm) // 3_600_000))
    if gap_h < 1:
        return 0
    rows = [r for r in fetch_binance_vol1h(symbol, limit=min(gap_h + 3, 1000))
            if r[0] > wm]
    return storage.upsert_vol1h(conn, symbol, rows)


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
                # 重算窗 10000 根 + FEATURE_WINDOW-1 根 pre-roll。
                # 没有 pre-roll 的话，窗口最老的那 399 根会在"上下文不足"的
                # 情况下被算出来——同一根 bar 全量算与截尾算给出不同 features
                # （实测 820 vs 420 根：331 个共同 ts 里 features 差 189 条）。
                # 多读的部分只作上下文，min_bars 保证不为它们产出状态行。
                hist = storage.get_ohlcv(conn, sym, tf, limit=10_000 + FEATURE_WINDOW - 1)
                existing = storage.state_ts_set(conn, sym, tf, RULES_VERSION, AUDIT_VERSION)
                new_states = rolling_states_missing(
                    hist, tf, existing, session_aware=session_aware, source=src
                )
                if new_states:
                    storage.upsert_states(conn, sym, tf, new_states)
                if len(hist) >= 10_000 + FEATURE_WINDOW - 1:
                    # 重算窗已满：窗外若还有旧版本行，升版永远重算不到它们，
                    # 且 get_states 无版本谓词、折叠会混版——必须让人看见
                    n_stale = conn.execute(
                        "SELECT count(*) FROM regime_history WHERE symbol=? AND tf=?"
                        " AND ts < ? AND (version<>? OR audit_version<>?)",
                        (sym, tf, int(hist["ts"].iloc[0].value // 10**6),
                         RULES_VERSION, AUDIT_VERSION),
                    ).fetchone()[0]
                    if n_stale:
                        errors.append(f"{sym} {tf}: 重算窗外有 {n_stale} 行旧版本状态（混版风险）")
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

    # 币安 1h 量流（VWAP 专用量源，全品种统一取币安——量最大；与 OHLCV 主源解耦）。
    # 冷启动回填 VOL1H_TARGET 根（覆盖 1d 的 480h 窗 + 250 根分位参照期），
    # 之后按水位增量。失败不拖累其他采集。
    for sym in symbols:
        try:
            added = sync_vol1h(conn, sym)
            if added:
                log.info("VWAP量流 %s: +%d 行", sym, added)
        except Exception as e:  # noqa: BLE001
            errors.append(f"vol1h {sym}: {e}")
            log.warning("vol1h %s 失败: %s", sym, e)

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
            # 结算周期与已结算费率每天补一次。独立 try + 成功后才记时间戳：
            # 用 _should 先记后做的话，一次网络失败要沉默等满 24h 才重试，
            # 而且异常会连带跳过同一 try 里的当轮快照。
            # 结算周期与结算历史各自独立成功闸：合用一个的话，间隔查询失败被
            # 静默当成 8h "成功"，真正 4h 结算的品种会 24h 不重试、年化少算一半
            ik = f"funding_interval_at_{sym}"
            last_i = storage.get_meta(conn, ik)
            if not last_i or time.time() - float(last_i) > 86_400:
                try:
                    iv = deriv_funding_interval(sym)   # 失败返回 None，不写默认值
                    if iv:
                        storage.set_meta(conn, f"funding_interval_{sym}", str(iv))
                        storage.set_meta(conn, ik, str(time.time()))
                    else:
                        errors.append(f"funding_interval {sym}: 接口未返回，沿用上次已知值")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"funding_interval {sym}: {e}")
            hk = f"funding_hist_at_{sym}"
            last_h = storage.get_meta(conn, hk)
            if not last_h or time.time() - float(last_h) > 86_400:
                try:
                    storage.upsert_deriv(conn, sym, deriv_funding_history(sym))
                    storage.set_meta(conn, hk, str(time.time()))
                except Exception as e:  # noqa: BLE001
                    errors.append(f"funding_hist {sym}: {e}")
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
