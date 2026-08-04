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


def test_earnings_proximity_sign_and_horizon():
    """邻近度：正=未来、负=已过、超出 horizon 返回 None。

    符号搞反会让"财报已过"被读成"财报将至"——对 IV 解释是相反的方向
    （事前隐波堆积 vs 事后崩塌）。
    """
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")

    def et_ms(d, hh, mm=0):
        """真实的盘中时刻——**不能用 day_ms(午夜)**：那样测试与被测代码共享
        "两端都锚在午夜"的假设，分数日四舍五入的 bug 永远不会暴露（实测漏掉过）。"""
        return int(datetime.combine(d, dtime(hh, mm), tzinfo=et).timestamp() * 1000)

    conn = _mem_conn()
    t0 = moomoo_iv.day_ms(date(2026, 8, 20))
    storage.upsert_earnings(conn, "moomoo", [
        {"symbol": "NVDA-USDT", "ts": t0, "pub_type": "AFTER", "period": "2026Q2"},
    ])
    # 盘中 10:00 ET 查三天后的财报
    p = storage.earnings_proximity(conn, "NVDA-USDT", et_ms(date(2026, 8, 17), 10))
    assert p is not None and p["days"] == 3, f"未来财报应为 +3，实得 {p}"

    # 当日盘中：必须是 0，不能因为 ts 锚在午夜就算成 -1
    p0 = storage.earnings_proximity(conn, "NVDA-USDT", et_ms(date(2026, 8, 20), 10))
    assert p0["days"] == 0, f"当日盘中应为 0，实得 {p0}"
    # 当日盘后（收盘后 2 小时）同样是 0
    p0b = storage.earnings_proximity(conn, "NVDA-USDT", et_ms(date(2026, 8, 20), 18))
    assert p0b["days"] == 0, f"当日盘后应为 0，实得 {p0b}"

    p2 = storage.earnings_proximity(conn, "NVDA-USDT", et_ms(date(2026, 8, 24), 14))
    assert p2 is not None and p2["days"] == -4, f"已过财报应为 -4，实得 {p2}"

    far = et_ms(date(2026, 9, 30), 10)
    assert storage.earnings_proximity(conn, "NVDA-USDT", far) is None, "超出窗口应为 None"
    assert storage.earnings_proximity(conn, "AAPL-USDT", et_ms(date(2026, 8, 17), 10)) is None


def test_earnings_picks_nearest_not_first():
    """多个财报日时取**距今最近**的那个，不是最早或最晚的。

    now 用真实 ET 盘中时刻：day_ms(午夜 UTC) 换算到 ET 会退到前一天晚上，
    日差就少算不了——这正是上一条测试暴露的同类陷阱。
    """
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    conn = _mem_conn()
    for d in (date(2026, 2, 20), date(2026, 5, 20), date(2026, 8, 20)):
        storage.upsert_earnings(conn, "moomoo", [
            {"symbol": "NVDA-USDT", "ts": moomoo_iv.day_ms(d), "pub_type": None, "period": None},
        ])
    now = int(datetime.combine(date(2026, 8, 18), dtime(10, 0),
                              tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    p = storage.earnings_proximity(conn, "NVDA-USDT", now)
    assert p["days"] == 2, f"应取 8-20 那个（+2 天），实得 {p}"


def test_vrp_uses_same_row_iv_and_hv():
    """VRP 必须用同一行的 iv−hv（同源同口径），且分位遵守样本下限。

    若错用"最新 iv 减最新 hv"而两者来自不同日期，在数据缺口处会算出无意义的差。
    """
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 2).toordinal()
    rows = []
    for i in range(200):
        rows.append({"ts": moomoo_iv.day_ms(date.fromordinal(base + i)),
                     "iv": 30.0 + i * 0.1, "hv": 25.0 + i * 0.1})
    # 末行 hv 缺失：VRP 应回退到最近一个 iv/hv 俱全的行，而非拿旧 hv 配新 iv
    rows.append({"ts": moomoo_iv.day_ms(date.fromordinal(base + 200)),
                 "iv": 99.0, "hv": None})
    storage.upsert_stock_vol(conn, "NVDA-USDT", "moomoo", rows)
    blk = dashboard._stock_iv_block(conn, "NVDA-USDT")
    assert blk["last"] == 99.0, "IV 主线应取最新值"
    # 最新完整行是第 200 个：iv=30+199*0.1=49.9, hv=25+199*0.1=44.9 → VRP=5.0
    assert abs(blk["vrp"] - 5.0) < 1e-6, f"VRP 应来自同一行，实得 {blk['vrp']}"
    assert blk["vrp_rank"] is not None


def test_vrp_rank_withheld_when_short():
    """VRP 分位与 IV 分位同一样本下限——不能一个给一个不给。"""
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 2).toordinal()
    storage.upsert_stock_vol(conn, "CRCL-USDT", "moomoo", [
        {"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": 20.0 + i, "hv": 15.0 + i}
        for i in range(50)
    ])
    blk = dashboard._stock_iv_block(conn, "CRCL-USDT")
    assert blk["rank"] is None and blk["vrp_rank"] is None, "样本不足时两个分位都不给"
    assert blk["vrp"] is not None, "但 VRP 数值本身可以给"


def test_term_structure_inversion_flags():
    """期限结构：比值 >1 判倒挂；两端同时倒挂另给 both_inverted。"""
    import dashboard

    conn = _mem_conn()
    # 构造：快端倒挂（VIX9D > VIX），慢端正挂（VIX < VIX3M）
    for i in range(30):
        ts = moomoo_iv.day_ms(date.fromordinal(date(2026, 1, 2).toordinal() + i))
        storage.upsert_usvol(conn, "VIX9D", [(ts, 30.0)])
        storage.upsert_usvol(conn, "VIX", [(ts, 25.0)])
        storage.upsert_usvol(conn, "VIX3M", [(ts, 28.0)])
    _, term = dashboard._term_structure(conn)
    assert term["fast"]["inverted"] is True, "VIX9D>VIX 应判快端倒挂"
    assert term["slow"]["inverted"] is False, "VIX<VIX3M 慢端不倒挂"
    assert term["both_inverted"] is False
    assert abs(term["fast"]["ratio"] - 1.2) < 1e-6


def test_settled_only_drops_unsettled_today():
    """当日未收盘的行必须滤掉——与 K 线 iloc[:-1] 同一条纪律。

    实测 3304 会返回当日行且其 iv 随盘滚动（盘中 47.238 vs 3303 实时 47.152）。
    写进去会让分位分母含一个还会变的值：同一天算两次分位得数不同。
    """
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    d_today = date(2026, 8, 4)      # 周二，正常交易日
    d_yest = date(2026, 8, 3)
    rows = [
        {"ts": moomoo_iv.day_ms(d_yest), "iv": 48.0},
        {"ts": moomoo_iv.day_ms(d_today), "iv": 47.2},
    ]
    # 盘中 09:45：当日行未结算，必须丢弃
    mid = datetime.combine(d_today, dtime(9, 45), tzinfo=et)
    kept = moomoo_iv.settled_only(rows, now=mid)
    assert len(kept) == 1 and kept[0]["iv"] == 48.0, "盘中不得写入当日行"

    # 收盘后 16:30：当日已定，应保留
    after = datetime.combine(d_today, dtime(16, 30), tzinfo=et)
    kept2 = moomoo_iv.settled_only(rows, now=after)
    assert len(kept2) == 2, "收盘后当日行应保留"

    # 次日任意时刻：两行都是历史
    nxt = datetime.combine(date(2026, 8, 5), dtime(6, 0), tzinfo=et)
    assert len(moomoo_iv.settled_only(rows, now=nxt)) == 2


def test_settled_only_uses_real_close_not_hardcoded_1600():
    """半日市（收 13:00）的当日行在 13:30 就该算结算，不能等到 16:00。"""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    from regime.calendar_nyse import session_close_et

    et = ZoneInfo("America/New_York")
    half = None
    for cand in (date(2026, 11, 27), date(2026, 7, 3), date(2026, 12, 24)):
        if session_close_et(cand) < dtime(16, 0):
            half = cand
            break
    if half is None:
        return  # 该年无半日市表则跳过
    rows = [{"ts": moomoo_iv.day_ms(half), "iv": 20.0}]
    at1330 = datetime.combine(half, dtime(13, 30), tzinfo=et)
    assert len(moomoo_iv.settled_only(rows, now=at1330)) == 1, \
        f"半日市 {half} 收 {session_close_et(half)}，13:30 应已结算"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
