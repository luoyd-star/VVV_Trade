# 主题7：永续合约衍生品指标与波动率的互动

## 一、文献地图

总体判断：**学术共识**是 funding/premium 衡量基差、杠杆需求与套利约束，清算具有顺周期放大作用，订单失衡能解释同窗价格冲击；**有争议**的是 funding 反转、OI 的领先方向及 tick 信号能否迁移到小时线；**从业者经验**倾向多指标联合，反对单因子硬阈值。

### Funding、premium/basis 与期限

- He、Manela、Ross、von Wachter（2022），[“Fundamentals of Perpetual Futures”](https://arxiv.org/abs/2212.06888)，工作论文。【已核实】Binance 1h 数据显示 perp-spot gap 受过去收益和共同情绪驱动，并在 funding 结算附近收敛；支持“拥挤/基差回归”，不等于现货方向反转。

- Schmeling、Schrimpf、Todorov（2026），[“Crypto Carry”](https://doi.org/10.1287/mnsc.2024.05069)，*Management Science*。【已核实】高 carry 主要来自小投资者追涨及套利资本受限，并预测未来一月隐波上升和空头清算；但研究主体是固定期限 BTC/ETH 期货，不能直接外推至 1h 永续。

- Chi、Hao、Hu、Ran（2023），[“An Empirical Investigation on Risk Factors in Cryptocurrency Futures”](https://onlinelibrary.wiley.com/doi/10.1002/fut.22425)，*Journal of Futures Markets*。【已核实】basis 是最强横截面期货收益因子；日频最强、周频减弱、月频不显著，说明信息衰减较快。

- Kim（2025），[“Perpetual Futures as Predictors of Bitcoin Volatility”](https://www.aifinconf.org/file/2025/6-3.pdf)，会议工作论文。【已核实】funding 均值、绝对值和波动可增量改善 BTC 实现波动率的样本外预测，周/月优于日；尚未同行评审，证据中等偏弱。

- **从业者研究**：Jung（2024），[“Can Funding Rate Predict Price Change?”](https://www.prestolabs.io/research/can-funding-rate-predict-price-change)，Presto Research。【已核实】BTC funding 与同期七日收益相关，但对下一七日收益的 \(R^2\) 接近零；横截面策略换手极高且未计成本。MarketTrace（2026），[“Extreme Funding Rates”](https://markettrace.ai/blog/funding-rate-extremes)。【已核实】789 日、六资产检验中，4h 效果近零；高 funding 后 72h 反转只见于 BTC、SOL，极端负 funding 在六币均未稳定预示逼空。属于单一市场阶段的非同行评审证据。

### OI、清算与杠杆周期

- Zhang、Ma、Liao（2023），[“Futures Trading Activity and the Jump Risk of Spot Market”](https://www.sciencedirect.com/science/article/pii/S0927538X23000161)，*Pacific-Basin Finance Journal*。【已核实】意外 OI 与现货跳跃风险双向均无 Granger 因果，反而是现货跳跃领先期货成交量；直接反驳“OI 上升必然领先跳水”。

- Soska 等（2021），[“Towards Understanding Cryptocurrency Derivatives: A Case Study of BitMEX”](https://cylab.cmu.edu/_files/documents/towards-understanding-cryptocurrency.pdf)，*The Web Conference*。【已核实】约九千七百万分钟观测显示高杠杆、小账户清算集中且与波动同现；作者明确无法确认清算是价格跳跃起因，较合理机制是“价格冲击触发强平，强平再放大”。

- Giagkiozis、Said（2024），[“Reconciling Open Interest with Traded Volume in Perpetual Swaps”](https://doi.org/10.5195/ledger.2024.325)，*Ledger*。【已核实】七家交易所中部分 OI 变化无法与成交量恒等式核对，强平消息还可能延迟或漏报，说明 OI 数据质量本身是模型风险。

### Taker/aggressor imbalance

- Cont、Kukanov、Stoikov（2014），[“The Price Impact of Order Book Events”](https://doi.org/10.1093/jjfinec/nbt003)，*Journal of Financial Econometrics*。【已核实】短区间盘口 OFI 与同窗价格变化近似线性；这是订单簿事件量，不是简单 taker 比，也没有证明下一根 bar 可预测。

- Makarov、Schoar（2020），[“Trading and Arbitrage in Cryptocurrency Markets”](https://doi.org/10.1016/j.jfineco.2019.07.001)，*Journal of Financial Economics*。【已核实】共同 signed volume 对同期 BTC 收益解释力很高，但 5min/1h 滞后项偏反转，日频效应减弱，提示聚合后的高解释度可能只是累计价格冲击。

- Kim、Hansen（2026），[“The Quarter-Hour Effect”](https://arxiv.org/abs/2607.09426)，工作论文。【已核实】六个 Binance 永续中，特定十五分钟边界后的首个十秒 imbalance 才预测 4–12h 收益，普通时点较弱；VVV_Trade 的 1h 汇总 taker 比无法复原该信号。

## 二、对 VVV_Trade 的具体启示

1. **建立衍生品因子影子层。** 文献支持其预测拥挤和尾部风险，但当前 deriv 数据只展示。新增 `funding_settled/pred_rank`、`abs_funding_rank`、`cum_funding_24/72h`、`hours_to_settlement`、`premium_rank/change`、价格调整后的 `Δlog(OI)_1/4/24h` 与 `OI/volume`。【改动：中；现有数据可做】应在 funding 原生结算频率计算后再因果 as-of join，不能将一个结算值复制八小时后参与排名。实验采用 expanding walk-forward，预测未来 1/4/24/72h 收益、RV及极端下行；比较现有特征基线与逐组增量的 OOS IC、QLIKE、尾部事件 AUPRC。

2. **构造而非预设“杠杆压力”交互项。** 候选 `leverage_build = 极端 funding/premium × OI上升`，`oi_flush = OI急降 × 当柱大波动/反向taker`；前者是风险预警，后者只能作去杠杆确认。【改动：中；现有数据可做】先作为可审计 `deriv_stress` 影子字段，不增加第六状态；检验交互项能否稳定提升未来 RV、最大回撤及 `high_vol_chop` 转入识别。只有跨折、跨 BTC/ETH/SOL 同号，才允许在规则树中缩短 `high_vol_chop` 确认一根。

3. **Taker 比仅做受限试验。** 将原始比率变换为有界 `taker_imb=(R-1)/(R+1)`，只测试 1h/4h，不进入 1d 硬规则。【改动：小；现有数据部分可做】用完成 bar 后的未来收益做 local projection，并控制同柱收益、成交量、RV和 funding；若显著性只存在于同柱，即判定为价格冲击解释而非 alpha。十秒级时钟效应超出当前数据能力，不扩展。

美股永续必须独立校准：仿 `atr_rank_ds` 对 premium、OI 变化和 taker 做 ET 小时×周末去季节化，历史不足仍返回 `None`；不得套用 crypto 阈值。

## 三、反方与陷阱

- funding 常由此前 premium 计算，预测 funding、premium 与价格趋势高度重叠；结算与快照时间错位会造成泄漏。
- OI 同时对应一多一空，不能单独判断拥挤方向；美元 OI 还含价格机械变化。应保留合约/币本位 OI，并检查 \(|\Delta OI|\leq volume\)。
- 清算量主要内生于价格冲击；1h 聚合已丢失先后顺序，不能宣称因果领先。新增 tick 清算数据不符合当前能力边界。
- funding 极端会连续多柱出现，必须合并为非重叠事件、按最大 horizon purge，并做 block bootstrap/FDR，避免伪造样本量。
- tokenized 美股永续的闭市 premium 可能反映陈旧现货锚和做市库存；crypto 文献迁移性目前没有严格证据。

## 四、优先级排序

1. **先做 funding/premium/OI 影子特征包并接入 P0 walk-forward。** 无需新数据源，且对“未来波动/尾部风险”的证据明显强于方向预测。

2. **再验证 `deriv_stress` 交互覆盖层。** 单独 funding 反转、单独 OI 方向和 1h taker 硬规则均应继续观察；清算数据源升级暂缓。