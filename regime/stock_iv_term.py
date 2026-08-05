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
PACE = 0.5


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


def build_codes(ctx, symbols) -> dict:
    """按 ET 日构建 {symbol: {target: {codes, tenor, expiry}}}（链静态，日缓存）。

    每品种：1 次到期日 + ≤3 次链 + 现价共享批量快照。61 品种约 4 分钟（限频内）。
    """
    from moomoo import RET_OK

    from . import moomoo_iv

    # 现价一次批量取
    ret, snap = ctx.get_market_snapshot([moomoo_iv.to_moomoo(s) for s in symbols])
    time.sleep(PACE)
    if ret != RET_OK:
        raise RuntimeError(f"spot snapshot: {snap}")
    spot = {r["code"]: float(r["last_price"]) for _, r in snap.iterrows()
            if r.get("last_price") == r.get("last_price")}

    out = {}
    for sym in symbols:
        code = moomoo_iv.to_moomoo(sym)
        s = spot.get(code)
        if not s:
            continue
        try:
            ret, exp = ctx.get_option_expiration_date(code)
            time.sleep(PACE)
            if ret != RET_OK or not len(exp):
                continue
            rows = [(str(r["strike_time"]), int(r["option_expiry_date_distance"]))
                    for _, r in exp.iterrows()
                    if int(r["option_expiry_date_distance"]) >= 1]
            picks = pick_expiries(rows)
            entry = {}
            seen_exp = {}
            for tgt, (etime, days) in picks.items():
                if etime not in seen_exp:
                    ret2, ch = ctx.get_option_chain(code, start=etime, end=etime)
                    time.sleep(PACE)
                    if ret2 != RET_OK or not len(ch):
                        continue
                    seen_exp[etime] = [(str(r["code"]), str(r["option_type"]),
                                        float(r["strike_price"]))
                                       for _, r in ch.iterrows()]
                pair = atm_pair(seen_exp[etime], s)
                if pair and pair[0] and pair[1]:
                    entry[str(tgt)] = {"call": pair[0], "put": pair[1],
                                       "strike": pair[2], "tenor": days,
                                       "expiry": etime}
            if entry:
                out[sym] = entry
        except Exception:  # noqa: BLE001  单品种失败不拖累整轮
            continue
    return out


def fetch_term(ctx, codes_map: dict, now_ms: int | None = None) -> list:
    """按日缓存的代码表 → 批量快照 → 每品种 {ts, iv3, t3, iv9, t9, iv30, t30}。

    ATM IV = 同行权价 call/put 的 option_implied_volatility 均值（探针实测两者差 <0.5pt）。
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
        row = {"symbol": sym, "ts": now}
        got = False
        for tgt in TARGETS:
            t = entry.get(str(tgt))
            iv = None
            if t:
                vals = [ivs.get(t["call"]), ivs.get(t["put"])]
                vals = [v for v in vals if v is not None]
                iv = round(sum(vals) / len(vals), 2) if vals else None
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
