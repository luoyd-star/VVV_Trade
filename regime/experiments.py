"""梯队一离线实验（预研 E1-E4）：只读库、不动生产审计键、结果进 trial ledger。

四个实验共用 P0 的协议纪律（regime/backtest.py）：锁箱按收线时刻裁剪、
版本分桶、网格断言、结论按 episode 计数。全部离线可重跑——生产
collector/classify 一行不改，谁胜出谁才有资格进正式规则（升版级变更）。

E1 估计器赛马：Parkinson/GK/RS/YZ/RMA-ATR vs 现行 SMA-ATR——只比
   **250 根滚动分位的排序**与阈值翻转率（预研主题 1：分位吸收后排序不变
   则换代无意义）。
E2 squeeze 事件研究：BBW×ATR 阈值网格的压缩事件 → 未来波幅扩张/方向，
   事件按 episode 去重（主题 5：幅度可测方向不可测，先验 0.15/0.30 待检）。
E3 去季节化稳健性：现行 48 桶均值因子 vs 中位数+MAD 稳健版 vs 无去季节化
   ——比 TOD 残余离散度（主题 4：均值易被跳跃污染）。
E4 迟滞网格：库内 raw_state 离线重放 confirm_states 变体（N_enter/N_exit
   1..4），比确认延迟/翻转率/状态驻留（主题 2：迟滞有据、1/2/3 根无据）。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import storage
from .backtest import LOCKBOX_START_MS, TF_MS, episode_ids, future_rv
from .classify import RULES_VERSION, AUDIT_VERSION
from .features.volatility import true_range

MIN_EVENTS = 12  # 事件研究每格最少去重事件数，低于只描述


# ---------------------------------------------------------------- 公共装载

def load_series(conn, sym, tf):
    """锁箱裁剪 + 网格断言的 OHLCV；状态行（若需要）另取。"""
    step = TF_MS[tf]
    df = storage.get_ohlcv(conn, sym, tf, limit=20_000)
    ts = np.array([int(t) for t in storage.ts_to_ms(df["ts"])])
    keep = ts + step <= LOCKBOX_START_MS
    df = df[keep].reset_index(drop=True)
    ts = ts[keep]
    if len(ts) >= 2 and not (np.diff(ts) == step).all():
        raise RuntimeError(f"{sym} {tf}: 网格缺口，拒绝")
    return df, ts


# ---------------------------------------------------------------- E1 估计器赛马

def _rolling_rank(s: pd.Series, win: int = 250) -> pd.Series:
    """与 features.utils.pct_rank 同口径的滚动分位（<=，含自身）。"""
    def _r(x):
        return (x <= x[-1]).mean()
    return s.rolling(win).apply(_r, raw=True)


def e1_estimator_race(df: pd.DataFrame, tf: str) -> dict:
    """六种波动率代理的 250 根分位排序一致性与阈值翻转率。

    比较对象全部先除以 close 折算成相对量再取分位（与 atr_pct_price 同口径）。
    产出：与现行 SMA-ATR14 分位的 Spearman、squeeze/high_vol 阈值
    （0.30/0.85）判定不一致的 bar 占比。排序几乎不变 => 换代无意义（预研闸门）。
    """
    o, h, l, c = (df[k].astype(float) for k in ("open", "high", "low", "close"))
    lo_, lh = np.log(l / o), np.log(h / o)
    lc = np.log(c / o)
    n = 14
    est = {
        "sma_atr": true_range(df).rolling(n).mean() / c,             # 现行基准
        "rma_atr": true_range(df).ewm(alpha=1 / n, adjust=False).mean() / c,
        "parkinson": np.sqrt((np.log(h / l) ** 2).rolling(n).mean() / (4 * np.log(2))),
        "gk": np.sqrt((0.5 * np.log(h / l) ** 2 - (2 * np.log(2) - 1) * lc ** 2)
                      .rolling(n).mean().clip(lower=0)),
        "rs": np.sqrt((lh * (lh - lc) + lo_ * (lo_ - lc)).rolling(n).mean()
                      .clip(lower=0)),
    }
    ranks = {k: _rolling_rank(v.dropna()) for k, v in est.items()}
    base = ranks["sma_atr"]
    out = {}
    for k, r in ranks.items():
        if k == "sma_atr":
            continue
        pair = pd.concat([base, r], axis=1, keys=["a", "b"]).dropna()
        if len(pair) < 100:
            out[k] = {"insufficient": len(pair)}
            continue
        # Spearman = 秩的 Pearson——不引 scipy 依赖
        rho = float(pair["a"].rank().corr(pair["b"].rank()))
        flip_sq = float(((pair["a"] < 0.30) != (pair["b"] < 0.30)).mean())
        flip_hv = float(((pair["a"] > 0.85) != (pair["b"] > 0.85)).mean())
        out[k] = {"spearman": round(rho, 4), "n": len(pair),
                  "flip_squeeze_gate": round(flip_sq, 4),
                  "flip_highvol_gate": round(flip_hv, 4)}
    return out


# ---------------------------------------------------------------- E2 squeeze 事件研究

def e2_squeeze_events(df: pd.DataFrame, tf: str,
                      bbw_gates=(0.05, 0.10, 0.15, 0.20),
                      atr_gates=(0.20, 0.30, 0.40)) -> dict:
    """阈值网格上的压缩事件 → 未来 H 根波幅扩张倍数与方向命中。

    事件定义：bbw_rank 与 atr_rank 同时低于门槛的**首根**（episode 去重：
    连续满足只计入进入那根）。前瞻：H 根内 [max(high)-min(low)]/ATR14 的
    扩张倍数（幅度轴）与 close_{t+H}/close_t 的符号（方向轴，仅记录）。
    无条件基线：全部 bar 的同一前瞻量。
    """
    from .features.volatility import atr, bb_width
    from .features.utils import pct_rank as _pct  # noqa: F401  口径参考
    c = df["close"].astype(float)
    atr14 = true_range(df).rolling(14).mean()
    bbw_rank = _rolling_rank(bb_width(c))
    atr_rank = _rolling_rank(atr14 / c)
    H = {"1h": 24, "4h": 18, "1d": 10}[tf]
    hi, lo = df["high"].astype(float), df["low"].astype(float)
    fwd_range = pd.concat(
        [hi.shift(-k) for k in range(1, H + 1)], axis=1).max(axis=1) - pd.concat(
        [lo.shift(-k) for k in range(1, H + 1)], axis=1).min(axis=1)
    expansion = (fwd_range / atr14).replace([np.inf, -np.inf], np.nan)
    fwd_ret = np.log(c.shift(-H) / c)
    base_med = float(expansion.dropna().median()) if expansion.notna().any() else None
    grid = {}
    for bg in bbw_gates:
        for ag in atr_gates:
            inside = (bbw_rank < bg) & (atr_rank < ag)
            entry = inside & ~inside.shift(1, fill_value=False)  # episode 首根
            idx = entry[entry].index
            vals = expansion.loc[idx].dropna()
            rets = fwd_ret.loc[idx].dropna()
            key = f"bbw<{bg}&atr<{ag}"
            if len(vals) < MIN_EVENTS:
                grid[key] = {"n_events": int(len(vals)), "insufficient": True}
                continue
            grid[key] = {
                "n_events": int(len(vals)),
                "expansion_med": round(float(vals.median()), 3),
                "expansion_vs_base": round(float(vals.median()) / base_med, 3)
                if base_med else None,
                "dir_up_share": round(float((rets > 0).mean()), 3),
            }
    return {"H": H, "base_expansion_med": round(base_med, 3) if base_med else None,
            "grid": grid}


# ---------------------------------------------------------------- E3 去季节化稳健性

def e3_deseason_robustness(df: pd.DataFrame) -> dict:
    """三种因子的 TOD 残余离散度（越低说明去季节化越干净）。

    指标：去季节化后 TR% 的 (ET小时×周末) 桶中位数们的变异系数——
    干净的因子应把 48 桶的桶间差抹平。仅对美股永续 1h 有意义。
    """
    from .features.volatility import session_bucket
    trp = (true_range(df) / df["close"]).rename("trp")
    bucket = session_bucket(df["ts"])

    def bucket_cv(series):
        med = series.groupby(bucket).median().dropna()
        med = med[med > 0]
        return float(med.std() / med.mean()) if len(med) >= 20 else None

    fac_mean = trp.groupby(bucket).transform(
        lambda s: s.rolling(30, min_periods=8).mean().shift(1))
    fac_med = trp.groupby(bucket).transform(
        lambda s: s.rolling(30, min_periods=8).median().shift(1))
    out = {
        "raw_cv": bucket_cv(trp),
        "mean_factor_cv": bucket_cv((trp / fac_mean).dropna()),
        "median_factor_cv": bucket_cv((trp / fac_med).dropna()),
    }
    return {k: (round(v, 4) if v is not None else None) for k, v in out.items()}


# ---------------------------------------------------------------- E4 迟滞网格

def _confirm_fold(raw, need_fn):
    """confirm_states 的参数化重放（与 classify.confirm_states 同构）。"""
    cur, pending, count = None, None, 0
    out = []
    for r in raw:
        if cur is None:
            cur = r
        elif r == cur:
            pending, count = None, 0
        else:
            if r == pending:
                count += 1
            else:
                pending, count = r, 1
            if count >= need_fn(r):
                cur = r
                pending, count = None, 0
        out.append(cur)
    return out


def e4_hysteresis_grid(raw_states, y, tf) -> dict:
    """N 根确认网格：churn / 平均驻留 / 条件 EWMA 跟踪偏差。

    对统一 N（1..4）与现行非对称方案重放折叠。跟踪偏差 = 按折叠态维护的
    未来窗风险因果 EWMA（α=0.05）与逐 bar 实际值的绝对偏差均值——衡量
    "拿这个折叠态做条件"对未来风险的跟踪好坏（低=好）。这是**描述性代理**
    不是 proper score：确认根数的正式裁决走 P0 框架的 CRPS 路径，此处只为
    网格快筛提供方向感。
    """
    from .classify import _confirm_need
    res = {}
    variants = {f"N={k}": (lambda r, k=k: k) for k in (1, 2, 3, 4)}
    variants["现行(非对称)"] = _confirm_need
    fin = np.isfinite(y)
    for name, fn in variants.items():
        folded = _confirm_fold(raw_states, fn)
        eps = episode_ids(folded)
        churn = eps[-1] + 1
        stay = len(folded) / churn
        ewma_by, dev = {}, []
        for s, yy in zip(folded, y):
            if not np.isfinite(yy):
                continue
            m = ewma_by.get(s)
            if m is not None:  # 先评后更：偏差只用过去信息（因果）
                dev.append(abs(yy - m))
                ewma_by[s] = m * 0.95 + yy * 0.05
            else:
                ewma_by[s] = yy
        res[name] = {
            "episodes": churn, "avg_stay_bars": round(stay, 1),
            "tracking_dev": round(float(np.mean(dev)), 6) if dev else None,
            "n_eval": int(fin.sum()),
        }
    return res


# ---------------------------------------------------------------- 装配

def run_tier1(conn, symbols, tfs=("1h", "4h", "1d")):
    us = {"AAPL-USDT", "MU-USDT", "NVDA-USDT", "QQQ-USDT", "SPY-USDT",
          "SOXL-USDT", "TSLA-USDT"}
    manifest = {}
    out = {"meta": {"protocol": 1, "lockbox_start_ms": LOCKBOX_START_MS,
                    "rules_version": RULES_VERSION, "audit_version": AUDIT_VERSION,
                    "min_events": MIN_EVENTS, "symbols": list(symbols),
                    "tfs": list(tfs), "data_manifest": manifest},
           "results": []}
    conn.execute("BEGIN")
    try:
        for sym in symbols:
            for tf in tfs:
                try:
                    df, ts = load_series(conn, sym, tf)
                except Exception as e:  # noqa: BLE001
                    out["results"].append({"symbol": sym, "tf": tf, "error": str(e)})
                    continue
                if len(df) < 300:
                    out["results"].append(
                        {"symbol": sym, "tf": tf, "insufficient": len(df)})
                    continue
                # 数据清单进 meta（进而进 experiment_id 哈希）：数据增长后
                # 重跑自然换 id，不会与旧账同 id 异结果相撞
                manifest[f"{sym}|{tf}"] = [len(df), int(ts[0]), int(ts[-1])]
                entry = {"symbol": sym, "tf": tf, "n_bars": len(df)}
                entry["e1"] = e1_estimator_race(df, tf)
                entry["e2"] = e2_squeeze_events(df, tf)
                if sym in us and tf == "1h":
                    entry["e3"] = e3_deseason_robustness(df)
                rows = [r for r in storage.get_states_audit(conn, sym, tf)
                        if r["version"] == RULES_VERSION
                        and r["audit_version"] == AUDIT_VERSION
                        and r["ts"] + TF_MS[tf] <= LOCKBOX_START_MS]
                if len(rows) >= 200:
                    s_ts = {r["ts"] for r in rows}
                    mask = [int(t) in s_ts for t in ts]
                    y = future_rv(df["close"], {"1h": 24, "4h": 18, "1d": 10}[tf])
                    y_states = y[np.array(mask)]
                    entry["e4"] = e4_hysteresis_grid(
                        [r["raw_state"] for r in rows], y_states, tf)
                out["results"].append(entry)
    finally:
        conn.execute("ROLLBACK")
    return out
