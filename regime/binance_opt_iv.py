"""币安期权近端 IV 合成（XAU/XAG，24/7）。

为什么是"近端"而不是 IV30：实测（2026-08-05）币安贵金属期权只挂两个到期
（当日 + 本周五，最远 3.2 天）。从 3 天外推 30 天是造假，不做。
30 天口径由 moomoo 的 GLD/SLV 代理承担（vol_proxy，3.1 年史）；本模块给的是
**它独有的东西：夜间与周末的贵金属隐波**——GLD 期权只在美股 RTH 更新，
而币安期权 24/7 报价。

合成法（2026-08-05 稳健化，历史清零重攒）：每个到期先剔除**占位 mark 簇**
（完全相同的 markIV 出现 ≥3 次＝平台钉住的占位值，实测 XAG 链一半报价钉在 110.0），
再取距指数价最近 K_NEAR_STRIKES 个行权价上的全部有效 call/put 报价，取**中位数**。
首版"单 ATM 行权价 C/P 均值"对单个坏 mark 极敏感——实测 XAU 35 分钟内 23.7→38.2→31.0
来回跳、XAG 瞬时 87.5，全是单点坏报价，非行情。中位数聚合后同链复探稳定。
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
# 近端 IV 覆盖的标的（2026-08-05 扩容：加密 6 个——持仓周期 1-3 天与近端期限天然匹配，
# DVOL/IV30 是 30 天口径、对该周期"太深"。实测到期结构：BTC/ETH 有日+周+月，
# SOL/BNB 至 ~24 天，XRP/DOGE 较浅——3 天目标期限全部可达）
UNDERLYINGS = {
    "XAU-USDT": "XAUUSDT", "XAG-USDT": "XAGUSDT",
    "BTC-USDT": "BTCUSDT", "ETH-USDT": "ETHUSDT",
    "SOL-USDT": "SOLUSDT", "BNB-USDT": "BNBUSDT",
    "XRP-USDT": "XRPUSDT", "DOGE-USDT": "DOGEUSDT",
}
TARGET_DAYS = 3.0          # 目标常数期限（贴近实际挂牌结构）
MIN_TENOR_DAYS = 0.2       # 距到期 <4.8 小时的合约临近结算 IV 不稳，剔除
YEAR_DAYS = 365.0
K_NEAR_STRIKES = 5         # ATM 邻域宽度：距指数价最近的 5 个行权价（其上全部报价进中位数）
DUP_PLACEHOLDER = 3        # 同一到期内完全相同的 markIV ≥3 次 → 占位值簇，整簇剔除


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


def snapshot_all(symbols=None, now_ms: int | None = None) -> list:
    """一轮取全部标的的近端 IV。exchangeInfo 与 mark 是**全市场响应**，
    只拉一次共用（8 个标的逐个调 fetch_chain 会浪费 14 次全量请求）；
    仅 index 价逐标的取。单个标的失败不拖累其余。"""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    want = {s: u for s, u in UNDERLYINGS.items()
            if symbols is None or s in symbols}
    if not want:
        return []
    info = _get("/exchangeInfo")
    by_u: dict[str, list] = {}
    for c in info["optionSymbols"]:
        by_u.setdefault(c.get("underlying"), []).append(c)
    marks = {m["symbol"]: m for m in _get("/mark")}
    out = []
    for sym, u in want.items():
        contracts = by_u.get(u) or []
        if not contracts:
            continue
        try:
            idx = float(_get(f"/index?underlying={u}")["indexPrice"])
            r = synth_near_iv(contracts, marks, idx, now)
            if r:
                r.update(symbol=sym, ts=now, index_price=idx)
                out.append(r)
        except Exception:  # noqa: BLE001  单标的失败不拖累整轮
            continue
    return out


def synth_near_iv(contracts, marks, index_price: float, now_ms: int) -> dict | None:
    """纯函数：期权链 → 近端 ATM IV。返回 {iv(百分比), tenor_days, method, n_expiries}。

    每到期的稳健 ATM 估计（防单点坏 mark）：
    1. 剔除占位值簇——同一到期内完全相同的 markIV 出现 ≥DUP_PLACEHOLDER 次
       （真实 mark 逐行权价必然不同，成簇相同＝平台钉住的占位报价）；
    2. 取距指数价最近 K_NEAR_STRIKES 个行权价上的全部有效报价（call/put 都算）；
    3. 取中位数——单个坏 mark（宽价差/陈旧报价）拉不动它。
    目标期限 TARGET_DAYS 被两到期夹住 → 总方差插值；
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
        quotes = d["quotes"]
        # ① 占位值簇剔除（在选邻域**之前**做——占位报价不许占据邻域名额）
        cnt: dict[float, int] = {}
        for _, iv in quotes:
            cnt[iv] = cnt.get(iv, 0) + 1
        quotes = [(dist, iv) for dist, iv in quotes if cnt[iv] < DUP_PLACEHOLDER]
        if not quotes:
            continue
        # ② ATM 邻域：最近 K 个不同距离档（对称行权价共享距离档，一并纳入）
        keep = set(sorted({dist for dist, _ in quotes})[:K_NEAR_STRIKES])
        sel = sorted(iv for dist, iv in quotes if dist in keep)
        # ③ 中位数
        n = len(sel)
        med = sel[n // 2] if n % 2 else (sel[n // 2 - 1] + sel[n // 2]) / 2
        points.append((d["tenor"], med))
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
