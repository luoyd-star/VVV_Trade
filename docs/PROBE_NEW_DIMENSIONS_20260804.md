# 新数据维度可得性实测（2026-08-04，OpenD 实连）

调研工作流在评估"值不值得建"；本文档只回答**"接口到底给不给"**——
文献价值再高，拿不到数据也是空谈。全部经本机 OpenD 真实调用验证。

## 已验证可用

| 维度 | 接口 | 实测所得 | 历史深度 |
|---|---|---|---|
| **期权定位** | `get_option_underlying_his_statistic` | 日频：option/call/put_volume、**put_call_volume_ratio**、三个 open_interest 及 **put_call_open_interest_ratio**、underlying_price | **2023-06-23 起，与 IV 同深（≈3.1 年）→ 分位可算** |
| **市场宽度** | `get_rise_fall_distribution(market="US")` | 全市场 `US.USAALL` 约 1.8 万只按涨跌幅分桶：<−7 / −7~−5 / −5~−3 / −3~0 / 0 / 0~3 / 3~5 / 5~7 / >7，各桶 stock_count | **仅当前快照，无历史**（须自行积累，同 CBOE 冷启动问题） |
| **宏观政策路径** | `get_fed_watch_target_rate` | 62 行：meeting_date × target_range × probability（完整利率路径概率分布） | 未测（当前快照为主） |
| **宏观指标** | `get_macro_indicator_list("US")` + `_history` | 24 个美国指标含 CPI/Core CPI/PPI/PCE/Retail Sales，带 indicator_id | 待测 |
| **事件风险** | `get_economic_calendar(begin,end)` | 50 行/两周：title、timestamp、country、**star（重要度）**、previous、**consensus**、**actual** → 含"意外"维度 | 前瞻+回溯 |
| **财报事件** | `get_earnings_calendar(market, begin, end)` | 1769 行/周（**窗口 ≤7 天**）：eps_actual/predict、revenue_actual/predict、**option_volume、iv、iv_rank、iv_percentile** | 前瞻+回溯 |
| **资金流** | `get_capital_flow` | 391 行：in_flow 按 super/big/mid/sml 分档 + main_in_flow | 未测深度 |
| **空头持仓** | `get_short_interest` | 10 行：shares_short、short_percent、avg_daily_share_volume、**days_to_cover** | 双月报（滞后） |

## 已知限制与坑

- **涨跌分布无历史**是最大制约：宽度指标要有分位就得自己攒，冷启动几个月。
  另注意返回里 **4,424 只股票涨跌幅恰为 0**（约占 25%）——几乎必然是停牌/无成交的
  僵尸证券，算宽度前必须剔除，否则 A/D 比会被系统性稀释。
- `get_option_volatility` 只接受**期权合约代码**，不接受标的——标的级波动率走 3303/3304。
- `get_earnings_calendar` 单次窗口 ≤7 天，回填要分段。
- put/call 的 **open_interest 当日为 0、比值 N/A**（T-1 延迟，与文档一致）——
  当日只有成交量口径可用，持仓量口径要等一天。
- `get_option_market_statistic(option_market, data_type, ...)` 是市场级而非单标的，未展开测。

## 与现有系统的接口

put/call 与 IV 同源同深、同为日频，**可直接复用 stock_vol 表的形态与回填管线**
（新增列或新 source）。宽度与宏观是新形态（非 per-symbol 日频），需要新表。

## 待调研工作流回答

哪些真有增量价值（vs 与现有价格/波动率/相关性指标共线）、怎么算、样本外证据强度。
本文档只保证"数据拿得到"，不主张"值得用"——判断权交给调研 + 回测。
