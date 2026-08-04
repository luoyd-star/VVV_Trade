#!/usr/bin/env python3
"""可视化面板服务：只读 SQLite（collector 负责写入），标准库 HTTP server，零新增依赖。

  .venv/bin/python dashboard.py --port 8787

前端在 web/，图表库 ECharts 走 CDN（CDN 不可达时表格部分仍可用）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from regime import instruments, storage
from regime.agent import chat as agent_chat
from regime.agent import load_config as agent_config
from regime.agent import system_is_custom
from regime.classify import (
    FEATURE_WINDOW, STATES, WARMUP_BARS, analyze_timeframe, confirm_states,
)
from regime.features.crsi import crsi_features
from regime.features.structure import ema, swing_pivots
from regime.features.utils import pct_rank, rolling_pct_rank
from regime.features.volatility import atr, bb_width, realized_vol
from regime.features.vwap import WIN_HOURS as VWAP_WIN_HOURS
from regime.features.vwap import rolling_vwap, vwap_payload

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(ROOT, "web")
CANDLES = 240
TIMEFRAMES = ("1d", "4h", "1h")
TF_SEC = {"1h": 3600, "4h": 14_400, "1d": 86_400}
# WARMUP_BARS 移入 regime/classify.py——a8 起逐行审计与面板角标共用同一定义

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _deep_clean(obj):
    """numpy 标量转原生类型、NaN/Inf 转 None，保证 JSON 可序列化。"""
    if isinstance(obj, dict):
        return {k: _deep_clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_deep_clean(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.floating):
        obj = float(obj)
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def _series(arr):
    out = []
    for v in arr:
        f = float(v)
        out.append(round(f, 6) if math.isfinite(f) else None)
    return out


def _tf_payload(conn, symbol: str, tf: str):
    df = storage.get_ohlcv(conn, symbol, tf, limit=1200)
    if len(df) < 90:  # 与采集端一致；90~280 根之间由 warmup 标志提示质量
        return None
    session_aware = instruments.get(symbol)["class"] == "us_stock_perp"
    # 与 collector 的 walk-forward 用同一个窗长——否则历史攒过 FEATURE_WINDOW 根后，
    # 同一根 bar 的面板读数与库内审计快照会分叉（atr_rank / atr_ds 对窗长敏感）
    feat = df.iloc[-FEATURE_WINDOW:]
    reg = analyze_timeframe(feat, tf, session_aware=session_aware)

    win = df.tail(CANDLES).reset_index(drop=True)
    offset = len(df) - len(win)
    ts_ms = storage.ts_to_ms(win["ts"])
    candles = [
        [t, float(o), float(h), float(l), float(c), float(v)]
        for t, o, h, l, c, v in zip(
            ts_ms, win["open"], win["high"], win["low"], win["close"], win["volume"]
        )
    ]

    ema50 = _series(ema(df["close"], 50).tail(len(win)))
    piv = swing_pivots(win)
    pivots = [
        {"i": int(r.idx), "kind": r.kind, "price": float(r.price)}
        for r in piv.itertuples()
    ]
    # 分位曲线同样在 FEATURE_WINDOW 内算，与上面的 reg 及库内审计口径一致
    foff = len(df) - len(feat)
    atr_ranks = _series(rolling_pct_rank(atr(feat) / feat["close"], 250)[offset - foff:])
    bbw_ranks = _series(rolling_pct_rank(bb_width(feat["close"]), 250)[offset - foff:])

    # 状态历史 -> 与 K 线窗口对齐的连续段（state 列已是迟滞确认态）
    states = storage.get_states(conn, symbol, tf, limit=1500)
    confirmed_now = states[-1]["state"] if states else reg.state
    raw_now = states[-1]["raw_state"] if states else reg.state
    candidate = None
    if states:
        _, candidate = confirm_states([r["raw_state"] for r in states])
    ts_to_idx = {t: i for i, t in enumerate(ts_ms)}
    segments = []
    for srow in states:
        i = ts_to_idx.get(srow["ts"])
        if i is None:
            continue
        if segments and segments[-1]["state"] == srow["state"] and segments[-1]["e"] == i - 1:
            segments[-1]["e"] = i
        else:
            segments.append({"s": i, "e": i, "state": srow["state"]})

    # VWAP 偏离（币安 1h 量流，显示/上下文层）：在全量 df 上算（分位要参照期），
    # 序列切到显示窗对齐。量流缺失/覆盖不足 → None/NaN，宁缺毋滥。
    vwap_block = None
    try:
        w_h = VWAP_WIN_HOURS[tf]
        all_ts = storage.ts_to_ms(df["ts"])
        vol = storage.get_vol1h(conn, symbol,
                                since_ms=int(all_ts[0]) - w_h * 3_600_000)
        if len(vol) >= 48:
            closes_ms = [int(t) + TF_SEC[tf] * 1000 for t in all_ts]
            vw = rolling_vwap(closes_ms, vol["ts"].astype("int64"),
                              vol["volume"], vol["quote_vol"], w_h)
            atr_full = atr(df).to_numpy()
            dev, dev_last, dev_rank = vwap_payload(df["close"], atr_full, vw)
            vwap_block = {
                "series": _series(vw[offset:]),
                "dev_series": _series(dev[offset:]),
                "dev": round(dev_last, 2) if dev_last is not None else None,
                "dev_rank": dev_rank,
                "win_hours": w_h,
            }
    except Exception:  # noqa: BLE001  展示层指标绝不拖垮面板主体
        vwap_block = None

    crsi = crsi_features(df)
    crsi_payload = {
        "crsi": _series(crsi["series"]["crsi"][offset:]),
        "db": _series(crsi["series"]["db"][offset:]),
        "ub": _series(crsi["series"]["ub"][offset:]),
        "divs": [
            {"i": d["i"] - offset, "kind": d["kind"]}
            for d in crsi["divergences"]
            if d["i"] >= offset
        ],
        "last": crsi["last"],
        "last_divergence": crsi["last_divergence"],
    }

    # 滚动预览：已收盘历史 + 形成中的 live bar 重算一次状态。
    # 只作预警展示，明确标注"未收线"，永不写入 regime_history。
    preview = None
    live = storage.get_live_bar(conn, symbol, tf)
    if live and live["ts"] and int(live["ts"]) > int(ts_ms[-1]):
        live_age = time.time() - live["fetched_at"] / 1000
        if live_age < 900:  # 采集正常（15 分钟内）才展示预览
            live_row = pd.DataFrame([{
                "ts": pd.to_datetime(int(live["ts"]), unit="ms", utc=True),
                "open": live["open"], "high": live["high"],
                "low": live["low"], "close": live["close"],
                "volume": live["volume"],
            }])
            regp = analyze_timeframe(
                pd.concat([feat.iloc[-(FEATURE_WINDOW - 1):], live_row], ignore_index=True),
                tf, session_aware=session_aware,
            )
            preview = {
                "state": regp.state,
                "label": regp.label,
                "confidence": regp.confidence,
                "bar_ts": int(live["ts"]),
                "close": float(live["close"]),
                "age_sec": int(live_age),
            }

    # 数据健康：预热（历史不足，分位仅供参考）与陈旧（最后收线太久，状态不可信）。
    # ohlcv.ts 是 K 线**开盘**时刻，所以要加一个周期才是收线时刻——原先直接用
    # 开盘 ts 算，恒定多报一整个周期（1d 上把 6.6 小时说成 30.6 小时）。
    # 阈值必须同步从 2.5×TF 收紧到 1.5×TF：old_age = new_age + TF，
    # old_age > 2.5×TF ⟺ new_age > 1.5×TF，判定边界完全不变（已逐分钟扫描验证）。
    last_close_age = time.time() - (int(ts_ms[-1]) / 1000 + TF_SEC[tf])
    health = {
        "warmup": bool(len(df) < WARMUP_BARS),
        "stale": bool(last_close_age > 1.5 * TF_SEC[tf]),
        "bars": int(len(df)),
        "last_close_age_min": round(last_close_age / 60),
    }

    return {
        "source": storage.last_source(conn, symbol, tf),
        "health": health,
        "candles": candles,
        "ema50": ema50,
        "pivots": pivots,
        "atr_rank_series": atr_ranks,
        "bbw_rank_series": bbw_ranks,
        "segments": segments,
        "crsi": crsi_payload,
        "vwap": vwap_block,
        "state": confirmed_now,
        "state_label": STATES.get(confirmed_now, confirmed_now),
        "raw_state": raw_now,
        "candidate": candidate,
        "preview": preview,
        "confidence": reg.confidence,
        "features": reg.features,
    }


def _dvol_payload(conn, symbol: str):
    base = symbol.upper().replace("/", "-").split("-")[0]
    if base not in ("BTC", "ETH"):
        return None
    dv = storage.get_dvol(conn, base, limit=730)
    if not len(dv):
        return None
    iv_pairs = [
        [int(t), round(float(v), 2)]
        for t, v in zip(storage.ts_to_ms(dv["ts"]), dv["dvol"])
    ]
    d1 = storage.get_ohlcv(conn, symbol, "1d", limit=1200)
    rv = realized_vol(d1["close"], 30, 365) * 100
    rv_pairs = [
        [int(t), round(float(v), 2)]
        for t, v in zip(storage.ts_to_ms(d1["ts"]), rv)
        if math.isfinite(float(v))
    ]
    iv_last = float(dv["dvol"].iloc[-1])
    rv_last = float(rv.dropna().iloc[-1]) if rv.notna().any() else None
    return {
        "iv": iv_pairs[-365:],
        "rv": rv_pairs[-365:],
        "iv_last": round(iv_last, 1),
        "iv_rank": round(pct_rank(dv["dvol"], 365), 3),
        "rv_last": round(rv_last, 1) if rv_last is not None else None,
        "spread": round(iv_last - rv_last, 1) if rv_last is not None else None,
    }


def _usvol_payload(conn, symbol: str):
    """美股波动率维度：CBOE 指数 IV（VXN/VIX）+ 自算 RV30 + 自采个股 iv30 + 期限结构。

    个股 iv30 无免费历史源，只能自采积累——iv30_days 告诉前端攒了多久，
    攒够约 20 个观测后 deriv 卡的 iv30 分位才开始有意义。
    """
    inst = instruments.get(symbol)
    if inst["class"] != "us_stock_perp":
        return None
    idx = inst.get("vol_index") or "VIX"
    dfv = storage.get_usvol(conn, idx, limit=800)
    if not len(dfv):
        return None
    idx_last = float(dfv["close"].iloc[-1])
    # usvol 只存交易日（约 252/年），分位窗口取 252 行才真是"一年"；
    # 图上多画一些（365 行 ≈ 1.4 年）看趋势，但分位不跟着放宽。
    tail = dfv["close"].tail(252)
    series = [[int(t), round(float(c), 2)] for t, c in zip(dfv["ts"], dfv["close"])][-365:]

    # RV30 与加密 DVOL 卡同口径（1d 收盘年化），同图对照 IV-RV 剪刀差
    rv_pairs, rv_last = [], None
    d1 = storage.get_ohlcv(conn, symbol, "1d", limit=1200)
    if len(d1) >= 40:
        rv = realized_vol(d1["close"], 30, 365) * 100
        rv_pairs = [
            [int(t), round(float(v), 2)]
            for t, v in zip(storage.ts_to_ms(d1["ts"]), rv)
            if math.isfinite(float(v))
        ][-365:]
        rv_last = rv_pairs[-1][1] if rv_pairs else None

    dd = storage.get_deriv(conn, symbol, limit=4000)
    iv30_last, iv30_days = None, 0.0
    if len(dd) and "iv30" in dd.columns:
        s = dd[["ts", "iv30"]].dropna()
        if len(s):
            iv30_last = round(float(s["iv30"].iloc[-1]), 1)
            iv30_days = round(
                (int(s["ts"].iloc[-1]) - int(s["ts"].iloc[0])) / 86_400_000, 1
            )

    # 期限结构：VIX9D/VIX3M > 1 = 近端恐慌（倒挂），< 0.9 = 平静升水
    ts_ratio = None
    d9 = storage.get_usvol(conn, "VIX9D", limit=5)
    d3 = storage.get_usvol(conn, "VIX3M", limit=5)
    if len(d9) and len(d3) and float(d3["close"].iloc[-1]) > 0:
        ts_ratio = round(float(d9["close"].iloc[-1]) / float(d3["close"].iloc[-1]), 3)

    return {
        "index": idx,
        "index_last": round(idx_last, 2),
        "index_rank": round(float((tail < idx_last).mean()), 3),
        "series": series,
        "rv": rv_pairs,
        "rv_last": rv_last,
        "spread": round(idx_last - rv_last, 1) if rv_last is not None else None,
        "iv30_last": iv30_last,
        "iv30_days": iv30_days,
        "ts_ratio": ts_ratio,
    }


def _hourly(col_df):
    """5m 快照与 1h 回填混在同一列时，重采样到 1h 格（每小时取末值）再作统计。

    不重采样的话，分位的分母会随运行时间从"1h 样本"漂成"5m 样本为主"——
    同一个百分位在系统跑两周前后不是同一个统计量。
    """
    if not len(col_df):
        return col_df
    g = col_df.copy()
    g["hb"] = g["ts"] // 3_600_000
    g = g.groupby("hb").last().reset_index(drop=True)
    return g


def _deriv_payload(conn, symbol: str):
    """持仓/杠杆维度（Binance 永续）。

    每个指标**独立取窗、独立算跨度与分位**——deriv 是稀疏宽表，四种时间格
    （funding 8h 结算 / OI·premium·taker 1h 回填 / 快照 5m / iv30 30min）混存，
    任何"整表"统计都会让最长的列替最短的列背书（iv30 攒了 1 天却顶着
    funding 的 168 天跨度显示"不预热"），或让高频列挤掉低频列的历史。
    """
    # oi/premium/taker/iv30 用 20000 行窗口（5m 满勤 ≈69 天，_hourly 再统一口径）；
    # funding 的结算行在 SQL 层按整点网格分流（LIMIT 只对结算行计数——否则
    # 每 5 分钟一行的快照预测值会把结算史重新挤出窗口，枯竭只是被推迟）。
    cols = {}
    for c in ("oi", "premium", "taker_ratio", "iv30"):
        cols[c] = storage.get_deriv_col(conn, symbol, c, limit=20_000)
    cols["funding"] = storage.get_deriv_col(conn, symbol, "funding", limit=50)
    f_settled = storage.get_deriv_col(conn, symbol, "funding", limit=1100, hourly_grid=True)
    if not any(len(v) for v in cols.values()):
        return None

    def span_of(cdf):
        if len(cdf) < 2:
            return 0.0
        return round((int(cdf["ts"].iloc[-1]) - int(cdf["ts"].iloc[0])) / 86_400_000, 1)

    def last_of(cdf, col):
        return (float(cdf[col].iloc[-1]), int(cdf["ts"].iloc[-1])) if len(cdf) else (None, None)

    def rank_of(series, current, span_days_):
        """分位可用的门槛：≥20 个观测 且 跨度 ≥7 天——1 天攒出的 26 个点
        算出来的"分位"没有任何历史含义，宁缺毋滥。"""
        s = series.dropna()
        if len(s) < 20 or span_days_ < 7 or current is None:
            return None
        return round(float((s < current).mean()), 3)

    oi_h = _hourly(cols["oi"])
    prem_h = _hourly(cols["premium"])
    taker_h = _hourly(cols["taker_ratio"])
    iv30_h = _hourly(cols["iv30"])

    oi, oi_ts = last_of(cols["oi"], "oi")
    funding_pred, _ = last_of(cols["funding"], "funding")
    funding_settled, _ = last_of(f_settled, "funding")
    premium, _ = last_of(cols["premium"], "premium")
    taker, _ = last_of(cols["taker_ratio"], "taker_ratio")
    iv30, _ = last_of(cols["iv30"], "iv30")

    spans = {
        "oi": span_of(cols["oi"]),
        "funding": span_of(f_settled),
        "premium": span_of(cols["premium"]),
        "taker": span_of(cols["taker_ratio"]),
        "iv30": span_of(cols["iv30"]),
    }

    def oi_change(hours):
        if oi is None or oi_ts is None or not len(cols["oi"]):
            return None
        past = cols["oi"][cols["oi"]["ts"] <= oi_ts - hours * 3_600_000]
        if not len(past):
            return None
        base = float(past["oi"].iloc[-1])
        return round(math.log(oi / base), 4) if base > 0 else None

    # 结算间隔由 collector 从 fundingInfo 接口取回存进 meta，面板只读库不打网络
    interval_h = float(storage.get_meta(conn, f"funding_interval_{symbol}", 8.0) or 8.0)
    per_year = 365 * 24.0 / interval_h if interval_h else 3 * 365.0

    return {
        "oi": oi,
        "oi_change_4h": oi_change(4),
        "oi_change_24h": oi_change(24),
        "oi_rank": rank_of(oi_h["oi"], oi, spans["oi"]),
        # 预测值与结算值分字段：两者是不同的量（预测=下一次的 lastFundingRate，
        # 结算=上一次真实收取的费率），分位算的是**结算值**在结算分布中的位置。
        # 挂在一起显示会让人把分位读成预测值的分位——实测 AAPL 预测 0 配分位
        # 0.905，而 0 在同一分布里其实只在 0.059。
        "funding_pct": round(funding_pred * 100, 5) if funding_pred is not None else None,
        "funding_annual_pct": round(funding_pred * per_year * 100, 1) if funding_pred is not None else None,
        "funding_interval_h": interval_h,
        "funding_settled_pct": round(funding_settled * 100, 5) if funding_settled is not None else None,
        "funding_rank": rank_of(f_settled["funding"], funding_settled, spans["funding"]),
        "premium_pct": round(premium * 100, 4) if premium is not None else None,
        "premium_rank": rank_of(prem_h["premium"], premium, spans["premium"]),
        "taker_ratio": round(taker, 3) if taker is not None else None,
        "taker_rank": rank_of(
            taker_h["taker_ratio"],
            float(taker_h["taker_ratio"].iloc[-1]) if len(taker_h) else None,
            spans["taker"],
        ),
        "iv30": round(iv30, 1) if iv30 is not None else None,
        "iv30_rank": rank_of(iv30_h["iv30"], iv30, spans["iv30"]),
        "spans": spans,
        "span_days": spans["oi"],          # 兼容旧前端字段：持仓图的核心序列是 OI
        "warmup": bool(spans["oi"] < 21),  # 预热指"持仓维度"（OI 侧），逐指标看 spans
        "oi_series": [
            [int(t), round(float(v), 1)] for t, v in zip(oi_h["ts"], oi_h["oi"])
        ][-500:],
        "funding_series": [
            [int(t), round(float(v) * 100, 5)]
            for t, v in zip(f_settled["ts"], f_settled["funding"])
        ][-500:],
    }


def _flips(conn, symbol: str, tfs):
    flips = []
    for tf in tfs:
        states = storage.get_states(conn, symbol, tf, limit=1500)
        for prev, curr in zip(states, states[1:]):
            if curr["state"] != prev["state"]:
                flips.append(
                    {
                        "ts": curr["ts"],
                        "tf": tf,
                        "from": prev["state"],
                        "to": curr["state"],
                        "confidence": curr["confidence"],
                    }
                )
    flips.sort(key=lambda x: -x["ts"])
    return flips[:40]


def _collector_info(conn):
    try:
        status = json.loads(storage.get_meta(conn, "status", "{}") or "{}")
    except json.JSONDecodeError:
        status = {}
    log_tail = []
    log_path = os.path.join(storage.DATA_DIR, "collector.log")
    if os.path.exists(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as f:
            log_tail = [line.rstrip("\n") for line in f.readlines()[-25:]]
    return {
        "last_run": int(storage.get_meta(conn, "last_run", 0) or 0),
        "interval": status.get("interval", 300),
        "cycle_sec": status.get("cycle_sec"),
        "errors": status.get("errors", []),
        "counts": storage.counts(conn),
        "log_tail": log_tail,
    }


def _instrument_payload(symbol: str) -> dict:
    """品种元信息；美股永续附带标的正股是否盘中（9:30-16:00 ET，工作日；不含假日历）。"""
    inst = instruments.get(symbol)
    market_open = None
    if inst["class"] == "us_stock_perp":
        now_et = datetime.now(ZoneInfo("America/New_York"))
        market_open = bool(
            now_et.weekday() < 5
            and (9, 30) <= (now_et.hour, now_et.minute) < (16, 0)
        )
    return {
        "class": inst["class"],
        "display": inst.get("display"),
        "market_open": market_open,
    }


def build_dashboard(symbol: str) -> dict:
    conn = storage.connect_ro()
    try:
        tfs_payload = {}
        for tf in TIMEFRAMES:
            payload = _tf_payload(conn, symbol, tf)
            if payload:
                tfs_payload[tf] = payload
        deriv = _deriv_payload(conn, symbol)

        # 数据健康汇总：问题清单直接给面板横幅与 VVVhermes
        issues = []
        for tf, t in tfs_payload.items():
            h = t["health"]
            if h["stale"]:
                issues.append(f"{tf} 数据陈旧（最后收线 {h['last_close_age_min']} 分钟前）")
            if h["warmup"]:
                issues.append(f"{tf} 预热中（仅 {h['bars']} 根，分位参照期不足）")
        if deriv and deriv.get("warmup"):
            issues.append(f"持仓数据预热中（仅 {deriv['span_days']} 天，分位仅供参考）")

        return _deep_clean(
            {
                "symbol": symbol,
                "instrument": _instrument_payload(symbol),
                "states_map": STATES,
                "tfs": tfs_payload,
                "dvol": _dvol_payload(conn, symbol),
                "usvol": _usvol_payload(conn, symbol),
                "deriv": deriv,
                "health": {"issues": issues},
                "flips": _flips(conn, symbol, tfs_payload.keys()),
                "collector": _collector_info(conn),
            }
        )
    finally:
        conn.close()


_COUPLING_CACHE = {"at": 0.0, "payload": None}


def coupling_payload():
    """跨资产耦合雷达（M3）：pair 状态 + 块三票 + 三面板全局量。5 分钟缓存
    ——FSM 全史重放约 1-2s，不能每请求算。诊断输出，阈值代随行。"""
    now = time.time()
    if _COUPLING_CACHE["payload"] is not None and now - _COUPLING_CACHE["at"] < 300:
        return _COUPLING_CACHE["payload"]
    from regime import coupling
    from regime.coupling_fsm import (
        THRESHOLD_VERSION, block_votes, pair_rho_series, run_pair_fsm,
    )
    out = {"updated_at": int(now * 1000), "threshold_version": THRESHOLD_VERSION,
           "panels": {}, "pairs": [], "blocks": []}
    conn = storage.connect_ro()
    try:
        for panel in ("all247", "usrth", "cross"):
            syms = coupling.panel_members(conn, panel)
            r = coupling.panel_returns(conn, syms, panel)
            if r.empty or len(r.columns) < 2:
                continue
            z, _ = coupling.ewma_vol_standardize(r)
            C = coupling.lw_shrink_corr(z)
            g = (coupling.global_stats(C, coupling.theme_blocks(list(C.columns)))
                 if C is not None else None)
            t = coupling.pair_table(r)
            out["panels"][panel] = {
                "n_symbols": len(r.columns), "n_rows": len(r),
                "status_counts": t["status"].value_counts().to_dict(),
                "global": ({"market_mode": g["market_mode"],
                            "mean_corr": g["mean_corr"],
                            "dispersion": g["dispersion"],
                            "blocks": g["blocks"]} if g else None),
            }
            if panel != "all247":
                continue  # pair FSM 目前只有 all247 有资格对
            pair_states = {}
            for _, row in t[t.status == "ELIGIBLE"].iterrows():
                ser = pair_rho_series(z[row.a], z[row.b])
                states, events = run_pair_fsm(ser)
                cur = str(states.iloc[-1])
                pair_states[tuple(sorted((row.a, row.b)))] = cur
                out["pairs"].append({
                    "a": row.a, "b": row.b, "state": cur,
                    "rho_fast": row.rho_fast, "rho_slow": row.rho_slow,
                    "c": row.c, "dz": row.dz,
                    "last_event": (events[-1]["reason"] if events else None),
                })
            bv = block_votes(z, coupling.theme_blocks(list(r.columns)), pair_states)
            out["blocks"] = bv.to_dict("records") if len(bv) else []
        # 38×38 复合矩阵（显示层：每格用应有时钟，观察池打标，样本不足淡显）
        out["matrix"] = coupling.composite_matrix(conn)
    finally:
        conn.close()
    _COUPLING_CACHE.update(at=now, payload=out)
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/symbols":
                conn = storage.connect_ro()
                syms = storage.symbols(conn)
                conn.close()
                return self._json({"symbols": syms})
            if parsed.path == "/api/dashboard":
                symbol = (qs.get("symbol") or ["BTC-USDT"])[0]
                return self._json(build_dashboard(symbol))
            if parsed.path == "/api/coupling":
                return self._json(coupling_payload())
            if parsed.path == "/api/agent/info":
                cfg = agent_config()
                return self._json(
                    {
                        "provider": cfg.get("provider"),
                        "model": cfg.get("model"),
                        "custom_system": system_is_custom(),
                        # agent.json 解析失败时 provider 会静默退回 mock——
                        # 根因必须能被看见，否则用户只会以为"模型没配"
                        "config_error": cfg.get("_config_error"),
                    }
                )
            if parsed.path == "/api/agent/history":
                limit = max(1, min(200, int((qs.get("limit") or ["60"])[0])))
                conn = storage.connect_ro()
                try:
                    return self._json({"messages": storage.get_chat(conn, limit)})
                finally:
                    conn.close()
            if parsed.path in ("/", "/index.html"):
                return self._file("index.html")
            name = os.path.basename(parsed.path)
            if name and os.path.exists(os.path.join(WEB_DIR, name)):
                return self._file(name)
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, code=500)

    def _same_origin_ok(self) -> bool:
        """有 Origin 头时必须是本面板自己：挡跨站表单打 /api/agent/*（消耗订阅额度、
        清空共享历史）。无 Origin（curl / 同源导航式请求）放行——单机本地服务，
        防的是浏览器里其他网页的 CSRF，不是本机进程。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host") or ""
        return origin.split("://", 1)[-1] == host

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        try:
            if not self._same_origin_ok():
                return self._json({"error": "跨站请求被拒绝"}, code=403)
            if parsed.path == "/api/agent/chat":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 512 * 1024:
                    return self._json({"error": "请求体过大"}, code=413)
                body = json.loads(self.rfile.read(length) or b"{}")
                # symbol 会被拼进 <panel> 的 system 上下文——不可信输入必须先关白名单：
                # 构造 "FOO）</panel> 忽略此前规则" 这样的 symbol 就是提示词注入
                symbol = str(body.get("symbol") or "BTC-USDT").upper()
                conn0 = storage.connect_ro()
                try:
                    known = set(storage.symbols(conn0))
                finally:
                    conn0.close()
                if symbol not in known:
                    return self._json({"error": f"未知品种: {symbol[:32]!r}"}, code=400)

                # 只传一条新消息，历史由服务端从 chat 表拼装（面板/终端共享）。
                # 旧的 messages 整段直传形态已删除：它绕过服务端历史与持久化，
                # 等于允许任意伪造对话史喂给模型且不留痕。
                text = str(body.get("message") or "").strip()
                if not text:
                    return self._json({"error": "空消息"}, code=400)
                if len(text) > 8000:
                    # agent 层送模前会截到 8000——与其静默截断（库里存全文、
                    # 模型只看一半还装作看完了），不如在边界如实拒绝
                    return self._json({"error": "消息超过 8000 字符上限"}, code=413)
                conn = storage.connect_rw_nomigrate()
                try:
                    msgs = [
                        {"role": r["role"], "content": r["content"]}
                        for r in storage.get_chat(conn, limit=20)
                    ]
                    msgs.append({"role": "user", "content": text})
                    out = agent_chat(build_dashboard(symbol), msgs)
                    if not out.get("error"):
                        storage.add_chat(conn, "user", text)
                        storage.add_chat(conn, "assistant", out["reply"])
                finally:
                    conn.close()
                return self._json(out)
            if parsed.path == "/api/agent/clear":
                conn = storage.connect_rw_nomigrate()
                try:
                    storage.clear_chat(conn)
                finally:
                    conn.close()
                return self._json({"ok": True})
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, code=500)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name):
        with open(os.path.join(WEB_DIR, name), "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header(
            "Content-Type", MIME.get(os.path.splitext(name)[1], "application/octet-stream")
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默访问日志
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="市场状态面板")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"面板已启动: http://{args.host}:{args.port}  （Ctrl+C 退出）")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
