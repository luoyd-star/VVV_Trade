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

from regime import instruments, moomoo_iv, storage
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
        # v3：美股品种传事件窗——与 collector 的确认路径**必须同参**，否则面板
        # 候选的 need 与库内确认逻辑不一致（显示 2/2 却迟迟不切）
        ewin = None
        if instruments.get(symbol).get("class") == "us_stock_perp":
            ewin = storage.earnings_event_windows(
                conn, symbol, [r["ts"] for r in states]
            )
            if not any(ewin):
                ewin = None
        _, candidate = confirm_states([r["raw_state"] for r in states], event_win=ewin)
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

    # margin 由 analyze_timeframe 的当根原始规则树产出；确认层只折叠 state，
    # 不会重算这项影子读数。仅在 dashboard 展示副本中补口径，不改变 a8 入库键集。
    display_features = dict(reg.features)
    display_features["margin_basis"] = "raw"

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
        "features": display_features,
    }


def _xopt_block(conn, symbol: str):
    """币安期权近端 IV（1-3 天期限，24/7）。**持仓前端层**：DVOL/IV30 是 30 天口径，
    对 1-3 天持仓"太深"；近端期限与持仓周期天然匹配。2 小时新鲜度闸（4 个采集周期），
    method/n_expiries 必须出到消费方——nearest+单到期时读数对单点 mark 极敏感。"""
    xdf = storage.get_opt_iv_near(conn, symbol, limit=1)
    if not len(xdf) or xdf["iv"].iloc[-1] != xdf["iv"].iloc[-1]:
        return None
    r = xdf.iloc[-1]
    age_ms = int(time.time() * 1000) - int(r["ts"])
    if age_ms > 2 * 3600_000:
        return None
    return {"iv": round(float(r["iv"]), 2),
            "tenor_days": float(r["tenor_days"]),
            "method": r["method"],
            "n_expiries": int(r["n_expiries"]) if r["n_expiries"] == r["n_expiries"] else None,
            "age_min": round(age_ms / 60_000),
            "captured_at": int(r["ts"])}


def _rv3_pairs(conn, symbol: str):
    """3 天已实现波动率：72×1h 收盘，年化 24×365。持仓前端层的 RV 对照——
    RV30 对 1-3 天持仓"太慢"，72 根 1h 窗与 3d IV 期限对齐。
    美股永续周末价格近似冻结，含周末的窗会被稀释——与本卡 RV30 同一已知口径。"""
    h1 = storage.get_ohlcv(conn, symbol, "1h", limit=2472)
    if len(h1) < 80:
        return [], None
    rv = realized_vol(h1["close"], 72, 24 * 365) * 100
    pairs = [
        [int(t), round(float(v), 2)]
        for t, v in zip(storage.ts_to_ms(h1["ts"]), rv)
        if math.isfinite(float(v))
    ][-2400:]
    return pairs, (pairs[-1][1] if pairs else None)


def _dvol_payload(conn, symbol: str):
    """加密波动率卡：DVOL（BTC/ETH，30 天制度层）+ 币安近端 IV（6 币，持仓前端层）。

    SOL/BNB/XRP/DOGE 无 DVOL，但有近端 IV 与 RV30——同样出卡，iv 序列为空。
    """
    base = symbol.upper().replace("/", "-").split("-")[0]
    if instruments.get(symbol).get("class") != "crypto":
        return None
    xopt = _xopt_block(conn, symbol)
    dv = storage.get_dvol(conn, base, limit=730) if base in ("BTC", "ETH") else []
    if not len(dv) and xopt is None:
        return None
    iv_pairs, iv_last, iv_rank = [], None, None
    if len(dv):
        iv_pairs = [
            [int(t), round(float(v), 2)]
            for t, v in zip(storage.ts_to_ms(dv["ts"]), dv["dvol"])
        ]
        iv_last = float(dv["dvol"].iloc[-1])
        iv_rank = round(pct_rank(dv["dvol"], 365), 3)
    d1 = storage.get_ohlcv(conn, symbol, "1d", limit=1200)
    rv_pairs, rv_last = [], None
    if len(d1) >= 40:
        rv = realized_vol(d1["close"], 30, 365) * 100
        rv_pairs = [
            [int(t), round(float(v), 2)]
            for t, v in zip(storage.ts_to_ms(d1["ts"]), rv)
            if math.isfinite(float(v))
        ]
        rv_last = float(rv.dropna().iloc[-1]) if rv.notna().any() else None
    # 3d 对照层：近端 IV 历史（30 分钟一点，自 2026-08-05 自攒）+ RV3（72×1h）。
    # 历史序列独立于 xopt 的 2h 新鲜度闸——采集停了历史照画，只是最新点消失
    xh = storage.get_opt_iv_near(conn, symbol, limit=4800)
    iv3_pairs = ([[int(t), round(float(v), 2)]
                  for t, v in zip(xh["ts"], xh["iv"]) if v == v]
                 if len(xh) else [])
    rv3_pairs, rv3_last = _rv3_pairs(conn, symbol)
    return {
        "iv": iv_pairs[-365:],
        "rv": rv_pairs[-365:],
        "iv_last": round(iv_last, 1) if iv_last is not None else None,
        "iv_rank": iv_rank,
        "rv_last": round(rv_last, 1) if rv_last is not None else None,
        "spread": (round(iv_last - rv_last, 1)
                   if iv_last is not None and rv_last is not None else None),
        "xopt": xopt,
        "iv3": iv3_pairs,
        "rv3": rv3_pairs,
        "rv3_last": rv3_last,
        # 3d 剪刀差 = 近端 IV − RV3：同期限对照，"这笔 1-3 天仓的保险贵不贵"
        "spread3": (round(xopt["iv"] - rv3_last, 1)
                    if xopt is not None and rv3_last is not None else None),
    }


IV_RANK_WIN = 252   # 交易日≈1 年，与指数分位同窗
IV_RANK_MIN = 120   # 少于此不给分位——新股样本短，宁可空着也不给假分位


def _stock_iv_block(conn, symbol: str):
    """个股自身 IV（moomoo 口径）+ 其分位。取代"拿 VXN 当个股 IV"的代理做法。

    分位窗自适应：满 252 交易日按年算；不足 IV_RANK_MIN 一律不给分位（新股如
    CRCL/CRWV 只有十几个月历史，硬给分位是拿噪音冒充统计量）。n 与 win 一并回传，
    前端据此显示"样本 N 日"而不是让用户以为分位同样可靠。

    **仅显示层**：个股 IV 进规则层需并入 RULES_VERSION v2 并过回测，此处不参与判定。
    """
    df = storage.get_stock_vol(conn, symbol, moomoo_iv.SOURCE, limit=1500)
    if df.empty:
        return None
    s = df[["ts", "iv"]].dropna()
    if s.empty:
        return None
    last = float(s["iv"].iloc[-1])
    win = s["iv"].tail(IV_RANK_WIN)
    n = int(len(s))
    rank_raw = round(float((win < last).mean()), 3) if n >= IV_RANK_MIN else None
    rank, in30 = _earn_conditioned_rank(conn, symbol, s, last, n)
    # rank_kind 让消费方知道 rank 到底是什么：cond=同财报状态内(2年窗)，raw=普通252日
    #（codex 审计：回退时若仍标"同财报状态内"就是谎称口径）
    rank_kind = "cond" if rank is not None else "raw"
    if rank is None:
        rank = rank_raw          # 财报样本不足时退回原始分位，由 rank_kind 标注
    hv = df[["ts", "hv"]].dropna()
    # 方差风险溢价（VRP = IV − HV）：把"贵"与"波动大"分开，ATR/BBW 分位做不到。
    # 低 ATR 分位 + 高 VRP = 安静但市场在买保险（脆弱的安静，低波挤压态会误判为健康）；
    # 高 ATR 分位 + 负 VRP = 已实现波动已超期权定价（尚未被充分定价，趋势可能加速）。
    # 用同一行的 iv 与 hv 相减——两者同源同口径，这是它比"IV 减自算 RV30"可靠的地方。
    vrp = df[["ts", "iv", "hv"]].dropna()
    vrp_last = vrp_rank = None
    if len(vrp):
        spread = vrp["iv"] - vrp["hv"]
        vrp_last = round(float(spread.iloc[-1]), 2)
        w = spread.tail(IV_RANK_WIN)
        if len(spread) >= IV_RANK_MIN:
            vrp_rank = round(float((w < spread.iloc[-1]).mean()), 3)
    # 财报邻近度：实测事前峰→事后谷崩塌 38%、占分位分母 9.9%。分母不清洗（财报是该股
    # IV 分布的结构性部分），但当下读数必须标注——否则日程驱动的高分位会被读成市场压力。
    earn = storage.earnings_proximity(
        conn, symbol, int(time.time() * 1000)
    )
    # asof/age：结算序列的最后日期与年龄——OpenD 断供后旧值不得无限期冒充当前
    #（审计：模拟断供 14 天，NVDA 仍显示 IV=48.0 而无任何陈旧标志）
    asof_ms = int(s["ts"].iloc[-1])
    age_days = round((time.time() * 1000 - asof_ms) / 86_400_000, 1)
    return {
        "source": moomoo_iv.SOURCE,
        "last": round(last, 2),
        "asof": asof_ms,
        "age_days": age_days,
        "stale": bool(age_days > 4.0),   # 结算日频+周末，>4 天即断供
        "live": _live_iv_block(conn, symbol, s, last, in30),
        "rank": rank,
        "n": n,
        "win": int(len(win)),
        "earnings_days": earn["days"] if earn else None,
        # rank 已是财报条件分位（同状态内比同状态）；rank_raw 保留供审计与对照
        "rank_raw": rank_raw,
        "rank_kind": rank_kind,   # cond=同财报状态内(2年窗) / raw=普通252日（回退）
        "earn_in30": in30,
        "vrp": vrp_last,
        "vrp_rank": vrp_rank,
        "days": round((int(s["ts"].iloc[-1]) - int(s["ts"].iloc[0])) / 86_400_000, 0),
        "series": [[int(t), round(float(v), 2)] for t, v in zip(s["ts"], s["iv"])][-365:],
        "hv_last": round(float(hv["hv"].iloc[-1]), 2) if len(hv) else None,
    }


def _live_iv_block(conn, symbol: str, settled_s, settled_last: float, in30):
    """盘中实时 IV 预览。**与结算值分列**，同"未收线 K 线"的双轨制。

    给三样东西：① 当前实时值；② 相对昨收的变动（今日的波动率重定价）；
    ③ 一个**预览分位**——若以此实时值计，在同财报状态的结算分布里会落在哪。
    预览分位打 `preview: True` 标记，前端必须显式标注，绝不与正式分位混用
    （正式分位永远只在已结算序列上算——盘中值还会变，同一天算两次得数不同）。
    """
    df = storage.get_stock_vol_live(conn, symbol, moomoo_iv.SOURCE, limit=200)
    if df.empty:
        return None
    row = df.iloc[-1]
    # 新鲜度闸：陈旧快照（采集停摆/隔夜残留）不得冒充"实时"——codex 审计：
    # 七月的旧行也会被当成当前实时值显示。RTH 内采集节奏 5 分钟，30 分钟即为陈旧。
    if int(time.time() * 1000) - int(row["ts"]) > 30 * 60_000:
        return None
    iv = row.get("iv")
    if iv is None or iv != iv:
        return None
    iv = float(iv)
    pre = row.get("pre_iv")
    pre = float(pre) if pre is not None and pre == pre else None
    # 预览分位的财报状态必须按**今天**算，不能沿用最后结算日的状态——
    # codex 审计抓到的真 P0：PLTR 结算日(8-03)是财报日(窗内)、今天(8-04)财报已过(窗外)，
    # 沿用昨天的状态把预览分位错报 0.026（对高IV财报日比），正确是 0.513（对窗外日比）。
    prev_rank = None
    in30_now = None
    rows = conn.execute(
        "SELECT ts FROM earnings WHERE symbol=? AND source=? ORDER BY ts",
        (symbol, moomoo_iv.SOURCE),
    ).fetchall()
    if rows:
        from datetime import datetime as _dt
        from datetime import timezone as _tz
        from zoneinfo import ZoneInfo

        ed = np.array([r[0] for r in rows], dtype="int64")
        now_ms = int(time.time() * 1000)
        # 今天的财报状态按 **ET 日历日**判（v3.1：UTC 毫秒差在财报夜会提前 4-5 小时
        # 切出条件组，把实时 IV 与错误的历史参照比）
        today_ord = _dt.fromtimestamp(now_ms / 1000,
                                      tz=ZoneInfo("America/New_York")).date().toordinal()
        e_ords = np.array([_dt.fromtimestamp(int(t) / 1000, tz=_tz.utc).date().toordinal()
                           for t in ed], dtype="int64")
        dd = e_ords - today_ord
        in30_now = bool(((dd >= 0) & (dd <= EARN_WINDOW_D)).any())
        sw = settled_s.tail(EARN_COND_WIN)    # 与正式条件分位同一 2 年窗
        ts = sw["ts"].to_numpy(dtype="int64")[:, None]
        ahead = (ed[None, :] - ts) / 86_400_000.0
        days_to = np.where(ahead >= 0, ahead, np.inf).min(axis=1)
        peer = sw["iv"].to_numpy()[(days_to <= EARN_WINDOW_D) == in30_now]
        if len(peer) >= EARN_COND_MIN:
            prev_rank = round(float((peer < iv).mean()), 3)
    elif len(settled_s) >= IV_RANK_MIN:
        # 无财报记录（ETF/商品代理）：与结算全集比，同结算侧 rank_raw 的口径
        ref = settled_s["iv"].tail(IV_RANK_WIN)
        prev_rank = round(float((ref < iv).mean()), 3)
    return {
        "iv": round(iv, 2),
        "pre_iv": round(pre, 2) if pre is not None else None,
        "chg": round(iv - pre, 2) if pre is not None else None,
        "chg_pct": round((iv / pre - 1) * 100, 2) if pre else None,
        "captured_at": int(row["ts"]),
        "rank_preview": prev_rank,
        "in30_now": in30_now,     # 预览分位所用的**今天**的财报状态（与结算日状态可能不同）
        "preview": True,          # 前端必须据此标注"未结算"
        "pc_volume_ratio": (round(float(row["pc_volume_ratio"]), 3)
                            if row.get("pc_volume_ratio") is not None
                            and row["pc_volume_ratio"] == row["pc_volume_ratio"] else None),
        "series": [[int(t), round(float(v), 2)]
                   for t, v in zip(df["ts"], df["iv"]) if v == v][-120:],
    }


EARN_WINDOW_D = 30    # IV30 的计价窗口——财报落在未来 30 天内即已被计价
EARN_COND_MIN = 60    # 同状态历史样本下限，不足则不给条件分位
# 条件分位的参照窗：504 交易日（约 2 年 = 8 个财报周期）。codex 审计指出原实现
# 用全历史（最多 1500 日）与 rank_raw 的 252 日窗口不一致；252 只含 4 个周期、
# 窗内(in30)样本 ~80 个贴着 EARN_COND_MIN，条件分位会抖；全历史则跨波动率制度。
# 504 是两者的折中，属版本化先验。
EARN_COND_WIN = 504


def _earn_conditioned_rank(conn, symbol: str, s, last: float, n: int):
    """财报条件分位：只与**同一财报状态**的历史日比较。返回 (分位|None, 当前状态)。

    为什么这样做而不是反解分解——实测（25 品种/18,325 品种日）表明财报对 IV30 的
    影响是一个**阶跃而非斜坡**：财报落在未来 0-30 天内时 IV/基线中位 1.20~1.27，
    31-35 天骤降到 1.02，>35 天为 0.93~0.97，阶跃恰在 30 天（正是跳跃进出 IV30
    计价窗口的位置），幅度 +30.9%。

    这个性质很关键：污染项**由提前公布的日历完全决定**，不含未知量，因此
    ① 不需要期权链反解（那要解决"一个观测两个成分"的识别问题）；
    ② 前向使用无障碍——今天就知道未来 30 天有没有财报。

    实测效果：分位≥0.8 的富集从 1.92× 降到 0.94×（1.00=无污染）。
    """
    import numpy as np

    # source 过滤：earnings 的 source 在主键里，不过滤则第二来源接入时会污染参照集
    rows = conn.execute(
        "SELECT ts FROM earnings WHERE symbol=? AND source=? ORDER BY ts",
        (symbol, moomoo_iv.SOURCE),
    ).fetchall()
    if not rows or n < IV_RANK_MIN:
        return None, None
    ed = np.array([r[0] for r in rows], dtype="int64")
    sw = s.tail(EARN_COND_WIN)                          # 2 年窗，与 rank_raw 的窗口哲学一致
    ts = sw["ts"].to_numpy(dtype="int64")[:, None]
    ahead = (ed[None, :] - ts) / 86_400_000.0
    ahead = np.where(ahead >= 0, ahead, np.inf)
    days_to = ahead.min(axis=1)
    in30 = days_to <= EARN_WINDOW_D
    cur = bool(in30[-1])
    peer = sw["iv"].to_numpy()[:-1][in30[:-1] == cur]   # 同状态的历史（不含当日）
    if len(peer) < EARN_COND_MIN:
        return None, cur
    return round(float((peer < last).mean()), 3), cur


def _term_structure(conn):
    """指数 IV 期限结构双速斜率 + 各自历史分位 + 倒挂态。

    Johnson (2017, JFQA) 证明 SLOPE 承载期限结构几乎全部信息。这是唯一能把
    "急性冲击"与"慢磨熊"分开的维度：文献明说倒挂能抓 2008/2020 式冲击、
    会错过 2022 式慢跌——**这个已知失败模式本身就是可用信息**，故 fast/slow
    两条都给，不合成单一读数。实证（4,243 日）：倒挂占 7.6%（文献≈8%），
    倒挂日后 21 日 VIX 中位变化 −6.64 vs 正挂 −0.12。

    返回 (ts_ratio 兼容旧字段, term 详情)。分位窗用全历史而非 252 日——
    这些指数有 16-36 年史，倒挂是罕见事件，短窗会把分位压成常数。
    """
    ser = {k: storage.get_usvol(conn, k, limit=9000)
           for k in ("VIX9D", "VIX", "VIX3M")}
    if any(s.empty for s in ser.values()):
        return None, None
    # 当日行可能是尚未被 CSV 确权的延迟报价（usvol 两级权威设计的临时值）。
    # 当前比值用最新值（活读数），但**分位的参照集只用已确权行**——
    # codex 审计点出：把临时值混进正式分位的分母，与"未结算不进分位"纪律相悖。
    csv_max = min(
        int(storage.get_meta(conn, f"usvol_csv_max_{k}", 0) or 0)
        for k in ("VIX9D", "VIX", "VIX3M")
    )

    def slope(num, den):
        a, b = ser[num], ser[den]
        m = a.merge(b, on="ts", suffixes=("_a", "_b"))
        m = m[m["close_b"] > 0]
        if m.empty:
            return None
        r = m["close_a"] / m["close_b"]
        cur = float(r.iloc[-1])
        settled = bool(csv_max) and int(m["ts"].iloc[-1]) <= csv_max
        ref = r[m["ts"] <= csv_max] if csv_max else r
        if not len(ref):
            ref = r
        # 倒挂是罕见事件（占 7.6%），分位参照须够长——不足 252 不给（宁缺毋滥）
        rank = round(float((ref < cur).mean()), 3) if len(ref) >= 252 else None
        return {
            "ratio": round(cur, 3),
            "rank": rank,
            "inverted": bool(cur > 1.0),
            "settled": settled,   # False = 当前比值来自盘中延迟报价（未确权）
            "n": int(len(ref)),
        }

    fast = slope("VIX9D", "VIX")     # 9 日 vs 30 日：最急的一端
    slow = slope("VIX", "VIX3M")     # 30 日 vs 3 月：制度端
    legacy = slope("VIX9D", "VIX3M")  # 旧字段口径，保持前端兼容
    term = {"fast": fast, "slow": slow}
    if fast and slow:
        # 两端同时倒挂 = 全曲线倒挂（急性且已传导到制度端），比单端更强
        term["both_inverted"] = bool(fast["inverted"] and slow["inverted"])
    return (legacy["ratio"] if legacy else None), term


def _iv30_fields(conn, symbol: str, cboe_last, cboe_hourly, spans) -> dict:
    """deriv 卡的个股 IV 来源绑定字段：优先 moomoo 正牌序列，退化 CBOE 影子值。

    分开写是因为两条路的分位口径不同：moomoo 优先走 504 日条件分位并可回退
    252 日原始分位，CBOE 是小时重采样后的短窗分位（样本不足时本就返回 None）。混在一个表达式里
    迟早会有人把两者的分位当同一个统计量。source/span/n/rank_kind 必须随最终值
    一起返回，前端不得再拿 deriv.iv30 的短史跨度替 moomoo 长史背书。
    """
    blk = _stock_iv_block(conn, symbol)
    if blk:
        return {
            "iv30": blk["last"],
            "iv30_rank": blk["rank"],
            "iv30_src": blk["source"],
            "iv30_span_days": blk["days"],
            "iv30_n": blk["n"],
            "iv30_rank_kind": blk["rank_kind"],
        }
    cboe_n = int(cboe_hourly["iv30"].dropna().shape[0]) if len(cboe_hourly) else 0
    return {
        "iv30": round(cboe_last, 1) if cboe_last is not None else None,
        "iv30_rank": _rank_or_none(cboe_hourly, cboe_last, spans),
        "iv30_src": "cboe" if cboe_last is not None else None,
        "iv30_span_days": spans["iv30"] if cboe_last is not None else None,
        "iv30_n": cboe_n if cboe_last is not None else 0,
        "iv30_rank_kind": "hourly" if cboe_last is not None else None,
    }


def _rank_or_none(hourly, current, spans):
    s = hourly["iv30"].dropna() if len(hourly) else hourly
    if current is None or not len(s) or len(s) < 20 or spans["iv30"] < 7:
        return None
    return round(float((s < current).mean()), 3)


def _usvol_payload(conn, symbol: str):
    """美股波动率维度：**个股自身 IV 为主线** + CBOE 指数 IV 作长历史锚 + 自算 RV30 + 期限结构。

    升级前的问题：31 个美股品种里 29 个把 VXN（纳指100 隐波≈QQQ 的 IV）当自己的 IV，
    个股 iv30 只有 CBOE 自采的 3.75 天、分位恒空。现在个股 IV 有 moomoo 的 3.1 年历史，
    分位可算，指数降为长周期锚（VIX/VXN 有 17-37 年史，个股比不了）。

    口径纪律：moomoo / CBOE / 指数三条线**各自独立算分位**，绝不混拼——算法不同源，
    绝对值有系统性偏差。CBOE 自采值保留为影子字段（iv30_last）供口径对比。
    """
    inst = instruments.get(symbol)
    is_us = inst["class"] == "us_stock_perp"
    proxy = inst.get("vol_proxy")   # 商品：XAU→GLD / XAG→SLV（30 天口径代理）
    if not (is_us or proxy):
        return None
    idx = inst.get("vol_index") or ("VIX" if is_us else None)
    idx_last = idx_rank = None
    idx_settled = None
    series = []
    if idx:
        dfv = storage.get_usvol(conn, idx, limit=800)
        if len(dfv):
            idx_last = float(dfv["close"].iloc[-1])
            # 当日行可能是未经 CSV 确权的延迟报价（usvol 两级权威）。显示值用最新
            #（活读数），但**分位参照只用已确权行** + ≥60 样本闸——codex 审计：
            # 临时值进正式分位分母与"未结算不进分位"纪律相悖；单行也能算出 rank=0。
            csv_max = int(storage.get_meta(conn, f"usvol_csv_max_{idx}", 0) or 0)
            idx_settled = bool(csv_max) and int(dfv["ts"].iloc[-1]) <= csv_max
            ref_df = dfv[dfv["ts"] <= csv_max] if csv_max else dfv
            tail = ref_df["close"].tail(252)
            if len(tail) >= 60:
                idx_rank = round(float((tail < idx_last).mean()), 3)
            series = [[int(t), round(float(c), 2)]
                      for t, c in zip(dfv["ts"], dfv["close"])][-365:]
    if is_us and idx_last is None:
        return None

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

    # VIX 期限结构是美股波动率环境的量——对贵金属无意义，商品不给
    ts_ratio, term = _term_structure(conn) if is_us else (None, None)

    iv = _stock_iv_block(conn, symbol)
    # 币安期权近端 IV（仅配了 vol_proxy 的商品）：与加密卡共用 _xopt_block
    #（同一新鲜度闸/同一字段集，两处口径不许漂移）
    xopt = _xopt_block(conn, symbol) if proxy else None
    # 个股 IV 期限曲线（3d/9d/30d ATM，仅美股）：持仓前端/中段/制度层分离。
    # 2h 新鲜度闸；无历史（自 2026-08-05 积累），暂不算分位
    term_stock = None
    if is_us:
        tdf = storage.get_stock_iv_term(conn, symbol, limit=1)
        if len(tdf):
            r0 = tdf.iloc[-1]
            age_ms = int(time.time() * 1000) - int(r0["ts"])
            if age_ms <= 2 * 3600_000:
                def _n(v):
                    return float(v) if v is not None and v == v else None
                iv3, iv30_c = _n(r0["iv3"]), _n(r0["iv30"])
                term_stock = {
                    "iv3": iv3, "t3": _n(r0["t3"]),
                    "iv9": _n(r0["iv9"]), "t9": _n(r0["t9"]),
                    "iv30": iv30_c, "t30": _n(r0["t30"]),
                    "slope": (round(iv3 - iv30_c, 2)
                              if iv3 is not None and iv30_c is not None else None),
                    "inverted": (iv3 > iv30_c
                                 if iv3 is not None and iv30_c is not None else None),
                    "age_min": round(age_ms / 60_000),
                }
    # 3d 对照层：IV3 历史（美股走 stock_iv_term 自攒；商品走币安近端 IV）+ RV3（72×1h 永续）
    rv3_pairs, rv3_last = _rv3_pairs(conn, symbol)
    iv3_hist = []
    if is_us:
        tfull = storage.get_stock_iv_term(conn, symbol, limit=700)
        if len(tfull):
            iv3_hist = [[int(t), round(float(v), 2)]
                        for t, v in zip(tfull["ts"], tfull["iv3"]) if v == v]
    elif proxy:
        xh = storage.get_opt_iv_near(conn, symbol, limit=4800)
        if len(xh):
            iv3_hist = [[int(t), round(float(v), 2)]
                        for t, v in zip(xh["ts"], xh["iv"]) if v == v]
    # 3d 剪刀差：美股用期限曲线的 iv3，商品用近端 IV——都与 RV3 同期限对照
    front3 = (term_stock["iv3"] if term_stock else None) if is_us \
        else (xopt["iv"] if xopt else None)
    base_iv = iv["last"] if iv else idx_last
    return {
        "index": idx,
        "index_last": round(idx_last, 2) if idx_last is not None else None,
        "index_rank": idx_rank,
        "index_settled": idx_settled,   # False = 显示值来自盘中延迟报价（未确权）
        "series": series,
        "rv": rv_pairs,
        "rv_last": rv_last,
        # 剪刀差改用个股自身 IV（有则用），退化到指数只是兜底
        "spread": (round(base_iv - rv_last, 1)
                   if rv_last is not None and base_iv is not None else None),
        "spread_src": (("stock" if iv else "index")
                       if rv_last is not None and base_iv is not None else None),
        "iv": iv,                 # 个股 IV 主线（moomoo 口径，含自有分位）
        "proxy": proxy,           # 非空表示 iv 来自代理标的（GLD/SLV），前端须标注
        "xopt": xopt,             # 币安期权近端 IV（24/7；期限 1-3 天，非 30 天口径）
        "term_stock": term_stock,  # 个股期限曲线 3d/9d/30d ATM（无历史，自采积累中）
        "iv3_hist": iv3_hist,     # 3d IV 历史序列（自 2026-08-05 自攒，初期很短）
        "rv3": rv3_pairs,
        "rv3_last": rv3_last,
        "spread3": (round(front3 - rv3_last, 1)
                    if front3 is not None and rv3_last is not None else None),
        "iv30_last": iv30_last,   # CBOE 自采影子值：口径对比用，不算分位
        "iv30_days": iv30_days,
        "ts_ratio": ts_ratio,
        "term": term,
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

    # 结算间隔由 collector 从 fundingInfo 接口取回存进 meta，面板只读库不打网络。
    # 缺键/坏值表示未知；不得猜成 8h，更不得据此生成伪年化。
    raw_interval = storage.get_meta(conn, f"funding_interval_{symbol}", None)
    try:
        interval_h = float(raw_interval) if raw_interval is not None else None
        if interval_h is not None and (not math.isfinite(interval_h) or interval_h <= 0):
            interval_h = None
    except (TypeError, ValueError):
        interval_h = None
    per_year = 365 * 24.0 / interval_h if interval_h is not None else None

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
        "funding_annual_pct": (round(funding_pred * per_year * 100, 1)
                               if funding_pred is not None and per_year is not None else None),
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
        # 个股 IV：有 moomoo 正牌序列（3.1 年）就用它及其真实分位；退化到 CBOE 自采
        # 影子值（仅几天，分位必被 20 样本闸挡成 None）。iv30_src 让前端能说清口径。
        **_iv30_fields(conn, symbol, iv30, iv30_h, spans),
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
        # 明细卡与 Hermes 共用此 payload：1h 噪音先在后端排除，再给 4h/1d 40 条预算。
        if tf == "1h":
            continue
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


_OPEND_PROBE = {"ts": 0.0, "ok": False}


def _opend_alive() -> bool:
    """OpenD TCP 探活（0.4s 超时，60s 缓存）。OpenD 登出→美股 IV 静默断流是
    已立案的哑故障模式（GAPS C3），心跳条必须能直接看见它，而不是等 IV 变陈旧才发现。"""
    import socket

    now = time.time()
    if now - _OPEND_PROBE["ts"] > 60:
        try:
            with socket.create_connection(("127.0.0.1", 11111), timeout=0.4):
                _OPEND_PROBE["ok"] = True
        except OSError:
            _OPEND_PROBE["ok"] = False
        _OPEND_PROBE["ts"] = now
    return _OPEND_PROBE["ok"]


def _heartbeat(conn) -> list:
    """分管线采集心跳：每条管线按自己的采集节奏判新鲜度。

    整体循环的"数据新鲜"徽章只能证明 collector 活着；某条管线静默断流
    （OpenD 登出、单接口持续失败）时循环照常转——这里按 MAX(ts) 逐管线看。
    RTH 门控的管线在盘外不算故障（idle），但盘外 OpenD 离线给 warn：
    不修复的话开盘后个股 IV 就会断流。"""
    from regime.calendar_nyse import is_rth

    now = int(time.time() * 1000)
    rth = bool(is_rth(now))

    def age(table):
        row = conn.execute(f"SELECT MAX(ts) FROM {table}").fetchone()  # noqa: S608 表名白名单
        return None if not row or row[0] is None else round((now - int(row[0])) / 60000.0, 1)

    def judge(a, ok_min, warn_min, gated=False):
        if gated and not rth:
            return "idle"
        if a is None:
            return "bad"
        return "ok" if a <= ok_min else ("warn" if a <= warn_min else "bad")

    lanes = []
    a = age("opt_iv_near")   # 30 分钟节流 → 45 分内新鲜
    lanes.append({"key": "近端IV", "age_min": a, "state": judge(a, 45, 120), "note": "币安期权 24/7·30分频"})
    # RTH 内**每轮都采、不节流**（collector.sync_moomoo_iv_live 的既定设计）→ 5 分钟一行。
    # warn 线钉在 30 分钟：与 _stock_iv_block 丢弃实时值的闸同值，否则会出现
    # "心跳说正常、面板已不显示实时 IV" 的自相矛盾。
    a = age("stock_vol_live")
    lanes.append({"key": "个股IV", "age_min": a, "state": judge(a, 15, 30, gated=True), "note": "moomoo RTH·每轮(5分)"})
    a = age("stock_iv_term")   # RTH 内小时频
    lanes.append({"key": "期限曲线", "age_min": a, "state": judge(a, 90, 180, gated=True), "note": "moomoo RTH·小时频"})
    a = age("deriv")           # 每轮 5 分钟
    lanes.append({"key": "衍生品", "age_min": a, "state": judge(a, 15, 30), "note": "币安永续·5分频"})
    op = _opend_alive()
    lanes.append({"key": "OpenD", "age_min": None,
                  "state": "ok" if op else ("bad" if rth else "warn"),
                  "note": "moomoo 网关探活" + ("" if op else "·离线（美股 IV 断流" + ("中" if rth else "风险，开盘前需恢复") + "）")})
    return lanes


def _instrument_payload(symbol: str) -> dict:
    """品种元信息；美股永续附带标的正股是否盘中。

    v3.1：改用仓库自己的 NYSE 日历（假日表 + 半日市收盘时刻）——5 路审计独立发现
    旧实现硬编码"工作日 9:30-16:00"，感恩节会显示"标的盘中"、半日市收盘后还绿三小时。
    """
    from regime.calendar_nyse import is_rth

    inst = instruments.get(symbol)
    market_open = None
    if inst["class"] == "us_stock_perp":
        market_open = bool(is_rth(int(time.time() * 1000)))
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
                "heartbeat": _heartbeat(conn),
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
