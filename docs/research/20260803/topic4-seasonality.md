# 主题4：日内/周内波动率季节性与去季节化方法

## 一、文献地图

结论口径：日内波动存在确定性周期是**学术共识**；具体形状是否恒定、隔夜信息如何纳入、加密周末效应是否衰减均**有争议**；现金股票规律能否迁移到美股永续属于**从业者假设**。

### 1. 日内周期与滤波方法

- Andersen、Bollerslev（1997），[Intraday Periodicity and Volatility Persistence in Financial Markets](https://scholars.duke.edu/publication/761297)，*Journal of Empirical Finance* 4(2–3)【已核实】。5 分钟外汇与 S&P 500 期货存在强周期；后者绝对收益呈开盘高、午间低、收盘再高的 U 型，FFF 用正余弦及虚拟变量分离日间波动水平与时段因子。对 1h bar，主要含义是首尾 RTH 小时仍不可直接与午间比较，但 9:00–10:00 ET 可能混合盘前与开盘半小时。

- Laakkonen（2007），[Exchange Rate Volatility, Macro Announcements and the Choice of Intraday Seasonality Filtering Method](https://publications.bof.fi/server/api/core/bitstreams/04e7025e-9f5b-4bc9-88b5-9c06cb067416/content)，Bank of Finland Discussion Paper 23/2007【已核实】。比较离散时段均值、LOWESS 与 FFF；短估计窗能更好追踪时变季节性，模拟中 FFF 综合最好，但所有滤波器都可能削弱真实新闻冲击。

- Boudt、Croux、Laurent（2011），[Robust Estimation of Intraweek Periodicity in Volatility and Jump Detection](https://researchportal.vub.be/en/publications/robust-estimation-of-intraweek-periodicity-in-volatility-and-jump/)，*Journal of Empirical Finance* 18(2)【已核实】。普通标准差时段因子会被跳跃显著抬高，Shortest-Half、WSD、截断似然更稳健。Yi（2020），[Jump Probability Using Volatility Periodicity Filters](https://ideas.repec.org/a/eee/ecofin/v53y2020ics1062940820300814.html)，*North American Journal of Economics and Finance* 53【已核实】，进一步验证 MAD/ShortH/WSD 会实质改变跳跃识别结果。

- Andersen、Thyrsgaard、Todorov（2019），[Time-Varying Periodicity in Intraday Volatility](https://ideas.repec.org/a/taf/jnlasa/v114y2019i528p1695-1707.html)，*JASA* 114(528)【已核实】。S&P 500 拒绝“固定 TOD 曲线”：高波动状态下，收盘前占全天波动的比例更高。

### 2. 隔夜、盘前与盘后

- Ahoniemi、Lanne（2013），[Overnight Stock Returns and Realized Volatility](https://pure.au.dk/portal/en/publications/overnight-stock-returns-and-realized-volatility/)，*International Journal of Forecasting* 29(4)【已核实】。S&P 500 指数适合优化纳入隔夜信息，但对多数个股，忽略隔夜反而产生更准确的 RV 估计，说明不存在统一处理法。

- Chen、Yu、Zivot（2012），[Predicting Stock Volatility Using After-Hours Information](https://ideas.repec.org/a/eee/intfor/v28y2012i2p366-383.html)，*International Journal of Forecasting* 28(2)【已核实】。30 只活跃 NASDAQ 股票中，盘前 RV 对次日波动最有预测力；盘后 RV 和单个隔夜平方收益较弱。

- Lyócsa、Todorova（2020），[Trading and Non-trading Period Realized Market Volatility](https://www.sciencedirect.com/science/article/pii/S0169207019302250)，*International Journal of Forecasting* 36(2)【已核实】。431 只 S&P 500 成分股自身的隔夜平方收益表现很差，而 E-mini 的市场级隔夜 RV 对未来 1–5 日波动显著有用。

- Barclay、Hendershott（2003），[Price Discovery and Trading After Hours](https://ideas.repec.org/a/oup/rfinst/v16y2003i4p1041-1073.html)，*Review of Financial Studies* 16(4)【已核实】。日间每小时价格发现更多；盘前交易虽稀疏，但单笔信息量及价格变化大于盘后。

### 3. 加密市场与 DST

- Kaiser（2019），[Seasonality in Cryptocurrencies](https://ideas.repec.org/a/eee/finlet/v31y2019ics1544612318304513.html)，*Finance Research Letters* 31【已核实】。十种加密资产周末平均成交量、波动率和价差较低，但收益日历效应不稳健。Ma、Tanizaki（2019），[The Day-of-the-Week Effect on Bitcoin Return and Volatility](https://www.sciencedirect.com/science/article/pii/S0275531918307827)，*Research in International Business and Finance* 49【已核实】，却在 2013–2018 样本发现周一、周四波动较高，体现样本依赖。

- Hansen、Kim、Kimbrough（2021 首发，持续更新），[Periodicity in Cryptocurrency Volatility and Liquidity](https://arxiv.org/abs/2109.12142)，arXiv 预印本【已核实】。BTC/ETH 在多个交易所存在小时、星期周期，周末尤其周六较低，且 00/08/16 UTC 附近出现与永续资金费相关的峰值。Mueller（2024），[Revisiting Seasonality in Cryptocurrencies](https://www.sciencedirect.com/science/article/pii/S1544612324004598)，*Finance Research Letters* 64【已核实】，则发现 500 种币的收益季节性不稳健、BTC 正周一效应在 2015 年后消失，但周末低交易活动仍在。故“收益效应衰减”不能推出“波动周期已消失”。

- Chaboud 等（2004），[The High-Frequency Effects of U.S. Macroeconomic Data Releases](https://www.federalreserve.gov/pubs/ifdp/2004/823/ifdp823.htm)，Federal Reserve IFDP 823【已核实】。因东京不实行 DST，作者按纽约夏令时/标准时分别展示曲线；惯例是让经济时钟跟随当地市场，而非固定 UTC 偏移。

## 二、对 VVV_Trade 的具体启示

统一验证采用逐月 walk-forward、全量 `shift(1)`；现有 1h OHLCV、funding 和 SPY/QQQ 永续已足够。比较下一 1/4/12/24h TR 或 RV 的 MAE、QLIKE，滤波后各时段中位数离散度，以及 `high_vol_chop` 对未来高波动、`squeeze` 对未来扩张的命中率、状态换手和净费后 PnL。

1. **稳健化现有 48 桶。** 文献表明均值易受跳跃污染；当前每桶 30 个观测仅相当于工作日约 6 周、周末约 15 周，适应速度不一致。保留 `atr_rank_ds`，新增“桶中位数＋MAD/Hampel 降权”、有效样本数和因子置信度；按 13/26/52 个日历周而非固定观测数比较，样本不足继续返回 `None`。TR% 是正值时，应以中位数估计季节水平、MAD 负责异常值和不确定性，不能机械地用 MAD 作除数。**改动量级：小。** 实验：当前均值 N=30 对比稳健窗口及指数衰减版本。

2. **增加 FFF 影子因子。** 离散桶可审计且保留开收盘突变，但噪声大；低阶 FFF 能跨小时共享样本。对 `log(TR%/慢速全局尺度)` 使用 2–4 阶正余弦，并加入 RTH 开盘、收盘、周末、假日/早收盘虚拟变量，避免平滑掉断点。**改动量级：中。** 实验：按资产比较离散桶、稳健桶、FFF；若残余 TOD 差异和预测损失均下降才允许进入阈值校准。

3. **把美股永续拆成交易时段分量。** 新增 `rv_rth / rv_pre / rv_post / rv_overnight / rv_weekend`、`rth_overlap_fraction`，并加入 SPY/QQQ 的对应市场级分量；先作为状态机影子特征，不直接改 `atr_rank>0.85`。**改动量级：中。** 现有数据可做；分别检验“个股自身隔夜量”与“SPY/QQQ 市场隔夜量”的增量预测力。整点 1h bar 无法纯化 9:30 开盘，须标记混合 bar。

4. **建立资产专属时钟。** 美股永续用 IANA `America/New_York`、NYSE 开市日历和 `is_dst/utc_offset/fold` 审计字段；加密使用 UTC 小时、星期及 00/08/16 funding 虚拟变量，不能套 ET 48 桶。**改动量级：小至中。** DST 本身现有时间戳即可验证；交易日历需增加轻量参考数据。4h 应由去季节化后的 1h 聚合，1d 只研究周内效应。

## 三、反方与陷阱

- 固定 U 型不是普遍真理，也可能是 L 型或随波动状态改变；不能把季节曲线写成永久规则。
- 滤波可能把定时宏观公告、财报和真实跳跃当作“正常季节性”除掉；必须同时保留 raw、ds、因子值与命中原因。
- 低阶 FFF 会抹平开收盘断点，高阶 FFF 又容易过拟合；阶数只能在训练折内选择。
- 加密周末**交易活动较低**较稳健，但具体哪天波动最高及是否衰减有争议，SOL 更不能直接继承 BTC 结论。
- 现金股票的盘前/盘后研究不包含 24/7 股永续的资金费、做市和指数锚定机制；迁移仅是从业假设，必须逐合约验证。tick 级微结构与期权做市超出当前 1h 单机能力，不展开。

## 四、优先级排序

1. **先完成“现有均值桶 vs 稳健桶 vs FFF”的因果 A/B 回测。** 它直接服务 P0 阈值校准，且无需新增高频数据；在结果出来前不替换生产 `atr_rank_ds`。
2. **随后上线 RTH/盘前/盘后/隔夜分解及加密 UTC/funding 时钟。** 这两项最可能解释当前被统一 ATR 分位误判的结构性波动，并保持规则树可审计。