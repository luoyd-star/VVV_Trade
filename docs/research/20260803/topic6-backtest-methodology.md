# 主题6：低频信号的回测方法学与阈值校准

## 一、文献地图

**时序验证与研究纪律**

- Tashman（2000），*Out-of-sample Tests of Forecasting Accuracy: An Analysis and Review*，*International Journal of Forecasting*【已核实】（[DOI](https://doi.org/10.1016/S0169-2070%2800%2900065-0)）：主张 rolling-origin、重复测试期和逐期重估，避免结论依赖单一切点。
- Cerqueira、Torgo、Mozetič（2020），*Evaluating Time Series Forecasting Models*，*Machine Learning*【已核实】（[DOI](https://doi.org/10.1007/s10994-020-05910-7)）：平稳序列可用 blocked CV；存在非平稳变化时，保持时间顺序的多期 OOS 更可靠。
- Arnott、Harvey、Markowitz（2019），*A Backtesting Protocol in the Era of Machine Learning*，*Journal of Financial Data Science*【已核实】（[DOI](https://doi.org/10.3905/jfds.2019.1.064)）：反复查看 OOS 后再修改模型，该区间就不再是真正 OOS。

**参数稳健性与多重检验**

- Wu等（2024），*On the Design of Searching Algorithm for Parameter Plateau in Quantitative Trading Strategies*，*Knowledge-Based Systems*【已核实】（[DOI](https://doi.org/10.1016/j.knosys.2024.111630)）：提出选择连续稳定区域而非训练集单点最优；但证据来自有限策略实验，平台不是显著性的替代品。
- White（2000），*A Reality Check for Data Snooping*，*Econometrica*【已核实】（[DOI](https://doi.org/10.1111/1468-0262.00152)）：用依赖保持 bootstrap 检验“搜索过的最佳模型是否优于基准”。
- Hansen（2005），*A Test for Superior Predictive Ability*，*JBES*【已核实】（[DOI](https://doi.org/10.1198/073500105000000063)）：SPA 对差或无关候选通常比 Reality Check 更有力，但仍是家族级渐近检验。
- Bailey、López de Prado（2014），*The Deflated Sharpe Ratio*，*Journal of Portfolio Management*【已核实】（[DOI](https://doi.org/10.3905/jpm.2014.40.5.094)）：DSR 校正挑优、短样本和非正态；只适用于以 Sharpe/PnL 为目标的检验。
- Bailey、Borwein、López de Prado、Zhu（2017），*The Probability of Backtest Overfitting*，*Journal of Computational Finance*【已核实】（[DOI](https://doi.org/10.21314/JCF.2016.322)）：CSCV/PBO 衡量样本内赢家跌至样本外候选中位数以下的概率，但不保持“过去训练、未来测试”的方向。

**状态评估与小样本**

- Hamilton（1989），*A New Approach to the Economic Analysis of Nonstationary Time Series and the Business Cycle*，*Econometrica*【已核实】（[DOI](https://doi.org/10.2307/1912559)）：确立转移概率、状态持久性及持续期分析框架。
- Maheu、McCurdy（2000），*Identifying Bull and Bear Markets in Stock Returns*，*JBES*【已核实】（[DOI](https://doi.org/10.2307/1392140)）：以状态条件均值、波动和持续期刻画牛熊状态。
- Pagan、Sossounov（2003），*A Simple Framework for Analysing Bull and Bear Markets*，*Journal of Applied Econometrics*【已核实】（[DOI](https://doi.org/10.1002/jae.664)）：反方证据是随机游走也能复现不少牛熊持续期和振幅特征，描述性分群不等于预测能力。
- Politis、Romano（1994），*The Stationary Bootstrap*，*JASA*【已核实】（[DOI](https://doi.org/10.1080/01621459.1994.10476870)）：连续随机块可为弱依赖平稳序列构造区间。
- Gneiting、Raftery（2007），*Strictly Proper Scoring Rules, Prediction, and Estimation*，*JASA*【已核实】（[DOI](https://doi.org/10.1198/016214506000001437)）：概率预测应以 proper score 评价，避免评分规则诱导虚假“好预测”。

结论分层：时间有序 OOS、完整记录搜索全集、依赖保持重采样属于学术共识；anchored 与 rolling 谁更优、平台几何能否预示泛化、五状态需要多少样本均有争议；热力图、one-SE 平台中心及具体样本门槛主要是从业者经验。

## 二、对 VVV_Trade 的具体启示

1. **把验收改成真正的未来风险预测。**  
   文献依据：上述 proper-score 与 regime 研究。现设想方向正确，但若仅展示状态当期 ATR/RV 差异，会因状态本来就由这些特征定义而自证。建议状态评估模块以 \(t\) 时状态预测严格位于未来的 \(t+1:t+H\)：主指标为状态条件经验分布相对无条件分布的 CRPS/分位损失；并报告未来 RV、下行方差、最大不利移动、bars 数、独立 episode 数、持续期、生存曲线和带区间的 \(5\times5\) 转移矩阵。非连续同状态日期不得拼成虚构净值；用固定窗口最大不利移动或连续 episode 回撤。方向对照只做次级指标：trend_up/down 映射为 \(+1/-1\)，其余为 0，计费用及已结算 funding。**改动量级：中。实验：现有 OHLCV、funding 足够。**

2. **建立嵌套、同日历 walk-forward。**  
   现有因果计算解决了未来函数，却未解决参数选择偏差。建议末尾共同日历的 20% 设一次性锁箱；前80%中，以最早50%为首个训练区，后30%分成4–6个连续外层测试块，每折只能用此前数据选参。当前规则拟长期冻结且1d稀缺，anchored/expanding 作为主协议；固定长度 rolling 仅作预注册漂移压力测试，不能看谁好再选谁。1h/4h/1d共用切点，高周期线收盘后才能执行；前向结果重叠处 purge \(H\)，250根历史窗口和向后确认本身不需 purge。每折用冻结参数重放暖机历史以恢复迟滞状态，禁止机械重置为 range。**改动量级：大。实验：1h/4h可完整实施；1d见样本门槛。**

3. **九维寻优选平台中心，并把全部搜索计入校正。**  
   “6阈值+3确认数”是九个轴，不是九次检验；若每轴三档，全因子即 \(3^9=19{,}683\) 个候选。参数搜索器应预先冻结网格、逻辑约束和主指标，保存每个候选逐时点损失；选择“跨折优于基准且位于最大连通合格区”的 medoid，并报告邻域10%分位、最差折及二维边际热力图。SPA作为主家族检验、White RC作保守对照；DSR仅用于方向 PnL；PBO仅作1h/4h辅助诊断，不能替代顺序 OOS。所有人工迭代、窗口、成本和预测期限也计入 trial ledger。**改动量级：中。实验：现有数据可做，SQLite按候选分批存储即可。**

4. **按精度而非“30根规则”控制小样本。**  
   时间块应跨品种同步重采样，不能在各状态内 IID 抽行；同时报告逐折离散度。没有通用最小样本：一次100日高波动期仍只是一段 episode。工程门槛可规定 \(n_{\rm eff}<30\) 或 episode<10 时只描述；约 \(n_{\rm eff}\ge100\) 且 episode≥20 才允许中等精度比较——这是从业者门槛，不是定理。单个转移概率即便 IID，95%区间要约±10个百分点，最坏情形也需约96次从该状态出发的转移，依赖下更多。**改动量级：中。实验：现有数据能估区间，但多数1d单品种预计只能探索。**

5. **增加预分析清单。**  
   审计库已有规则版本，但还需不可变的 `experiment_id`：数据截止日、代码哈希、资产池、网格、搜索次数、切点、\(H\)、成本、基准、随机种子、tie-break和通过标准。最终锁箱只开一次，失败后修改必须标为新探索。Brodeur等（2024），*Do Preregistration and Preanalysis Plans Reduce p-Hacking and Publication Bias?*，*JPE Microeconomics*【已核实】（[DOI](https://doi.org/10.1086/730455)）发现空泛“已登记”无明显作用，详细分析计划才与偏差下降相关。**改动量级：小。实验：直接扩展现有SQLite审计表。**

样本分配上，1h/4h可按“资产类×周期”校准公共参数，避免逐品种寻优；1d不应独立做完整九维搜索，只检验原先验及从开发阶段预定的少数候选。所有周期仍使用同一末端20%锁箱，不能让1h测试日期出现在4h/1d训练结果选择中。

## 三、反方与陷阱

- 参数平台可能只是平坦噪声；二维热力图还会隐藏九维交互，故不能替代 SPA、区间和锁箱。
- PBO不检验前视错误或结构突变；DSR不能证明状态划分有效；SPA未拒绝也可能只是日线功效不足。
- stationary bootstrap 假设平稳或局部平稳，无法生成历史中没出现过的危机；最大回撤尾部尤其不应给出虚假精确区间。
- Bajgrowicz、Scaillet（2012），*Technical Trading Revisited: False Discoveries, Persistence Tests, and Transaction Costs*，*Journal of Financial Economics*【已核实】（[DOI](https://doi.org/10.1016/j.jfineco.2012.06.001)）发现历史赢家无法事前选出，低交易成本即可抹去样本内表现。因此廉价方向实验必须含费用、funding与多重检验，不能升级为主验收。
- 随机K-fold、逐行IID bootstrap、看完结果再选择 anchored/rolling、重复开启锁箱，都会制度性破坏结论。

## 四、优先级排序

1. **先完成大改动的嵌套 walk-forward + 一次性锁箱 + 逐时点风险评分与 trial ledger。** 这是所有阈值校准和多重检验的共同底座，也是当前P0真正缺口。
2. **随后实现参数邻域平台、SPA及时间块区间。** 1h/4h先形成可校准证据；1d若未通过 episode/精度门槛，保持先验并继续积累数据，不强行给出“最优阈值”。