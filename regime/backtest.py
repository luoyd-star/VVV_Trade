"""P0 回测框架核心：状态对未来风险的信息量——严格未来窗、proper score、因果构造。

设计来源：docs/PRERESEARCH_VOL_20260803.md 第 3 节（主题 6 对原验收设想的修订）。
原设想"state→当期风险条件分布"有自证缺陷（状态就是由 ATR/RV 定义的），
本模块回答修订后的问题：**t 收线时的确认态，对 t+1..t+H 的已实现波动
是否有超越无条件基线的预测信息**，用 CRPS（严格 proper）计分。

因果纪律（tests/test_backtest_causality.py 钉死）：
- t 的条件预测分布 = 所有 u <= t-H 的同状态观测的未来窗结果 {y_u}
  （u+H <= t：y_u 在 t 收线时已完全实现）；
- 无条件基线同一构造、同一训练范围，只去掉状态过滤——信息集严格相同；
- 目标在**完整 K 线网格**上计算后再映射到状态行（评审 p2->p3：若先按状态行
  过滤再算收益，版本空洞会把不相邻 K 线压成相邻、悄悄改变 H 的含义）；
  状态行必须构成连续尾段（Δts == TF），断裂即报错，不许静默续算。

锁箱（一次性，前向累积）：现有历史都被开发过程看过，回溯切分没有处女数据。
锁箱 = LOCKBOX_START 起将要产生的数据，按构造从未被看过。裁剪按**收线时刻**
（ts 是开盘时刻——评审发现按 ts 裁会放进最后一根跨界 bar 的箱内价格）。

统计诚实（p3 协议，评审强制降级）：
- 环移 surrogate 带是**描述性参照**，不是显著性检验：状态由 ~250 根回看窗
  生成，序列总长仅 ~300，环移后的状态见过未来、无法构造干净零假设，
  且未做多重检验校正。出带 = "值得跟踪的探索性观察"，不是"真关联"。
- 技能符号（vs 无条件基线）与带位置（vs surrogate）是两个独立的轴，
  报告分开陈述，禁止互相翻译。
- 结论门槛按**被评估记录覆盖的 episode 数**计，不按 bar 数。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import numpy as np
import pandas as pd

from . import storage
from .classify import AUDIT_VERSION, RULES_VERSION

# ---------------------------------------------------------------- 冻结的协议常量

# 锁箱起点（UTC，含）。此刻起采集的数据对一切校准/评估不可见，直到正式开箱
# （开箱记录进 ledger，只允许一次）。改动此值 = 换实验族，必须以新
# experiment 系列出现，不允许原地改。
LOCKBOX_START = "2026-09-01"
LOCKBOX_START_MS = 1_788_220_800_000  # 2026-09-01T00:00:00Z，写死避免解析歧义

TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

# 未来窗（根）。短窗看状态的即时风险含义，长窗看持续性。
HORIZONS = {"1h": [6, 24], "4h": [6, 18], "1d": [5, 10]}

MIN_TRAIN = 20      # 条件池最小训练观测数：不足则该 t 跳过（计入 skipped）
MIN_EVAL = 30       # 描述门槛：低于只描述、不进观察清单
MIN_EPISODES = 10   # 按被评估记录实际覆盖的 episode 数计（评审 p3）
MAX_SHIFTS = 120    # surrogate 偏移：全枚举，超过则等距抽（确定性，无 RNG）

FEE_BPS = 5.0       # 方向对照（次级）单边手续费，taker 挂靠币安永续量级

LEDGER_PATH = None  # 默认 data/backtest_ledger.sqlite3，见 _ledger_conn


# ---------------------------------------------------------------- proper score

def crps_empirical(sample: np.ndarray, y: float) -> float:
    """经验分布 F_n 对观测 y 的 CRPS（严格 proper）。

    CRPS(F_n, y) = mean|x_i - y| - 0.5 * mean_{i,j}|x_i - x_j|
    第二项用排序前缀和 O(n log n)：ΣΣ|xi-xj| = 2·Σ_i (2i-n+1)·x_(i)（x 已排序）。
    """
    x = np.sort(np.asarray(sample, dtype=float))
    n = len(x)
    if n == 0:
        raise ValueError("empty sample")
    term1 = np.abs(x - y).mean()
    i = np.arange(n)
    term2 = ((2 * i - n + 1) * x).sum() / (n * n)
    return float(term1 - term2)


# ---------------------------------------------------------------- 目标构造

def future_rv(close: pd.Series, horizon: int) -> np.ndarray:
    """y_t = sqrt(mean_{k=1..H} r_{t+k}^2)，r 为 log 收益，跨 H 可比。

    必须在**完整连续网格**上调用（_load_series 已断言）；末端 H 根 NaN。
    """
    r = np.log(close / close.shift(1)).to_numpy()
    r2 = r * r
    n = len(r2)
    out = np.full(n, np.nan)
    for t in range(n - horizon):
        w = r2[t + 1: t + 1 + horizon]
        if np.isfinite(w).all():
            out[t] = float(np.sqrt(w.mean()))
    return out


# ---------------------------------------------------------------- 条件技能

def episode_ids(states) -> list:
    """逐行 episode 编号（状态变化即递增）——结论门槛按它计数。"""
    ids, cur = [], -1
    prev = object()
    for s in states:
        if s != prev:
            cur += 1
            prev = s
        ids.append(cur)
    return ids


def conditional_skill(states, y, horizon, min_train=MIN_TRAIN):
    """逐 t 因果评分：条件池 vs 无条件池的 CRPS。

    t 的训练集 = 所有 u <= t-horizon 且 y_u 有限的观测（u+H <= t 已实现）。
    返回 (records, skipped)；record 带 ep（episode 编号）供有效样本计数。
    """
    n = len(states)
    eps = episode_ids(states)
    pool_all: list = []
    pool_by_state: dict = {}
    records, skipped = [], {"no_target": 0, "thin_pool": 0}
    for t in range(n):
        u = t - horizon
        if u >= 0 and np.isfinite(y[u]):
            pool_all.append(float(y[u]))
            pool_by_state.setdefault(states[u], []).append(float(y[u]))
        if not np.isfinite(y[t]):
            skipped["no_target"] += 1
            continue
        cond = pool_by_state.get(states[t], [])
        if len(cond) < min_train or len(pool_all) < min_train:
            skipped["thin_pool"] += 1
            continue
        records.append({
            "t": t, "state": states[t], "ep": eps[t],
            "crps_c": crps_empirical(np.array(cond), y[t]),
            "crps_u": crps_empirical(np.array(pool_all), y[t]),
            "n_cond": len(cond),
        })
    return records, skipped


def skill_summary(records, block: int, n_boot: int = 1000, seed: int = 7):
    """技能 = 1 - Σc/Σu；循环移动块 bootstrap 给 CI。

    评审 p2->p3 修正：块用 (s+arange(block)) % n 取环，保证每次重抽恰好 n 条
    ——旧实现边界起点抽出短块，CI 是截断制造的。基线 Σu 退化（≈0）返回 None。
    """
    if not records:
        return None
    c = np.array([r["crps_c"] for r in records])
    u = np.array([r["crps_u"] for r in records])
    if not (np.isfinite(c).all() and np.isfinite(u).all()) or u.sum() <= 1e-300:
        return None  # 退化基线（如恒定价格）：不可定义，禁止给数
    skill = 1.0 - c.sum() / u.sum()
    n = len(c)
    n_ep = len({r["ep"] for r in records})
    out = {"skill": round(float(skill), 4), "n_eval": n, "n_episodes_eval": n_ep}
    if n_boot > 0:
        rng = np.random.default_rng(seed)
        block = max(1, min(block, n))
        n_blocks = int(np.ceil(n / block))
        boots = []
        for _ in range(n_boot):
            idx = np.concatenate([
                (s + np.arange(block)) % n for s in rng.integers(0, n, n_blocks)
            ])[:n]
            bu = u[idx].sum()
            if bu > 1e-300:
                boots.append(1.0 - c[idx].sum() / bu)
        if boots:
            lo, hi = np.percentile(boots, [2.5, 97.5])
            out["ci95"] = [round(float(lo), 4), round(float(hi), 4)]
    return out


def surrogate_band(states, y, horizon):
    """环移 surrogate 的技能范围——**描述性参照，不是显著性检验**。

    已知局限（评审 p3，写进协议）：状态由 ~250 根回看窗生成而序列总长仅
    ~300，环移后的状态标签"见过"目标窗的收益，无法构成干净零假设；带宽
    也未做多重检验校正。带的用途：把"条件池比无条件池小"的纯样本量 CRPS
    惩罚可视化，供解读时参照。出带 = 值得跟踪的探索性观察。

    偏移全枚举 [H+1, n-H-1]（闭区间，评审修正端点）；超过 MAX_SHIFTS 时
    等距抽取（确定性，无放回，无 RNG）。
    """
    n = len(states)
    lo, hi = horizon + 1, n - horizon - 1
    if hi < lo:
        return None
    offs = np.arange(lo, hi + 1)
    if len(offs) > MAX_SHIFTS:
        offs = offs[np.linspace(0, len(offs) - 1, MAX_SHIFTS).astype(int)]
    skills = []
    for off in offs:
        shifted = list(states[int(off):]) + list(states[:int(off)])
        recs, _ = conditional_skill(shifted, y, horizon)
        s = skill_summary(recs, block=2 * horizon, n_boot=0)
        if s is not None and np.isfinite(s["skill"]):
            skills.append(s["skill"])
    if len(skills) < 10:
        return None
    arr = np.array(skills)
    return {
        "lo": round(float(np.percentile(arr, 2.5)), 4),
        "hi": round(float(np.percentile(arr, 97.5)), 4),
        "mean": round(float(arr.mean()), 4),
        "n_shifts": len(skills),
    }


# ---------------------------------------------------------------- episode 统计

def episode_stats(states, ts_ms):
    runs = []
    for i, s in enumerate(states):
        if runs and runs[-1]["state"] == s:
            runs[-1]["bars"] += 1
        else:
            runs.append({"state": s, "start_ts": int(ts_ms[i]), "bars": 1})
    durations: dict = {}
    for r in runs:
        durations.setdefault(r["state"], []).append(r["bars"])
    trans: dict = {}
    for a, b in zip(runs, runs[1:]):
        trans.setdefault(a["state"], {}).setdefault(b["state"], 0)
        trans[a["state"]][b["state"]] += 1
    return {
        "n_episodes": len(runs),
        "durations": {
            s: {"n": len(d), "median": float(np.median(d)), "max": int(max(d))}
            for s, d in durations.items()
        },
        # 末段仍在进行中：其"转出"未知，转移计数天然比 episode 少 1
        "transitions": trans,
    }


# ---------------------------------------------------------------- 方向对照（次级）

def direction_pnl(states, close, ts_ms, tf_ms, fee_bps=FEE_BPS, funding=None):
    """trend_up→+1 / trend_down→-1 / 其余 0，作用于下一根收益。次级指标。

    口径（评审 p3 统一）：实际持仓路径 = pos[:-1]（末根信号没有收益腿，
    不计持仓、不计费）；费用 = 建仓 |pos[0]| + 路径内换仓 |Δpos|，末根
    信号引发的换仓不计。funding 结算落在 bar i 记给 pos[i-1]（bar i 期间
    实际持有的仓位，与 gross = pos[i-1]·r[i] 同口径）；ts 必须等距
    （_load_series 已断言网格），bar 区间 [ts, ts+tf_ms)。
    """
    n = len(states)
    if n < 2:
        return {"insufficient": n}
    closes = np.asarray(close, dtype=float)
    if not np.isfinite(closes).all() or (closes <= 0).any():
        return {"error": "close 含非有限或非正值"}
    r = np.concatenate([[np.nan], np.log(closes[1:] / closes[:-1])])
    pos = np.array([1.0 if s == "trend_up" else (-1.0 if s == "trend_down" else 0.0)
                    for s in states])
    held = pos[:-1]                       # bar i (i>=1) 期间持有 pos[i-1]
    gross = float((held * r[1:]).sum())
    trades = abs(pos[0]) + float(np.abs(np.diff(pos[:-1])).sum())
    fees = trades * fee_bps / 10_000
    out = {
        "n_bars": n,
        "n_position_bars": int((held != 0).sum()),
        "gross_sum": round(gross, 5),
        "fee_sum": round(fees, 5),
        "net_after_fee": round(gross - fees, 5),
    }
    if funding:
        fund = 0.0
        f_sorted = sorted(funding)
        j = 0
        for i in range(1, n):
            lo_t, hi_t = int(ts_ms[i]), int(ts_ms[i]) + int(tf_ms)
            while j < len(f_sorted) and f_sorted[j][0] < lo_t:
                j += 1
            k = j
            while k < len(f_sorted) and f_sorted[k][0] < hi_t:
                fund += pos[i - 1] * float(f_sorted[k][1])  # 多头付正费率
                k += 1
        out["funding_sum"] = round(fund, 5)
        out["net_after_fee_funding"] = round(gross - fees - fund, 5)
    return out


# ---------------------------------------------------------------- 装配与账本

def _load_series(conn, symbol, tf):
    """取完整 K 线网格 + 对齐的状态尾段。

    硬断言（评审 p3）：状态行按 (RULES_VERSION, AUDIT_VERSION) 过滤后必须
    构成 Δts == TF 的**连续尾段**——版本空洞/缺根一律报错，绝不静默把不
    相邻 K 线压成相邻。锁箱按收线时刻（ts + TF <= 锁箱起点）裁剪 K 线与状态。
    """
    step = TF_MS[tf]
    df = storage.get_ohlcv(conn, symbol, tf, limit=20_000)
    ts_all = np.array([int(t) for t in storage.ts_to_ms(df["ts"])])
    inbox = ts_all + step <= LOCKBOX_START_MS
    df = df[inbox].reset_index(drop=True)
    ts_all = ts_all[inbox]
    if len(ts_all) >= 2 and not (np.diff(ts_all) == step).all():
        raise RuntimeError(f"{symbol} {tf}: K 线网格有缺根，回测拒绝该序列")

    rows_all = storage.get_states_audit(conn, symbol, tf)
    rows = [r for r in rows_all
            if r["version"] == RULES_VERSION and r["audit_version"] == AUDIT_VERSION
            and r["ts"] + step <= LOCKBOX_START_MS]
    stats = {
        "dropped_version": sum(
            1 for r in rows_all
            if not (r["version"] == RULES_VERSION and r["audit_version"] == AUDIT_VERSION)),
        "dropped_lockbox": sum(
            1 for r in rows_all if r["ts"] + step > LOCKBOX_START_MS),
    }
    if not rows:
        return df, [], -1, stats
    s_ts = np.array([r["ts"] for r in rows])
    if len(s_ts) >= 2 and not (np.diff(s_ts) == step).all():
        raise RuntimeError(f"{symbol} {tf}: 状态序列有版本空洞/缺根，需重算后再回测")
    idx = np.searchsorted(ts_all, s_ts[0])
    if idx >= len(ts_all) or ts_all[idx] != s_ts[0] or ts_all[-1] != s_ts[-1] \
            or len(ts_all) - idx != len(rows):
        raise RuntimeError(
            f"{symbol} {tf}: 状态行不是 K 线连续尾段（K线 {len(ts_all)} 根 vs "
            f"状态 {len(rows)} 行, 首状态对位 {idx}）")
    return df, rows, int(idx), stats


def _funding_events(conn, symbol):
    """已结算 funding 事件 [(ts_ms, rate)]。ts 已是毫秒整数（评审实证：
    再过 ts_to_ms 会 //1e6 变成 1970 年，全部结算被静默跳过）。无数据返回 None，
    有无 funding 由数据说话，不做品种白名单。"""
    try:
        d = storage.get_deriv_col(conn, symbol, "funding", limit=6000, hourly_grid=True)
    except Exception:  # noqa: BLE001
        return None
    if d is None or not len(d):
        return None
    return [(int(t), float(v)) for t, v in zip(d["ts"], d["funding"])
            if v is not None and np.isfinite(float(v))]


def run_experiment(conn, symbols, tfs=("1h", "4h", "1d"), note=""):
    """对每个 symbol×tf×H 出条件技能 + surrogate 带 + episode + 方向对照。

    整轮读取包在一个读事务里（WAL snapshot）——否则 collector 并发写入
    会让先后两次 SELECT 看到不同世代的数据。
    """
    results, manifest = [], {}
    meta = {
        # 评估协议版本：评分/零假设/分层任何口径变化必须递增（进 experiment_id）
        # p2: 环移零带; p3: 评审修正批——完整网格目标/收线锁箱/环块 bootstrap/
        #     surrogate 降级为描述性/episode 门槛/funding 时间戳与归属/费用口径
        "protocol": 3,
        "rules_version": RULES_VERSION, "audit_version": AUDIT_VERSION,
        "lockbox_start": LOCKBOX_START, "min_train": MIN_TRAIN,
        "min_eval": MIN_EVAL, "min_episodes": MIN_EPISODES, "fee_bps": FEE_BPS,
        "horizons": HORIZONS, "symbols": list(symbols), "tfs": list(tfs),
        "max_shifts": MAX_SHIFTS,
    }
    conn.execute("BEGIN")  # 只读事务：钉住整轮的 WAL 快照
    try:
        for sym in symbols:
            for tf in tfs:
                try:
                    df, rows, idx, drops = _load_series(conn, sym, tf)
                except Exception as e:  # noqa: BLE001  单序列失败不拖垮整轮
                    results.append({"symbol": sym, "tf": tf, "error": str(e)})
                    continue
                if len(rows) < MIN_TRAIN * 2:
                    results.append(
                        {"symbol": sym, "tf": tf, "insufficient": len(rows), **drops})
                    continue
                states = [r["state"] for r in rows]
                warm = [bool(r["features"].get("warmup", False)) for r in rows]
                ts_ms = [int(t) for t in storage.ts_to_ms(df["ts"])]
                s_ts = ts_ms[idx:]
                manifest[f"{sym}|{tf}"] = [len(rows), rows[0]["ts"], rows[-1]["ts"]]
                entry = {"symbol": sym, "tf": tf, "n_rows": len(rows),
                         "n_warmup": int(sum(warm)), **drops, "horizons": {}}
                for H in HORIZONS[tf]:
                    # 目标在完整网格上算，再切到状态尾段——版本空洞已被断言排除，
                    # 这里防的是"状态起点之前的 K 线也参与收益差分"的对位错误
                    y = future_rv(df["close"], H)[idx:]
                    recs, skipped = conditional_skill(states, y, H)
                    strata = {"all": skill_summary(recs, block=2 * H)}
                    recs_m = [r for r in recs if not warm[r["t"]]]
                    strata["mature"] = skill_summary(recs_m, block=2 * H)
                    per_state = {
                        s: skill_summary([r for r in recs if r["state"] == s],
                                         block=2 * H, n_boot=0)
                        for s in set(states)
                    }
                    band = surrogate_band(states, y, H)
                    sk = strata["all"]
                    entry["horizons"][f"H{H}"] = {
                        "skipped": skipped, "strata": strata,
                        "per_state": per_state, "surrogate": band,
                        "vs_band": (
                            None if (band is None or sk is None) else
                            ("above" if sk["skill"] > band["hi"] else
                             "below" if sk["skill"] < band["lo"] else "inside")),
                    }
                entry["episodes"] = episode_stats(states, s_ts)
                entry["direction"] = direction_pnl(
                    states, df["close"].iloc[idx:].to_numpy(), s_ts, TF_MS[tf],
                    funding=_funding_events(conn, sym))
                results.append(entry)
    finally:
        conn.execute("ROLLBACK")
    meta["data_manifest"] = manifest
    meta["data_cutoff_ms"] = max(
        (v[2] + TF_MS[k.split("|")[1]] for k, v in manifest.items()), default=0)
    return {"meta": meta, "results": results}


def _ledger_conn():
    path = LEDGER_PATH or (storage.DATA_DIR + "/backtest_ledger.sqlite3")
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS experiments ("
        " id TEXT PRIMARY KEY, created_utc TEXT, git_hash TEXT,"
        " data_cutoff_ms INTEGER, params TEXT, metrics TEXT, note TEXT)"
    )
    return conn


def record_experiment(payload, code_digest, note=""):
    """trial ledger：不可变追加。

    id = sha256(参数含数据清单 + 代码摘要)[:12]——参数、协议版本、代码内容、
    每序列行数/首末 ts 任一变化都会换 id；同 id 重跑（同设置同数据同代码，
    bootstrap 有种子故确定）幂等忽略。id 相同而 metrics 不同视为异常，报错
    而不是静默保留旧值（评审 p3：OR IGNORE 会让报告与账本各说各话）。
    """
    params = json.dumps(payload["meta"], sort_keys=True, ensure_ascii=False,
                        allow_nan=False)
    exp_id = hashlib.sha256(f"{params}|{code_digest}".encode()).hexdigest()[:12]
    body = json.dumps(payload["results"], sort_keys=True, ensure_ascii=False,
                      default=lambda o: None)
    conn = _ledger_conn()
    try:
        with conn:
            old = conn.execute(
                "SELECT metrics FROM experiments WHERE id=?", (exp_id,)).fetchone()
            if old is not None and old[0] != body:
                raise RuntimeError(
                    f"experiment_id {exp_id} 已存在但 metrics 不同——"
                    "同一设置产生了不同结果，先查非确定性来源，禁止覆盖")
            conn.execute(
                "INSERT OR IGNORE INTO experiments VALUES (?,?,?,?,?,?,?)",
                (exp_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 code_digest, int(payload["meta"].get("data_cutoff_ms", 0)),
                 params, body, note),
            )
    finally:
        conn.close()
    return exp_id
