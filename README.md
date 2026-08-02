# VVV_Trade · 市场状态系统（v1）

用**价格结构 × 成交量 × 波动率**三组特征，把市场分类为五种状态。规则完全显式、可解释，
是为日后回测校准和策略路由准备的地基，不是预测器。

## 五种状态

| 状态 | 含义 | 判定核心 |
|---|---|---|
| `trend_up` 趋势上行 | 结构抬升 + 走得有效率 | ER 分位 ≥ 0.60 且方向分 ≥ +0.30 |
| `trend_down` 趋势下行 | 结构压低 + 走得有效率 | ER 分位 ≥ 0.60 且方向分 ≤ −0.30 |
| `squeeze` 低波动挤压 | 波动率压缩到历史低位，蓄势待发（方向未知） | BBW 分位 < 0.15 且 ATR 分位 < 0.30 |
| `high_vol_chop` 高波动非趋势 | 波动率极高且未过趋势判定（**未证明无序**——可能是高波新趋势的启动段） | ATR 分位 > 0.85 且不成趋势 |
| `range` 震荡 | 以上都不是 | 兜底 |

判定优先级：趋势 > 挤压 > 高波动非趋势 > 震荡。`confidence` 表示子信号的一致程度（0–1）。

**非对称迟滞（2026-07-31 起）**：`regime_history.raw_state` 存逐根原始判定，`state` 存确认态——
一般状态连续 2 根确认进入、恢复震荡需 3 根、高波冲击立即进入；未确认的切换以"候选状态
(n/need)"暴露给面板与 VVVhermes。实测 BTC 1h 翻转从 26 次降到 12 次。

**审计化快照**：每行随存 `features`（键集随 `AUDIT_VERSION` 演进，当前 a6 为 23 项）、`rules`（触发/未满足规则清单）与
`version`（RULES_VERSION，改阈值必须递增）——回测无需重算特征，也不会混用规则版本。

**数据健康**：每周期标记 `warmup`（历史 <280 根，分位参照期不足）与 `stale`
（**距真实收线**超过 1.5×周期——`ohlcv.ts` 是 K 线开盘时刻，龄要加一个周期才是距收线；
2026-08-02 修正前从开盘起算、恒定多报一整个周期，阈值同步由 2.5× 收紧到 1.5× 保持判定边界不变），贯通面板横幅、状态卡与 VVVhermes 上下文（要求其回答时声明）。

**滚动预览**：collector 每轮另存形成中的 K 线（live_bars），面板以"预览(未收线)"虚线标注
试算状态——只作预警，永不写入确认历史。

## 三大支柱 → 特征

| 支柱 | 模块 | 特征 |
|---|---|---|
| 价格结构 | `regime/features/structure.py` | 摆动高低点序列（HH/HL vs LH/LL，左右各 4 根确认、无未来函数）、EMA50 坡度（ATR 归一化）、100 根 Donchian 位置、EMA200 上下方；加权合成方向分 `direction ∈ [-1,1]`。另有 Kaufman 效率比（ER）衡量趋势效率，替代 ADX。 |
| 成交量 | `regime/features/volume.py` | 20 根量能 z 分数、量分位、近 20 根多空量差 `tilt ∈ [-1,1]`、突破（60 根 Donchian）当根的量能分位确认。 |
| 波动率 | `regime/features/volatility.py` | **SMA-ATR14**（非 Wilder RMA，迁移阈值到其他平台须换算）及其相对价格的历史分位、布林带宽分位、30 根年化已实现波动率；**波动率加速度**（快RV12/慢RV72，>1 扩张中）与**下行方差占比**（近 48 根，>0.5 跌出来的波动）。核心思想：波动率绝对值无意义，**自身历史分位**才可比。 |
| 路径几何（影子） | `regime/features/pathgeom.py` | **不参与判定**：chop_freq（线性去趋势残差每 100 根的均值穿越次数，区分震荡的"慢摆动 vs 快噪声"）、dom_period（主导周期）、Kendall τ（秩趋势，与 direction 交叉验证）；另有 margin（到最近**可实际翻转输出**的决策边界距离，<0.15 亮"边界过渡中"，是候选状态的领先互补）与 lag_bars 滞后声明。源自 regime-spectrum 评审采纳批次一；进规则之日必须升 RULES_VERSION 并清空重算。测试：`tests/test_pathgeom.py`（含输出级因果断言）。 |
| 持仓与杠杆 | `regime/deriv.py` | Binance 永续 OI（Δ4h/Δ24h/分位）、资金费率（实测结算间隔年化+分位）、Premium、Taker 主动买卖比。与价格行为正交的信息轴；目前仅展示与注入 VVVhermes，不进状态机。**OI/taker 历史仅约 30 天保留期，靠持续自采积累。** |

多周期：默认同时看 1d / 4h / 1h，报告给出对照解读（如"日线趋势 + 4小时整理 = 中继情景"）。

## 品种注册表与美股永续（2026-07-31）

`instruments.json`（热读）声明每个品种的类别与数据源路由；**未登记的 symbol 走加密默认，存量零变化**。
第一批美股永续：NVDA / AAPL / TSLA / MU / SOXL / SPY / QQQ（币安合约区 EQUITY 标的，
`underlyingType=EQUITY` 共 131 个可选，对账脚本 `scripts/probe_stock_perps.py`）。

- K 线：`binance_futures`（fapi）主源；有 `hl_coin` 的用 Hyperliquid 兜底（builder dex 命名如 `xyz:NVDA`）
- 衍生品四件套（OI/Funding/Premium/Taker）：与加密永续同接口，`deriv.py` 零改动复用
- **标的休市标注**：正股仅 9:30-16:00 ET 交易而合约 24/7——休市期波动塌陷可能产生"假挤压"。
  面板顶栏显示 标的盘中/休市 徽章，VVVhermes 上下文自动带该状态与解读警示（不含假日历，仅工作日+时段判断）
- 上市较新标的（1d 不足 90 根，如 SOXL）该周期暂缺，数据攒够自动出现；90–280 根之间由 warmup 标志提示

### 美股波动率与 session 效应（2026-08-01，L0/L1/L2 批次）

美股永续无 DVOL 类免费个股 IV 历史，按"能拿到什么就先拿什么"分三层落地：

- **L0 去季节化 ATR 分位（影子字段 `atr_rank_ds`）**：实测美股永续 TR% 盘中/盘外 2.2-2.7 倍、
  周末塌陷 2-5 倍，`atr_rank` 在盘外主要在识别"现在是夜里"。`volatility.py::_deseasonalized_atr_rank`
  按 (**ET 小时**, 是否周末) 桶做因子归一（rolling 30 同桶均值 +shift(1)，walk-forward 无泄漏；
  必须按 ET——时段效应锚在交易所本地时间，DST 会让 ET 时段相对 UTC 平移一小时，
  实测 UTC 分桶在 DST 前后把开盘小时的因子错配近 3 倍），
  仅 us_stock_perp 的 1h/4h 计算，**不参与判定**，入审计快照 `atr_ds`。因子样本不足返回 None（宁缺毋滥）。
  测试 `tests/test_session_ds.py`：合成时段效应下 raw 分位差 0.72 被压到 0.01，真实全时段抬升信号保留。
- **L1 CBOE 指数波动率（`regime/usvol.py` + usvol 表）**：VIX/VXN/RVX/VIX9D/VIX3M。usvol 表
  **只存交易日 00:00 UTC 一种时间格**（迁移键 `usvol_ts_aligned_v1`），日线格里不会混进盘中点位。
  两级权威：**CSV 官方收盘价 > 延迟报价**——报价按 `last_trade_time`（CBOE 返回的 ET 挂牌时刻，
  非本机时钟）推出交易日，且**只写 CSV 尚未覆盖的交易日**；CSV 在"报价进入未覆盖交易日"时重拉确权
  （实测该 CSV 当日收盘后即更新），另有 24h 兜底刷新与 1h 重试下限。所以当前交易日的行是报价值
  （盘中临时、收盘后收敛），次日之前由 CSV 确权成官方收盘价，官方值绝不会被报价盖回去。
  品种经 `instruments.json` 的 `vol_index` 映射（SPY→VIX，其余→VXN），
  面板波动率卡显示 指数IV vs 本品种RV30 + 9D/3M 期限结构（>1 倒挂=近端恐慌）。
- **L2 个股 iv30 自采**：CBOE `delayed_quotes/quotes/{TICKER}.json` 的 `iv30` 字段，存 deriv 表 iv30 列。
  **无免费历史，自采积累**——攒够 ~20 个观测分位才有意义，面板标注自采天数。
- **合规节流**：CBOE delayed_quotes 属 ToS 灰区，整个 usvol 模块经 meta 表节流闸限频 30 分钟。
- session/weekday 的**转正路径**（尚未动规则）：影子期收集 ds vs raw 分歧样本，
  4-6 周后评估误报率，若转正则并入 v2 升版（与批次三"外生日历剔除"捆绑，涉改品种清空重算）。
  weekday（星期几）细分暂不做——每桶样本=周数，8-12 周后数据才够稳健估计。

## 数据

- 数据源优先级（可用 `--sources` 调整）：**Deribit（默认主源，实测更稳定）→ OKX → Binance**
  （`data-api.binance.vision`）。全部免费、无需 API key。
- Deribit 注意事项：
  - 给出的是**永续合约**价格（BTC/ETH 为币本位永续，SOL 等主流币为 USDC 永续），与现货有微小基差；
  - 日线按 Deribit 结算时间收线（**08:00 UTC**），与 OKX/Binance 的 0 点 UTC 不同——报告会打印实际收线时间；
  - 4h 由 1h 重采样得到（Deribit 无 240 分钟档），按 UTC 整点对齐；**首桶必须丢弃**——
    取数窗口起点不对齐 4h 边界时首桶只装 1~3 根，而 upsert 按 ts 覆盖，这根残值会在滑出
    窗口那一刻被定格并永久留库（2026-08-02 修复前每天啃掉 6 根，已用宽窗重采样修回 24 根）；
  - 品种覆盖窄（BTC/ETH + 部分主流币 USDC 永续），没有的品种自动落到 OKX/Binance（如 PEPE）。
- BTC/ETH 额外拉取 **Deribit DVOL**（30 天隐含波动率指数，加密版 VIX），报告展示
  IV 水平、近一年分位和 IV−RV 差（波动率风险溢价的粗略读数）。
- 每周期取约 300 根**已收盘** K 线（未收盘的最后一根被丢弃，避免状态盘中闪烁）。
- 网络不通时：`--demo` 用合成数据自检；如在需要代理的网络环境，`requests` 会自动读取
  `HTTPS_PROXY` 环境变量。

## 架构（采集与展示解耦）

```
collector.py ──写──> data/market.db (SQLite, WAL) <──读── dashboard.py ──> web/ 面板
     │                    ├ ohlcv           K 线（越攒越长，分位参照期随之变长）
     └ 每 5 分钟一轮       ├ dvol            Deribit 隐含波动率指数
                          └ regime_history  逐根 walk-forward 状态（无未来函数，与回测同口径）
main.py                   独立的 CLI 报告（不依赖数据库，直接拉接口）
```

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

采集器（每 5 分钟一轮）：

```bash
.venv/bin/python collector.py
```

可视化面板（工程模式，深色，zero 额外依赖；ECharts 走 CDN）：

```bash
.venv/bin/python dashboard.py    # → http://127.0.0.1:8787
```

CLI 快速报告：

```bash
.venv/bin/python main.py --symbols SOL-USDT --timeframes 1d,4h
```

开机常驻采集（macOS launchd，可选）：安装 `launchd/com.vvv.collector.plist` 到
`~/Library/LaunchAgents/` 并 `launchctl load`，launchd 会每 300 秒拉起一次 `collector.py --once`
（自愈：单轮崩溃不影响下一轮）。卸载用 `launchctl unload`。

## 面板内容（工程模式 · 浅色 · 四层信息架构）

按"**结论 → 证据 → 历史 → 原始数据**"自上而下分四层（2026-07-31 重构）：

1. **状态总览**：三张状态卡——确认态 + conf 主行（数据健康以 ⚠ 角标悬浮提示）、
   **动态行**（统一收纳未收线预览 / 酝酿中候选 / 原始判定）、六格核心特征、
   优先级前 4 的标记 chips（SQZ/HV/cRSI 区域/背离/突破）
2. **主图区**：价格结构三段式图（K 线 + 状态色带 + EMA50 + 摆动点 / 成交量 / cRSI 副图，
   共享十字线缩放）｜右列**波动率合并卡**（IV vs RV 整年 + 当前周期 ATR/BBW 分位两窗格）
   与**持仓与杠杆卡**（OI/funding 双窗格图 + 八格指标）
3. **状态历史**：walk-forward 确认态时间线色带 + 翻转日志并排
4. **明细与运维**：特征明细表（**按支柱分组表头**：标识/结构/波动率/量能/cRSI/路径几何·影子，26 列）+
   采集器监控（行数/耗时/日志尾部）
- 60 秒自动刷新；数据异常汇总进顶部横幅；状态五色与折线配对色均通过 dataviz
  六项校验（浅色底、全配对模式，含色盲区分度）

## cRSI 指标（Pine 本地化）

`regime/features/crsi.py` 由用户提供的 Pine v6 脚本（von Thienen 派生的 Cyclic Smoothed RSI）
本地化：相位超前 + 扭矩平滑的 RSI 主线、40 根周期记忆内的**自适应分位带**（10% leveling）、
带内位置 pos（0=下带 100=上带，可超界）、枢轴确认背离（滞后 piv_len 根确认，无未来函数）。
语义对齐说明见模块 docstring（v6 浮点除法、lmax/lmin 的 else-if 原版行为、RMA 的 SMA 种子）。
目前 cRSI 仅作展示与 Hermes 上下文，**不进状态机**——是否有增量留给回测回答。

## VVVhermes 面板助手

（原名 Hermes，为与用户本机同名工具区分而更名。）面板右侧内嵌对话栏，
**每次提问自动注入面板当前实时数据**（三周期状态与全部特征、cRSI、IV/RV、翻转历史、
采集器状态），因此它"读得到面板"。后端可插拔，配置在 `agent.json`
（参考 `agent.example.json`，已 gitignore）：

| provider | 说明 |
|---|---|
| `codex` | **当前启用**：调用本机官方 Codex CLI（ChatGPT.app 内置二进制），用其订阅登录，本项目不经手任何 token；`model` 留空则用 `~/.codex/config.toml` 默认。注意每问约消耗 1–2 万 token 订阅额度、延迟 1–3 分钟 |
| `anthropic` | 官方 SDK（默认模型 `claude-opus-5`；Opus 5/Fable 5 自动带服务端安全回退），key 走 `ANTHROPIC_API_KEY` |
| `openai` | 任意 OpenAI 兼容接口（OpenAI 官方 / OpenRouter 等），需按用量计费的 API key |
| `ollama` | 本地模型（如 `ollama pull hermes3`） |
| `mock` | 无模型自检：回显注入的上下文，验证链路 |

**对话共享与持久化**：历史存服务端 SQLite `chat` 表——**面板与终端是同一份对话**。
面板发消息只传新句子（`POST /api/agent/chat {message}`），历史由服务端拼装；
刷新自动恢复（`GET /api/agent/history`），且每 60 秒与库同步（终端里问的会出现在面板）；
"清空"按钮 / 终端 `/clear` 清的是同一份（`POST /api/agent/clear`）。
发给模型的历史取最近 20 条；侧栏开合状态仍记在 localStorage。

**系统提示词由用户掌控**：`hermes_system.md` 的内容原样作为 Hermes 的人设与规则，
**每次提问热读**（保存后下一次提问即生效，无需重启）；文件为空/缺失时回退内置默认。
面板字段说明（panel_legend）与 `<panel>` 实时数据由服务端自动附加在用户提示词之后，属于机制、不占用该文件。

**终端入口（代号 `VVVhermes`）**：`vvvhermes.py`——`.venv/bin/python vvvhermes.py "问题"`
单问单答，不带参数进 REPL（`/symbol ETH-USDT` 切品种，`/q` 退出），`-s` 指定品种。
与面板共用全部后端（配置/提示词/上下文注入均热读），不需要面板服务在运行。
建议加 alias 后直接敲 `VVVhermes` 召唤。

**配置也是热读的**：`agent.json` 每次提问重新读取——在终端写好后**刷新页面即可用，无需重启**。
唯一例外：用 `api_key_env`（环境变量放 key）时，变量必须在面板服务进程里可见，
需在设好变量的终端里重启 `dashboard.py`。Hermes 标题栏会显示当前 provider·model 及是否使用自定义提示词。

## 已知限制（v1 刻意为之）

1. **阈值未校准**：`regime/classify.py` 里的 THRESHOLDS 是合理先验，不是回测结论。
2. **无状态迟滞**：临界值附近状态可能在两次运行间翻转，之后应加持续性/迟滞机制。
3. **历史窗口约 300 根**：分位数的参照期有限（1h 约 12 天）。之后可分页拉长历史。
4. **IV 仅作展示**：DVOL 已进报告，但尚未作为状态机的输入特征——是否有增量、如何加权，留给回测回答。

## 路线图

- [ ] 回测框架：主目标统计"各状态下的风控背景条件分布"（条件波动率/最大回撤/状态持续时长/
      状态间转移概率），外加一个低成本的方向性对照实验；据此校准六个先验阈值
- [ ] 分页拉取更长历史；本地缓存（parquet）
- [x] 接入 Deribit 行情 + DVOL 展示（2026-07-31）
- [x] 5 分钟采集器 + SQLite 本地历史 + walk-forward 状态历史（2026-07-31）
- [x] 衍生品持仓采集：OI/Funding/Premium/Taker + 回填 + 面板卡（2026-07-31，crypto-monitor 启发 4+2 之一）
- [x] 快照审计化：特征值+规则命中+版本号随行入库（2026-07-31）
- [x] WARMUP/DATA_DEGRADED 数据健康标志贯通（2026-07-31）
- [x] 非对称迟滞（2/3 确认+冲击立即）+ 候选状态（2026-07-31）
- [x] 未收线滚动预览（明确标注，不入历史）（2026-07-31）
- [x] 波动率加速度 + 下行方差占比（2026-07-31）
- [x] regime-spectrum 采纳批次一：频率轴/主周期/Kendall τ/margin 边界距离/滞后声明（影子字段，2026-08-01；双路对抗校验通过，margin 经 3 万组暴力对拍零失配）
- [x] 美股波动率 L0/L1/L2：去季节化 ATR 分位影子字段 + CBOE 指数 IV（VIX/VXN 等5指数全历史）+ 个股 iv30 自采（2026-08-01；audit v4 重算 5160 行零回归）
- [x] 全量系统审计 → `SYSTEM_LOG_20260731.md`（能力/决策/实证盘点/25 条缺口/版本纪律；12 个 agent 并行审计 + 三路对抗核实）
- [x] 修 usvol 日线/盘中时间格混存（缺口 #1）+ 面板 span9 布局 bug（缺口 #5），2026-08-01；
      对抗校验又揪出「官方收盘价被延迟报价盖回」的写序 bug，改为 CSV>报价 两级权威并补上
      `tests/test_usvol_authority.py`（8 组断言，数据层首个单测）
- [x] **外部审阅采纳批次**（2026-08-02，31 条断言逐条核实后按严重度修复）：
      CI 回归网（pytest+Actions）、面板只读连接（拆掉"一条 GET 清空状态历史"的
      实测雷）、K 线修订自动失效重算、版本谓词取代全表 DELETE（升版即原地重算）、
      deriv 逐指标窗口（funding 结算行 SQL 层分流+每日补采）、真·因果回归测试
      （旧断言实为确定性测试，经突变体验证换新）、RULES_VERSION v2
      （direction 权重重归一，实测 0.4% 行翻转）、4h 中段残桶丢弃、
      SMA-ATR14 命名归实、"高波动非趋势"标签全链路统一
- [x] **全系统时间戳对齐批次**（2026-08-02，四路并行审计 52 条发现 → 复核后 20 条成立）：
      4h 重采样残桶永久固化（每天啃掉 6 根，已修 24 根存量）、funding 列混结算/预测两种口径
      （约 1.2 天后年化虚增 96 倍）、premium 回填未收线（10 个坏点已补）、health age 从开盘
      起算多报一整周期（同步收紧 stale 阈值 2.5→1.5×TF）、日界跨源锚点断言止血、
      atr_ds 改按 ET 分桶（DST 前后会错配 3 倍）、特征窗口统一 FEATURE_WINDOW=400、
      DVOL 丢未收线、lag 字段按算子计算。合并一次 `regime_audit_v5` 全量重算 5449 行
- [ ] 阈值校准 + 影子转正流程（对 direction_trend/tilt_confirm 用目标占比法；检验清单自定，
      可借鉴参考项目 regime-spectrum 的 03-CALIBRATION，但那不是规范）
- [ ] 死盘外生日历剔除 + EMA 种子 k≥1.5n 规则（并入 v2 升版，涉改品种清空重算）
- [x] Web 面板（工程模式，2026-07-31；同日切换浅色主题）
- [x] cRSI（Pine v6 本地化：自适应带 + 枢轴背离，2026-07-31）
- [x] Hermes 面板助手（可插拔后端 + 面板上下文注入，2026-07-31）
- [ ] 持仓/杠杆特征（funding/OI/taker/DVOL）进状态机（数据已在采，需回测验证增量）
- [ ] 对照实验：规则分类 vs HMM（隐马尔可夫）状态模型
- [ ] 面板简洁模式（工程模式之外的第二视图）

> 本工具输出的是市场状态描述，用于研究，不构成投资建议。
