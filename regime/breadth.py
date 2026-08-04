"""市场宽度采集（moomoo 快照，**影子字段**）。

为什么只做影子：调研判定宽度族的大部分与耦合层数学同构
（截面离散度 ≡ 横截面相关，R²(mean_corr~disp+vol)=0.70；market_mode=λ1/N 即
Kritzman 吸收率），只有"池外覆盖"这一条是真增量——我们 38 个品种里 30 个是 AI 链，
看不到那 2600 只票在干什么。但该接口**零历史**，故必须自攒约 2 年（≈500 个日频点、
≈8 个独立 63 日 episode）才够评审转正。见 docs/DIMENSIONS_VERDICT_20260804.md。

采集纪律（对应调研点出的坑）：
- **采样时刻即口径**：09:45 采到的是隔夜跳空后的盘中，15:59 采到的是近收盘，
  两者不可混入同一序列 → slot 进主键，分列存放。
- **分母一并落库**：市值门槛在下跌中会把票踢出样本，N_t 自己会缩，
  不记录就无法区分"宽度下降"与"样本缩水"。（实测 N 不随周期漂移：五个周期同为 2659。）
- **壳股必须剔除**：涨跌分布里约 1/4 是当日零变动的不活跃标的，
  算尾部占比时分母用 total−flat。
- 返回体无时间戳，captured_at 记本地采样时刻，供事后核对 cron 漂移。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

SOURCE = "moomoo"
MCAP_FLOOR = 1e9   # 市值下限：滤掉微盘噪声，同时保持分母稳定


def _rank_count(ctx, period_type, extra=None):
    """某周期下满足条件的家数（只取 all_count，不拉榜单）。"""
    from moomoo import RET_OK, PeriodChangeIndicatorType as IT
    from moomoo import PeriodChangeRankFilter as F

    flt = [F(indicator_type=IT.MARKET_CAP, interval_min=MCAP_FLOOR)]
    if extra is not None:
        flt.append(extra)
    ret, payload = ctx.get_period_change_rank(
        "US", period_type=period_type, count=1, filter_list=flt
    )
    if ret != RET_OK:
        raise RuntimeError(f"period_change_rank: {payload}")
    return int(payload[0])   # (all_count, DataFrame)


def fetch_breadth(ctx) -> dict:
    """一次采样：宽度期限结构 + 涨跌分布尾部。约 11 次请求，限频 60/30s 富余。"""
    from moomoo import RET_OK, RankPeriodType as RP
    from moomoo import PeriodChangeIndicatorType as IT
    from moomoo import PeriodChangeRankFilter as F

    pos = F(indicator_type=IT.CHANGE_RATIO, interval_min=0)
    periods = {"1d": RP.ONE_DAY, "20d": RP.TWENTY_DAY, "60d": RP.SIXTY_DAY,
               "250d": RP.TWO_FIFTY_DAY, "ytd": RP.YTD}
    denom = _rank_count(ctx, RP.ONE_DAY)
    time.sleep(0.4)
    out = {"denom": denom}
    for k, pt in periods.items():
        out[f"up_{k}"] = _rank_count(ctx, pt, pos)
        time.sleep(0.4)

    # 涨跌分布：9 桶直方图 → 上下尾家数
    ret, dist = ctx.get_rise_fall_distribution(market="US")
    if ret != RET_OK:
        raise RuntimeError(f"rise_fall_distribution: {dist}")
    up = dn = flat = total = 0
    for b in dist.get("range_list") or []:
        n = int(b.get("stock_count") or 0)
        total += n
        t, lo, hi = b.get("type"), b.get("left_border"), b.get("right_border")
        if t == "POSITIVE_INFINITY":
            up += n
        elif t == "NEGATIVE_INFINITY":
            dn += n
        elif lo == 0 and hi == 0:
            flat += n          # 当日零变动＝停牌/无成交的壳股，算尾部占比时须剔除
    out.update(tail_up=up, tail_dn=dn, flat=flat, total=total)
    out["captured_at"] = int(time.time() * 1000)
    return out


def day_ms(dt: datetime | None = None) -> int:
    """采样所属交易日 00:00 UTC（与 usvol/stock_vol 同一日频时间格）。"""
    d = (dt or datetime.now(timezone.utc)).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
