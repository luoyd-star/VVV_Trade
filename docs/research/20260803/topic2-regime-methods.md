# 主题2：波动率/市场状态识别——统计法与规则法

## 一、文献地图

### Markov switching / HMM

- Hamilton（1989），[《A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle》](https://doi.org/10.2307/1912559)，*Econometrica*【已核实】；Ang、Timmermann（2012），[《Regime Changes and Financial Markets》](https://doi.org/10.1146/annurev-financial-110311-101808)，*Annual Review of Financial Economics*【已核实】【学术共识】：有限隐状态能描述突变、持续、异方差、厚尾和相关性变化；Hamilton 的实证是季度 GNP，并非 1h 交易或实盘验证。
- Rydén、Teräsvirta、Åsbrink（1998），[《Stylized Facts of Daily Return Series and the Hidden Markov Model》](https://onlinelibrary.wiley.com/doi/abs/10.1002/%28SICI%291099-1255%28199805/06%2913%3A3%3C217%3A%3AAID-JAE476%3E3.0.CO%3B2-V)，*Journal of Applied Econometrics*【已核实】发现不同子样本参数可大幅漂移；Dacco、Satchell（1999），[《Why Do Regime-Switching Models Forecast So Badly?》](https://onlinelibrary.wiley.com/doi/abs/10.1002/%28SICI%291099-131X%28199901%2918%3A1%3C1%3A%3AAID-FOR685%3E3.0.CO%3B2-B)，*Journal of Forecasting*【已核实】证明很小的状态误判即可抹去预测优势；Psaradakis、Spagnolo（2003），[《On the Determination of the Number of Regimes…》](https://doi.org/10.1111/1467-9892.00305)，*Journal of Time Series Analysis*【已核实】【有争议/反证】：只有状态差异足够大且持久时，状态数选择才可靠。

### 无监督聚类

- Horvath、Issa、Muguruza（2024），[《Clustering Market Regimes Using the Wasserstein Distance》](https://doi.org/10.21314/JCF.2024.005)，*Journal of Computational Finance*【已核实】【有争议】：Wasserstein k-means 在合成和历史样本上优于传统聚类，但未证明跨时期 walk-forward 稳定。Hendricks、Gebbie、Wilcox（2016），[《Detecting Intraday Financial Market States Using Temporal Clustering》](https://www.oxford-man.ox.ac.uk/wp-content/uploads/2020/04/Detecting-Intraday-Financial-Market-States-Using-Temporal-Clustering.pdf)，*Quantitative Finance*【已核实】【反证】在其设定下认为 60 分钟状态不能可靠识别。
- Botte、Bao（2021），[《A Machine Learning Approach to Regime Modeling》](https://www.twosigma.com/articles/a-machine-learning-approach-to-regime-modeling/)，Two Sigma 研究札记【已核实】【从业者经验】：以 17 个因子拟合四状态 GMM；作者明确说明各期按 IID 独立分类且模型不具预测性。因此“大行式聚类”更多是情景描述，不是稳定实时信号。

### 变点与跳跃

- Andreou、Ghysels（2006），[《Monitoring Disruptions in Financial Markets》](https://doi.org/10.1016/j.jeconom.2005.07.023)，*Journal of Econometrics*【已核实】【学术共识】：顺序 CUSUM 可实时监控条件方差，但功效受采样频率、波动持续性和厚尾影响。Bai、Perron（2003），[《Computation and Analysis of Multiple Structural Change Models》](https://doi.org/10.1002/jae.659)，*Journal of Applied Econometrics*【已核实】是全局回看分段，适合复盘而非 live 切换。Adams、MacKay（2007），[《Bayesian Online Changepoint Detection》](https://arxiv.org/abs/0710.3742)，技术报告【已核实】以因果 run-length 后验检测断点，并演示 DJIA 方差变化；延迟取决于变幅、hazard 和报警阈值。
- Barndorff-Nielsen、Shephard（2004/2006）的 [bipower variation](https://doi.org/10.1093/jjfinec/nbh001)【已核实】及[正式跳跃检验](https://doi.org/10.1093/jjfinec/nbi022)【已核实】，*Journal of Financial Econometrics*：RV−BPV 可分离连续与跳跃变差，但有限样本结果到约 72 个日内观测才较可靠；加密 1h 每日仅 24 个，美股现金时段更少。Lee、Mykland（2008），[《Jumps in Financial Markets》](https://doi.org/10.1093/rfs/hhm056)，*Review of Financial Studies*【已核实】【有条件可用】明确建议小时数据取局部窗 \(K=78\)，但只能标记发生极端收益的小时，不能证明小时内价格路径不连续。

### 规则、迟滞与正面对比

- Kole、van Dijk（2017），[《How to Identify and Forecast Bull and Bear Markets?》](https://doi.org/10.1002/jae.2511)，*Journal of Applied Econometrics*【已核实】【有争议】：规则法更适合事后定年，Markov switching 在其 S&P 500 样本外预测中更好。Kirby（2023），[《A Closer Look at the Regime-Switching Evidence…》](https://doi.org/10.1016/j.frl.2022.103369)，*Finance Research Letters*【已核实】【反证】指出所谓收益状态可能只是模型在拟合负偏度，并无真实收益可预测性。两者均非 VVV_Trade 五状态的直接比较；本次未找到完全对应的同行评审对照。
- Fama、Blume（1966），[《Filter Rules and Stock-Market Trading》](https://doi.org/10.1086/294849)，*Journal of Business*【已核实】的运行极值过滤器具有记忆，但交易成本消除了多数优势。Lunde、Timmermann（2004），[《Duration Dependence in Stock Prices》](https://doi.org/10.1198/073500104000000136)，*JBES*【已核实】允许牛熊进入、退出阈值不对称，是金融版双阈值迟滞对应物。Adnan、Izadi、Chen（2011），[《On Expected Detection Delays for Alarm Systems with Deadbands and Delay-Timers》](https://doi.org/10.1016/j.jprocont.2011.06.019)，*Journal of Process Control*【已核实】【相邻学科证据】量化了 N 次确认：其示例中 \(N=1\to5\) 令假警率由 25.26% 降至 1.01%，检测延迟由 0.21 升至 7.66 个样本；方向可迁移，数值不能照搬金融市场。

## 二、对 VVV_Trade 的具体启示

1. **概率状态影子层——改动量级：中。** HMM 能提供概率，但误判、状态数和漂移风险显著 → 当前规则树只有硬标签 → 按“品种×周期”增加 2/3 状态粗粒度波动 HMM，GMM/k-means 仅作基线；输入先限于收益、RV30、波动加速度、下行方差及可用的 `atr_rank_ds`。只入库 filtered probability、熵、预期风险、`fit_end_ts`、标签映射和模型版本，严禁全样本 Viterbi/smoothing；标签按发射波动率排序，不强行对应现有五状态。  
   **实验：现有数据可做。** 嵌套 walk-forward 比较 \(K=2,3\)，BIC仅作参考；主指标为测试期预测对数似然/QLIKE、重估重叠区 ARI、状态占比、驻留期、转场反转率及参数漂移。P0 回测完成后再比较净 Sharpe、回撤、换手；不能稳定胜过规则树和简单 RV 分位基线即弃用。

2. **波动断点影子层——改动量级：中。** 现有 ATR/RV 均有平滑延迟 → 增加双向 CUSUM 与 Student-t/稳健 BOCPD，输入加密的 `log(TR%)`、美股永续的去时段化尺度；`atr_rank_ds=None` 时宁缺毋滥。入库 `cusum_score`、`break_prob`、`run_length`，初期不参与主判定。  
   **实验：现有数据可做。** 在历史平稳块注入已知方差倍增，按相同年度假警 ARL 比较中位数/95%检测延迟；真实数据仅以 Bai–Perron 作事后参照，禁止回填交易信号。再检验断点后 6/12/24 根 RV 是否显著抬升，以及是否改善高波状态召回。

3. **校准现有迟滞——改动量级：小。** 金融文献支持迟滞结构，却不支持固定 1/2/3 根 → 为各目标状态增加显式进入/退出阈值，并将 `N_enter/N_exit∈{1,2,3,4}` 纳入 P0 回测参数。  
   **实验：完全使用现有数据，但依赖 P0。** 嵌套 walk-forward、预先限定网格；同时报告“进入后 M 根内反转率”、相对离线断点的延迟、状态换手、手续费/funding 后收益。优化目标应惩罚参数复杂度，避免为每品种、周期、状态各自挖最优值。

## 三、反方与陷阱

- HMM/GMM 的“状态”无客观真标签；有效样本是少量独立转场，不是几十万根 bar。状态数、特征、频率或重估窗口一变，标签可能整体重排。
- Bai–Perron、ICSS、平滑 HMM 标签都是事后工具；拿其回填历史状态会制造前视收益。BOCPD 又可能把单次 jump 当永久断点，须用厚尾似然和持续性复核。
- 1h 下不应上线正式 BNS 显著性检验。Lee–Mykland 最多作为 `jump_candidate`；OHLCV 无法识别小时内“跳后回撤”，也无法严格区分跳跃与快速连续运动，故本轮不列优先升级。
- Fama–Blume 不支持“过滤必有 alpha”；确认越多、迟滞越宽必然牺牲响应速度。美股永续若不先处理 ET 小时/周末季节性，统计模型大概率只是识别开盘时段。

## 四、优先级排序

1. **先完成 P0 回测并校准双阈值与 1/2/3 根确认。** 成本最低，直接解决现有最大未知量，也是评价任何统计影子层的前提。
2. **再做 CUSUM/稳健 BOCPD 与 2/3 状态 HMM 的影子赛马。** 前者更轻、更可审计且直接补足断点延迟；HMM/GMM 保持 challenger，只有跨品种、周期和多个 walk-forward 折均稳定增益，才讨论进入判定链。