"""美股个股 IV 期限曲线（3d/9d/30d ATM，moomoo 期权链）。

回答"IV30 对 1-3 天持仓太深"的美股侧方案：三个期限点把
**持仓前端（3d）/ 中段（9d）/ 制度层（30d）**分开。示范读数（探针实测）：
NVDA 3d ATM 44.4 vs IV30 48.0——差值正是 21 天后财报（在 30d 窗内、
不在 3d 窗内）的定价。倒挂（3d > 30d）= 近期有事；整条抬升 = 环境变了。

请求经济学：期权链与到期日**盘中不变 → 按 ET 日缓存**（每日一次 ~183 次链请求，
分摊在首轮）；盘中每轮只做 1-2 次批量快照（≤400 代码/次）取 ATM IV。
无厂商历史 → 只能自今日起积累（同宽度影子字段模式），暂不算分位。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TARGETS = (3, 9, 30)          # 目标期限（日历日）
MAX_SNAPSHOT = 300            # 单次快照代码数上限（官方 400，留余量）
# 首个 RTH 实测（2026-08-05）：PACE=0.5 即 2 次/秒 = 60 次/30 秒，**正好等于 moomoo
# 限额、零余量** → 61 品种 244 次请求中途撞线，散布式失败（只建成 26 品种、其中 10 个
# 仅 1 个期限）。单独复跑缺失品种（GOOGL）三条链全部正常，证明不是品种问题。
# 与 moomoo_iv.PACE=0.6（限额一半速率）对齐并再留余量。
PACE = 0.7
# 单轮建链的品种数上限：跨轮分批建，避免一轮打爆限频。
# **限频额度是 moomoo 账号级共享的**（2026-08-05 23:31 实测：本轮建链 24 品种
# ≈97 次请求，与同轮的"实时IV 63 品种批量"挤在一起 → 33 例链请求 ret=-1）。
# 三条 moomoo 管线各自独立计速，加总才是真实速率——故单轮建链量必须给其他管线留额度。
BUILD_BATCH = 16
# 为什么是 16，以及**加品种时必须重算这个数**（2026-08-07 实测教训）：
# 期权链按 ET 日缓存，每个交易日要为全部美股品种重建一次。单日建链容量 =
#     (RTH 秒数 / BUILD_INTERVAL_S) × BUILD_BATCH × 实测成功率
# 旧值 BUILD_BATCH=12 配 3600 秒节流，理论容量 6×12=72，是按当时 61 个美股品种
# 定的（余量 11）。X3 扩容到 71 个后余量只剩 1，而实测每轮只成功约 7 个
# （批次里的失败照样占名额），真实容量约 6×7=42 —— 每天有近 30 个品种拿不到
# 期限曲线。实测覆盖：8/05 56 个 → 8/06 45 个 → 8/07 只剩 13 个。
# 现值配 1800 秒节流：13 轮 × 16 × 0.58 ≈ 121，对 71 个品种留 70% 余量。
# 单轮请求量 16×3=48 次 × PACE 0.7s ≈ 34 秒，仍在 300 秒采集周期内，
# 且比"一轮塞更多"更不容易撞账号级限频——**宁可多轮小批，不要单轮大批**。
BUILD_INTERVAL_S = 1800   # 起步值，待校准；与 collector.sync_stock_iv_term 的节流同源


def build_capacity(n_symbols: int, rth_hours: float = 6.5,
                   success_rate: float = 0.58) -> dict:
    """单日建链容量测算——供 collector 自检，别再让扩容悄悄压垮这条管线。

    success_rate 起步值 0.58 来自 2026-08-07 实测（BUILD_BATCH=12 时"本轮补 7"）：
    批次里的失败品种不会退出 missing 集合，下一轮继续占名额，所以有效吞吐低于批量。
    """
    rounds = max(int(rth_hours * 3600 // BUILD_INTERVAL_S), 1)
    capacity = rounds * BUILD_BATCH * success_rate
    return {
        "rounds": rounds, "batch": BUILD_BATCH, "capacity": capacity,
        "need": n_symbols, "ok": capacity >= n_symbols,
        "margin_pct": (capacity / n_symbols - 1) * 100 if n_symbols else 0.0,
    }


def pick_expiries(exp_rows, targets=TARGETS) -> dict:
    """[(strike_time, distance_days)] → {target: (strike_time, actual_days)}。

    每个目标取**距离最近**的到期；美股周度期权极密（实测 NVDA 1/3/6/8/10/13…天），
    实际期限≈目标。不同目标可落到同一到期（如极端稀疏时），如实记录各自 actual。
    """
    out = {}
    if not exp_rows:
        return out
    for tgt in targets:
        best = min(exp_rows, key=lambda r: abs(r[1] - tgt))
        out[tgt] = (best[0], int(best[1]))
    return out


def atm_pair(chain_rows, spot: float):
    """链 [(code, option_type, strike)] → ATM 行权价上的 (call_code, put_code)。"""
    if not chain_rows or not spot:
        return None
    strikes = sorted({r[2] for r in chain_rows}, key=lambda k: abs(k - spot))
    k = strikes[0]
    call = next((r[0] for r in chain_rows if r[2] == k and r[1] == "CALL"), None)
    put = next((r[0] for r in chain_rows if r[2] == k and r[1] == "PUT"), None)
    return call, put, k


BUILD_GROUPS = 10        # 起步值，待校准；建链的固定分组数（见 build_group 的取舍）
MAX_DAY_FAILS = 2        # 起步值，待校准；同一 ET 日内失败几次后当天不再重试


def build_group(symbols, round_idx: int, n_groups: int = BUILD_GROUPS) -> list:
    """按**固定分组**取本轮该建链的品种（2026-08-07 用户裁决）。

    与旧的"轮换起点 + 取前 BUILD_BATCH 个缺失"相比，固定分组换来**确定性**：
    品种 X 永远落在第 (index % n_groups) 组，什么时候建可预期、出问题可排查。
    另一个好处是每轮请求量更小更均匀——71 品种分 10 组每组约 7 个、
    每品种 ~4 次请求 ≈28 次/轮，比一轮 16 个（≈64 次）更不容易撞账号级限频。

    **只对建链分组，快照绝不分组**：快照是 2 次请求覆盖全部品种（批量的意义所在），
    拆开会让成本涨 10 倍，还会毁掉横截面可比性——不同品种采样于不同时刻就没法
    横向比"今天谁的 3d IV 最贵"，而横截面正是总览页的立身之本。
    """
    if not symbols:
        return []
    n = max(int(n_groups), 1)
    idx = int(round_idx) % n
    return [s for i, s in enumerate(symbols) if i % n == idx]


def build_codes(ctx, symbols, existing=None, skip=None) -> tuple:
    """按 ET 日构建 {symbol: {target: {codes, tenor, expiry}}}（链静态，日缓存）。

    每品种：1 次到期日 + ≤3 次链 + 现价共享批量快照。

    **增量补全 + 跨轮分批**（2026-08-05 首个 RTH 暴露）：`existing` 传入当日已建成的
    代码表，本次只补缺失品种、且单轮最多 BUILD_BATCH 个——一轮打满 61 品种会撞
    moomoo 限频，导致散布式静默失败。返回 (合并后的表, 失败明细)；**失败必须
    上报给调用方打日志**，绝不静默 continue（与 CBOE iv30 的同型缺陷 GAPS E1 一致）。
    """
    from moomoo import RET_OK

    from . import moomoo_iv

    out = dict(existing or {})
    fails: list = []
    # 只补缺失品种；期限不全（<len(TARGETS)）的也算缺失，下一轮继续补。
    # skip 是当日已达失败上限的品种：它们**不该继续占名额**——实测每轮 16 个槽位里
    # 有 4~6 个在重试注定失败的品种（PANW/EWY/EWJ/APP），成功率因此只有 58%。
    blocked = set(skip or ())
    todo = [s for s in symbols
            if len(out.get(s) or {}) < len(TARGETS) and s not in blocked][:BUILD_BATCH]
    if not todo:
        return out, fails

    # 现价一次批量取（只取本批，省额度）
    ret, snap = ctx.get_market_snapshot([moomoo_iv.to_moomoo(s) for s in todo])
    time.sleep(PACE)
    if ret != RET_OK:
        raise RuntimeError(f"spot snapshot: {snap}")
    spot = {r["code"]: float(r["last_price"]) for _, r in snap.iterrows()
            if r.get("last_price") == r.get("last_price")}

    for sym in todo:
        code = moomoo_iv.to_moomoo(sym)
        s = spot.get(code)
        if not s:
            fails.append(f"{sym}:无现价")
            continue
        try:
            ret, exp = ctx.get_option_expiration_date(code)
            time.sleep(PACE)
            if ret != RET_OK or not len(exp):
                fails.append(f"{sym}:到期日({ret})")
                continue
            rows = [(str(r["strike_time"]), int(r["option_expiry_date_distance"]))
                    for _, r in exp.iterrows()
                    if int(r["option_expiry_date_distance"]) >= 1]
            picks = pick_expiries(rows)
            entry = dict(out.get(sym) or {})
            seen_exp = {}
            for tgt, (etime, days) in picks.items():
                if str(tgt) in entry:
                    continue          # 该期限上一轮已建成，不重复请求
                if etime not in seen_exp:
                    ret2, ch = ctx.get_option_chain(code, start=etime, end=etime)
                    time.sleep(PACE)
                    if ret2 != RET_OK or not len(ch):
                        fails.append(f"{sym}/{tgt}d:链({ret2})")
                        continue
                    seen_exp[etime] = [(str(r["code"]), str(r["option_type"]),
                                        float(r["strike_price"]))
                                       for _, r in ch.iterrows()]
                pair = atm_pair(seen_exp[etime], s)
                if pair and pair[0] and pair[1]:
                    entry[str(tgt)] = {"call": pair[0], "put": pair[1],
                                       "strike": pair[2], "tenor": days,
                                       "expiry": etime}
                else:
                    fails.append(f"{sym}/{tgt}d:无ATM对")
            if entry:
                out[sym] = entry
        except Exception as e:  # noqa: BLE001  单品种失败不拖累整轮，但必须留痕
            fails.append(f"{sym}:{type(e).__name__}")
            continue
    return out, fails


def fetch_term(ctx, codes_map: dict, now_ms: int | None = None) -> list:
    """按日缓存的代码表 → 批量快照 → 每品种期限曲线与内存 ``legs`` 标记。

    ATM IV 正常取同行权价 C/P 的 option_implied_volatility 均值；单腿缺失时
    如实退化为有效单腿。返回行的 ``legs`` 为 ``{"3": 1|2, ...}``，仅存在
    IV 的期限会出现，供 collector 告警；该标记只在内存传递，不改存储表结构。
    快照分批 ≤MAX_SNAPSHOT。IV<=0 或缺失按 None，品种整行全空则跳过。
    """
    from moomoo import RET_OK

    now = int(now_ms if now_ms is not None else time.time() * 1000)
    all_codes = []
    for entry in codes_map.values():
        for t in entry.values():
            all_codes += [t["call"], t["put"]]
    ivs = {}
    for i in range(0, len(all_codes), MAX_SNAPSHOT):
        batch = all_codes[i:i + MAX_SNAPSHOT]
        ret, q = ctx.get_market_snapshot(batch)
        time.sleep(PACE)
        if ret != RET_OK:
            continue
        for _, r in q.iterrows():
            v = r.get("option_implied_volatility")
            if v is not None and v == v and float(v) > 0:
                ivs[r["code"]] = float(v)
    out = []
    for sym, entry in codes_map.items():
        row = {"symbol": sym, "ts": now, "legs": {}}
        got = False
        for tgt in TARGETS:
            t = entry.get(str(tgt))
            iv = None
            if t:
                vals = [ivs.get(t["call"]), ivs.get(t["put"])]
                vals = [v for v in vals if v is not None]
                iv = round(sum(vals) / len(vals), 2) if vals else None
                if vals:
                    row["legs"][str(tgt)] = len(vals)
            row[f"iv{tgt}"] = iv
            row[f"t{tgt}"] = t["tenor"] if t else None
            got = got or iv is not None
        if got:
            out.append(row)
    return out


def et_date_key() -> str:
    return datetime.now(ET).date().isoformat()


def load_codes_cache(conn) -> dict | None:
    """日缓存读取：跨 ET 日即失效（到期日的 distance 每天都在变）。"""
    from . import storage

    raw = storage.get_meta(conn, "iv_term_codes")
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if d.get("date") != et_date_key():
        return None
    return d.get("codes") or None


def save_codes_cache(conn, codes: dict) -> None:
    from . import storage

    storage.set_meta(conn, "iv_term_codes",
                     json.dumps({"date": et_date_key(), "codes": codes}))


def load_build_fails(conn) -> dict:
    """当日建链失败计数（跨 ET 日清零，与代码表缓存同一失效口径）。"""
    from . import storage

    raw = storage.get_meta(conn, "iv_term_build_fails")
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return d.get("fails") or {} if d.get("date") == et_date_key() else {}


def save_build_fails(conn, fails: dict) -> None:
    from . import storage

    storage.set_meta(conn, "iv_term_build_fails",
                     json.dumps({"date": et_date_key(), "fails": fails}))


def count_fails(prev: dict, fail_lines, attempted, built_now: int) -> dict:
    """把本轮失败明细并入当日计数——**但只在能区分"品种问题"与"限频"时才计**。

    这是本函数存在的唯一理由：撞账号级限频时**整批一起失败**，若无脑计数，
    一次限频就会把当轮全部品种拉黑一天，比不做退避更糟。
    判据：本轮**至少建成一个**品种，才说明链路是通的、失败是品种自身的问题。
    整批全灭一律视为系统性故障，不计任何品种的账。

    fail_lines 形如 ``"PANW-USDT/3d:链(-1)"`` 或 ``"EWJ-USDT:无现价"``——
    同一品种的多个期限只算一次失败，否则三个 target 会让计数三倍速膨胀。
    """
    out = dict(prev or {})
    if built_now <= 0:
        return out
    attempted = list(attempted or ())
    failed_syms = set()
    for line in fail_lines or ():
        sym = str(line).split(":", 1)[0].split("/", 1)[0]
        if sym in attempted:
            failed_syms.add(sym)
    for sym in failed_syms:
        out[sym] = int(out.get(sym, 0)) + 1
    # 本轮建成的品种把账清掉：偶发失败后成功了就不该继续记恨
    return out


def blocked_symbols(fails: dict, limit: int = MAX_DAY_FAILS) -> set:
    """当日已达失败上限、不该再占建链名额的品种。"""
    return {s for s, n in (fails or {}).items() if int(n) >= limit}
