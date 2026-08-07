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
    """当日未收盘的行必须滤掉——与 K 线按理论收线时刻裁剪同一条纪律。

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


def test_earn_conditioned_rank_compares_within_same_state():
    """条件分位必须只与同一财报状态的历史比——这是全部价值所在。

    构造：财报窗内的 IV 一律 80、窗外一律 20。若错误地混起来比，
    今天(窗内, IV=80) 会得到接近 1.0 的分位（比所有窗外日子都高）；
    正确做法只与窗内日子比，应落在中间。
    """
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 1).toordinal()
    # 财报日全集先定好，再据此生成 IV——两边必须用同一份日历，
    # 否则测试自己制造出"窗内却给低 IV"的行，看起来像代码错
    edays = [date.fromordinal(base + 60 + q * 90) for q in range(4)]
    edays.append(date.fromordinal(base + 375))
    rows = []
    for i in range(360):
        d = date.fromordinal(base + i)
        near = any(0 <= (e - d).days <= 30 for e in edays)
        rows.append({"ts": moomoo_iv.day_ms(d), "iv": 80.0 if near else 20.0, "hv": 15.0})
    # 末日处于窗内（距 base+375 仅 15 天），IV 居中偏低，看它与谁比
    rows.append({"ts": moomoo_iv.day_ms(date.fromordinal(base + 360)),
                 "iv": 70.0, "hv": 15.0})
    storage.upsert_stock_vol(conn, "NVDA-USDT", "moomoo", rows)
    storage.upsert_earnings(conn, "moomoo", [
        {"symbol": "NVDA-USDT", "ts": moomoo_iv.day_ms(e), "pub_type": None, "period": None}
        for e in edays])

    blk = dashboard._stock_iv_block(conn, "NVDA-USDT")
    assert blk["earn_in30"] is True, "末日应处于财报窗内"
    # 与同状态比：70 低于所有窗内日(80)，分位必须是 0
    assert blk["rank"] == 0.0, f"条件分位应只与窗内(80)比而为 0，实得 {blk['rank']}"
    # 与全体比：窗外的低值(20)把它抬起来——这正是被修掉的污染
    assert blk["rank_raw"] > 0.4, f"原始分位应被窗外低值抬起，实得 {blk['rank_raw']}"
    assert blk["rank_raw"] - blk["rank"] > 0.4, \
        f"两者差距即污染幅度，实得 raw={blk['rank_raw']} cond={blk['rank']}"


def test_earn_conditioned_rank_falls_back_when_no_earnings():
    """无财报记录的品种（如 ETF）应退回原始分位而不是报错或给 None。"""
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 1).toordinal()
    storage.upsert_stock_vol(conn, "QQQ-USDT", "moomoo", [
        {"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": 20.0 + i * 0.05}
        for i in range(300)])
    blk = dashboard._stock_iv_block(conn, "QQQ-USDT")
    assert blk["rank"] is not None and blk["rank"] == blk["rank_raw"]
    assert blk["earn_in30"] is None, "无财报记录时状态应为 None 而非 False"


def test_live_never_contaminates_settled_percentile():
    """实时表与结算表严格分离——写入实时值不得改变正式分位。

    这是整个双轨制的核心不变量：盘中值还会滚动，若混进 stock_vol，
    同一天算两次分位会得到不同结果。
    """
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 1).toordinal()
    storage.upsert_stock_vol(conn, "NVDA-USDT", "moomoo", [
        {"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": 40.0, "hv": 30.0}
        for i in range(300)])
    before = dashboard._stock_iv_block(conn, "NVDA-USDT")
    # 写入一个极端实时值。ts 必须动态取当前时刻：30 分钟新鲜度闸对墙钟比较，
    # 写死的时间戳会在钟走过后让测试变成定时炸弹（实发过：写死"当天 01:00 UTC"
    # 的行在 01:30 后全部转陈旧，两个测试无声失效）
    import time as _t
    storage.upsert_stock_vol_live(conn, "moomoo", [
        {"symbol": "NVDA-USDT", "ts": int(_t.time() * 1000) - 60_000,
         "iv": 999.0, "pre_iv": 40.0}])
    after = dashboard._stock_iv_block(conn, "NVDA-USDT")
    assert after["last"] == before["last"], "结算值不得被实时值改写"
    assert after["rank"] == before["rank"], "正式分位不得被实时值污染"
    assert after["live"]["iv"] == 999.0 and after["live"]["preview"] is True
    assert after["live"]["chg"] == 959.0


def test_live_preview_rank_uses_settled_reference():
    """预览分位＝把实时值代入**结算**参照集，而非与实时序列比。"""
    import dashboard

    conn = _mem_conn()
    base = date(2026, 1, 1).toordinal()
    # 结算序列 20~319 递增；无财报记录 → in30 为 None，预览分位应回退为 None
    storage.upsert_stock_vol(conn, "QQQ-USDT", "moomoo", [
        {"ts": moomoo_iv.day_ms(date.fromordinal(base + i)), "iv": 20.0 + i}
        for i in range(300)])
    import time as _t
    storage.upsert_stock_vol_live(conn, "moomoo", [   # ts 动态：见上一测试的定时炸弹注释
        {"symbol": "QQQ-USDT", "ts": int(_t.time() * 1000) - 60_000,
         "iv": 100.0, "pre_iv": 319.0}])
    blk = dashboard._stock_iv_block(conn, "QQQ-USDT")
    # 无财报记录（ETF）：退回全集参照（tail 252，与结算侧 rank_raw 同口径）——
    # 序列 20..319 的 tail(252)=68..319，iv=100 → (100−68)/252 ≈ 0.127
    pr0 = blk["live"]["rank_preview"]
    assert pr0 is not None and 0.10 < pr0 < 0.15, f"应回退全集参照≈0.127，实得 {pr0}"
    assert blk["live"]["in30_now"] is None, "无财报记录时今日状态应为 None"

    # 有财报记录后：100.0 在 20~319 里应落在约 (100-20)/300 ≈ 0.27
    storage.upsert_earnings(conn, "moomoo", [
        {"symbol": "QQQ-USDT", "ts": moomoo_iv.day_ms(date.fromordinal(base + 400)),
         "pub_type": None, "period": None}])
    blk2 = dashboard._stock_iv_block(conn, "QQQ-USDT")
    pr = blk2["live"]["rank_preview"]
    assert pr is not None and 0.2 < pr < 0.35, f"预览分位应≈0.27，实得 {pr}"


def test_vol_proxy_mapping():
    """XAU→GLD / XAG→SLV 代理映射；美股不受影响。"""
    assert moomoo_iv.to_moomoo("XAU-USDT") == "US.GLD"
    assert moomoo_iv.to_moomoo("XAG-USDT") == "US.SLV"
    assert moomoo_iv.to_moomoo("NVDA-USDT") == "US.NVDA"
    syms = moomoo_iv.iv_symbols(["XAU-USDT", "BTC-USDT", "NVDA-USDT", "CL-USDT"])
    assert syms == ["XAU-USDT", "NVDA-USDT"], "商品仅 vol_proxy 者入列，加密/原油不入"


def _mk_chain(specs, now_ms=0):
    """specs: [(tenor_days, strike, side, markIV)] → (contracts, marks)。"""
    contracts, marks = [], {}
    for i, (td, k, side, iv) in enumerate(specs):
        sym = f"T-{i}"
        contracts.append({"symbol": sym, "expiryDate": int(now_ms + td * 86_400_000),
                          "strikePrice": str(k), "side": side})
        marks[sym] = {"markIV": str(iv)}
    return contracts, marks


def test_binance_opt_synth_interp_and_atm():
    """两到期夹住目标期限 → 总方差插值；每到期取 ATM 邻域报价的**中位数**。"""
    from regime import binance_opt_iv as b

    # 到期 1d 与 5d，指数 100：邻域含 K=100/95/105 的全部报价，取中位数
    contracts, marks = _mk_chain([
        (1.0, 100, "CALL", 0.20), (1.0, 100, "PUT", 0.22), (1.0, 95, "CALL", 0.40),
        (5.0, 100, "CALL", 0.30), (5.0, 100, "PUT", 0.32), (5.0, 105, "PUT", 0.50),
    ])
    out = b.synth_near_iv(contracts, marks, 100.0, 0)
    assert out["method"] == "interp" and out["tenor_days"] == b.TARGET_DAYS
    # 手工复算：σ1=median(0.20,0.22,0.40)=0.22@1d, σ2=median(0.30,0.32,0.50)=0.32@5d,
    # t*=3 → w=(5-3)/(5-1)=0.5
    var = (0.5 * 0.22**2 * 1 + 0.5 * 0.32**2 * 5) / 3.0
    assert abs(out["iv"] - round(var**0.5 * 100, 2)) < 1e-9, out


def test_binance_opt_synth_robust_to_pins_and_outliers():
    """占位 pin 簇（≥3 个完全相同的 markIV）整簇剔除；单个坏 mark 拉不动中位数。

    实测原型（2026-08-05 XAG 链）：一半报价钉在 110.0，正常簇 32-44 里混一个 88.5——
    首版单 ATM 点估计器读出 87.5 的假 IV，XAU 同因 35 分钟内 23.7→38.2→31.0 来回跳。
    """
    from regime import binance_opt_iv as b

    contracts, marks = _mk_chain([
        # 占位 pin ×4（值完全相同）——若不剔除，中位数会被拖进 1.10
        (2.0, 100, "PUT", 1.10), (2.0, 99, "CALL", 1.10),
        (2.0, 101, "PUT", 1.10), (2.0, 98, "CALL", 1.10),
        # 正常簇 + 一个坏 mark（0.885）
        (2.0, 100, "CALL", 0.33), (2.0, 99, "PUT", 0.34),
        (2.0, 101, "CALL", 0.35), (2.0, 98, "PUT", 0.885),
        (2.0, 102, "CALL", 0.36),
    ])
    out = b.synth_near_iv(contracts, marks, 100.0, 0)
    # pin 全剔后剩 5 个报价：0.33/0.34/0.35/0.885/0.36 → 中位数 0.35
    assert out["method"] == "nearest" and out["iv"] == 35.0, out


def test_binance_opt_synth_nearest_and_tenor_recorded():
    """夹不住（全部短于目标）→ 取最近到期，**期限如实记录**而非谎称目标期限。"""
    from regime import binance_opt_iv as b

    contracts, marks = _mk_chain([
        (0.5, 100, "CALL", 0.25), (1.5, 100, "CALL", 0.28),
    ])
    out = b.synth_near_iv(contracts, marks, 100.0, 0)
    assert out["method"] == "nearest"
    assert out["tenor_days"] == 1.5, "必须记实际期限"
    assert out["iv"] == 28.0


def test_binance_opt_synth_skips_expiring_and_bad_iv():
    """距到期 <MIN_TENOR 的合约剔除；markIV<=0 剔除；全无效返回 None。"""
    from regime import binance_opt_iv as b

    contracts, marks = _mk_chain([
        (0.05, 100, "CALL", 0.99),     # 临近结算，须剔除
        (2.0, 100, "CALL", 0.0),       # 无效 IV
    ])
    assert b.synth_near_iv(contracts, marks, 100.0, 0) is None
    contracts2, marks2 = _mk_chain([(0.05, 100, "CALL", 0.99), (2.0, 100, "CALL", 0.30)])
    out = b.synth_near_iv(contracts2, marks2, 100.0, 0)
    assert out is not None and out["tenor_days"] == 2.0


def test_opt_iv_near_storage_roundtrip():
    conn = _mem_conn()
    storage.upsert_opt_iv_near(conn, {"symbol": "XAU-USDT", "ts": 1785900000000,
                                      "iv": 21.5, "tenor_days": 3.0, "method": "interp",
                                      "n_expiries": 2, "index_price": 4089.6})
    df = storage.get_opt_iv_near(conn, "XAU-USDT")
    assert len(df) == 1 and df["iv"].iloc[0] == 21.5 and df["tenor_days"].iloc[0] == 3.0
    assert storage.get_opt_iv_near(conn, "XAG-USDT").empty


def test_proximity_et_late_night_picks_today_not_tomorrow():
    """ET 深夜（=UTC 次日凌晨）须选今天的财报（0）而非明天的（+1）——按毫秒距离会选错。"""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    conn = _mem_conn()
    for d in (date(2026, 8, 4), date(2026, 8, 5)):
        storage.upsert_earnings(conn, "moomoo", [
            {"symbol": "NVDA-USDT", "ts": moomoo_iv.day_ms(d), "pub_type": None, "period": None}])
    late = int(datetime.combine(date(2026, 8, 4), dtime(23, 30),
                                tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    p = storage.earnings_proximity(conn, "NVDA-USDT", late)
    assert p["days"] == 0, f"ET 23:30 应选今天(0)，实得 {p}"


def test_prune_stale_future_earnings():
    """财报改期：未来窗内旧日期被清、新日期保留、known_at 不被刷新、历史行不动。"""
    import time as _t

    conn = _mem_conn()
    hist = moomoo_iv.day_ms(date(2026, 5, 20))     # 历史行
    old = moomoo_iv.day_ms(date(2026, 8, 20))      # 将被改期的未来行
    new = moomoo_iv.day_ms(date(2026, 8, 21))
    storage.upsert_earnings(conn, "moomoo", [
        {"symbol": "NVDA-USDT", "ts": hist, "pub_type": None, "period": "2026Q1"},
        {"symbol": "NVDA-USDT", "ts": old, "pub_type": None, "period": "2026Q2"}])
    ka0 = conn.execute("SELECT known_at FROM earnings WHERE ts=?", (old,)).fetchone()[0]
    _t.sleep(0.01)
    fresh = [{"symbol": "NVDA-USDT", "ts": new, "pub_type": None, "period": "2026Q2"}]
    storage.upsert_earnings(conn, "moomoo", fresh)
    n = storage.prune_stale_future_earnings(
        conn, "moomoo", ["NVDA-USDT"],
        moomoo_iv.day_ms(date(2026, 8, 1)), moomoo_iv.day_ms(date(2026, 11, 1)), fresh)
    assert n == 1, f"应清掉 1 条旧日期，实清 {n}"
    left = {r[0] for r in conn.execute("SELECT ts FROM earnings").fetchall()}
    assert left == {hist, new}, "历史行保留、旧未来行删除、新行保留"
    assert ka0 is not None


def test_settled_only_drops_weekend_rows():
    """厂商异常给出的周末行必须被防御性丢弃。"""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    rows = [{"ts": moomoo_iv.day_ms(date(2026, 8, 8)), "iv": 20.0},   # 周六
            {"ts": moomoo_iv.day_ms(date(2026, 8, 7)), "iv": 21.0}]   # 周五
    now = datetime.combine(date(2026, 8, 9), dtime(12, 0),
                           tzinfo=ZoneInfo("America/New_York"))
    kept = moomoo_iv.settled_only(rows, now=now)
    assert [r["iv"] for r in kept] == [21.0], "周六行应被丢弃"


def test_settled_only_cross_year_history_uses_probable_calendar():
    """跨年历史清洗：表外工作日保留、周末拒绝、表内假日精确拒绝。

    2026-08-05 前 settled_only 误用实时 fail-closed 日历，2023/2024 的正常
    工作日会被整段删除；历史防御闸现在只在有假日表的年份做精确判定。
    """
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    rows = [
        {"ts": moomoo_iv.day_ms(date(2023, 6, 26)), "iv": 23.0},  # 周一
        {"ts": moomoo_iv.day_ms(date(2023, 6, 24)), "iv": 23.1},  # 周六
        {"ts": moomoo_iv.day_ms(date(2024, 6, 3)), "iv": 24.0},   # 周一
        {"ts": moomoo_iv.day_ms(date(2024, 6, 8)), "iv": 24.1},   # 周六
        {"ts": moomoo_iv.day_ms(date(2026, 7, 3)), "iv": 26.0},   # 表内独立日观察日
    ]
    now = datetime.combine(date(2026, 8, 5), dtime(12),
                           tzinfo=ZoneInfo("America/New_York"))
    kept = moomoo_iv.settled_only(rows, now=now)
    assert [r["iv"] for r in kept] == [23.0, 24.0]


def test_numeric_cleaners_reject_inf():
    """±inf 必须落 None——inf 进分位分母会吞掉整个分布。"""
    assert storage._f(float("inf")) is None
    assert storage._f(float("-inf")) is None
    assert moomoo_iv._num("inf") is None
    assert moomoo_iv._num("-inf") is None
    assert storage._f(1.5) == 1.5


def test_iv_term_pick_and_atm():
    """期限选取：每目标取最近到期、实际期限如实记录；ATM 取最贴现价行权价的 C/P 对。"""
    from regime import stock_iv_term as t

    rows = [("2026-08-05", 1), ("2026-08-07", 3), ("2026-08-12", 8),
            ("2026-08-14", 10), ("2026-09-04", 31)]
    picks = t.pick_expiries(rows)
    assert picks[3] == ("2026-08-07", 3)
    assert picks[9] in (("2026-08-12", 8), ("2026-08-14", 10))   # 8 与 10 等距，取其一
    assert picks[30] == ("2026-09-04", 31), "实际期限如实记录（31 而非谎称 30）"
    assert t.pick_expiries([]) == {}

    chain = [("C210", "CALL", 210.0), ("P210", "PUT", 210.0),
             ("C212", "CALL", 212.5), ("P212", "PUT", 212.5)]
    call, put, k = t.atm_pair(chain, 211.9)
    assert (call, put, k) == ("C212", "P212", 212.5)
    assert t.atm_pair([], 100.0) is None


def test_iv_term_marks_single_leg_degradation(monkeypatch):
    """ATM 正常为 C/P 均值；缺一腿时保留单腿值并通过内存 legs 标记暴露。"""
    import sys
    from types import SimpleNamespace

    import pandas as pd

    from regime import stock_iv_term as t

    monkeypatch.setitem(sys.modules, "moomoo", SimpleNamespace(RET_OK=0))
    monkeypatch.setattr(t, "PACE", 0)

    class Ctx:
        def get_market_snapshot(self, batch):
            vals = {
                "C3": 20.0, "P3": 22.0,
                "C9": 30.0, "P9": float("nan"),
                "C30": 0.0, "P30": None,
            }
            return 0, pd.DataFrame([
                {"code": code, "option_implied_volatility": vals[code]}
                for code in batch
            ])

    entry = {
        str(tenor): {
            "call": f"C{tenor}", "put": f"P{tenor}",
            "tenor": tenor, "expiry": f"2026-08-{tenor:02d}", "strike": 100.0,
        }
        for tenor in (3, 9, 30)
    }
    rows = t.fetch_term(Ctx(), {"NVDA-USDT": entry}, now_ms=1_785_000_000_000)

    assert len(rows) == 1
    assert rows[0]["iv3"] == 21.0 and rows[0]["legs"]["3"] == 2
    assert rows[0]["iv9"] == 30.0 and rows[0]["legs"]["9"] == 1
    assert rows[0]["iv30"] is None and "30" not in rows[0]["legs"]


def test_collector_warns_when_iv_term_uses_one_leg(monkeypatch, caplog):
    import logging

    import collector
    from regime import calendar_nyse, stock_iv_term

    class Ctx:
        def close(self):
            pass

    conn = _mem_conn()
    monkeypatch.setattr(calendar_nyse, "is_rth", lambda now: True)
    monkeypatch.setattr(moomoo_iv, "opend_alive", lambda: True)
    monkeypatch.setattr(moomoo_iv, "open_ctx", lambda: Ctx())
    monkeypatch.setattr(collector, "_should", lambda *args: True)
    # 缓存须是"三期限齐全"的完整条目，否则增量补全逻辑会去真建链（本测试只测单腿告警）
    monkeypatch.setattr(stock_iv_term, "load_codes_cache", lambda conn: {
        "NVDA-USDT": {str(t): {"call": "c", "put": "p", "strike": 100.0,
                               "tenor": t, "expiry": "2026-08-08"}
                      for t in stock_iv_term.TARGETS},
    })
    monkeypatch.setattr(stock_iv_term, "fetch_term", lambda *args, **kwargs: [{
        "symbol": "NVDA-USDT", "ts": 1_785_000_000_000,
        "iv3": 30.0, "t3": 3, "iv9": 31.0, "t9": 9,
        "iv30": 32.0, "t30": 30, "legs": {"3": 1, "9": 2, "30": 2},
    }])
    caplog.set_level(logging.WARNING, logger="collector")

    collector.sync_stock_iv_term(conn, ["NVDA-USDT"])

    assert "ATM 单腿退化" in caplog.text and "NVDA-USDT 3d" in caplog.text


def test_collector_advances_iv_term_build_group_across_failed_rounds(monkeypatch):
    """R2-9 的新形态：轮换起点已换成固定分组，但**意图不变**——
    即使每轮建链都失败，轮次计数也必须前进，否则某一组会永久饿死。

    另一半同样重要：整轮失败（built_now=0）视为限频而非品种问题，
    **不得给任何品种记失败账**，否则一次限频就把当轮品种拉黑一整天。
    """
    import collector
    from regime import calendar_nyse, stock_iv_term

    class Ctx:
        def close(self):
            pass

    conn = _mem_conn()
    syms = [f"S{i}-USDT" for i in range(20)]
    seen = []
    monkeypatch.setattr(calendar_nyse, "is_rth", lambda now: True)
    monkeypatch.setattr(moomoo_iv, "opend_alive", lambda: True)
    monkeypatch.setattr(moomoo_iv, "open_ctx", lambda: Ctx())
    monkeypatch.setattr(collector, "_should", lambda *args: True)
    monkeypatch.setattr(collector.instruments, "get", lambda symbol: {"class": "us_stock_perp"})
    monkeypatch.setattr(stock_iv_term, "load_codes_cache", lambda conn: {})

    def build(ctx, symbols, existing=None, skip=None):
        seen.append(list(symbols))
        return {}, [f"{s}:RATE_LIMIT" for s in symbols]

    monkeypatch.setattr(stock_iv_term, "build_codes", build)
    for _ in range(3):
        collector.sync_stock_iv_term(conn, syms)

    # 每轮**以不同的组打头**——计数器在失败轮也前进了。
    # 用前缀而不是相等：本组不足一批时会用其余缺失品种补满（见补位逻辑），
    # 所以 todo 会比本组长；分组仍必须是主序。
    for r in range(3):
        grp = stock_iv_term.build_group(syms, r)
        assert seen[r][:len(grp)] == grp, f"第{r}轮的主序不是第{r}组"
    assert seen[0][:1] != seen[1][:1], "失败轮没有推进分组，该组会永久饿死"
    # 三轮全灭，但一个品种都不许被拉黑
    assert stock_iv_term.blocked_symbols(
        stock_iv_term.load_build_fails(conn)) == set()


def test_iv_term_storage_roundtrip():
    conn = _mem_conn()
    storage.upsert_stock_iv_term(conn, [
        {"symbol": "NVDA-USDT", "ts": 1785900000000,
         "iv3": 44.4, "t3": 3, "iv9": 45.1, "t9": 8, "iv30": 48.0, "t30": 31}])
    df = storage.get_stock_iv_term(conn, "NVDA-USDT")
    assert len(df) == 1 and df["iv3"].iloc[0] == 44.4 and df["t30"].iloc[0] == 31
    assert storage.get_stock_iv_term(conn, "AAPL-USDT").empty


def test_earnings_event_windows_et_semantics():
    """v3.1 事件窗 = ET 日历日差 ∈ [0, 10]，**含财报当天**。

    v3 的 UTC 毫秒严格比较有两个真 P0（12 路审计 6 路独立发现）：
    ① 财报当天整天被排除在窗外——恰是开盘跳空、最需要门槛的一天；
    ② 固定 240h 窗口在跨 DST 时与 ET 日历差漂移 1 小时。
    注意 00:00 UTC 锚的 ts 换到 ET 是**前一天晚上**，日差按 ET 日期算。
    """
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")

    def et_ms(d, hh, mm=0):
        return int(datetime.combine(d, dtime(hh, mm), tzinfo=et).timestamp() * 1000)

    conn = _mem_conn()
    e = moomoo_iv.day_ms(date(2026, 8, 20))
    storage.upsert_earnings(conn, "moomoo", [
        {"symbol": "NVDA-USDT", "ts": e, "pub_type": None, "period": None}])

    # 用真实 ET 盘中时刻测（不是午夜锚，避免测试与实现共享同一盲区）：
    ts = [
        et_ms(date(2026, 8, 9), 10),    # 距财报 11 个 ET 日 → False
        et_ms(date(2026, 8, 10), 10),   # 恰 10 日 → True
        et_ms(date(2026, 8, 19), 15),   # 前一日盘中 → True
        et_ms(date(2026, 8, 20), 10),   # **财报当天 RTH → True（v3 的 P0：曾为 False）**
        et_ms(date(2026, 8, 20), 21),   # 财报当天盘后 → True（AMC 发布前后同日保守含）
        et_ms(date(2026, 8, 21), 10),   # 次日 → False
    ]
    assert storage.earnings_event_windows(conn, "NVDA-USDT", ts) == \
        [False, True, True, True, True, False]
    assert storage.earnings_event_windows(conn, "AAPL-USDT", ts) == [False] * 6

    # DST 跨越（2026-11-01 结束夏令时）：固定 240h 窗会漂移，ET 日差不会
    conn2 = _mem_conn()
    e2 = moomoo_iv.day_ms(date(2026, 11, 2))
    storage.upsert_earnings(conn2, "moomoo", [
        {"symbol": "NVDA-USDT", "ts": e2, "pub_type": None, "period": None}])
    ts2 = [
        et_ms(date(2026, 10, 22), 20),  # ET 日差 11 → False（毫秒口径曾误判 True）
        et_ms(date(2026, 10, 23), 10),  # 恰 10 → True
        et_ms(date(2026, 11, 1), 19),   # 前一日（DST 切换日）→ True（毫秒口径曾误判 False）
    ]
    assert storage.earnings_event_windows(conn2, "NVDA-USDT", ts2) == [False, True, True]


def test_breadth_slot_respects_half_day_close():
    """半日市（收 13:00）：12:50 是近收盘槽、15:50 不是。"""
    from datetime import datetime, time as dtime
    from zoneinfo import ZoneInfo

    import collector
    from regime.calendar_nyse import session_close_et

    et = ZoneInfo("America/New_York")
    half = None
    for cand in (date(2026, 11, 27), date(2026, 7, 2), date(2026, 12, 24)):
        if session_close_et(cand) < dtime(16, 0):
            half = cand
            break
    if half is None:
        return
    assert collector.breadth_slot(datetime.combine(half, dtime(12, 50), tzinfo=et)) == "1559"
    assert collector.breadth_slot(datetime.combine(half, dtime(15, 50), tzinfo=et)) is None


def test_heartbeat_lane_states(monkeypatch):
    """分管线心跳：空表盘中=断流；落库=转正常；盘外 RTH 门控管线=idle 不算故障。"""
    import time as _t

    import dashboard
    import regime.calendar_nyse as cal

    conn = _mem_conn()
    monkeypatch.setattr(cal, "is_rth", lambda ms: True)
    by = {l["key"]: l for l in dashboard._heartbeat(conn)}
    for k in ("近端IV", "个股IV", "期限曲线", "衍生品"):
        assert by[k]["state"] == "bad", k
    assert "OpenD" in by  # 探活结果依环境，只验存在

    storage.upsert_opt_iv_near(conn, {"symbol": "BTC-USDT", "ts": int(_t.time() * 1000),
                                      "iv": 30.0, "tenor_days": 3.0, "method": "interp",
                                      "n_expiries": 5, "index_price": 1.0})
    assert {l["key"]: l for l in dashboard._heartbeat(conn)}["近端IV"]["state"] == "ok"

    monkeypatch.setattr(cal, "is_rth", lambda ms: False)
    by3 = {l["key"]: l for l in dashboard._heartbeat(conn)}
    assert by3["个股IV"]["state"] == "idle" and by3["期限曲线"]["state"] == "idle"
    assert by3["近端IV"]["state"] == "ok", "24/7 管线不受 RTH 门控"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))


def test_iv_term_build_codes_incremental_and_reports_fails(monkeypatch):
    """build_codes 增量补全 + 失败留痕（2026-08-05 首个 RTH 限频事故的回归）。

    事故：PACE=0.5 恰等于 moomoo 限额 → 61 品种散布式失败，只建成 26 个；
    且 `except: continue` 零日志，与 CBOE iv30 同型静默失败。
    修法三条：降速、跨轮分批增量补、失败上报调用方。
    """
    from regime import stock_iv_term
    from types import SimpleNamespace

    assert stock_iv_term.PACE >= 0.6, "PACE 必须留出限频余量（moomoo 60次/30秒）"
    assert stock_iv_term.BUILD_BATCH < 61, "单轮建链必须分批，不得一轮打满全宇宙"

    # 纯单元测试只消费 RET_OK；避免真实 SDK 导入时向沙箱外 HOME 写日志。
    monkeypatch.setitem(sys.modules, "moomoo", SimpleNamespace(RET_OK=0))
    monkeypatch.setattr(stock_iv_term, "PACE", 0)

    class _Ctx:
        """只对 want 里的品种给链；其余模拟限频失败（ret!=0）。"""

        def __init__(self, ok_syms):
            self.ok = ok_syms
            self.chain_calls = 0

        def get_market_snapshot(self, codes):
            import pandas as pd
            return 0, pd.DataFrame([{"code": c, "last_price": 100.0} for c in codes])

        def get_option_expiration_date(self, code):
            import pandas as pd
            if code.split(".")[-1] not in self.ok:
                return -1, "RATE_LIMIT"
            return 0, pd.DataFrame([
                {"strike_time": "2026-08-08", "option_expiry_date_distance": 3},
                {"strike_time": "2026-08-14", "option_expiry_date_distance": 9},
                {"strike_time": "2026-09-04", "option_expiry_date_distance": 30},
            ])

        def get_option_chain(self, code, start=None, end=None):
            import pandas as pd
            self.chain_calls += 1
            return 0, pd.DataFrame([
                {"code": f"{code}{start}C100", "option_type": "CALL", "strike_price": 100.0},
                {"code": f"{code}{start}P100", "option_type": "PUT", "strike_price": 100.0},
            ])

    syms = ["NVDA-USDT", "AMD-USDT", "MU-USDT"]
    # 第一轮：只有 NVDA 能建（模拟其余撞限频）
    ctx = _Ctx({"NVDA"})
    out1, fails1 = stock_iv_term.build_codes(ctx, syms)
    assert set(out1) == {"NVDA-USDT"} and len(out1["NVDA-USDT"]) == 3
    assert len(fails1) == 2, f"失败必须上报而非静默吞掉，实得 {fails1}"
    assert all("到期日" in f for f in fails1)

    # 第二轮：限频恢复，增量只补缺失的两个——已建成的 NVDA 不得重复请求链
    ctx2 = _Ctx({"NVDA", "AMD", "MU"})
    out2, fails2 = stock_iv_term.build_codes(ctx2, syms, existing=out1)
    assert set(out2) == set(syms) and not fails2
    assert ctx2.chain_calls == 6, f"只应为 AMD/MU 各请求 3 条链，实得 {ctx2.chain_calls}"
    assert out2["NVDA-USDT"] == out1["NVDA-USDT"], "已建成条目必须原样保留"

    # 第三轮：全部完整 → 零请求
    ctx3 = _Ctx({"NVDA", "AMD", "MU"})
    out3, fails3 = stock_iv_term.build_codes(ctx3, syms, existing=out2)
    assert out3 == out2 and not fails3 and ctx3.chain_calls == 0
