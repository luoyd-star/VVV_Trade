"""个股 IV（stock_vol）存储与分位口径的回归。

钉三件事：① source 进主键让不同口径物理隔离，混算分位不可能发生；
② 分位窗自适应且样本不足时**不给**分位（新股宁可空着）；③ 符号映射与倒序陷阱。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import moomoo_iv, storage  # noqa: E402


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)
    return conn


def test_source_isolates_series():
    """同一品种同一天、两个口径的值必须共存且互不覆盖——分位混算的结构性防线。"""
    conn = _mem_conn()
    ts = moomoo_iv.day_ms(date(2026, 8, 3))
    storage.upsert_stock_vol(conn, "NVDA-USDT", "moomoo", [{"ts": ts, "iv": 48.0, "hv": 42.4}])
    storage.upsert_stock_vol(conn, "NVDA-USDT", "cboe", [{"ts": ts, "iv": 44.3, "hv": None}])

    mm = storage.get_stock_vol(conn, "NVDA-USDT", "moomoo")
    cb = storage.get_stock_vol(conn, "NVDA-USDT", "cboe")
    assert len(mm) == 1 and len(cb) == 1
    assert mm["iv"].iloc[0] == 48.0, "moomoo 口径被覆盖了"
    assert cb["iv"].iloc[0] == 44.3, "cboe 口径被覆盖了"
    # 不指定口径拿不到数据——get_stock_vol 强制 source 必填
    assert storage.get_stock_vol(conn, "NVDA-USDT", "orats").empty


def test_upsert_coalesce_keeps_existing():
    """重跑增量时后写的 None 不得抹掉已有值（与 deriv 表同一纪律）。"""
    conn = _mem_conn()
    ts = moomoo_iv.day_ms(date(2026, 8, 3))
    storage.upsert_stock_vol(conn, "AAPL-USDT", "moomoo",
                             [{"ts": ts, "iv": 29.3, "hv": 36.9, "underlying_price": 303.4}])
    storage.upsert_stock_vol(conn, "AAPL-USDT", "moomoo",
                             [{"ts": ts, "iv": 29.5, "hv": None, "underlying_price": None}])
    df = storage.get_stock_vol(conn, "AAPL-USDT", "moomoo")
    assert df["iv"].iloc[0] == 29.5, "新值应覆盖"
    assert df["hv"].iloc[0] == 36.9, "None 不得抹掉已有 hv"
    assert df["underlying_price"].iloc[0] == 303.4


def test_rank_withheld_when_sample_short():
    """样本不足 IV_RANK_MIN 一律不给分位——新股宁可空着也不给假统计量。"""
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 2).toordinal()
    # 只有 50 个观测：远少于 IV_RANK_MIN(120)
    rows = [{"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": 20.0 + i}
            for i in range(50)]
    storage.upsert_stock_vol(conn, "CRCL-USDT", "moomoo", rows)
    blk = dashboard._stock_iv_block(conn, "CRCL-USDT")
    assert blk is not None and blk["n"] == 50
    assert blk["rank"] is None, "样本不足时不得给分位"

    # 补到 200 个（≥120）后分位出现，且窗口不超过实际样本
    rows2 = [{"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": 20.0 + i}
             for i in range(50, 200)]
    storage.upsert_stock_vol(conn, "CRCL-USDT", "moomoo", rows2)
    blk2 = dashboard._stock_iv_block(conn, "CRCL-USDT")
    assert blk2["rank"] is not None
    assert blk2["win"] == min(200, dashboard.IV_RANK_WIN)
    # 单调递增序列的末值是最大值 → 分位应逼近上界 (n-1)/n
    assert blk2["rank"] > 0.99


def test_symbol_mapping():
    """内部符号（币安永续命名）→ moomoo 代码。"""
    assert moomoo_iv.to_moomoo("NVDA-USDT") == "US.NVDA"
    assert moomoo_iv.to_moomoo("QQQ-USDT") == "US.QQQ"


def test_day_ms_is_utc_midnight():
    """日频时间格与 usvol/ref_daily 一致：交易日 00:00 UTC，不含盘中时刻。"""
    ts = moomoo_iv.day_ms(date(2026, 8, 3))
    assert ts % 86_400_000 == 0, "必须落在 UTC 日界上"
    from datetime import datetime, timezone
    assert datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date() == date(2026, 8, 3)


def test_series_returned_ascending():
    """get_stock_vol 必须升序返回——3304 原始接口是倒序的，踩过坑。"""
    conn = _mem_conn()
    base = date(2026, 1, 2).toordinal()
    rows = [{"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": float(i)}
            for i in range(10)]
    storage.upsert_stock_vol(conn, "SPY-USDT", "moomoo", list(reversed(rows)))
    df = storage.get_stock_vol(conn, "SPY-USDT", "moomoo")
    assert list(df["ts"]) == sorted(df["ts"]), "必须升序"
    assert df["iv"].iloc[-1] == 9.0, "末行应是最新的一天"


def test_optstat_na_never_becomes_zero():
    """'N/A'、None、NaN 必须落成 NULL 而非 0——put/call 比落成 0 会是灾难性的假信号。"""
    assert moomoo_iv._num("N/A") is None
    assert moomoo_iv._num(None) is None
    assert moomoo_iv._num(float("nan")) is None
    assert moomoo_iv._num("0.5504") == 0.5504
    assert moomoo_iv._num(0) == 0.0  # 真实的 0 要留住（成交量可以为 0）


def test_optstat_source_isolated_and_coalesced():
    """期权流表同样 source 隔离；当日 OI 为 T-1 延迟先空后补，补时不得抹掉成交量。"""
    conn = _mem_conn()
    ts = moomoo_iv.day_ms(date(2026, 8, 3))
    # 首写：只有成交量，持仓量当日尚未发布
    storage.upsert_stock_option_stat(conn, "NVDA-USDT", "moomoo", [{
        "ts": ts, "option_volume": 4495132.0, "call_volume": 3005949.0,
        "put_volume": 1489183.0, "pc_volume_ratio": 0.495412,
        "option_oi": None, "pc_oi_ratio": None,
    }])
    # 次日补 OI
    storage.upsert_stock_option_stat(conn, "NVDA-USDT", "moomoo", [{
        "ts": ts, "option_oi": 14686311.0, "pc_oi_ratio": 0.807885,
    }])
    df = storage.get_stock_option_stat(conn, "NVDA-USDT", "moomoo")
    assert len(df) == 1
    assert df["pc_volume_ratio"].iloc[0] == 0.495412, "补 OI 不得抹掉成交比"
    assert df["option_volume"].iloc[0] == 4495132.0
    assert df["pc_oi_ratio"].iloc[0] == 0.807885, "OI 应补上"
    assert storage.get_stock_option_stat(conn, "NVDA-USDT", "cboe").empty


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
