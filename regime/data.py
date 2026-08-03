"""OHLCV 数据获取：默认 Deribit 优先（用户实测更稳定），OKX、Binance 依次兜底。

全部为免费公开接口，无需 API key。返回按时间升序的 DataFrame，
列为 [ts, open, high, low, close, volume]，并丢弃最后一根未收盘的 K 线
（未收盘 K 线会让状态在盘中来回闪烁）。

注意：Deribit 提供的是永续合约价格（BTC/ETH 为币本位，少数主流币有 USDC 永续），
与现货存在微小基差；品种覆盖窄，Deribit 没有的品种会自动落到 OKX/Binance。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import requests

from . import instruments

DERIBIT_BASE = "https://www.deribit.com"
OKX_BASE = "https://www.okx.com"
HYPERLIQUID_INFO = "https://api.hyperliquid.xyz/info"
BINANCE_BASES = [
    "https://data-api.binance.vision",  # Binance 公共行情专用域名
    "https://api.binance.com",
]

_OKX_BARS = {"1h": "1H", "4h": "4H", "1d": "1Dutc"}
_BINANCE_INTERVALS = {"1h": "1h", "4h": "4h", "1d": "1d"}
_QUOTES = ("USDT", "USDC", "USD")

COLUMNS = ["ts", "open", "high", "low", "close", "volume"]


def _okx_symbol(symbol: str) -> str:
    inst = symbol.upper().replace("/", "-")
    if "-" not in inst:
        for q in _QUOTES:
            if inst.endswith(q):
                inst = f"{inst[:-len(q)]}-{q}"
                break
        else:
            inst = f"{inst}-USDT"
    return inst


def _binance_symbol(symbol: str) -> str:
    sym = symbol.upper().replace("/", "").replace("-", "")
    if not sym.endswith(_QUOTES):
        sym = f"{sym}USDT"
    return sym


_DERIBIT_RES = {"1h": "60", "4h": "60", "1d": "1D"}  # Deribit 无 240 分钟档，4h 由 1h 重采样
_BAR_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _deribit_instrument(symbol: str) -> str:
    base = symbol.upper().replace("/", "-").split("-")[0]
    if base in ("BTC", "ETH"):
        return f"{base}-PERPETUAL"
    return f"{base}_USDC-PERPETUAL"  # Deribit 线性永续命名，如 SOL_USDC-PERPETUAL


def assert_no_gaps(df: pd.DataFrame, timeframe: str, source: str) -> None:
    """已收盘序列中间不得有缺口。

    丢弃残缺桶（首桶/中段）解决了"残值入库"，但留下的是**带洞的序列**：
    下游一切按"每根 = 一个周期"计算的量（收益率、RV 年化、rolling 窗口）
    都会在洞口失真——close.shift(1) 实际跨越了 8h 而代码以为是 4h。
    有洞就拒绝本源，让 fetch_ohlcv 的兜底链换一个源，而不是带病分类。
    """
    if len(df) < 2:   # 两根就够算一个差分
        return
    step = _BAR_MS[timeframe]
    gaps = (df["ts"].astype("int64") // 10**6).diff().dropna()
    bad = gaps[gaps != step]
    if len(bad):
        worst = float(bad.max()) / step
        raise RuntimeError(
            f"{source} {timeframe} 序列有 {len(bad)} 处缺口（最大 {worst:.0f}×周期）"
            "——拒绝使用，转兜底源"
        )


def _resample_4h(df: pd.DataFrame) -> pd.DataFrame:
    """1h -> 4h，按 UTC 整点分桶（与交易所 4h K 线对齐），不依赖 pandas 频率别名。

    **首桶必须丢弃**：取数窗口起点不对齐到 4h 边界时，第一个桶只装到 1~3 根
    1h K 线，聚合出来是残值（实测成交量只有真值的 0.1~0.6 倍）。而 upsert 按
    (symbol,tf,ts) 覆盖，这根残桶会在滑出取数窗口的**那一刻被定格**并永久留在库里
    ——历史每天被啃掉 6 根。末桶同样不完整，但那是"形成中"的当前 4h，
    collector 要拿它写 live_bars 做未收线预览，必须保留、由调用方另行剥离。
    """
    sec = df["ts"].astype("int64") // 10**9
    bucket = (sec // 14_400) * 14_400
    g = df.groupby(bucket).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        n=("close", "size"),
    )
    # 非末桶必须完整（n==4）：不止首桶——取数窗口中段的 1h 缺口（交易所维护/
    # 断流）同样会产生残桶，经 upsert 定格入库就是当年 4h 残桶事故的翻版。
    # 末桶无论残否都保留：它是"形成中"的当前 4h，live_bars 预览要用。
    if len(g) > 1:
        keep = (g["n"] == 4).values
        keep[-1] = True
        g = g[keep]
    g = g.drop(columns=["n"])
    g.insert(0, "ts", pd.to_datetime(g.index, unit="s", utc=True))
    return g.reset_index(drop=True)


def fetch_deribit(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    fetch_tf = "1h" if timeframe == "4h" else timeframe
    n_source = limit * 4 + 8 if timeframe == "4h" else limit + 2
    end = int(time.time() * 1000)
    start = end - n_source * _BAR_MS[fetch_tf]
    if timeframe == "4h":
        # 起点对齐到 4h 边界：与 _resample_4h 的丢首桶是两道独立保险，
        # 对齐后正常情况下一根都不浪费，丢首桶只在边界抖动时兜底。
        start = (start // _BAR_MS["4h"]) * _BAR_MS["4h"]
    r = requests.get(
        f"{DERIBIT_BASE}/api/v2/public/get_tradingview_chart_data",
        params={
            "instrument_name": _deribit_instrument(symbol),
            "resolution": _DERIBIT_RES[timeframe],
            "start_timestamp": start,
            "end_timestamp": end,
        },
        timeout=10,
    )
    r.raise_for_status()
    payload = r.json()
    res = payload.get("result") or {}
    if res.get("status") != "ok":
        raise RuntimeError(f"Deribit: {payload.get('error') or res.get('status')}")
    df = pd.DataFrame(
        {
            "ts": res["ticks"],
            "open": res["open"],
            "high": res["high"],
            "low": res["low"],
            "close": res["close"],
            "volume": res["volume"],
        }
    )
    df = _normalize(df)
    if timeframe == "4h":
        df = _resample_4h(df)
    return df


def fetch_dvol(currency: str, days: int = 365) -> pd.DataFrame:
    """Deribit DVOL 隐含波动率指数（日线收盘），仅 BTC/ETH。返回 [ts, dvol]。"""
    end = int(time.time() * 1000)
    r = requests.get(
        f"{DERIBIT_BASE}/api/v2/public/get_volatility_index_data",
        params={
            "currency": currency.upper(),
            "resolution": "1D",
            "start_timestamp": end - days * 86_400_000,
            "end_timestamp": end,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = (r.json().get("result") or {}).get("data") or []
    if not data:
        raise RuntimeError("DVOL: 无数据")
    df = pd.DataFrame(data, columns=["ts", "open", "high", "low", "dvol"])
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    df = df[["ts", "dvol"]].sort_values("ts").reset_index(drop=True)
    # 与 fetch_ohlcv 同口径丢掉未收线的当日：末行是形成中的值，同一天内会来回跳
    # （实测 20 轮内跳了 4 次），进分位与 IV−RV 剪刀差会引入假波动
    return df.iloc[:-1].reset_index(drop=True)


def fetch_okx(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    r = requests.get(
        f"{OKX_BASE}/api/v5/market/candles",
        params={
            "instId": _okx_symbol(symbol),
            "bar": _OKX_BARS[timeframe],
            "limit": str(min(limit, 300)),
        },
        timeout=10,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("code") != "0":
        raise RuntimeError(f"OKX: {payload.get('msg') or payload.get('code')}")
    rows = payload["data"]  # 最新在前: [ts, o, h, l, c, vol, ...]
    df = pd.DataFrame([row[:6] for row in rows], columns=COLUMNS)
    return _normalize(df)


def fetch_binance(symbol: str, timeframe: str, limit: int = 500) -> pd.DataFrame:
    last_err: Exception | None = None
    for base in BINANCE_BASES:
        try:
            r = requests.get(
                f"{base}/api/v3/klines",
                params={
                    "symbol": _binance_symbol(symbol),
                    "interval": _BINANCE_INTERVALS[timeframe],
                    "limit": min(limit, 1000),
                },
                timeout=10,
            )
            r.raise_for_status()
            df = pd.DataFrame([row[:6] for row in r.json()], columns=COLUMNS)
            return _normalize(df)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"Binance: {last_err}")


def fetch_binance_futures(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """币安合约区 K 线（fapi）——美股永续（EQUITY 区）与加密永续通用。"""
    last_err: Exception | None = None
    for base in ("https://fapi.binance.com",):
        try:
            r = requests.get(
                f"{base}/fapi/v1/klines",
                params={
                    "symbol": _binance_symbol(symbol),
                    "interval": _BINANCE_INTERVALS[timeframe],
                    "limit": min(limit, 1000),
                },
                timeout=10,
            )
            r.raise_for_status()
            df = pd.DataFrame([row[:6] for row in r.json()], columns=COLUMNS)
            return _normalize(df)
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise RuntimeError(f"binance_futures: {last_err}")


def fetch_binance_vol1h(symbol: str, end_ms: int | None = None, limit: int = 1000):
    """币安 fapi 1h 量流（VWAP 专用）：[(ts, volume, quoteVolume), ...] 仅已收线。

    quoteVolume/volume 即该小时精确 VWAP。与 OHLCV 主源解耦——加密价格主源
    是 Deribit，但 VWAP 的量按决策统一取币安（量最大）；任意周期任意日界锚
    的滚动 VWAP 都从这条 1h 流按时间窗聚合，不受 4h/1d 网格相位影响。
    """
    params = {"symbol": _binance_symbol(symbol), "interval": "1h",
              "limit": min(limit, 1000)}
    if end_ms is not None:
        params["endTime"] = int(end_ms)
    r = requests.get("https://fapi.binance.com/fapi/v1/klines",
                     params=params, timeout=15)
    r.raise_for_status()
    now = int(time.time() * 1000)
    out = []
    for row in r.json():
        ts = int(row[0])
        if ts + 3_600_000 > now:  # 未收线的当前小时不入库
            continue
        out.append((ts, float(row[5]), float(row[7])))
    return out


_HL_INTERVALS = {"1h": "1h", "4h": "4h", "1d": "1d"}


def fetch_hyperliquid(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    """Hyperliquid K 线（info/candleSnapshot）。coin 名从 instruments.json 的
    hl_coin 读取（builder dex 命名如 xyz:NVDA）。"""
    coin = instruments.get(symbol).get("hl_coin")
    if not coin:
        raise RuntimeError("hyperliquid: instruments.json 未配置该 symbol 的 hl_coin")
    end = int(time.time() * 1000)
    start = end - int(limit * 1.1) * _BAR_MS.get(timeframe, 86_400_000)
    r = requests.post(
        HYPERLIQUID_INFO,
        json={
            "type": "candleSnapshot",
            "req": {
                "coin": coin,
                "interval": _HL_INTERVALS[timeframe],
                "startTime": start,
                "endTime": end,
            },
        },
        timeout=15,
    )
    r.raise_for_status()
    rows = r.json()
    if not rows:
        raise RuntimeError(f"hyperliquid: {coin} 无 K 线返回")
    df = pd.DataFrame(
        {
            "ts": [int(k["t"]) for k in rows],
            "open": [k["o"] for k in rows],
            "high": [k["h"] for k in rows],
            "low": [k["l"] for k in rows],
            "close": [k["c"] for k in rows],
            "volume": [k["v"] for k in rows],
        }
    )
    return _normalize(df)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms", utc=True)
    return df.sort_values("ts").reset_index(drop=True)


SOURCES = {
    "deribit": fetch_deribit,
    "okx": fetch_okx,
    "binance": fetch_binance,
    "binance_futures": fetch_binance_futures,
    "hyperliquid": fetch_hyperliquid,
}
DEFAULT_SOURCES = ("deribit", "okx", "binance")


def fetch_ohlcv(
    symbol: str,
    timeframe: str,
    limit: int = 300,
    sources=None,
    drop_unclosed: bool = True,
):
    """按 sources 顺序逐个尝试。sources=None 时按 instruments 注册表路由
    （加密默认 deribit -> okx -> binance；美股永续 binance_futures -> hyperliquid）。
    返回 (df, 数据源名)。

    默认丢弃最后一根未收盘 K 线；drop_unclosed=False 时保留（collector 用它
    存"形成中"的 live bar 供滚动预览——预览永不写入确认历史）。
    """
    if sources is None:
        sources = instruments.get(symbol)["sources"]
    errors = []
    for name in sources:
        fn = SOURCES.get(name)
        if fn is None:
            errors.append(f"{name}: 未知数据源（可选 {'/'.join(SOURCES)}）")
            continue
        try:
            df = fn(symbol, timeframe, limit)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            continue
        closed = df.iloc[:-1].reset_index(drop=True)
        if len(closed) < 90:  # 上市较新的美股永续（如 1d 仅 ~130 根）也放行，靠 warmup 标志提示质量
            errors.append(f"{name}: 仅 {len(closed)} 根已收盘 K 线，不足 90")
            continue
        try:
            assert_no_gaps(closed, timeframe, name)
        except RuntimeError as e:
            errors.append(str(e))
            continue  # 带洞的序列不如换一个源
        return (closed if drop_unclosed else df), name
    raise RuntimeError("；".join(errors))


def demo_ohlcv(timeframe: str, n: int = 300, seed: int = 7) -> pd.DataFrame:
    """合成数据自检：低波震荡 -> 上行趋势 -> 高波动，用于无网络时验证管线。"""
    rng = np.random.default_rng(seed)
    drift = np.concatenate([np.zeros(n - 180), np.full(120, 0.002), np.zeros(60)])
    vol = np.concatenate([np.full(n - 180, 0.008), np.full(120, 0.012), np.full(60, 0.03)])
    ret = rng.normal(drift, vol)
    close = 50_000 * np.exp(np.cumsum(ret))
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) * (1 + rng.uniform(0.001, 0.008, n))
    low = np.minimum(open_, close) * (1 - rng.uniform(0.001, 0.008, n))
    volume = rng.uniform(100, 300, n) * (1 + 5 * np.abs(ret))
    step = {"1h": 3600, "4h": 14_400, "1d": 86_400}[timeframe]
    ts = pd.to_datetime((1_700_000_000 + np.arange(n) * step) * 1000, unit="ms", utc=True)
    return pd.DataFrame(
        {"ts": ts, "open": open_, "high": high, "low": low, "close": close, "volume": volume}
    )
