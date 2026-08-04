"""币安期权近端 IV 合成（XAU/XAG，24/7）。

为什么是"近端"而不是 IV30：实测（2026-08-05）币安贵金属期权只挂两个到期
（当日 + 本周五，最远 3.2 天）。从 3 天外推 30 天是造假，不做。
30 天口径由 moomoo 的 GLD/SLV 代理承担（vol_proxy，3.1 年史）；本模块给的是
**它独有的东西：夜间与周末的贵金属隐波**——GLD 期权只在美股 RTH 更新，
而币安期权 24/7 报价。

合成法：每个到期取 ATM（行权价最贴指数价）的 call/put markIV 均值；
目标常数期限 3 天，两到期夹住则按总方差线性插值（σ²T 线性），
夹不住取最近到期并记 method='nearest'。**期限逐日漂移（日内合约 1 天 +
周五周合约），tenor_days 必须随值一起落库**——不同期限的 IV 不是同一个量，
不记期限的序列没有分位意义（当前不算分位，仅参考展示）。
"""
from __future__ import annotations

import json
import time
import urllib.request

EAPI = "https://eapi.binance.com/eapi/v1"
UNDERLYINGS = {"XAU-USDT": "XAUUSDT", "XAG-USDT": "XAGUSDT"}
TARGET_DAYS = 3.0          # 目标常数期限（贴近实际挂牌结构）
MIN_TENOR_DAYS = 0.2       # 距到期 <4.8 小时的合约临近结算 IV 不稳，剔除
YEAR_DAYS = 365.0


def _get(path: str):
    req = urllib.request.Request(f"{EAPI}{path}", headers={"User-Agent": "vvv/1.0"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def fetch_chain(underlying: str) -> tuple:
    """(合约列表, markIV 字典, 指数价)。三个公开端点，无需鉴权。"""
    info = _get("/exchangeInfo")
    contracts = [s for s in info["optionSymbols"] if s.get("underlying") == underlying]
    marks = {m["symbol"]: m for m in _get("/mark")}
    idx = float(_get(f"/index?underlying={underlying}")["indexPrice"])
    return contracts, marks, idx


def synth_near_iv(contracts, marks, index_price: float, now_ms: int) -> dict | None:
    """纯函数：期权链 → 近端 ATM IV。返回 {iv(百分比), tenor_days, method, n_expiries}。

    每到期：ATM=行权价最贴指数价；IV=该行权价上可得的 call/put markIV 均值
    （markIV<=0 视为无效）。目标期限 TARGET_DAYS 被两到期夹住 → 总方差插值；
    否则取最近到期（method='nearest'，tenor_days 记实际期限）。
    """
    by_exp: dict[int, dict] = {}
    for c in contracts:
        exp = int(c["expiryDate"])
        tenor = (exp - now_ms) / 86_400_000.0
        if tenor < MIN_TENOR_DAYS:
            continue
        by_exp.setdefault(exp, {"tenor": tenor, "quotes": []})
        m = marks.get(c["symbol"])
        if not m:
            continue
        try:
            iv = float(m["markIV"])
        except (TypeError, ValueError, KeyError):
            continue
        if iv <= 0:
            continue
        by_exp[exp]["quotes"].append(
            (abs(float(c["strikePrice"]) - index_price), iv)
        )

    points = []   # (tenor_days, atm_iv)
    for exp, d in sorted(by_exp.items()):
        if not d["quotes"]:
            continue
        best = min(q[0] for q in d["quotes"])
        atm = [iv for dist, iv in d["quotes"] if abs(dist - best) < 1e-9]
        points.append((d["tenor"], sum(atm) / len(atm)))
    if not points:
        return None

    t_star = TARGET_DAYS
    lo = [p for p in points if p[0] <= t_star]
    hi = [p for p in points if p[0] >= t_star]
    if lo and hi and lo[-1][0] < hi[0][0]:
        (t1, v1), (t2, v2) = lo[-1], hi[0]
        # 总方差线性插值：σ*² t* = w·σ1²t1 + (1−w)·σ2²t2，w 按期限距离
        w = (t2 - t_star) / (t2 - t1)
        var_star = (w * v1 * v1 * t1 + (1 - w) * v2 * v2 * t2) / t_star
        return {"iv": round(var_star ** 0.5 * 100, 2), "tenor_days": t_star,
                "method": "interp", "n_expiries": len(points)}
    # 夹不住：取距目标最近的到期，期限如实记录
    t, v = min(points, key=lambda p: abs(p[0] - t_star))
    return {"iv": round(v * 100, 2), "tenor_days": round(t, 2),
            "method": "nearest", "n_expiries": len(points)}


def snapshot(symbol: str, now_ms: int | None = None) -> dict | None:
    """一个品种的当前近端 IV 快照（网络 + 合成）。"""
    u = UNDERLYINGS.get(symbol)
    if u is None:
        return None
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    contracts, marks, idx = fetch_chain(u)
    out = synth_near_iv(contracts, marks, idx, now)
    if out:
        out.update(symbol=symbol, ts=now, index_price=idx)
    return out
