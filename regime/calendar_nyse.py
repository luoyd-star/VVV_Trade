"""NYSE 交易日历：X1 治理件，L0 共同 RTH 面板的前置（预研 §6.2 缺口落地）。

设计取显式日期表而非规则引擎——节假日规则（观察日平移、耶稣受难日等）
写成代码极易出错，三年硬编码表可逐条对账，**每年 12 月须人工补下一年**
（缺表年份 is_rth 一律返回 False，宁缺毋滥地把未知当休市）。
时区经 zoneinfo America/New_York，DST 自动正确。
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

# 全日休市（NYSE 官网核对：新年/MLK/华盛顿/受难日/阵亡将士/六月节/独立日/
# 劳动节/感恩节/圣诞，含观察日平移）
HOLIDAYS = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
    date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
    date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}
# 13:00 ET 提前收盘
EARLY_CLOSE = {
    date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24),
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
}
_YEARS = {2025, 2026, 2027}


def is_trading_day(d: date) -> bool:
    if d.year not in _YEARS:
        return False  # 表外年份当休市——补表前禁止静默外推
    return d.weekday() < 5 and d not in HOLIDAYS


def is_probable_trading_day(d: date) -> bool:
    """历史行的防御性交易日筛选，不用于实时 RTH 开关。

    年份在显式假日表内时复用 :func:`is_trading_day` 精确判定；表外年份因
    无法识别当地假日，仅按 ``weekday() < 5`` 排除周末。该宽松退化只用于
    避免历史回填被有限年份日历整段误删，不能据此判断市场此刻是否开盘。
    """
    if d.year in _YEARS:
        return is_trading_day(d)
    return d.weekday() < 5


def session_close_et(d: date) -> dtime:
    return dtime(13, 0) if d in EARLY_CLOSE else dtime(16, 0)


def is_rth(ts_ms: int) -> bool:
    """该毫秒时刻是否处于 NYSE 正股交易时段（9:30 至当日收盘 ET）。"""
    et = datetime.fromtimestamp(ts_ms / 1000, tz=_ET)
    d = et.date()
    if not is_trading_day(d):
        return False
    return dtime(9, 30) <= et.time() < session_close_et(d)


def bar_full_rth(ts_ms: int, tf_ms: int) -> bool:
    """整根 bar [开盘, 收线) 是否完整落在 RTH 内——共同 RTH 面板的成员判定。

    正常日 9:30-16:00 含 6 根完整 1h bar（ET 10:00-16:00 整点），提前收盘日
    3 根；9:30 开盘那半根混合 bar 被排除（预研主题4：整点 1h 无法纯化开盘）。
    """
    et_open = datetime.fromtimestamp(ts_ms / 1000, tz=_ET)
    et_close = datetime.fromtimestamp((ts_ms + tf_ms) / 1000, tz=_ET)
    d = et_open.date()
    if not is_trading_day(d) or et_close.date() != d:  # 跨日 bar 直接拒绝
        return False
    return (et_open.time() >= dtime(9, 30)
            and et_close.time() <= session_close_et(d))
