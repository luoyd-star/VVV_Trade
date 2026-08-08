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
from regime.data import (
    HEALTHY_BARS, closed_ohlcv, fetch_binance_spot_daily, fetch_binance_vol1h,
    fetch_dvol, fetch_ohlcv, fetch_yahoo_daily,
)
from regime.policy import assemble as policy_assemble
from regime.policy import tracking as setup_tracking
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

# X1 扩容（2026-08-04，docs/PRERESEARCH_XASSET_20260803.md）：37 品种。
# 核心池 29 + 观察池 8（观察池同样全量采集与判态；"不进核心相关矩阵"由
# 消费层按 instruments.pool 过滤——采集层不做区别，保证数据自始完整）。
DEFAULT_SYMBOLS = (
    "BTC-USDT,ETH-USDT,SOL-USDT,HYPE-USDT,"
    "NVDA-USDT,AMD-USDT,MU-USDT,SNDK-USDT,MRVL-USDT,INTC-USDT,TSM-USDT,"
    "ARM-USDT,NBIS-USDT,SKHY-USDT,"
    "GOOGL-USDT,META-USDT,AMZN-USDT,ORCL-USDT,PLTR-USDT,MSFT-USDT,"
    "AAPL-USDT,TSLA-USDT,COIN-USDT,MSTR-USDT,CRCL-USDT,"
    "QQQ-USDT,SPY-USDT,SOXL-USDT,XAU-USDT,CL-USDT,"
    "NOW-USDT,SNOW-USDT,CRM-USDT,ADBE-USDT,CRWD-USDT,PANW-USDT,APP-USDT,"
    "CRWV-USDT,"
    # X2 扩容（2026-08-05）：补已实测盲区（金融/医疗/必需消费/能源/工业/国际/小盘）
    # + 贵金属 XAG/XPT/XPD + 有期权的加密 BNB/XRP/DOGE。原则=补盲区而非加厚科技维度
    "AMAT-USDT,ASML-USDT,ASTS-USDT,AVGO-USDT,BABA-USDT,BNB-USDT,CAT-USDT,COST-USDT,CSCO-USDT,DIS-USDT,DOGE-USDT,EWJ-USDT,EWY-USDT,EWZ-USDT,GEV-USDT,GS-USDT,HOOD-USDT,IBM-USDT,IWM-USDT,JPM-USDT,LLY-USDT,NFLX-USDT,NVO-USDT,RKLB-USDT,SMH-USDT,UBER-USDT,UVXY-USDT,V-USDT,VRT-USDT,WMT-USDT,XAG-USDT,XBI-USDT,XLE-USDT,XPD-USDT,XPT-USDT,XRP-USDT,"
    # X3 扩容（2026-08-07 用户批准）：SKHYNIX=韩股本体（KR_EQUITY，KRX 000660 经
    # USDTKRW 汇率腿定价——与美股定价的 SKHY 是**两个不同标的**，并存不互替）；
    # 其余为半导体链（QCOM/KLAC/LRCX/TER/COHR/CRDO）+ 存储（WDC/DRAM，与海力士
    # 同板块）+ 韩台 ETF（KORU/EWT）。币安股票永续的 underlyingType 分板：
    # EQUITY=美股 / KR_EQUITY=韩股 / HK_EQUITY=港股——按 EQUITY 过滤会漏掉后两板。
    "SKHYNIX-USDT,KORU-USDT,EWT-USDT,QCOM-USDT,KLAC-USDT,LRCX-USDT,TER-USDT,"
    "COHR-USDT,CRDO-USDT,WDC-USDT,DRAM-USDT"
)
SETUP_BARS_PER_CYCLE = 2000
DEFAULT_TFS = "1d,4h,1h"
DVOL_CURRENCIES = ("BTC", "ETH")
# 起步值，待校准；单轮 setup 装配的全局根数预算。实测约 30 根/秒，
# 2000 根 ≈ 65 秒，留在 300 秒采集周期内且不挤压 IV 建链。


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


def breadth_slot(now=None) -> str | None:
    """当前时刻属于哪个宽度采样槽位；不在窗内返回 None。

    两个槽位刻意分开存：09:45 采到的是隔夜跳空后的开盘初（大量票尚未成交，
    实测零变动股占 44%），15:59 采到的是近收盘（实测约 25%）——两者的"宽度"
    根本不是同一个统计量，混入同一序列会让分位失去意义。
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from regime.calendar_nyse import is_trading_day, session_close_et

    now = now or _dt.now(ZoneInfo("America/New_York"))
    if not is_trading_day(now.date()):
        return None
    hm = now.hour * 60 + now.minute
    if 9 * 60 + 40 <= hm < 10 * 60 + 10:      # 09:40-10:10 ET
        return "0945"
    # 近收盘槽位相对**真实收盘**取窗（半日市 13:00）——codex 抓到硬编码 15:45-16:05
    # 会在半日市把盘后 3 小时的陈旧快照标成"近收盘"口径
    close = session_close_et(now.date())
    close_m = close.hour * 60 + close.minute
    if close_m - 15 <= hm < close_m + 5:
        return "1559"
    return None


def sync_breadth(conn) -> tuple:
    """市场宽度快照 → breadth 表（**影子字段，只采不消费**）。

    每个槽位每交易日只采一次（主键幂等 + meta 节流双保险）。
    OpenD 不在就静默跳过，与个股 IV 同一纪律。
    """
    from regime import breadth as _b

    slot = breadth_slot()
    if slot is None:
        return None, []
    from regime import moomoo_iv
    if not moomoo_iv.opend_alive():
        return None, []
    ts = _b.day_ms()
    key = f"breadth_{slot}_{ts}"
    if storage.get_meta(conn, key, ""):
        return None, []
    try:
        ctx = moomoo_iv.open_ctx()
    except Exception as e:  # noqa: BLE001
        return None, [f"breadth 连接: {e}"]
    try:
        row = _b.fetch_breadth(ctx)
        storage.upsert_breadth(conn, ts, slot, _b.SOURCE, row)
        storage.set_meta(conn, key, "1")
        eff = max((row["total"] or 0) - (row["flat"] or 0), 1)
        return (f"宽度[{slot}] 1D {row['up_1d']}/{row['denom']} "
                f"尾部 +{row['tail_up']}/-{row['tail_dn']} (有效 {eff})"), []
    except Exception as e:  # noqa: BLE001
        return None, [f"breadth {slot}: {e}"]
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001, S110
            pass


def sync_binance_opt_iv(conn) -> tuple:
    """币安期权近端 IV（XAU/XAG + BTC/ETH/SOL/BNB/XRP/DOGE，24/7）→ opt_iv_near。30 分钟节流。

    贵金属：GLD/SLV 代理（美股 RTH 才更新）给不了的夜间与周末隐波。
    加密：~3d 常数期限（总方差插值），持仓前端层（1-3 天持仓周期直接对应的定价）。
    分位待历史自积累（期限逐日漂移，tenor_days 随值落库）。
    """
    import time as _t

    from regime import binance_opt_iv

    if not _should(conn, "binance_opt_iv_last", 1800):
        return None, []
    errors, parts = [], []
    now = int(_t.time() * 1000)
    try:
        rows = binance_opt_iv.snapshot_all(now_ms=now)
    except Exception as e:  # noqa: BLE001
        return None, [f"binance_opt: {e}"]
    for row in rows:
        try:
            storage.upsert_opt_iv_near(conn, row)
            parts.append(f"{row['symbol'].split('-')[0]}={row['iv']:.1f}(~{row['tenor_days']}d)")
        except Exception as e:  # noqa: BLE001
            errors.append(f"binance_opt {row.get('symbol')}: {e}")
    return (f"币安近端IV: {' '.join(parts)}" if parts else None), errors


def sync_stock_iv_term(conn, symbols) -> tuple:
    """美股个股 IV 期限曲线（3d/9d/30d ATM）→ stock_iv_term。RTH 内每小时。

    期权链按 ET 日缓存（链盘中不变；到期 distance 每日在变故跨日失效）；
    盘中每轮只做批量快照（61 品种 ≈366 代码 ≈2 次请求）。
    """
    import time as _t

    from regime import moomoo_iv, stock_iv_term
    from regime.calendar_nyse import is_rth

    now = int(_t.time() * 1000)
    if not is_rth(now):
        return None, []
    if not moomoo_iv.opend_alive():
        return None, []
    # 节流由 stock_iv_term 单源提供：它与 BUILD_BATCH 共同决定单日建链容量，
    # 分开写必然漂移（旧值 3600 与 BUILD_BATCH=12 是按 61 个品种配的，
    # 扩容到 71 后容量不够、每天近 30 个品种拿不到曲线，无人察觉）。
    if not _should(conn, "stock_iv_term_last", stock_iv_term.BUILD_INTERVAL_S):
        return None, []
    us = [s for s in symbols if instruments.get(s)["class"] == "us_stock_perp"]
    if not us:
        return None, []
    # 容量自检：品种数增长会静默压垮这条管线（链按 ET 日失效，当天补不满就是当天缺）。
    # 心跳只看"最后落库年龄"，13/71 也是绿的——所以这里必须自己喊。
    cap = stock_iv_term.build_capacity(len(us))
    if not cap["ok"]:
        log.warning(
            "IV期限链容量不足：%d 品种需建链，单日容量约 %.0f（%d 轮 × 批 %d）"
            "——今日预计有约 %d 个品种拿不到期限曲线。请调大 BUILD_BATCH 或缩短 BUILD_INTERVAL_S",
            cap["need"], cap["capacity"], cap["rounds"], cap["batch"],
            max(int(cap["need"] - cap["capacity"]), 0),
        )
    try:
        ctx = moomoo_iv.open_ctx()
    except Exception as e:  # noqa: BLE001
        return None, [f"iv_term 连接: {e}"]
    try:
        # 增量补全：缓存不足（首轮为 None，或上轮撞限频只建了一部分）时补建，
        # 单轮上限 BUILD_BATCH。RTH 每小时一轮，几轮内补满 61 品种。
        cached = stock_iv_term.load_codes_cache(conn) or {}
        want_full = len(stock_iv_term.TARGETS)
        day_fails = stock_iv_term.load_build_fails(conn)
        blocked = stock_iv_term.blocked_symbols(day_fails)
        missing = [s for s in us
                   if len(cached.get(s) or {}) < want_full and s not in blocked]
        if missing:
            # 固定分组取代旧的"轮换起点"（2026-08-07 用户裁决）：品种 X 永远落在
            # 第 (index % BUILD_GROUPS) 组，何时建可预期、出问题可排查，且每轮
            # 请求量更小更均匀（约 7 品种 ≈28 次请求，旧写法 16 品种 ≈64 次）。
            # 轮次计数器先写 next：整轮异常也会前进，某一组不会永久饿死。
            round_key = "stock_iv_term_build_round"
            try:
                round_idx = int(storage.get_meta(conn, round_key, 0) or 0)
            except (TypeError, ValueError):
                round_idx = 0
            storage.set_meta(conn, round_key, str(round_idx + 1))
            group = [s for s in stock_iv_term.build_group(us, round_idx) if s in missing]
            # 确定性分组 + 机会补位：本组保证每个品种有可预期的槽位，但本组不足
            # 一批时必须用其余缺失品种补满，否则容量白白浪费——实测第0组只剩 4 个
            # 待建就只做 4 个，而同时还有 41 个在等、BUILD_BATCH 明明是 16。
            todo = list(group)
            if len(todo) < stock_iv_term.BUILD_BATCH:
                todo += [s for s in missing if s not in set(todo)][
                    : stock_iv_term.BUILD_BATCH - len(todo)]
            codes, fails = stock_iv_term.build_codes(
                ctx, todo, existing=cached, skip=blocked,
            )
            if codes:
                stock_iv_term.save_codes_cache(conn, codes)
            built_now = len(codes) - len(cached)
            # 失败记账**只在本轮有成功时**进行——整批全灭是限频而非品种问题，
            # 无脑记账会让一次限频把当轮品种全部拉黑一天，比不做退避更糟
            day_fails = stock_iv_term.count_fails(day_fails, fails, todo, built_now)
            for s in todo:
                if len(codes.get(s) or {}) >= want_full:
                    day_fails.pop(s, None)   # 建成即销账，不记恨偶发失败
            stock_iv_term.save_build_fails(conn, day_fails)
            done = sum(1 for s in us if len(codes.get(s) or {}) >= want_full)
            now_blocked = stock_iv_term.blocked_symbols(day_fails)
            log.info(
                "IV期限链构建 %d/%d 品种完整（第%d组 %d 个，本轮补 %d，待补 %d）%s%s",
                done, len(us), round_idx % stock_iv_term.BUILD_GROUPS, len(todo),
                built_now, len(us) - done - len(now_blocked),
                f"；当日跳过 {len(now_blocked)} 个（连续失败）: "
                + ", ".join(sorted(now_blocked)[:6]) if now_blocked else "",
                f"；失败 {len(fails)} 例: " + "; ".join(fails[:4]) if fails else "",
            )
        else:
            codes = cached
        if not codes:
            return None, ["iv_term: 代码表构建为空"]
        rows = stock_iv_term.fetch_term(ctx, codes, now_ms=now)
        single_legs = [
            f"{r['symbol']} {tenor}d"
            for r in rows for tenor, n in (r.get("legs") or {}).items() if n == 1
        ]
        if single_legs:
            log.warning("IV期限曲线 ATM 单腿退化（仅一侧有有效 IV）: %s",
                        ", ".join(single_legs))
        if rows:
            storage.upsert_stock_iv_term(conn, rows)
        inv = sum(1 for r in rows
                  if r.get("iv3") and r.get("iv30") and r["iv3"] > r["iv30"])
        return f"IV期限曲线 {len(rows)} 品种（3d>30d 倒挂 {inv} 个）", []
    except Exception as e:  # noqa: BLE001
        return None, [f"iv_term: {e}"]
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001, S110
            pass


def sync_moomoo_iv_live(conn, symbols) -> tuple:
    """盘中实时 IV 快照 → stock_vol_live（**每轮都跑，不节流**）。

    只在 RTH 内采集：盘外该值恒等于昨日结算值，存进去只是重复行。
    一次批量调用取回全部美股品种（3303 单次 ≤500 标的），成本≈1 次请求/轮。
    **与结算序列严格分表**——分位永远只在 stock_vol 上算，实时值只做预览。
    """
    import time as _t

    from regime import moomoo_iv
    from regime.calendar_nyse import is_rth

    now = int(_t.time() * 1000)
    if not is_rth(now):
        return None, []
    if not moomoo_iv.opend_alive():
        return None, []
    us = moomoo_iv.iv_symbols(symbols)   # 美股永续 + 配了 vol_proxy 的商品（XAU→GLD/XAG→SLV）
    if not us:
        return None, []
    try:
        ctx = moomoo_iv.open_ctx()
    except Exception as e:  # noqa: BLE001
        return None, [f"moomoo_live 连接: {e}"]
    try:
        rows = moomoo_iv.fetch_live(ctx, us, now_ms=now)
        if rows:
            storage.upsert_stock_vol_live(conn, moomoo_iv.SOURCE, rows)
        moved = sum(1 for r in rows
                    if r["iv"] is not None and r["pre_iv"] is not None
                    and abs(r["iv"] - r["pre_iv"]) > 0.01)
        return f"实时IV {len(rows)} 品种（{moved} 个已偏离昨收）", []
    except Exception as e:  # noqa: BLE001
        return None, [f"moomoo_live: {e}"]
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001, S110
            pass


def sync_moomoo_iv(conn, symbols) -> tuple:
    """个股 IV 日频增量（moomoo 口径）→ stock_vol。返回 (日志串或 None, 错误列表)。

    需本机 OpenD 常驻。**OpenD 不在就静默跳过**——它是可选增强而非必需依赖，
    不能因为用户没开网关就让整轮采集报错。日频数据每日只变一次，1h 节流足够；
    每次自库内最后一日往前重叠一天拉，覆盖当日可能的未定值。
    """
    from datetime import date, datetime, timedelta, timezone

    from regime import moomoo_iv

    if not moomoo_iv.opend_alive():
        return None, []
    us = moomoo_iv.iv_symbols(symbols)   # 美股永续 + 配了 vol_proxy 的商品（XAU→GLD/XAG→SLV）
    if not us:
        return None, []
    from zoneinfo import ZoneInfo as _ZI

    # "今天"必须取 ET 日历日：宿主机时区在 UTC+8/9 时 date.today() 比 ET 提前一天，
    # 财报权威窗与 prune 起点会跳到 ET 的明天，漏掉 ET 当日的新增/改期（审计 3 路发现）
    errors, today = [], datetime.now(_ZI("America/New_York")).date()
    try:
        ctx = moomoo_iv.open_ctx()
    except Exception as e:  # noqa: BLE001
        return None, [f"moomoo_iv 连接: {e}"]

    def incr(sym, getter, fetch, put, tag):
        """自库内最后一日重叠一天起拉，覆盖当日可能的未定值。返回写入行数。"""
        have = getter(conn, sym, moomoo_iv.SOURCE, limit=5)
        if have.empty:
            begin = moomoo_iv.DATA_FLOOR
        else:
            last = datetime.fromtimestamp(
                int(have["ts"].iloc[-1]) / 1000, tz=timezone.utc
            ).date()
            if last >= today:
                return 0
            begin = (last - timedelta(days=1)).isoformat()
        rows = moomoo_iv.settled_only(fetch(ctx, sym, begin, today.isoformat()))
        if rows:
            put(conn, sym, moomoo_iv.SOURCE, rows)
        return len(rows)

    n_iv = n_pc = 0
    try:
        for sym in us:
            try:
                n_iv += incr(sym, storage.get_stock_vol, moomoo_iv.fetch_history,
                             storage.upsert_stock_vol, "iv")
            except Exception as e:  # noqa: BLE001
                errors.append(f"moomoo_iv {sym}: {e}")
            try:
                # 期权流（put/call）纯采集，不进判定——历史易逝，先留住
                n_pc += incr(sym, storage.get_stock_option_stat,
                             moomoo_iv.fetch_option_stat,
                             storage.upsert_stock_option_stat, "pc")
            except Exception as e:  # noqa: BLE001
                errors.append(f"moomoo_optstat {sym}: {e}")
        # 财报日历：只补未来 90 天。**未来窗内以最新日历为权威**——改期后的旧日期
        # 会被清掉（否则旧行永久残留，继续污染邻近度与条件分位）；历史行不动。
        try:
            rows = moomoo_iv.fetch_earnings(
                ctx, us, today.isoformat(), (today + timedelta(days=90)).isoformat()
            )
            if rows:
                storage.upsert_earnings(conn, moomoo_iv.SOURCE, rows)
            t0 = moomoo_iv.day_ms(today)
            t1 = moomoo_iv.day_ms(today + timedelta(days=90))
            n_stale = storage.prune_stale_future_earnings(
                conn, moomoo_iv.SOURCE, us, t0, t1, rows or []
            )
            if n_stale:
                log.info("财报改期清理：删除 %d 条过期未来行", n_stale)
        except Exception as e:  # noqa: BLE001
            errors.append(f"moomoo_earnings: {e}")
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001, S110
            pass
    parts = []
    if n_iv:
        parts.append(f"IV {n_iv} 行")
    if n_pc:
        parts.append(f"期权流 {n_pc} 行")
    return (f"moomoo({'/'.join(parts)}) / {len(us)} 品种" if parts else None), errors


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


def snapshot_universe(conn, symbols) -> None:
    """point-in-time 成员快照（append-only）：instruments 配置变化时记一版。

    回测/相关矩阵按 snapped_at 取当时成员，杜绝"用今天的名单回填历史"
    （Shumway 幸存者偏差；预研 X1 治理件）。对账靠内容哈希，改 pool/theme
    即触发新版；绝不 UPDATE 旧行。
    """
    import hashlib
    rows = []
    hash_rows = []
    for sym in sorted(symbols):
        cfg = instruments.get(sym)
        rows.append((sym, cfg.get("pool") or "core",
                     json.dumps(cfg.get("theme") or [], ensure_ascii=False),
                     cfg.get("class"), cfg.get("valid_from")))
        # 哈希须覆盖**所有改变口径的字段**——审计实测：换数据源、换 vol_index、
        # 换 vol_proxy 都不触发新快照（旧哈希只看 pool/theme/class/valid_from）
        hash_rows.append(rows[-1] + (
            json.dumps(cfg.get("sources") or []), cfg.get("vol_index"),
            cfg.get("vol_proxy"), cfg.get("hl_coin"),
        ))
    digest = hashlib.sha256(
        json.dumps(hash_rows, sort_keys=True).encode()).hexdigest()[:16]
    if storage.get_meta(conn, "universe_hash") == digest:
        return
    now = int(time.time() * 1000)
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO universe_snapshot VALUES (?,?,?,?,?,?)",
            [(now, s, p, t, c, v) for s, p, t, c, v in rows])
    storage.set_meta(conn, "universe_hash", digest)
    log.info("宇宙快照已记录：%d 品种（hash %s）", len(rows), digest)


def sync_bbo(conn, symbols) -> int:
    """BBO 快照：一次 bookTicker 全量请求，落所有已注册品种的最优买卖价量。

    报价质量门槛（价差/深度）的原料——SEC 闪崩报告的教训是 ADV 不能替代
    报价质量（预研 X1）。每轮一行/品种，5 分钟粒度足够月度评审用。
    """
    import requests as _rq
    r = _rq.get("https://fapi.binance.com/fapi/v1/ticker/bookTicker", timeout=15)
    r.raise_for_status()
    keep = {s.replace("-", ""): s for s in symbols}
    now = int(time.time() * 1000)
    rows = []
    for t in r.json():
        sym = keep.get(t.get("symbol"))
        if sym:
            rows.append((sym, now, float(t["bidPrice"]), float(t["bidQty"]),
                         float(t["askPrice"]), float(t["askQty"])))
    with conn:
        conn.executemany("INSERT OR IGNORE INTO bbo VALUES (?,?,?,?,?,?)", rows)
    return len(rows)


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


REF_YAHOO = {"NDX": "^NDX", "QQQ": "QQQ", "MSTR": "MSTR", "COIN": "COIN",
             "GLD": "GLD", "GC": "GC=F", "CL": "CL=F", "VIX": "^VIX"}


def sync_reference(conn) -> None:
    """底层参考日线（耦合历史层）的日更：每 UTC 日一次，拉近 10 天补尾。

    全量回填走 scripts/backfill_reference.py；这里只维持新鲜。
    FRED 当前网络持续超时，日更仅覆盖价格层（BTC + Yahoo 八序列）。
    """
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if storage.get_meta(conn, "ref_last_sync") == today:
        return
    p1 = int(time.time() * 1000) - 10 * 86_400_000
    n = 0
    try:
        n += storage.upsert_ref_daily(
            conn, "BTC", fetch_binance_spot_daily(start_ms=p1), "binance_spot")
    except Exception as e:  # noqa: BLE001
        log.warning("ref BTC 日更失败: %s", e)
    for name, tk in REF_YAHOO.items():
        try:
            n += storage.upsert_ref_daily(
                conn, name, fetch_yahoo_daily(tk, p1_ms=p1), "yahoo")
            time.sleep(0.3)
        except Exception as e:  # noqa: BLE001
            log.warning("ref %s 日更失败: %s", name, e)
    storage.set_meta(conn, "ref_last_sync", today)
    log.info("参考日线日更完成：+%d 行", n)


def cycle(conn, symbols, timeframes, source_order) -> list:
    errors = []
    # 治理件：宇宙快照（配置变化才写）+ BBO 报价质量快照（每轮一批）
    try:
        snapshot_universe(conn, symbols)
    except Exception as e:  # noqa: BLE001
        errors.append(f"universe_snapshot: {e}")
    try:
        sync_bbo(conn, symbols)
    except Exception as e:  # noqa: BLE001
        errors.append(f"bbo: {e}")
    try:
        sync_reference(conn)
    except Exception as e:  # noqa: BLE001
        errors.append(f"ref_daily: {e}")
    for sym in symbols:
        session_aware = instruments.get(sym)["class"] == "us_stock_perp"
        for tf in timeframes:
            try:
                df_full, src = fetch_ohlcv(
                    sym, tf, sources=source_order, drop_unclosed=False
                )
                df = closed_ohlcv(df_full, tf)
                fetched_bars = len(df)
                has_live_bar = len(df_full) > len(df)
                # 序列下限（公司行动/合约重定价断点治理）：源端服务的是未复权
                # 原始历史，删掉的断点前段会被下一轮 fetch 重新灌回（CRWD 4:1
                # 重定价实测：4h 每轮拉 50 天、必够到旧口径段）。floor 之前的
                # 行一律不入库；设置＝手工清修时写 meta series_floor_{sym}
                floor = storage.get_meta(conn, f"series_floor_{sym}")
                if floor:
                    from regime.storage import ts_to_ms as _t2m
                    keep = [t >= int(floor) for t in _t2m(df["ts"])]
                    if not all(keep):
                        df = df[keep].reset_index(drop=True)
                storage.upsert_ohlcv(conn, sym, tf, df, src)
                # 只有理论收线时刻尚未来到时，末根才是 live bar。若源在休市期间
                # 只返回已收线历史，末根进入幂等 OHLCV upsert，不再被按位置误删。
                if has_live_bar:
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
                    # commit=False：与下方确认折叠合并成单事务（见 conn.commit() 处）
                    storage.upsert_states(conn, sym, tf, new_states, commit=False)
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
                # 非对称迟滞：对整条 raw 序列做确认折叠，把确认态写回 state 列。
                # v3：美股品种传事件窗（未来 10 日历日有财报），事件窗内
                # squeeze→趋势的确认 +1 根；无财报数据的品种 event_win=None（同 v2）
                rows_all = storage.get_states(conn, sym, tf, limit=100_000)
                ewin = None
                if instruments.get(sym)["class"] == "us_stock_perp":
                    ewin = storage.earnings_event_windows(
                        conn, sym, [r["ts"] for r in rows_all]
                    )
                    if not any(ewin):
                        ewin = None   # 无财报记录（ETF）：与 v2 完全一致
                confirmed, cand = confirm_states(
                    [r["raw_state"] for r in rows_all], event_win=ewin
                )
                fixes = [
                    (confirmed[i], rows_all[i]["ts"])
                    for i in range(len(rows_all))
                    if rows_all[i]["state"] != confirmed[i]
                ]
                if fixes:
                    storage.set_confirmed(conn, sym, tf, fixes, commit=False)
                # 与上面的 upsert_states(commit=False) 同一事务提交：消灭
                # "raw 已可见、确认未落"的中间窗口（审计 3 路独立发现）
                conn.commit()
                now_state = confirmed[-1] if confirmed else "?"
                log.info(
                    "%s %s src=%s quality=%s fetched=%d bars=%d states+%d 确认=%s%s%s",
                    sym, tf, src,
                    "healthy" if fetched_bars >= HEALTHY_BARS else "short",
                    fetched_bars, len(hist), len(new_states), now_state,
                    f" 原始={rows_all[-1]['raw_state']}" if rows_all and rows_all[-1]["raw_state"] != now_state else "",
                    f" 候选={cand['state']}({cand['count']}/{cand['need']})" if cand else "",
                )
            except Exception as e:  # noqa: BLE001
                errors.append(f"{sym} {tf}: {e}")
                log.warning("%s %s 失败: %s", sym, tf, e)

    # 币安 1h 量流（VWAP 专用量源，全品种统一取币安——量最大；与 OHLCV 主源解耦）。
    # 冷启动回填 VOL1H_TARGET 根（覆盖 1d 的 480h 窗 + 250 根分位参照期），
    # 之后按水位增量。失败不拖累其他采集。
    # setup 装配的**全局预算**：首轮水位为空时目标是全部历史，实测约 30 根/秒、
    # 85 品种 6.8 万根 ≈ 38 分钟，会把整个采集轮堵死（2026-08-07 实测把 RTH 的
    # IV 建链饿死过）。用全局根数预算把单轮耗时钉住，跨轮收敛；轮换起点保证
    # 预算用尽时靠后的品种下一轮优先，不会永久排不上。
    budget = {"bars": SETUP_BARS_PER_CYCLE}
    try:
        setup_start = int(storage.get_meta(conn, "setup_scan_offset", 0) or 0)
    except (TypeError, ValueError):
        setup_start = 0
    setup_order = symbols[setup_start % len(symbols):] + symbols[:setup_start % len(symbols)] \
        if symbols else []
    setup_done = 0
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
            log.info("个股 iv30(CBOE影子): %s", " ".join(iv_line))

    # 个股 IV 主线（moomoo 口径，日频）——OpenD 不在就静默跳过，不拖累整轮
    if _should(conn, "moomoo_iv_last_fetch", 3600):
        msg, errs = sync_moomoo_iv(conn, symbols)
        if msg:
            log.info("%s", msg)
        errors.extend(errs)

    # 币安期权近端 IV（XAU/XAG，24/7）：30 分钟节流
    msg, errs = sync_binance_opt_iv(conn)
    if msg:
        log.info("%s", msg)
    errors.extend(errs)

    # 个股 IV 期限曲线（3d/9d/30d）：RTH 内每小时，链日缓存 + 批量快照
    msg, errs = sync_stock_iv_term(conn, symbols)
    if msg:
        log.info("%s", msg)
    errors.extend(errs)

    # 盘中实时 IV：每轮都采（不节流），只在 RTH 内——一次批量调用取全部美股
    msg, errs = sync_moomoo_iv_live(conn, symbols)
    if msg:
        log.info("%s", msg)
    errors.extend(errs)

    # 市场宽度影子字段：零历史，须自攒约 2 年才够评审——每天不采都是永久损失
    msg, errs = sync_breadth(conn)
    if msg:
        log.info("%s", msg)
    errors.extend(errs)

    # setup 装配单独成段：它依赖本轮 1d/4h/1h OHLCV 与 vol1h 都已同步。
    for sym in setup_order:
        if budget["bars"] <= 0:
            break
        try:
            scanned, written = sync_setup_history(conn, sym, budget=budget)
            if scanned:
                setup_done += 1
                log.info("setup %s: 扫描+%d 根，关键区间+%d 行（本轮余额 %d）",
                         sym, scanned, written, budget["bars"])
        except Exception as e:  # noqa: BLE001
            errors.append(f"setup {sym}: {e}")
            log.warning("setup %s 失败: %s", sym, e)
    if setup_order:
        storage.set_meta(conn, "setup_scan_offset",
                         str((setup_start + max(setup_done, 1)) % len(setup_order)))

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


def sync_setup_history(conn, symbol: str, budget: dict | None = None) -> tuple[int, int]:
    """增量装配一个品种的 4h setup；返回（扫描 bar 数，落库 setup 数）。

    setup_history 故意不写 middle，因此不能把“表里没有该 ts”直接解释成未算。
    版本化扫描水位记录稀疏表之外的完成度；历史修订会在 storage 入口清掉水位，
    RULES/ASSEMBLE 任一升版则自然换一条新水位，从而重建而非混版。
    """
    rules_version = setup_tracking.RULES_GENERATION
    assemble_version = policy_assemble.ASSEMBLE_VERSION
    frame_4h = storage.get_ohlcv(
        conn, symbol, "4h", limit=10_000 + FEATURE_WINDOW - 1,
    )
    if not len(frame_4h):
        return 0, 0
    all_ts = storage.ts_to_ms(frame_4h["ts"])
    watermark = storage.setup_scan_watermark(
        conn, symbol, rules_version, assemble_version,
    )
    targets = [int(ts) for ts in all_ts if watermark is None or int(ts) > watermark]
    if not targets:
        return 0, 0
    # 全局预算裁剪：只取本轮还付得起的前 N 根。**必须按时间升序取前缀**——
    # walk-forward 的水位语义是"扫到这里为止"，取中间一段会让水位跳过未算的根。
    if budget is not None:
        room = max(int(budget.get("bars", 0)), 0)
        if room <= 0:
            return 0, 0
        targets = targets[:room]
        budget["bars"] = room - len(targets)
    # setup_history 里的已有行只有两个版本都匹配才可跳过。middle 没有行，仍会
    # 进入 missing_targets；扫描完成后由水位记住，下一轮不再重复计算。
    existing = storage.setup_ts_set(
        conn, symbol, rules_version, assemble_version,
    )
    missing_targets = [ts for ts in targets if ts not in existing]

    frames = {
        "4h": frame_4h,
        # 1h/1d 只供 cRSI 辅助票；400 根是 assemble 自己公开的统一上下文。
        "1h": storage.get_ohlcv(
            conn, symbol, "1h", limit=10_000 + FEATURE_WINDOW - 1,
        ),
        "1d": storage.get_ohlcv(
            conn, symbol, "1d", limit=10_000 + FEATURE_WINDOW - 1,
        ),
    }
    timelines = {
        tf: storage.get_states(conn, symbol, tf, limit=100_000)
        for tf in policy_assemble.POLICY_RESONANCE_TFS
    }
    vol1h = storage.get_vol1h(
        conn,
        symbol,
        since_ms=(
            min(targets) - setup_tracking.VOL1H_LOOKBACK_HOURS * 3_600_000
        ),
    )
    if missing_targets:
        processed, rows = setup_tracking.walk_forward(
            symbol,
            ohlcv_by_tf=frames,
            regime_by_tf=timelines,
            instrument=instruments.get(symbol),
            target_ts=missing_targets,
            vol1h=vol1h,
            rules_version=rules_version,
            assemble_version=assemble_version,
        )
    else:
        processed, rows = [], []
    written = storage.replace_setup_scan(
        conn,
        symbol,
        processed,
        rows,
        rules_version=rules_version,
        assemble_version=assemble_version,
        # targets 已按预算裁剪过，max 就是本轮真正扫到的位置——不能用全量的 max，
        # 否则被预算切掉的那些根会被水位永久跳过、再也不会补算。
        scanned_through=max(targets),
    )
    return len(targets), written


def _status_payload(interval: int, elapsed: float, errors: list) -> dict:
    """采集循环健康摘要；品种全集只在启动日志记录，不重复塞入每轮 meta。"""
    return {
        "interval": interval,
        "cycle_sec": round(elapsed, 1),
        "errors": errors[-10:],
    }


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
            storage.set_meta(
                conn, "status",
                json.dumps(_status_payload(args.interval, elapsed, errors), ensure_ascii=False),
            )
            log.info("本轮完成 %.1fs 错误=%d 库=%s", elapsed, len(errors), storage.counts(conn))
            if args.once:
                break
            time.sleep(max(5.0, args.interval - elapsed))
    except KeyboardInterrupt:
        log.info("收到中断，采集器退出")


if __name__ == "__main__":
    main()
