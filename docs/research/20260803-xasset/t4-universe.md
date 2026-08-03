# 主题4：品种池（universe）构建方法学

## 一、文献与方法地图（逐条：出处+确定度+一句话结论）

- **实务共识｜高**：MSCI 先定义广泛权益宇宙，再以3/12个月流动性、交易频率等筛成可投资宇宙；小型新股通常需交易满三个月，季度复审，现有成分可按约2/3门槛留存以减少换手。【已核实】（[MSCI GIMI 2026](https://www.msci.com/eqb/methodology/meth_docs/MSCI_GIMIMethodology_May2026.pdf)）

- **有争议｜高**：S&P Composite 1500要求IPO交易满12个月，但Total Market Index允许大型IPO快速纳入并豁免流动性条件；seasoning没有唯一“行业标准”，取决于稳定性与代表性目标。【已核实】（[S&P U.S. Indices 2026](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-sp-us-indices.pdf)）

- **学术共识｜高**：Shumway发现负面退市收益大量缺失且幅度很大；仅回填今天仍存在的标的会系统性美化历史。【已核实】（[Shumway 1997](https://www.tylergshumway.org/Shumway-DelistingBiasCRSP-1997.pdf)）

- **反方证据｜高**：2010年闪崩中成交量仍很高，但价差扩大、深度消失，证明ADV不能替代报价质量和可执行性。【已核实】（[SEC/CFTC联合报告](https://www.cftc.gov/sites/default/files/idc/groups/public/%40otherif/documents/ifdocs/staff-findings050610.pdf)）

- **实务共识｜高**：GICS以主营业务、收入和盈利分类，年度及重大事件复审，并忽略短期波动；2026年又专门就AI商业模式分类发起咨询，说明主题标签必须版本化。【已核实】（[GICS方法论](https://www.spglobal.com/spdji/en/documents/methodologies/methodology-gics.pdf)、[AI分类咨询](https://ir.msci.com/news-releases/news-release-details/sp-dow-jones-indices-and-msci-announce-consultation-potential)）

- **学术证据｜中高**：Hoberg–Phillips用年度10-K文本形成随时间变化的竞争网络；Marti等则指出相关聚类窗口太短会产生伪簇，太长又会抹平变化。【已核实】（[Hoberg–Phillips 2016](https://www.journals.uchicago.edu/doi/10.1086/688176)、[Marti等 2016](https://www.ijcai.org/Proceedings/16/Papers/367.pdf)）

- **从业先例｜高**：CME CF加密基准同时审查成交、价差、深度、数据可靠性并使用多个场馆，明确认为成交量不是唯一准入依据。【已核实】（[CME CF方法分析](https://www.cmegroup.com/education/articles-and-reports/analysis-of-cme-cf-bitcoin-reference-rate)）

- **场馆事实｜高**：币安确认EQUITY/Stock Perps为24/7、USDT结算的新品类；正式规则允许移除、公司行动后重上及剩余仓位结算。【已核实】（[产品说明](https://academy.binance.com/en/articles/how-to-trade-stock-perpetual-contracts-on-binance)、[Exchange Procedures](https://bin.bnbstatic.com/static/cms/cg08ou2ak0tn7mcplvfg/file/08457d5d2da5ed76cc52136079d93bf8ba404d697423819bcdb7b34dbd88d667.pdf)）

## 二、对 VVV_Trade 的具体启示（依据→差距→建议+量级+实验）

1. **建立时点化、分层宇宙**  
   依据是幸存者偏差及MSCI的“广泛宇宙→可投资宇宙”。当前13品种固定名单无法表示历史上被拒绝、关闭或更换规格的合约。建议建立不可覆盖的 `instrument_master`、`universe_snapshot` 和 `contract_vintage`：

   - 候选池：曾被场馆观察到的全部合约；
   - 隔离期：上市0—3日，只采集；
   - 观察池：第4—89日，只做数据质量与影子研究，不进入正式状态机、相关矩阵或下单；
   - 核心池：满90自然日、至少约60个底层RTH日，并通过容量和质量门槛；
   - 退役池：永久保留末端收益、结算和退市原因。

   禁止把正股历史拼成“可交易永续历史”，也禁止上市前补零。历史候选资料缺失的区间只能标注“条件于当前幸存者”，不能事后修复。**量级：中；现数据：只能部分验证。**实验比较固定13品种与今后真实point-in-time成员在相关边稳定性、轮动换手和CRPS上的差异。

2. **门槛采用容量约束、时段分层和迟滞**  
   预注册的试行门槛可设为：预期K线完整率≥99%；RTH中 `trade_count>0` 的小时≥95%；`MDV30/MDV90`均通过；计划单笔名义额不超过执行时段中位小时成交额的1%。当前仅4个加密、9个股票永续，P20/P30分位过于离散，只宜告警，不能单独决定去留。

   股票永续分别维护 `eligible_rth` 与 `eligible_overnight`；24小时总ADV会掩盖盘外塌陷。现有OHLCV只能计算缺失、陈旧价、价格冲击代理；真实报价质量需在5分钟采集轮次保存最佳bid/ask及数量。指标每日更新、月末生效、季度审查方法；保留门槛可取准入门槛约2/3，连续两次月审失败才退出，`status != TRADING`则立即停用。**量级：K线门槛小，BBO采集小到中；现数据：部分可验证。**实验预锁定30/90/180日seasoning、0.1%/0.5%/1%容量及10/20/50bp价差网格，比较覆盖率、池换手、相关矩阵条件数和bootstrap边稳定性，不只看收益。

3. **静态语义标签与动态相关簇双轨运行**  
   静态层采用版本化多标签：`asset_class、underlying_type、theme、weight、confidence、source、valid_from/to`。例如MSFT可同时有AI基础设施与应用暴露；SOXL应标为“杠杆ETF/半导体代理”，SPY、QQQ标为宽基，不能与单一公司等票计数。GICS本身不分类ETF，因此只能借鉴治理方式。

   动态层保存 `cluster_id@timeframe/lookback`，不得自动改写静态标签。对30/60/120日窗口使用ET时段处理后的共同样本，报告原始相关和去市场模式结果；以分块bootstrap、ARI/Jaccard及连续两个窗口确认漂移。**量级：中；现数据：1h/4h可探索，美股1d证据不足。**在锁箱内比较“手工标签、纯聚类、混合”三组的未来窗CRPS、簇稳定性和轮动误报率。

4. **把经济标的与币安合约分离**  
   单场馆宇宙只能解释为“t时点币安已上市可交易的EQUITY永续机会集”，不能外推为整个美股市场。应分别保存 `economic_underlying_id` 与 `venue_contract_vintage`，定时快照状态、交易时段、资金费、规格哈希及last/mark/index价格；公司行动后重上视为新vintage。主回测采用variable-N：退出后资金留现金；按t−1流动性补位的fixed-K仅作稳健性。

   币安Academy称2026-05-16起采用订单簿EWMA模式，但当前Clearing Procedures又描述数据供应商加分时段EWMA/固定模式，两份官方材料存在口径冲突。【已核实】（[Clearing Procedures](https://bin.bnbstatic.com/static/cms/cg08ou2ak0tn7mcplvfg/file/53197b612332da02c20b5b7d19b81ff53ee5f4938c6330c72a30a1ca4f91049f.pdf)）因此应补充独立来源的正股/ETF RTH 1h参考价，按实际归档数据识别制度断点。**量级：中；现数据：不能区分产业相关与场馆定价因子。**实验比较永续与底层的分时段相关、basis及制度变更前后残差相关。

## 三、反方与陷阱

分层结论是：point-in-time留痕、保留退役标的及统一共同样本属于学术共识；90日、1%容量等只是待验证的工程先验；核心/观察池、月审和迟滞属于成熟治理经验，不是alpha证据。

- seasoning会漏掉真正的新主题，因此观察池可发布影子告警，但不能绕过正式准入。
- 主题爆发本身会抬高成交量；若立即按成交量扩池，就是用结果变量选样，必须滞后并固定生效日。
- `pandas.corr()`的成对缺失会令每条边使用不同历史；应使用共同时间交集，绝不把上市前收益补零。
- SOXL、QQQ、SPY与成分股存在机械重叠；否则一次半导体上涨会被重复计为多票“资金迁移”。
- 相关、taker量或聚类只能证明共振，不能证明资金从“AI基建”流向“AI应用”。
- 本次检索未发现可核实的EQUITY永续实际下架案例，故只能确认规则风险，不能编造下架发生率。

## 四、优先级（本主题内最值得先做的 1-2 件事）

1. 先建append-only的合约生命周期、point-in-time成员快照及“观察—核心—退役”状态，立即停止用当前名单回填历史。  
2. 在自动主题聚类前，先采5分钟BBO、mark/index及独立底层1h参考价，排除报价和场馆机制制造的伪轮动。