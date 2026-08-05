"""美股波动率数据（CBOE 官方 CDN，免费延迟数据）。

- 指数族：VIX(标普)/VXN(纳指100)/RVX(罗素2000)/VIX9D/VIX3M——历史日线 CSV
  （VIX 自 1990 年，T+1 更新）+ 延迟报价 JSON（约 15 分钟延迟）补当日。
- 个股 iv30：delayed_quotes/quotes/{TICKER}.json 的 iv30 字段（CBOE 现成的
  30 天隐含波动率），单标的仅约 500 字节。

合规约束：delayed_quotes 属 CBOE 免费延迟数据的灰色地带，**必须低频轮询**
——collector 对本模块整体限频 30 分钟一次；历史 CSV 属公开下载，相对安全。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

CDN = "https://cdn.cboe.com"
UA = {"User-Agent": "Mozilla/5.0"}
VOL_INDEXES = ("VIX", "VXN", "RVX", "VIX9D", "VIX3M")
ET = ZoneInfo("America/New_York")


def day_ms(d: date) -> int:
    """交易日 → 该日 00:00 UTC 的毫秒时间戳（usvol 表的唯一时间格）。"""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def backfill_index_history(name: str) -> list:
    """CBOE 历史日线 CSV → [(ts_ms, close)]。日期记为当日 00:00 UTC。

    实测该 CSV **当日收盘后即更新**（非 T+1）。它是本表的权威来源：collector 在
    "报价进入了 CSV 尚未覆盖的交易日"时重拉，把该日的临时值确权成官方收盘价。
    """
    r = requests.get(
        f"{CDN}/api/global/us_indices/daily_prices/{name}_History.csv",
        headers=UA,
        timeout=20,
    )
    r.raise_for_status()
    rows = []
    for rec in csv.DictReader(io.StringIO(r.text)):
        try:
            d = datetime.strptime(rec["DATE"], "%m/%d/%Y").date()
            rows.append((day_ms(d), float(rec["CLOSE"])))
        except (KeyError, ValueError):
            continue
    return rows


def _quote_trading_day(d: dict, now: datetime | None = None) -> date:
    """报价所属的交易日：优先用 last_trade_time（ET 挂牌时刻），否则退到 ET 当天。

    用挂牌时刻而非本机 time.time()，周末/盘后拉到的仍是上一交易日的收盘价——
    记到那个交易日名下才对，否则会凭空造出周六、周日的行。时间字段损坏或
    候选日非交易日时向前回退；``now`` 仅供确定性测试注入。
    """
    lt = d.get("last_trade_time")
    candidate = None
    if lt:
        try:
            candidate = datetime.strptime(str(lt)[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    if candidate is None:
        clock = now.astimezone(ET) if now is not None else datetime.now(ET)
        candidate = clock.date()

    from .calendar_nyse import is_probable_trading_day

    while not is_probable_trading_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def fetch_index_quote(name: str) -> dict:
    """延迟报价补当日值（指数符号带下划线前缀）。

    时间格与 CSV 日线**统一为交易日 00:00 UTC**，所以日线格里不会混进盘中点位。
    但要清楚它的地位：**当前交易日的行是报价值**（盘中为临时值，收盘后收敛到
    官方收盘价），要等 CSV 覆盖到这一天才算确权。collector 负责保证报价
    只写 CSV 尚未覆盖的交易日——权威方向是 CSV 覆盖报价，不是反过来。
    """
    r = requests.get(
        f"{CDN}/api/global/delayed_quotes/quotes/_{name}.json", headers=UA, timeout=10
    )
    r.raise_for_status()
    d = r.json()["data"]
    return {"ts": day_ms(_quote_trading_day(d)), "close": float(d["current_price"])}


def fetch_stock_iv30(ticker: str):
    """个股/ETF 的 30 天隐含波动率（CBOE 现成 iv30 字段）。无期权或字段缺失返回 None。"""
    r = requests.get(
        f"{CDN}/api/global/delayed_quotes/quotes/{ticker.upper()}.json",
        headers=UA,
        timeout=10,
    )
    r.raise_for_status()
    v = (r.json().get("data") or {}).get("iv30")
    return float(v) if v not in (None, 0) else None
