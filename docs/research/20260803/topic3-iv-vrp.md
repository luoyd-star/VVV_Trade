# 主题3：隐含波动率、已实现波动率与方差风险溢价

## 一、文献地图

总体判断：**学术共识**是匹配期限的 IV 含未来 RV 增量信息，但并非无偏；**有争议**的是 VRP 能否稳定预测标的收益；IV Rank/Percentile 的固定窗口和阈值主要是**从业者经验**。

### 1. IV、RV 与收益预测

- Christensen、Prabhala（1998），*The Relation Between Implied and Realized Volatility*，*Journal of Financial Economics*，【已核实】[论文](https://www.sciencedirect.com/science/article/pii/S0304405X98000348)：非重叠月度样本中，OEX IV 优于并可部分吸收历史波动信息，但样本外检验很短，不能解释成“IV 完全有效”。
- Busch、Christensen、Nielsen（2011），*The Role of Implied Volatility in Forecasting Future Realized Volatility and Jumps…*，*Journal of Econometrics*，【已核实】[论文](https://www.sciencedirect.com/science/article/pii/S0304407610000564)：IV 对股票、外汇和债券的连续波动及跳跃均有增量预测信息；无偏性并非跨市场普遍成立。
- Bollerslev、Tauchen、Zhou（2009），*Expected Stock Returns and Variance Risk Premia*，*Review of Financial Studies*，【已核实】[论文](https://academic.oup.com/rfs/article-abstract/22/11/4463/1565787)：高 VRP 预测较高市场收益，正式版在约三个月期限最强；这与 VVV 的 1h/4h 方向规则并不直接同频。
- Bollerslev、Marrone、Xu、Zhou（2014），*Stock Return Predictability and Variance Risk Premia*，*JFQA*，【已核实】[论文](https://doi.org/10.1017/S0022109014000453)：多国样本复现约 2–4 个月的驼峰关系。
- Goyal、Welch、Zafirov（2024），*A Comprehensive 2022 Look at the Empirical Performance of Equity Premium Prediction*，*RFS*，【已核实】[论文](https://academic.oup.com/rfs/article/37/11/3490/7749383)：将 BTZ 作者更新的 VRP 延长至 2021 年后，扩展样本显著性消失、后半样本系数转负，样本外 \(R^2\) 小且不显著，是“VRP 可稳定择时”的强反证。

### 2. IV rank 与期限结构

- Options Industry Council（年份不确定），*The Crush Is Real*，行业教育材料，【已核实】[原文](https://www.optionseducation.org/news/the-crush-is-real)：IV Rank 是一年高低区间位置，IV Percentile 是经验分布位置；两者均不预测方向。未找到同行评审证据支持“IVR>50”等固定阈值。
- Johnson（2017），*Risk Premia and the VIX Term Structure*，*JFQA*，【已核实】[论文](https://doi.org/10.1017/S0022109017000825)：VIX 曲线形状主要反映方差风险价格，而非未来 VIX 变化的无偏预期。
- Fassas、Hourvouliades（2019），*VIX Futures as a Market Timing Indicator*，*JRFM*，【已核实】[论文](https://doi.org/10.3390/jrfm12030113)：倒挂后股票收益往往为正，支持“压力后的反弹”而非继续 risk-off。Simon、Campasano（2014）也发现 VIX 基差不能显著预测现货 VIX 变化，【已核实】[论文](https://doi.org/10.3905/jod.2014.21.3.054)。

### 3. Crypto 与个股

- Hoang、Baur（2020），*Forecasting Bitcoin Volatility*，*Journal of Futures Markets*，【已核实】[论文](https://doi.org/10.1002/fut.22144)：BTC IV 在 1 日预测弱于 ARMA/HAR，在 7–15 日更强；IV 与历史模型组合在各期限最好。
- Alexander、Imeraj（2021），*The Bitcoin VIX and Its Variance Risk Premium*，*Journal of Alternative Investments*，【已核实】[论文](https://sussex.figshare.com/articles/journal_contribution/The_Bitcoin_VIX_its_variance_risk_premium/23306975)：以 Deribit 期权构造 1 周至 3 个月指数并记录高度状态依赖的 BTC VRP；但样本仅一年。
- Alexander、Deng、Feng、Wan（2023），*Net Buying Pressure and the Information in Bitcoin Option Trades*，*Journal of Financial Markets*，【已核实】[论文](https://www.sciencedirect.com/science/article/pii/S1386418122000544)：BTC 上下跳跃都重要，IV 曲线较股票指数更对称；不能把 DVOL 简化为股票式“恐慌指数”。
- Taylor、Yadav、Zhang（2010），*The Information Content…Individual Stocks*，*Journal of Banking & Finance*，【已核实】[论文](https://www.sciencedirect.com/science/article/pii/S0378426609002489)：个股 IV 的优势取决于期限和期权活跃度；一天期预测并不稳定。Goyal、Saretto（2009）进一步发现有信息的是每只股票的 IV–RV 差，而非原始 IV 水平，【已核实】[论文](https://ideas.repec.org/a/eee/jfinec/v94y2009i2p310-326.html)。

## 二、对 VVV_Trade 的具体启示

1. **先建独立 IV 风险覆盖层，不改写五状态。**  
   文献支持预测波动幅度而非短线方向；目前 IV 仅展示。新增 `iv_var30`、`rv30_hat`、`vrp30_proxy=iv_var30-rv30_hat`、`iv_age_hours/source/n_obs`，其中 IV 与 RV 均换算为同一 30 日累计方差。输出连续 `risk_multiplier`，置于状态机之后、仓位模块之前，并全量审计；缺失或过期时不生效。BTC/ETH 用 DVOL，美股指数用 VIX/VXN，SOL 留空。**改动量级：中。**  
   **实验：**已有 1h bar 和日线 IV 可做。以未来 30 日小时收益平方和为标签，比较“现有特征/HAR”“+IV”“+VRP”三组滚动预测；30 日 purge/embargo，报告 QLIKE、RMSE及未来进入 high_vol_chop 的 Brier 分数。完成 P0 回测后再比较覆盖层对净收益、最大回撤、CVaR、换手的增量，禁止先设硬阈值。

2. **VIX 期限结构只作影子风险分数。**  
   新增 CBOE `VIX9D`、`VIX3M`，构造 `log(VIX9D/VIX3M)`、变化率和持续根数；仅映射美股永续。VXN 期货已停止挂牌，【已核实】[CFE 文件](https://cdn.cboe.com/resources/regulation/rule_filings/approved/2015/SR-CFE-2015-030.pdf)，缺少与 VIX 等强度的长期曲线证据，故 VXN/VIX 只能先作辅助字段。**改动量级：中；现有数据不能做，需新增 CBOE 日线源。**  
   **实验：**分别检验倒挂“开始、持续、解除”后的未来波动、回撤和反弹收益；若只降低当期尾损却明显错过反弹，不应成为清仓开关。

3. **个股 iv30 暂不生产化 rank。**  
   标准“一年 IV Rank/Percentile”至少要求完整约 252 个交易日；数月历史只能标记 `provisional` 并保存 `n_obs`。先做每只标的内部的 IV/RV 或 VRP 标准化，禁止直接横比 TSLA 与 AAPL 的原始 IV。**改动量级：小。**现有数据可做描述性和合并面板实验，但不足以支持单股票稳定阈值；生产校准应等满一年。

## 三、反方与陷阱

- `IV−过去RV` 不是严格 VRP；严格定义是风险中性预期方差减物理测度下的未来方差预测。未来 RV 只能作标签，不能进入特征。
- IV 高、VIX 倒挂都不等于下一根下跌；倒挂常是同步压力指标，甚至可能预示反弹。
- IV Rank 的 min–max 公式极易被单个极值扭曲；短历史 percentile 也有严重抽样误差和制度漂移。
- 美股 IV 只能在其实际发布时间后用于 24/7 永续；必须 as-of join，周末记录陈旧度，禁止回填到当日更早的 1h bar。年化口径须统一为累计方差。
- crypto 文献以 BTC 为主，ETH 较少、SOL 几乎无直接证据；早期自建指数也不等同于当前正式 DVOL。
- 期权卖方收益文献不能直接转化为本系统标的方向收益；tick 级期权做市不适用于当前能力。

## 四、优先级排序

1. **最高优先：完成 P0 回测，并用现有 DVOL/VIX/VXN/iv30 建因果影子层。**证据最强、无需先改状态语义，可直接回答 IV 是否改善未来波动与尾部风险。
2. **第二优先：补 VIX9D/VIX3M，而非先做个股 IV Rank。**期限结构数据长、计算轻，但只能先验证连续风险降杠杆；个股 rank 等满一年后再校准。