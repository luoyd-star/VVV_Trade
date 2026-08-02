"""衍生品持仓数据（Binance USDⓈ-M 公共接口）：OI、资金费率、Premium、主动买卖比。

这是与价格行为正交的"持仓/杠杆"信息轴（启发自 crypto-monitor 的设计）。
注意：Binance 的 OI 历史统计与主动买卖统计只保留约 30 天——因此首次运行立即回填
可得历史（OI/taker 按 1h×500≈20 天；funding 结算 1000 条≈333 天；premium 1h×500），
此后每个采集周期自采自存，历史越攒越长。
v1 仅采集/展示/注入 VVVhermes 上下文，不进状态机（是否有增量留给回测回答）。
"""
from __future__ import annotations

import time

import requests

FAPI = "https://fapi.binance.com"
_QUOTES = ("USDT", "USDC", "USD")


def binance_perp(symbol: str) -> str:
    sym = symbol.upper().replace("/", "").replace("-", "")
    if not sym.endswith(_QUOTES):
        sym = f"{sym}USDT"
    return sym


def _get(path: str, params: dict) -> object:
    r = requests.get(f"{FAPI}{path}", params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_snapshot(symbol: str) -> dict:
    """当前快照：funding/premium（premiumIndex）+ 当前 OI + 最近一根 5m 主动买卖比。"""
    sym = binance_perp(symbol)
    pi = _get("/fapi/v1/premiumIndex", {"symbol": sym})
    oi = _get("/fapi/v1/openInterest", {"symbol": sym})
    mark = float(pi["markPrice"])
    index = float(pi["indexPrice"])
    row = {
        "ts": int(time.time() * 1000),
        "oi": float(oi["openInterest"]),
        "oi_notional": float(oi["openInterest"]) * mark,
        "funding": float(pi["lastFundingRate"]),
        "premium": (mark - index) / index if index else None,
        "taker_ratio": None,
    }
    try:
        tk = _get(
            "/futures/data/takerlongshortRatio",
            {"symbol": sym, "period": "5m", "limit": 1},
        )
        if tk:
            row["taker_ratio"] = float(tk[-1]["buySellRatio"])
    except Exception:  # noqa: BLE001  taker 接口偶发不可用时不拖累其余指标
        pass
    return row


def backfill(symbol: str) -> list:
    """一次性回填历史，返回稀疏行列表（不同指标各自的时间网格，入库时按 ts 合并）。

    funding 每 8h 结算×1000≈333 天；OI/taker/premium 按 1h×500≈20.8 天
    （OI 与 taker 受 Binance 约 30 天保留期限制，这是能拿到的全部）。
    """
    sym = binance_perp(symbol)
    rows: list = []

    fr = _get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 1000})
    rows += [
        {"ts": int(x["fundingTime"]), "funding": float(x["fundingRate"])} for x in fr
    ]

    oh = _get(
        "/futures/data/openInterestHist",
        {"symbol": sym, "period": "1h", "limit": 500},
    )
    rows += [
        {
            "ts": int(x["timestamp"]),
            "oi": float(x["sumOpenInterest"]),
            "oi_notional": float(x["sumOpenInterestValue"]),
        }
        for x in oh
    ]

    tk = _get(
        "/futures/data/takerlongshortRatio",
        {"symbol": sym, "period": "1h", "limit": 500},
    )
    rows += [
        {"ts": int(x["timestamp"]), "taker_ratio": float(x["buySellRatio"])} for x in tk
    ]

    pk = _get(
        "/fapi/v1/premiumIndexKlines",
        {"symbol": sym, "interval": "1h", "limit": 500},
    )
    # [:-1] 丢掉未收线的末根——k[4] 是区间收盘值，末根此刻还在变。回填是一次性的，
    # 不裁的话那一行会带着"半根"的值永久留在库里（实测 10 个品种各坏 1 行）。
    # 只有这个接口需要裁：openInterestHist 是 ts 当刻点采样、takerlongshortRatio
    # 返回的本就是已收区间，两者都已定稿。
    rows += [{"ts": int(k[0]), "premium": float(k[4])} for k in pk[:-1]]
    return rows


def fetch_funding_history(symbol: str, limit: int = 1000) -> list:
    """已结算资金费率（8h 结算网格）。collector 每天补拉一次。

    没有这条补采，结算行就只有首轮回填那一批——窗口一旦滚过去，
    面板的"结算行过滤"会因样本归零而静默退回混口径兜底，修复悄悄失效。
    """
    fr = _get("/fapi/v1/fundingRate", {"symbol": binance_perp(symbol), "limit": limit})
    return [{"ts": int(x["fundingTime"]), "funding": float(x["fundingRate"])} for x in fr]


def funding_interval_h(symbol: str) -> float:
    """结算周期（小时）。Binance 部分品种已从 8h 改为 4h，直接问接口而不是猜。

    失败回退 8.0——比从数据反推安全：deriv.funding 一列里混着 8h 结算网格的
    历史行与每 300s 一行的快照预测值，用 ts 差分中位数反推，快照行一多就会
    把间隔算成 0.1h、年化虚增近百倍。
    """
    sym = binance_perp(symbol)
    try:
        for x in _get("/fapi/v1/fundingInfo", {}):
            if x.get("symbol") == sym:
                return float(x["fundingIntervalHours"])
    except Exception:  # noqa: BLE001
        pass
    return 8.0
