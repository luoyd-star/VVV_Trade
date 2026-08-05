"""NYSE 日历钉桩：节假日/早收/DST/整根 RTH bar 判定。"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime.calendar_nyse import (  # noqa: E402
    bar_full_rth, is_probable_trading_day, is_rth, is_trading_day,
)

H = 3_600_000


def _ms(y, m, d, hh, mm=0):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def test_holidays_and_weekends():
    assert not is_trading_day(date(2026, 7, 3))    # 独立日观察（周五）
    assert not is_trading_day(date(2026, 8, 2))    # 周日
    assert is_trading_day(date(2026, 8, 3))        # 周一
    assert not is_trading_day(date(2024, 6, 3))    # 表外年份宁缺毋滥当休市


def test_probable_day_is_exact_in_table_and_weekday_only_outside():
    assert is_probable_trading_day(date(2023, 6, 26))   # 表外周一：历史清洗保留
    assert is_probable_trading_day(date(2024, 6, 3))    # 表外周一：历史清洗保留
    assert not is_probable_trading_day(date(2024, 6, 8))  # 表外周六仍拒绝
    assert not is_probable_trading_day(date(2026, 7, 3))  # 表内假日精确拒绝


def test_rth_dst_boundaries():
    # 2026-08-03 是 EDT：RTH 9:30-16:00 ET = 13:30-20:00 UTC
    assert not is_rth(_ms(2026, 8, 3, 13, 29))
    assert is_rth(_ms(2026, 8, 3, 13, 30))
    assert is_rth(_ms(2026, 8, 3, 19, 59))
    assert not is_rth(_ms(2026, 8, 3, 20, 0))
    # 2026-12-15 是 EST：RTH = 14:30-21:00 UTC（DST 平移一小时）
    assert not is_rth(_ms(2026, 12, 15, 14, 0))
    assert is_rth(_ms(2026, 12, 15, 14, 30))
    assert not is_rth(_ms(2026, 12, 15, 21, 0))


def test_full_rth_bars_per_day():
    # EDT 正常日：完整 1h bar 应为 UTC 14..19 共 6 根；13 点 bar 含开盘前半小时被拒
    hours = [h for h in range(24) if bar_full_rth(_ms(2026, 8, 3, h), H)]
    assert hours == [14, 15, 16, 17, 18, 19], hours
    # 早收日（2026-11-27，13:00 ET 收）：EST 下完整 bar = UTC 15,16,17 共 3 根
    hours = [h for h in range(24) if bar_full_rth(_ms(2026, 11, 27, h), H)]
    assert hours == [15, 16, 17], hours
