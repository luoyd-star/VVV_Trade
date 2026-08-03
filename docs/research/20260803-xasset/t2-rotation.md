# 主题2：板块/主题轮动检测

## 一、文献与方法地图（出处＋确定度＋一句话结论）

- 高｜学术共识（现象）：Moskowitz–Grinblatt 用1963–1995年20个美国行业发现显著行业动量，典型“过去6个月排序、持有6个月”约0.43%/月，并能解释部分个股动量；但效应随持有期衰减，不能直接外推到1h。【已核实】([原文](https://onlinelibrary.wiley.com/doi/10.1111/0022-1082.00146))

- 高｜有争议（稳健性）：Grundy–Martin 在市值加权且跳过最近一个月时，将行业动量降至0.16%/月、统计不显著；反之，Hou–Xue–Zhang 的长样本标准化复验仍发现1/6/12月行业动量，因此较准确的结论是“构造敏感”，不是“已消失”。【已核实】([Grundy–Martin](https://academic.oup.com/rfs/article-abstract/14/1/29/1587146)，[异常复验](https://academic.oup.com/rfs/article/33/5/2019/5236964))

- 高｜分类风险：1963–2018年的复验显示，粗行业分类产生动量，细分类可能转为短中期反转，且结果随时期变化；“AI主题”这种主观、重叠分类比传统行业更容易产生符号翻转。【已核实】([Li 2022](https://www.sciencedirect.com/science/article/pii/S1544612322001490))

- 高｜机制边界：行业组合动量主要来自自身收益自相关，而非行业间交叉相关；所以“排名延续”不等于“A主题领先B主题”。【已核实】([Pan–Liano–Huang](https://www.sciencedirect.com/science/article/pii/S0927539803000495))

- 高｜lead–lag证据：大公司可领先同一行业小公司，供应商与客户行业也可相互预测；该关系在关注度、机构持仓较低时更强，支持“信息沿经济联系扩散”，但不保证任意叙事主题存在固定顺序。【已核实】([Hou 2007](https://academic.oup.com/rfs/article-abstract/20/4/1113/1615954)，[Menzly–Ozbas 2010](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2010.01578.x))

- 高｜统计解释：Granger只是“加入A的过去能否改善B的预测”，不是结构因果；非同步交易会机械制造自相关和交叉相关，恰好对应本系统“加密连续交易、正股仅RTH”的风险。【已核实】([Granger](https://www.jstor.org/stable/1912791)，[Lo–MacKinlay](https://www.nber.org/papers/w2960))

- 中｜从业者方法：RRG以归一化RS-Ratio和RS-Momentum形成四象限；官方明确其存在滞后、路径不一定顺时针且不是预定义交易系统。本次检索未发现原版专有公式的高质量独立样本外收益验证，证据等级应定为“可视化工具”。【已核实】([官方方法说明](https://chartschool.stockcharts.com/table-of-contents/chart-analysis/chart-types/relative-rotation-graphs-rrg-charts))

- 高｜资金流边界：真正的板块轮动研究使用有符号主动净订单流；成交量仅表示交易活跃度，OI总多头恒等于总空头，故量与ΔOI不能称“净流入”。【已核实】([板块订单流研究](https://academic.oup.com/rfs/article/24/11/3688/1589538)，[CFTC定义](https://www.cftc.gov/MarketReports/CommitmentsofTraders/ExplanatoryNotes/index.htm))

- 中｜轮动—相关结构连接：相关网络研究确实观察到与板块轮动相符的拓扑环；但最大特征向量通常首先代表市场模式，而非资金流方向。尚无“轮动必然导致块间相关下降或PC1载荷按固定顺序迁移”的通用实证定律。【已核实】([Leibon等](https://pmc.ncbi.nlm.nih.gov/articles/PMC2634955/)，[Plerou等](https://arxiv.org/abs/cond-mat/0108023))

- 高｜失败案例：对商业周期轮动的穷举检验发现，传统规则在成本和周期误判后优势迅速消失；放开规则后，行业间预测力与随机机会无显著差别。【已核实】([Molchanov–Stangl 2024](https://onlinelibrary.wiley.com/doi/10.1002/ijfe.2882))

## 二、对 VVV_Trade 的具体启示（依据→差距→建议＋量级＋实验）

1. 相对强度与排名迁移层——小改；现有数据可做工程验证，不能证明长期规律。新增带 `valid_from/valid_to` 的主题成员表，防止事后改标签。对美股以QQQ为主基准、SPY作稳健性检验，计算：

\[
RS_i^{(L)}=\sum_{k=1}^{L}(r_{i,t-k}-\hat\beta_{i,t}r_{b,t-k})
\]

横轴用标准化RS，纵轴用其变化，形成透明的“RRG-like”图；同时记录横截面排名、Δrank和主题内正RS成员占比。基础设施→应用用两篮子RS中位数之差检测，而非假设价差必然均值回归。加密和美股先在各自袖套内排名，不直接混排原始收益。

实验预注册5/20个RTH交易日窗口，预测未来1/5日主题残差收益差；比较“RS”“RS＋排名速度/广度”“再加量/OI”“再加相关结构”四臂，以严格未来窗ΔCRPS为主、rank-IC及扣除手续费/滑点/资金费率的组合收益为辅，沿用walk-forward、purge、锁箱和trial ledger。

2. lead–lag与活跃度层——中改；现有量数据可测，OI若未留存只能前向积累。只在共同有效时钟上比较：美股之间用RTH，币股关系用RTH交集，周末和盘外另报；ET去季节化不能恢复陈旧价格中的信息。先剔除QQQ/SPY共同因子、时段效应和自身AR，再比较B的AR基准与“加入A的1–4个预注册滞后”的ARX/低维VAR。

13资产已有156条有向边，禁止逐图挑最小p值；应按“方向×lag×频率×规格”整体做多变量块bootstrap及Romano–Wolf校正，并要求严格样本外CRPS改善和跨折方向稳定。【已核实】([多重检验方法](https://doi.org/10.1198/016214504000000539)) 五状态只作协变量或分层报告，不分别训练五张网络。量、ΔOI仅作“参与度确认”；若已有主动买卖拆分，优先使用signed imbalance。

3. 相关结构层——中改；现有数据仅适合描述性预研。对市场残差收益计算Ledoit–Wolf收缩相关矩阵，跟踪主题内相关、主题间相关、\(\lambda_1/\mathrm{tr}(C)\)及对齐后的特征子空间角度；不要直接比较未做符号/Procrustes对齐的PC载荷。【已核实】([收缩估计](https://www.sciencedirect.com/science/article/pii/S0047259X03000964))

以RS轮动触发点做事件研究，并与同状态、同波动的非事件窗匹配后块bootstrap。纯相对收益迁移可能完全不改变相关矩阵；主题特质分化时块间残差相关可能下降，而宏观去杠杆时所有相关与PC1占比反而上升。因此相关特征只能作为状态雷达或置信度门控，不能定义轮动方向。

当前美股永续仅约七个月，且“AI应用”独立成员过少，无法稳健验证基础设施→应用；扩充并回补多个非重叠应用标的是大改。在此之前结果必须标为可行性证据。

## 三、反方与陷阱

- 文献使用几十年月频数据；当前美股样本连一个完整6×6月非重叠实验都不足，名义24/7 bar数又夸大了有效RTH样本。

- SOXL是每日3倍半导体ETF，其指数已包含MU、AMD、NVDA；与这些股票并列入篮子会重复计权，并把杠杆复利误认成主题强度，应只作替代代理或稳健性臂。【已核实】([Direxion官方说明](https://www.direxion.com/product/daily-semiconductor-bull-bear-3x-etfs))

- 4h bar不能自然贴合390分钟RTH；跨开盘、收盘及盘外的bar可能制造伪lead–lag。1h也应删除陈旧/零成交bar并检查不同锚点。

- RRG的“领先”可能只是熊市中少跌；必须同时显示绝对趋势。

- 高相关不表示价差平稳；配对RS的趋势延续与价差z-score均值回归是相反假设，不应混成一个分数。

- PC1载荷迁移也可能仅来自波动率、市场beta或SOXL杠杆变化；相关矩阵本身对称，不能给出领先方向。

- 主题成员、窗口、基准、状态和滞后都属于试验次数；事后修改“AI应用”定义是隐蔽的数据窥探。

## 四、优先级（本主题内最值得先做的 1–2 件事）

1. 先做“小改”的固定主题本体＋市场残差RS、排名迁移和广度雷达；排除SOXL重复暴露，输出研究状态而非交易信号。

2. 再做“中改”的RTH对齐lead–lag与收缩相关审计：预注册少量主题边，整族bootstrap校正，并启动OI积累。只有其对RS基线产生稳定的锁箱外ΔCRPS增益，才升级为状态机特征。