# 主题6：跨资产 regime 与 risk-on/off 层

## 一、文献与方法地图（逐条：出处+确定度+一句话结论）

- 【已核实｜高】IMF 以日收益比较 2017–19 与 2020–21：BTC–S&P 相关由 0.01 升至 0.36，BTC/ETH 与 S&P、Nasdaq、Russell 的波动相关约增至原来的 4–8 倍；收益、波动溢出在 2020 年后分别增加约 8–10、12–16 个百分点，且压力期最强。[IMF 2022](https://www.imf.org/en/publications/global-financial-stability-notes/issues/2022/01/10/cryptic-connections-511776)

- 【已核实｜高】IMF 的加密共同因子研究发现，2020 年后其与全球科技、小盘股联系最强；机构参与和共同风险厌恶可解释相当部分相关性，美国货币收紧同时压低股票和加密因子。[IMF 2023](https://www.imf.org/-/media/files/publications/wp/2023/english/wpiea2023163-print-pdf.pdf) 反方是纽约联储的日内事件研究：BTC 对 FOMC、CPI 等即时意外大多不显著，说明“慢变量流动性通道”不能等同于“公告即刻反应”。[纽约联储](https://www.newyorkfed.org/research/staff_reports/sr1052.html)

- 【已核实｜高】Longin–Solnik 发现相关上升主要属于负尾/熊市，而非高波本身；Forbes–Rigobon 又证明异方差会机械推高危机期普通相关，调整后三次经典危机的新增“传染”几乎消失。[Longin–Solnik](https://doi.org/10.1111/0022-1082.00340)、[Forbes–Rigobon](https://www.nber.org/papers/w7267)

- 【已核实｜高】完整 RORO 先例不是单一相关指标：KC Fed 指数取信用利差、股票收益及隐波、融资条件、美元与黄金日变化的第一主成分，且优于单独 VIX。[KC Fed 2024](https://www.kansascityfed.org/documents/10594/rwp24-12charistedmanlundblad.pdf) VVV 缺少信用、融资和安全资产，故只能称“本宇宙共振/压力层”，不能声称测到全球风险偏好。

- 【已核实｜高】三类一致性指标：`AAC=mean|ρij|`；Choueifaty–Coignard 的 `DR=Σwiσi/√(w'Σw)`，越低表示分散化压缩；Kritzman 的 `AR_k=Σ前k个特征值/Σ全部特征值`，越高表示风险集中于少数共同因子。[DR 原文](https://doi.org/10.3905/JPM.2008.35.1.40)、[AR 原文](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1633027) 但 DR 原本是组合构建量，不是危机预警器；AR 表示脆弱度，不保证随后下跌。

- 【已核实｜高】平均相关样本内能描述危机、样本外预测未来损失却很弱，特征值指标相对好但仍不稳定。[Pastorino–Uberti 2024](https://link.springer.com/article/10.1007/s11135-023-01746-0) Giglio–Kelly–Pruitt 的递归样本外检验中，AR/ΔAR 对多组美国宏观下行尾部预测的 \(R^2\) 为负，是直接失败案例。[JFE 2016](https://stefanogiglio.org/papers/giglio-kelly-pruitt-jfe-2016.pdf)

- 【已核实｜中高】Ang–Bekaert 提供“高相关、高波、低均值熊市状态→调整配置”的先例，但纯股票组合收益有限，允许现金后价值才明显。[原文](https://business.columbia.edu/faculty/research/international-asset-allocation-regime-shifts) 更广泛的 103 个策略实时检验则发现波动门控没有系统优势，主因是结构不稳定。[Cederburg 等](https://doi.org/10.1016/j.jfineco.2020.04.015)

分层结论：学术共识是相关与波动时变、熊市共振且 2020 后币股整合增强；有争议的是因果来源、提前预警及可交易性；从业上更适合作为风险预算调制器，而非独立方向信号。

## 二、对 VVV_Trade 的具体启示（依据→差距→建议+量级+实验）

- **MVP 指标管线｜中量级｜现有数据可做测量，不能验证长期危机。** 核心矩阵用 4 加密＋6 单股；SPY、QQQ、SOXL 仅作基准和全 13 品种敏感性版本，避免指数成分重叠及 SOXL 杠杆机械抬高 AR。分别计算币内、股内、币股间的有符号平均相关 `C+`，另存 `AAC`；再算个体实现波动历史分位数的中位数 `V`、等风险篮子收益及负收益广度 `B`、固定 block 权重 DR、收缩相关矩阵的标准化 `AR₂`。窗口只预注册 60/240 个有效 1h bar。

- **交易时段处理｜小量级。** 跨币股状态仅用现有 ET 活跃时段掩码下的共同闭合 bar 更新，非 RTH 冻结；加密内部另保留 24/7 fast 状态。4h/1d 仅作稳健性检查。每条记录保存 `asof_ts、universe_hash、coverage、window、C+/AAC/V/B/DR/AR₂、threshold_version`。

- **操作化｜工程启发式。** `C+高、V高、B广泛向下` 才称压力/risk-off 候选；同样高相关高波但猛烈上涨且此前深跌，应标 `panic_rebound`；高相关低波且上涨为同步 risk-on/共同因子拥挤；低相关高波为轮动或特异冲击；低相关低波为平静分化。阈值只由当期训练窗历史分位数确定并锁定。

- **与五状态机组合｜小量级影子层，中量级验证。** 保留原单资产判定不变，仅新增 `shadow_regime` 与模拟的 `gate_multiplier`。第一阶段乘数恒为 1，只审计各五状态在不同跨资产层下的 CRPS、收益和尾部风险；第二阶段才锁定测试 `{1,0.75,0.5}` 的软缩放，不翻转方向、不改状态标签。

- **实验设计。** 四臂比较：原系统、同平均风险的无条件缩仓、仅波动缩放、`C×V×B` 软 gate。全部滞后一根 bar，沿用锁箱、trial ledger 和严格未来窗；评价增量 CRPS、未来 24/72h 波动与最大回撤、净收益/ES/换手，并按连续压力 episode 做块 bootstrap。只有联合 gate 在样本外稳定优于 vol-only，才考虑启用。

## 三、反方与陷阱

1. `|ρ|` 会把 −1 的完美对冲也记作最高一致性；共同下跌判断必须使用有符号相关和广度。
2. AAC、DR、AR 并非三票独立确认；在等权、等波、等相关特例中几乎互为变换。
3. 加密高波会支配协方差 AR；QQQ/SOXL 与成分股重复、盘外近零收益及缺失填零都会制造伪状态。
4. 相关是对称、无方向的，不能证明“AI 基建资金迁往 AI 应用”。主题轮动还需在剔除第一共同因子后观察主题篮子相对收益、广度及 VWAP 量流。
5. 美股永续仅自 2026 年开始，滚动小时数不能伪装成多个独立危机；加密长日线也不能替代混合宇宙历史。因此当前只能完成工程验收和前瞻影子检验，不能宣称 gate 已获危机样本验证。

## 四、优先级（本主题内最值得先做的 1-2 件事）

1. 先上线只读影子层：去重核心宇宙、共同 RTH、`C+/AAC/V/B/DR/AR₂` 与完整审计。
2. 立即启动预注册的前瞻四臂实验；在出现多个独立压力 episode 且联合层样本外胜过 vol-only 前，不启用生产门控。