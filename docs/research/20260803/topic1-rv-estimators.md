# 主题1：已实现波动率估计器与波动率预测

## 一、文献地图

结论分层：OHLC range 在连续扩散假设下比 close-to-close 更高效、波动率应按预测期限评价，属**学术共识**；现实中哪种估计器或模型胜出属**有争议**；ATR 平滑核及固定迟滞主要是**从业者经验**。

### 1. Range-based 估计器与 ATR

- Michael Parkinson（1980），《The Extreme Value Method for Estimating the Variance of the Rate of Return》，*Journal of Business*，【已核实】[原文](https://doi.org/10.1086/296071)：零漂移、连续随机游走下，high-low 方差估计的抽样方差效率约为 close-to-close 的 5.2 倍；原文按不同口径概括为 2.5～5 倍。

- Mark Garman、Michael Klass（1980），《On the Estimation of Security Price Volatilities from Historical Data》，*Journal of Business*，【已核实】[原文](https://www.cmegroup.com/trading/fx/files/a_estimation_of_security_price.pdf)：常用 OHLC 式效率约 7.4 倍，但依赖零漂移、连续监测；有限成交会使 high/low 范围显著向下偏。

- L. C. G. Rogers、Stephen Satchell（1991），《Estimating Variance from High, Low and Closing Prices》，*Annals of Applied Probability*，【已核实】[原文](https://doi.org/10.1214/aoap/1177005835)：对任意常数漂移无偏；零漂移下由论文方差可推得约 6.0 倍效率，但不处理开盘缺口和一般跳跃，离散极值误差可能很严重。

- Dennis Yang、Qiang Zhang（2000），《Drift-Independent Volatility Estimation Based on High, Low, Open, and Close Prices》，*Journal of Business*，【已核实】[原文](https://www.atmif.com/papers/range.pdf)：多期 YZ 同时处理漂移与 opening jump；典型十日美股样本效率约 7.3，所谓 14 倍只是特殊参数下峰值，opening jump 主导时可退化至约 1 倍。

- J. Welles Wilder（1978），《New Concepts in Technical Trading Systems》，Trend Research 专著，【已核实】[书目](https://books.google.com/books/about/New_Concepts_in_Technical_Trading_System.html?id=WesJAQAAMAAJ)：ATR 是实务型 range/gap 指标，原始 ATR14 采用递推 RMA（α=1/14），并非具有无偏性、效率定理的 integrated-variance 估计器。本次检索未发现 SMA14 与 RMA14 在波动率预测上的权威正面对比。

### 2. 波动率预测

- Fulvio Corsi（2009），《A Simple Approximate Long-Memory Model of Realized Volatility》，*Journal of Financial Econometrics*，【已核实】[论文](https://doi.org/10.1093/jjfinec/nbp001)：HAR-RV 用日、周、月三个成分近似长记忆；在一日、一周、两周预测上优于短记忆 AR，和更复杂的 ARFIMA 大致相当。

- Andersen、Bollerslev、Lange（1999），《Forecasting Financial Market Volatility: Sample Frequency vis-à-vis Forecast Horizon》，*Journal of Empirical Finance*，【已核实】[论文](https://doi.org/10.1016/S0927-5398(99)00013-4)：采样频率与预测期限必须分开，高频收益尤其能改善较长日际预测。

- Hansen、Lunde（2005），《A Forecast Comparison of Volatility Models: Does Anything Beat a GARCH(1,1)?》，*Journal of Applied Econometrics*，【已核实】[论文](https://doi.org/10.1002/jae.800)：比较 330 个 ARCH 模型后，汇率上复杂模型未击败 GARCH(1,1)，但 IBM 上带杠杆效应模型更优；GARCH 是强基准而非通用冠军。

- Andrew Patton（2011），《Volatility Forecast Comparison Using Imperfect Volatility Proxies》，*Journal of Econometrics*，【已核实】[论文](https://doi.org/10.1016/j.jeconom.2010.03.034)：噪声波动率代理可能反转模型排名；MSE、尤其 QLIKE 更适合比较方差预测。

- Dumitru、Hizmeri、Izzeldin（2025），《Forecasting the Realized Variance in the Presence of Intraday Periodicity》，*Journal of Banking & Finance*，【已核实】[论文](https://doi.org/10.1016/j.jbankfin.2024.107342)：日内周期会扭曲 RV 与预测；去周期 HAR 在股票及 SPY 的各预测期限均显著改善。

- Korkusuz、Kambouroudis、McMillan（2023），《Do Extreme Range Estimators Improve Realized Volatility Forecasts?》，*Finance Research Letters*，【已核实】[论文](https://www.sciencedirect.com/science/article/pii/S1544612323003641)：G7 市场中没有 range 扩展稳定胜出，复杂的 RS/YZ 未持续增加 HAR 信息，简单 overnight 或 close-to-close 变量反而更常有效，构成重要反证。

## 二、对 VVV_Trade 的具体启示

1. **先做估计器排序赛马，而非直接替换。**理论效率针对方差水平，不保证改变 250 根分位排序。建议新增 `rv_p30/rv_gk30/rv_rs30/rv_yz30` 原值及 rank、`atr14_rma_rank` 影子字段，生产状态仍用现规则。连续加密 K 线若 \(O_t=C_{t-1}\)，TR 退化为 H−L、YZ overnight 项为零；美股永续则应另算“NYSE session open 对上一 RTH close”的 YZ，并复用 `atr_rank_ds` 的因果 ET 桶去季节化。**改动量级：小。**现有 OHLC 足够；逐品种/周期比较 Spearman/Kendall、0.15/0.30/0.85 阈值分歧、状态切换时点及未来 \(h=1,2,3\) 根累计方差。若排序近似不变，即停止升级。

2. **新增轻量 HAR 预测层，GARCH 只作基准。**建立 walk-forward log-HAR：1h 使用 1/24/168 根、4h 使用 1/6/42 根、1d 加密使用 1/7/30 日成分；输出 `forecast_var_h1/h2/h3` 及其因果 250-rank，连同训练窗、系数和版本入库。基准为当前 RV30 持续值、校准 EWMA、GARCH(1,1)/GJR；日频可再加入已有 DVOL/VIX/VXN 的 HAR-X，短历史 iv30 暂不用于定版。**改动量级：中。**无需新数据；4h/1d 目标可由未来 1h 平方收益累加，1h 因无 sub-bar 数据，应同时用下一根平方收益与 range 代理，降低结论确定度。

3. **预测期限不能直接等同迟滞根数。**1/2/3 根确认是分类滤波，\(h\) 是未来风险区间；文献没有证明二者应相等。先把 \(h=k\) 作为影子挑战规则，不立刻替换迟滞。**改动量级：小（依赖上一项）。**比较提前正确切换根数、误切换率、驻留期、状态 churn、后续方差和策略损失；所有阈值须在嵌套 walk-forward 内校准。预测评价以 QLIKE 为主、MSE 与 Mincer–Zarnowitz 校准为辅，重叠目标用 HAC/block bootstrap，多模型用 Model Confidence Set。

## 三、反方与陷阱

- 5～14 倍是理想扩散下的抽样效率，不是预测收益；漂移、跳跃、薄成交、异常 high/low 会造成时变偏差，转成分位只能消除恒定尺度误差。
- 24/7 不等于假设成立：加密仍有清算跳跃；美股永续的 RTH、隔夜和周末流动性差异更强。未经 session 定义的小时 YZ opening jump 缺乏经济含义。
- RMA14 比 SMA14 记忆更长；“恢复 Wilder 原版”不是性能证据，必须按转换延迟与 OOS 损失选择。
- HAR 的强证据主要来自更细粒度数据构造的日度 RV；不得把日频结论直接外推到 1h。GARCH 复杂变体、tick 微结构及期权做市在当前数据能力下不展开。

## 四、优先级排序

1. **P0 回测框架内先完成 OHLC/RMA 影子字段及“是否改变排序”的闸门。**成本最低，且能直接判定理论效率对现有状态机是否有实际价值。
2. **随后上线去季节化的 HAR h1/h2/h3 影子预测，优先验证 4h、1d；GARCH 仅作强基准。**它最可能改善迟滞造成的事后识别，同时保持单机 pandas/SQLite 可承受的复杂度。