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
    """内部符号（NVDA-USDT）→ moomoo 代码（US.NVDA）。

    支持 vol_proxy 映射：XAU-USDT→US.GLD、XAG-USDT→US.SLV——币安贵金属期权
    最远只有 3 天到期，合成不出诚实的 30 天 IV；黄金/白银 ETF 期权的 IV
    是标准的 30 天口径代理，且 moomoo 有其 3.1 年历史。数据仍落在
    XAU-USDT 名下（source='moomoo'），代理关系由 instruments.json 声明。
    """
    from . import instruments

    proxy = instruments.get(symbol).get("vol_proxy")
    return "US." + (proxy or symbol.split("-")[0])


def iv_symbols(symbols) -> list:
    """哪些品种走 moomoo IV 管线：美股永续 + 配了 vol_proxy 的商品。"""
    from . import instruments

    out = []
    for s in symbols:
        cfg = instruments.get(s)
        if cfg.get("class") == "us_stock_perp" or cfg.get("vol_proxy"):
            out.append(s)
    return out


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


def fetch_live(ctx, symbols, now_ms: int | None = None) -> list:
    """盘中实时快照（3303，单次 ≤500 标的）→ [{symbol, ts, iv, pre_iv, ...}]。

    实测该接口**盘中滚动更新**（ET 09:40 后 5 分钟内四个品种全部变动），
    且自带 `pre_iv`＝昨日结算值——与我们 stock_vol 存的完全一致，
    是现成的对照基准（NVDA 实测 pre_iv 48.004 == 库内 48.004）。

    同时给实时的 call/put 成交量，故盘中期权流也一并取回。
    ts 用采集时刻（本地墙钟），因为返回体不带时间戳。
    """
    import time as _t

    from moomoo import RET_OK

    ts = int(now_ms if now_ms is not None else _t.time() * 1000)
    codes = [to_moomoo(s) for s in symbols]
    ret, data = ctx.get_option_underlying_overview(codes)
    if ret != RET_OK:
        raise RuntimeError(f"3303 live: {data}")
    back = {to_moomoo(s): s for s in symbols}
    out = []
    for _, r in data.iterrows():
        sym = back.get(r.get("code"))
        if sym is None:
            continue
        cv, pv = _num(r.get("call_volume")), _num(r.get("put_volume"))
        out.append({
            "symbol": sym, "ts": ts,
            "iv": _num(r.get("iv")), "pre_iv": _num(r.get("pre_iv")),
            "hv_30d": _num(r.get("hv_30d")),
            # 厂商自算的 rank/percentile（52 周口径）——与我们的条件分位**不是同一个量**，
            # 只作参考展示，绝不混用
            "vendor_iv_rank": _num(r.get("iv_rank")),
            "vendor_iv_pct": _num(r.get("iv_percentile")),
            "call_volume": cv, "put_volume": pv,
            "pc_volume_ratio": (pv / cv) if (cv and pv is not None and cv > 0) else None,
        })
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


def settled_only(rows: list, now=None) -> list:
    """滤掉尚未结算的当日行——**与"未收盘 K 线双轨制"同一条纪律**。

    实测（2026-08-04 盘中 ET 09:45）：3304 会返回当日行且其 iv 随盘滚动
    （47.238 vs 3303 实时 47.152），若照写入库，分位分母里就混进了一个还会变的值，
    同一天算两次分位得数不同——这正是 data.closed_ohlcv 按理论收线时刻剥离
    形成中 K 线要防的事。

    判据：某交易日 D 的行，仅当"当前 ET 时刻已过 D 的收盘"才算结算。
    用 NYSE 日历的真实收盘时刻（半日市 13:00），不硬编码 16:00。
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from .calendar_nyse import is_probable_trading_day, session_close_et

    et = ZoneInfo("America/New_York")
    # 传入的 now 必须统一转 ET——调用方若传 UTC 时刻，now.time() 会拿 UTC 钟点
    # 与 ET 收盘时刻比（19:45Z 会被当成"已过 16:00 收盘"提前放行当日行）
    now = (now.astimezone(et) if now is not None else _dt.now(et))
    out = []
    for r in rows:
        d = _dt.fromtimestamp(r["ts"] / 1000, tz=timezone.utc).date()
        if not is_probable_trading_day(d):
            continue   # 周末/休市日的行属厂商异常，防御性丢弃（codex 审计）
        if d < now.date():
            out.append(r)
            continue
        if d == now.date() and now.time() >= session_close_et(d):
            out.append(r)   # 已收盘，当日值已定
    return out


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
    """数值化；'N/A'、None、NaN、±inf 一律 None——绝不让占位字符串变成 0，
    也绝不让 inf 进分位分母（codex 审计：'inf' 字符串可被 float() 接受）。"""
    import math

    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None
