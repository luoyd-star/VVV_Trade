"""moomoo OpenAPI 个股波动率（协议 3303/3304，SDK ≥10.8.6808）。

为什么要它：CBOE 免费延迟源只能从今天起往前攒（实测攒了 3.75 天），分位算不出来；
moomoo 一次能回填到 2023-06-26（实测 3.10 年、778 个交易日、零缺口）。

口径警告：moomoo 的 iv 是其自家期权链聚合值（加权法未公开），与 CBOE iv30、
ORATS iv30d **不同源**，绝对值有系统性偏差。因此 stock_vol 表把 source 放进主键，
分位只在单一口径内计算——见 storage.upsert_stock_vol。

依赖本机 OpenD 常驻（凭据只在 OpenD 侧，本模块只连 127.0.0.1:11111）。
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

SOURCE = "moomoo"
HOST, PORT = "127.0.0.1", 11111
PACE = 0.6           # 秒/次；官方限频 60 次/30 秒，取一半速率
MAX_SPAN_DAYS = 360  # 单次请求跨度上限 364 天，留余量
# 服务端统一保留边界（2026-08-04 实测：AAPL/NVDA/QQQ/SPY 起点完全一致）
DATA_FLOOR = "2023-06-26"


def day_ms(d: date) -> int:
    """交易日 → 该日 00:00 UTC 毫秒。与 usvol/ref_daily 的日频时间格一致。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def to_moomoo(symbol: str) -> str:
    """内部符号（NVDA-USDT，币安永续命名）→ moomoo 代码（US.NVDA）。"""
    return "US." + symbol.split("-")[0]


def opend_alive(host: str = HOST, port: int = PORT) -> bool:
    """TCP 预检。SDK 连不上时无限重试而不抛错，无人值守下会静默挂死——必须先探端口。"""
    import socket

    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex((host, port)) == 0


def open_ctx(host: str = HOST, port: int = PORT):
    from moomoo import OpenQuoteContext

    return OpenQuoteContext(host=host, port=port)


def fetch_overview(ctx, symbols) -> dict:
    """批量当前值（协议 3303，单次 ≤500 标的）→ {symbol: {iv, iv_rank, iv_percentile, hv_30d}}。

    注意：实测该值等于上一交易日结算值（盘前探测时与历史末行完全相等），
    是否盘中滚动更新须在 RTH 内复测——见 docs/RESEARCH_IV_PLATFORMS_20260804.md 开放项。
    """
    from moomoo import RET_OK

    codes = [to_moomoo(s) for s in symbols]
    ret, data = ctx.get_option_underlying_overview(codes)
    if ret != RET_OK:
        raise RuntimeError(f"3303 失败: {data}")
    back = {to_moomoo(s): s for s in symbols}
    out = {}
    for _, r in data.iterrows():
        sym = back.get(r["code"])
        if sym is None:
            continue
        out[sym] = {
            k: (None if r.get(k) is None else float(r[k]))
            for k in ("iv", "iv_rank", "iv_percentile", "hv_30d")
            if k in data.columns
        }
    return out


def fetch_history(ctx, symbol: str, begin: str, end: str) -> list:
    """日频历史（协议 3304）→ [{ts, iv, hv, underlying_price}]，按 ts 升序。

    单次跨度上限 364 天，故按 MAX_SPAN_DAYS 分段；每段内还要处理分页。
    **返回值按时间倒序**——踩过坑：取首行当"最早日期"会拿到最晚的一天。
    """
    from moomoo import RET_OK

    code = to_moomoo(symbol)
    b, e = date.fromisoformat(begin), date.fromisoformat(end)
    rows: dict[int, dict] = {}
    seg_start = b
    while seg_start <= e:
        seg_end = min(seg_start + timedelta(days=MAX_SPAN_DAYS), e)
        key = None
        while True:
            ret, data, key = ctx.get_option_underlying_his_volatility(
                code,
                begin_time=seg_start.isoformat(),
                end_time=seg_end.isoformat(),
                page_req_key=key,
            )
            time.sleep(PACE)
            if ret != RET_OK:
                raise RuntimeError(f"3304 {symbol} {seg_start}~{seg_end} 失败: {data}")
            for _, r in data.iterrows():
                d = date.fromisoformat(str(r["time"])[:10])
                rows[day_ms(d)] = {
                    "ts": day_ms(d),
                    "iv": _num(r.get("iv")),
                    "hv": _num(r.get("hv")),
                    "underlying_price": _num(r.get("underlying_price")),
                }
            if key is None:
                break
        seg_start = seg_end + timedelta(days=1)
    return [rows[k] for k in sorted(rows)]


def fetch_option_stat(ctx, symbol: str, begin: str, end: str) -> list:
    """期权流日频历史（get_option_underlying_his_statistic）→ put/call 成交比与持仓比。

    **纯采集**：这份数据的样本外证据尚在调研，先留住易逝的历史，不进任何判定。
    与 3304 同形态（≤364 天分段 + 分页 + 倒序），故复用同一套循环骨架。
    注意当日 open_interest 为 0、比值为 'N/A'（T-1 延迟），故 OI 列会先空后补。
    """
    from moomoo import RET_OK

    code = to_moomoo(symbol)
    b, e = date.fromisoformat(begin), date.fromisoformat(end)
    rows: dict[int, dict] = {}
    seg_start = b
    while seg_start <= e:
        seg_end = min(seg_start + timedelta(days=MAX_SPAN_DAYS), e)
        key = None
        while True:
            ret, data, key = ctx.get_option_underlying_his_statistic(
                code,
                begin_time=seg_start.isoformat(),
                end_time=seg_end.isoformat(),
                page_req_key=key,
            )
            time.sleep(PACE)
            if ret != RET_OK:
                raise RuntimeError(f"his_statistic {symbol} {seg_start}~{seg_end}: {data}")
            for _, r in data.iterrows():
                d = date.fromisoformat(str(r["time"])[:10])
                rows[day_ms(d)] = {
                    "ts": day_ms(d),
                    "option_volume": _num(r.get("option_volume")),
                    "call_volume": _num(r.get("call_volume")),
                    "put_volume": _num(r.get("put_volume")),
                    "pc_volume_ratio": _num(r.get("put_call_volume_ratio")),
                    "option_oi": _num(r.get("option_open_interest")) or None,
                    "call_oi": _num(r.get("call_open_interest")) or None,
                    "put_oi": _num(r.get("put_open_interest")) or None,
                    "pc_oi_ratio": _num(r.get("put_call_open_interest_ratio")),
                }
            if key is None:
                break
        seg_start = seg_end + timedelta(days=1)
    return [rows[k] for k in sorted(rows)]


def fetch_earnings(ctx, symbols, begin: str, end: str) -> list:
    """财报日历 → [{symbol, ts, pub_type, period}]，只保留我们宇宙内的品种。

    接口**单次窗口上限 7 天**（超出报错），故按 7 天分段；一次返回全市场约 1700 行/周，
    在本地按 symbol 过滤比逐品种查快得多。
    """
    from moomoo import RET_OK

    want = {to_moomoo(s): s for s in symbols}
    b, e = date.fromisoformat(begin), date.fromisoformat(end)
    out: dict[tuple, dict] = {}
    seg = b
    while seg <= e:
        seg_end = min(seg + timedelta(days=6), e)
        ret, data = ctx.get_earnings_calendar(
            "US", begin_date=seg.isoformat(), end_date=seg_end.isoformat()
        )
        time.sleep(PACE)
        if ret != RET_OK:
            raise RuntimeError(f"earnings_calendar {seg}~{seg_end}: {data}")
        for _, r in data.iterrows():
            sym = want.get(r.get("security"))
            if sym is None:
                continue
            try:
                d = date.fromisoformat(str(r["earnings_date"])[:10])
            except (TypeError, ValueError):
                continue
            out[(sym, day_ms(d))] = {
                "symbol": sym, "ts": day_ms(d),
                "pub_type": str(r.get("pub_type") or "") or None,
                "period": str(r.get("period_text") or "") or None,
            }
        seg = seg_end + timedelta(days=1)
    return sorted(out.values(), key=lambda x: (x["symbol"], x["ts"]))


def _num(v):
    """数值化；'N/A'、None、NaN 一律 None——绝不让占位字符串变成 0。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f
