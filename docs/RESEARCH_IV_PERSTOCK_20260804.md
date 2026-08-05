# 个股 IV 化调研（2026-08-04）

背景：面板美股主 IV 线 29/31 品种映射 VXN（纳指100 隐波 ≈ QQQ 的 IV）、SPY/CRCL 映射 VIX；
个股 iv30 走 CBOE 免费延迟源自采（30 分钟限频），最深仅 3.75 天，分位不可用。
瓶颈不是当前值而是**历史回填**。用户已注册 IBKR，问能否全部个股化。

调研方式：5 方向并行研究 + 承重结论逐条对抗核实（web 一手来源，2026-08-04 抓取）。

## 结论：可以，两条可行路径

### 路径 A：IBKR（用户已有账号）

**能力（已核实）**：TWS API `reqHistoricalData(whatToShow='OPTION_IMPLIED_VOLATILITY')`
对**正股合约**（非期权）请求，返回 IB 自算 30 天 ATM IV 的日线 OHLC；支持 Stocks/ETFs/Indices。
日线单请求 duration 上限 68Y；31 标的回填远低于 60 请求/10 分钟的 pacing。
实时侧 `reqMktData` generic tick 106 → tick 24（同口径 30 天 IV）。
每标的实际可回看深度文档不承诺，须 `reqHeadTimestamp` 逐个探测。
Client Portal Web API **无 IV 历史**（history 端点仅 Last/Bid_Ask/Midpoint），不能替代 socket 通路。
Python 客户端用 `ib_async`（ib_insync 已停维；PyPI 的 ibapi 停在 9.81 勿用）。

**硬门槛（已核实）**：
- 未入金不行：须 IBKR PRO、净值 ≥$500、入金次一营业日生效；Lite 要转 PRO。
- 订阅：US Securities Snapshot & Futures Value Bundle $10/月（当月佣金 ≥$30 免）
  + OPRA L1 $1.50/月（佣金 ≥$20 免），合计 ≤$11.50/月。
- **待实测的免费口子**：现行文档写明延迟数据（type 3）可用于 reqHistoricalData，
  且延迟 generic tick 白名单含 106——账号获批后（哪怕未入金）值得先试
  延迟模式能否直接回填历史 IV；旧文档"历史必须订阅"的说法可能已过时（核实员判 unverifiable）。

**运维现实（已核实）**：须本机常驻 IB Gateway + IBC；每日 AutoRestart 免重登；
**每周日 ~01:00 ET 全量重认证，须手机 IB Key 点一次**（硬性人工环节）；
同一用户名全平台单会话——用户自己开 TWS/网页看盘会踢掉采集器
（标准解法开第二用户名，但订阅按用户名另计费）；Mac 不能睡眠。

### 路径 B：ORATS 单月回填（横评第一名）

$99/月（持 Tradier 账户五折 $49，条款须确认），现成 `iv30d` 字段，
**历史回 2007 年**，REST API 与现架构（launchd + requests）零摩擦。
20,000 请求/月 vs 实际需求 ~8,500。打法：订 1 个月拉完 31 标的全历史 → 退订，
增量回到 CBOE 免费源积累，用重叠期做口径校验。**零运维负担**。

其余候选均否：Polygon 无历史 IV；CBOE DataShop/IVolatility 太贵（后者 31 标的×5 年 ≈$2.3 万）；
dxFeed 企业向；Tradier 本身无历史 IV（价值=ORATS 折扣门票）；yfinance 无可靠历史。

## 覆盖边界（与选源无关，都要面对）

| 品种 | 期权史 | 处置 |
|---|---|---|
| ARM | ~2.9 年（2023-09 挂牌） | 可用，分位窗口自适应 |
| NBIS / SNDK / CRWV / CRCL | 21/17/16/14 个月 | 历史天然短且含 IPO 初期畸形高波（当前 iv30 均 ~100%），分位窗口按品种自适应 + 治理标注 |
| SOXL | 期权活跃 | IV≈3× 半导指数 IV：分位排名可用，绝对阈值不可与非杠杆品种共用 |
| SK海力士 | 美股 ADR 无期权；KRX 有个股期权但 IBKR 大概率不通 | 维持指数代理（唯一现实选择） |
| QQQ / SPY | VIX/VXN 即其指数版 | **指数留作长历史分位锚**，个股 iv30 只作横截面，两序列不混拼 |

## 三条口径纪律（升版前提）

1. IB 30 天 IV（两到期插值）/ ORATS iv30d（曲面平滑）/ CBOE iv30 三者算法不同，
   绝对值有系统偏差——**分位只能在单一口径内算**，混拼历史属违规。
2. 换源 = 新序列，按版本谓词架构走：per-symbol valid_from + 源标注入库。
3. 指数（VIX/VXN 17-37 年史）永久保留为锚；个股 IV 转正进规则层须在下一次 RULES_VERSION 升版时评审。

## 建议决策顺序

1. IBKR 账号获批后先做**免费实验**：Gateway + 延迟模式试回填 1 个标的的历史 IV
   （半小时定案：成 → IBKR 免费路线；败 → 看 2/3）。
2. 若用户本来就打算入金 IBKR 交易：入金 ≥$500 + 订 $11.50/月，走路径 A。
3. 若只为数据、不想常驻 Gateway：路径 B（ORATS 单月 $99/$49）工程契合度最高。

（调研执行：8 agent 工作流，5 研究 + 3 组对抗核实，一手来源为 IBKR 现行官方文档、
定价页、GitHub issue、OCC/交易所通告；个别未决点已在文中标注"待实测"。）
