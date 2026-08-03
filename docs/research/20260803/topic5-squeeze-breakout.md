# 主题5：波动率压缩、挤压与突破

## 一、文献地图

### 1. 波动率聚集、均值回复与 vol-of-vol

- **学术共识。** Bollerslev（1986），[Generalized Autoregressive Conditional Heteroskedasticity](https://public.econ.duke.edu/~boller/Published_Papers/joe_86.pdf)，*Journal of Econometrics*；Andersen、Bollerslev、Diebold、Labys（2003），[Modeling and Forecasting Realized Volatility](https://doi.org/10.1111/1468-0262.00418)，*Econometrica*。【已核实】平稳波动过程会向长期均值回复，但实证持久性很强，所以短期通常仍是“低波跟低波”，并非极低波后立即爆发。

- **反方证据。** Ning、Xu、Wirjanto（2015），[Is Volatility Clustering of Asset Returns Asymmetric?](https://doi.org/10.1016/j.jbankfin.2014.11.016)，*Journal of Banking & Finance*。【已核实】高波聚集显著强于低波聚集，进一步否定“压得越低、下一根越容易爆”的机械解释。

- **有争议。** Corsi、Mittnik、Pigorsch、Pigorsch（2008），[The Volatility of Realized Volatility](https://doi.org/10.1080/07474930701853616)，*Econometric Reviews*。【已核实】显式建模时变 vol-of-vol 改善 S&P 500 期货波动预测；但 Kambouroudis、McMillan、Tsakou（2021），[Forecasting Realized Volatility](https://onlinelibrary.wiley.com/doi/10.1002/fut.22241)，*Journal of Futures Markets*。【已核实】发现该增益主要限于美国指数，而隐含波动率在十个市场更稳定有效，故不能把 vol-of-vol 当通用触发器。

### 2. BBW、收缩形态与出口

- Fang、Jacobsen、Qin（2017），[Popularity versus Profitability: Evidence from Bollinger Bands](https://www.pm-research.com/content/iijpormgmt/43/4/152)，*Journal of Portfolio Management*。【已核实】十四国长样本中，Bollinger 规则在公开普及后持续衰减；其“低 BBW＋穿越带宽”版本在现代样本几乎没有方向预测力。论文主要检验有符号收益，而非未来绝对波幅，因此文献不能证明“方向可预测”，也尚未严格证明“幅度一定扩大”。

- **从业者经验。** John Bollinger（2021），[Bollinger Bands Confirmed Breakouts Method IV](https://www.bollingerbands.com/_files/ugd/58be43_d09c50b6e8ea4afd9af0523ef94de876.pdf)【已核实】把 squeeze 定义为约125期带宽低点，并以连续两次收盘越带确认出口；Toby Crabel（1990），[Day Trading with Short Term Price Patterns and Opening Range Breakout](https://books.google.com/books/about/Day_Trading_with_Short_Term_Price_Patter.html?id=xpgbAAAACAAJ)【已核实】是 NR7/inside-day 实务来源；Mark Minervini（2013），[Trade Like a Stock Market Wizard](https://www.mheducation.com/highered/mhp/product/trade-like-stock-market-wizard-how-achieve-super-performance-stocks-any-market.html)【已核实】是 VCP 主要来源。三者均缺少现代、无幸存者偏差、扣费后的同行评审样本外验证。

- Lo、Mamaysky、Wang（2000），[Foundations of Technical Analysis](https://doi.org/10.1111/0022-1082.00265)，*Journal of Finance*。【已核实】部分算法化图形含增量信息，但未检验 NR7、inside bar、VCP，且“收益分布不同”不等于可交易利润。Zarattini、Aziz、Barbon（2024，2025修订），[Beat the Market](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4824172)，Swiss Finance Institute 工作论文。【已核实】在其 SPY 分钟级策略中 NR7 分组较好、inside day 不显著；这是单一ETF条件分析，并非独立形态检验。

### 3. 量能确认

- Blume、Easley、O’Hara（1994），[Market Statistics and Technical Analysis: The Role of Volume](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.1994.tb04424.x)，*Journal of Finance*。【已核实】理论上成交量包含价格之外的信号质量信息，但没有证明放量能降低假突破率。

- Llorente、Michaely、Saar、Wang（2002），[Dynamic Volume-Return Relation of Individual Stocks](https://doi.org/10.1093/rfs/15.4.1005)，*Review of Financial Studies*。【已核实】高量后的延续或反转取决于信息交易还是风险分担；止损、再平衡和流动性冲击同样会制造高量。因此“放量＝真突破”不是学术共识。

### 4. Donchian/CTA 的长期与衰减证据

- Szakmary、Shen、Sharma（2010），[Trend-following Trading Strategies in Commodity Futures](https://www.sciencedirect.com/science/article/pii/S037842660900199X)，*Journal of Banking & Finance*。【已核实】28个商品期货、48年月频样本中，多组价格通道规则扣成本后多数为正，但1996—2007盈利已下降；它支持中慢速通道类别，不是严格复刻 Turtle 参数。

- Lempérière等（2014），[Two Centuries of Trend Following](https://arxiv.org/abs/1404.3274)，*Journal of Investment Strategies*。【已核实】数月级趋势未显著退化，约三日的快速趋势自1990年代明显萎缩。Goulding、Harvey、Mazzoleni（2024），[Breaking Bad Trends](https://doi.org/10.1080/0015198X.2023.2270084)，*Financial Analysts Journal*。【已核实】2008年后趋势断裂增多，可解释标准月度趋势策略的低迷。故不能把月频证据直接外推到1h/4h。

## 二、对 VVV_Trade 的具体启示

1. **先校准“幅度”，再讨论方向。** 文献依据：波动持久性与 Fang 等的方向反证。现状差距是 `bbw_rank<0.15 && atr_rank<0.30` 同时承担状态识别和“即将突破”含义。建议保留 squeeze 为无方向风险状态，方向仅由事前的结构方向分给出。**改动量级：小。** 用现有数据网格检验 BBW 分位5/10/15/20%与ATR分位20/30/40%；美股永续并列比较 `atr_rank` 和 `atr_rank_ds`。在1/3/6/12/24 bars报告未来平方收益、high-low/ATR、MFE/MAE及方向命中率，事件去重后做 purged walk-forward；已有数据完全可做。

2. **显式出口先做影子字段，不立即改五状态。** 新增 `squeeze_id`、压缩区间高低点、`Δlog(BBW)`、收盘突破方向、确认根数和 `false_break`，全部进入现有审计键。**改动量级：中。** 比较三种 challenger：现有自然切换；BBW分位迟滞出口；“带宽扩张＋收盘越过冻结区间”并确认1/2根。假突破定义为先回到区间、未先走出1 ATR；比较延迟、状态转移准确率及扣费/资金费后收益。OHLCV足够完成。

3. **量能 tilt 暂不进入硬状态规则。** 建议增加按ET小时×周末桶、严格 `shift(1)` 的相对量影子字段，并与已有 taker 买卖比、tilt 交互。**改动量级：中；现有数据可做。** 在控制 BBW、ATR、结构方向后，检验量能是否跨 walk-forward 折稳定提高真突破的Brier/AUPRC和固定召回率下精度；通过后最多升级为置信度或减少一根确认，不作为 squeeze 出口的必要条件。

4. **vol-of-vol 只作 challenger。** 增加 `vov_rank=rank(std(Δlog RV))`，并在 BTC/ETH、SPY/QQQ 上加入 DVOL/VIX/VXN 与RV差值。**改动量级：中。** 与现有12/72波动加速度做嵌套增量检验；日频IV不可在每个1h bar重复计作独立样本，个股 iv30 历史过短，暂不足以定规则。

## 三、反方与陷阱

- 极低波是当前状态，不是爆发倒计时；长时间滞留 squeeze 应被视为正常结果。
- 不得先观察上破/下破再回填“方向预测正确”；幅度标签和事前方向标签必须分离。
- 阈值、确认根数、形态和持有期形成大参数族。Rink（2023），[The Predictive Ability of Technical Trading Rules](https://link.springer.com/article/10.1007/s11408-023-00433-2)，*Financial Markets and Portfolio Management*。【已核实】检验6,406条规则后发现近期样本外持续性近乎消失且对成本敏感；因此需嵌套walk-forward及SPA/FDR校正。
- 1h Donchian 的换手、滑点、资金费必须单独计入；日/月频CTA证据不能背书快速突破。
- tick级订单簿、期权做市证据超出当前数据能力，不应列入本轮升级。

## 四、优先级排序

1. **P0：完成 squeeze 事件研究和阈值校准。** 无需新数据，能首先回答现有0.15/0.30阈值是否增加未来波幅，以及结构方向是否真有增量。
2. **P1：上线可审计的出口影子字段。** 优先检验“带宽扩张＋收盘确认”；量能仅作条件变量。NR7、inside bar、VCP及硬量能门槛在获得跨品种、跨周期样本外证据前均不进入正式规则。