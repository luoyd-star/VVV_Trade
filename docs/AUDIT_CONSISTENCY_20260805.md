# 一致性与死代码专项审查（2026-08-05）

**触发**：用户指出「同一个事情在不同文件呈现不一样；多次升级后有 dead code 与干扰内容」，
要求用尽可能多的 gpt-5.6-sol high 全面审查核对。

**方法**：14 路 codex（gpt-5.6-sol · reasoning=high · 只读沙箱）按正交维度并行审查
→ 14 路 Claude 核验 agent 逐条对抗查证（每条回代码/库独立复现）→ Claude 监工抽查关键条目。
原始材料全部存档于 `docs/research/consistency_20260805/`（14 份任务书 + 14 份报告 + 核验判定 JSON）。

**统计**：178 条判定 = **171 CONFIRMED + 7 CORRECTED + 0 REFUTED**；
按严重度 **P1×45 / P2×133**（P0 级三条已在上午的全系统盘点入档，见 GAPS E1-E3，本轮无新增 P0）；
按类型 不一致×82 / 死代码×54 / 干扰×42；核验者顺手另发现 34 条同区域遗漏（missed_nearby）。
监工亲自复核抽样：settled_only 日历越界、Hermes 加密分支丢近端 IV、错误数恒等式等，全部属实。

---

## 一、P1 主题分簇（45 条按病灶归类）

### 簇 1：判定语义的双源分叉（最重要的一簇——同一「状态」两套答案）

| 发现 | 病灶 |
|---|---|
| **margin 按 raw_state 算，却与确认态并排展示** | `_boundary_margin` 用的是当次原始分类；确认层随后单独改 state。两态不同时（如 CAT 1h：确认态 trend_up、原始 range、margin 0.02 to_trend），Hermes 文本自相矛盾 |
| **CLI 与面板对「当前状态」语义不同** | main.py 直接输出单根规则树原始态；面板输出 v3.1 迟滞确认态。📊 最新 180 序列里 **24 个两口径不一致** |
| **CLI 特征窗违反统一纪律** | main.py 不传 limit → 至多 299 根，而生产/面板统一 `FEATURE_WINDOW=400`。同一根 K 线 CLI 与库会给出不同 atr_rank/state |
| **五状态中文标签双源** | 规则端带「蓄势/趋势条件未齐」限定语，前端 STATE_META 删掉了 |
| **前端分位条配色与实际规则不符** | 六条分位条统一按 <0.15/>0.85 配色，实际 squeeze 是 `BBW<0.15 且 ATR<0.30`、高波只看 ATR。ATR∈(0.15,0.30) 被涂中性色但满足挤压侧条件 |

### 簇 2：分位口径分叉（同名不同算法）

| 发现 | 病灶 |
|---|---|
| **实验私有 `_rolling_rank` 用 `<=`，生产 `pct_rank` 用严格 `<`**，docstring 却自称同口径 | 满 250 窗恒差 1/250，并列值时 0.0 vs 1.0；E1/E2 的翻转率统计因此系统性偏移；NaN 窗口语义也不同 |
| **PANEL_LEGEND 说 252 日普通分位，实际是 504 日同财报状态条件分位** | Hermes 固定图例误导模型；前端窗外只写「分位」不标 kind |
| **个股 IV rank 预注册门槛 252 交易日，代码 120 个观测即放行** | 文档与代码两个门槛 |

### 簇 3：默认值违诺（「绝不猜」承诺被下游打破）

| 发现 | 病灶 |
|---|---|
| **funding_interval「未知时绝不猜 8h」，面板 `get_meta(...,8.0)` 默认 8h 并年化，前端再 `\|\| 8` 回退** | 三层各自违诺，deriv.py 的纪律被消费端架空 |
| **stock_iv_term ATM 称 C/P 均值，单腿缺失时静默退化为单腿值** | docstring 与实现不符（且正是 GAPS C15 担心的单点敏感面） |
| **report.interpret 在 1h 缺失时仍会输出「三周期同向」** | 缺周期不声明 |

### 簇 4：日历/裁剪纪律的复用越界

| 发现 | 病灶 |
|---|---|
| **`settled_only` 的周末防御闸复用 2025–2027 日历清洗 2023 起历史**（监工亲验） | `is_trading_day(2023-xx)` 恒 False → 今后任何重灌/修复性回填会**静默丢弃 2025 年前整段**（现有 47,185 行是闸加上之前灌的）。修法：日历表外年份改为「仅周末判定」或扩表 |
| **`drop_unclosed` 无条件删末行，与 vol1h 的 `ts+周期<=now` 判据语义不同** | 休市期间末根其实已收线也被删；两处「未收线」定义不一致 |
| **test_usvol_authority 把周六写入并断言保留**，同文件另一节又声明「不得造出周末行」 | 测试自相矛盾，靠 isinstance 宽断言绿着 |

### 簇 5：Hermes / 面板的信息断层

| 发现 | 病灶 |
|---|---|
| **Hermes 加密分支丢近端 IV/tenor/method，只留 RV3 与 spread3** | 剪刀差缺 IV 侧可审计数字（当日新增代码的疏漏） |
| **无 DVOL 品种（SOL 等）Hermes 输出 DVOL=None 且丢近端 IV** | 前端有、Hermes 无 |
| **五路心跳只进前端，不进 Hermes `<panel>`** | 问 Hermes「数据健康吗」它看不见心跳 |
| **Hermes 无条件称个股 IV「昨结算」**，丢 asof/age_days/stale（✎修正：盘中确权后文案会错一天） | 陈旧值伪装新鲜 |
| **OI 分位：前端标「25.7d 参照窗」，Hermes 跨度≥21 天后永久不提窗长** | 同一个 0.83 两种语义 |
| **美股持仓卡把 moomoo 长史 IV 配上 CBOE 影子列的短史跨度** | 跨源张冠李戴 |
| **「翻转明细只列 4h/1d」实为「全周期截 40 条再前端丢 1h」** | 旧 4h/1d 翻转被 1h 噪音挤出预算；「Hermes/API 可查全量」的注释不实 |

### 簇 6：文档滞后与自相矛盾（GAPS/README/SYSTEM_LOG）

| 发现 | 病灶 |
|---|---|
| **GAPS 同时宣告 A1 已完成又把它列为唯一优先待办**，且「走 v2 升版」会从 v3.1 倒退；同款过期指令散落三份 IV 文档 | 主动错误的优先级指引 |
| **GAPS C1 承诺规则层强制过滤 known_at，v3.1 实际消费 NULL 行** | 承诺与实现相反 |
| **calibrated-1 报告唯一「推荐」行是 elig=0.35，代码是 0.40/0.35**（裁决记录只在代码注释里） | 顺着「推导链」读文档会得出错误结论 |
| **README 五处结构性过期**：confidence 公式、「清空重算」措辞、CBOE 当 L2 主线、默认源顺序、回测路线图未勾且目标已被 p3 否决 | 入口文档整体滞后一代 |
| **SYSTEM_LOG_20260805「只有两个阈值真正决定状态」表述错误**（0731 附录本已订正过，新版回退）；「平均置信度」实为按 raw_state 分组未声明 | 今晨新文档自身的两处错 |
| **M1 规格声称计算 β_ret/β_spread，pair_table 无任何 beta 输出** | 规格与实现 |
| **run_coupling_m2.py 用法说明写 prior-1，实际写入 calibrated-1** | 会让人误解账本版本 |

### 死代码要目（54 条中的代表，全表见附录）

- **`regime_history.rules` 列**：236,830 行全量写入，**全仓零读取**（get_states/audit 都不选它）
- **`ref_daily` 表**：15,331 行且每日续写，零硬编码消费者；「耦合历史层」实际从 ohlcv 取数；`get_ref_daily`/`get_breadth` 是死函数
- **列级采而不用**：`deriv.oi_notional`（65,224 值）、`stock_vol.underlying_price`（47,185）、`opt_iv_near.index_price`、`stock_vol_live` 五列（hv_30d/vendor_iv_rank/vendor_iv_pct/call_volume/put_volume）、`earnings.pub_type/period`
- **死常量**：`YEAR_DAYS=365.0`、`REP_PAIRS_IDX=2`、experiments 的装饰性 `_pct` 导入（其「口径参考」注释还掩盖了簇 2 的分叉）
- **运维盲区**：`storage.counts()` 只报 4/19 张表、前端只显示 3 张；Hermes 只读 SQL 提示列 8 张表、漏 11 张（反而列了零消费的 ref_daily）
- **meta**：15 个硬死键（已知）+ 新发现 `status.symbols` 每轮写入零读取

---

## 二、修复批次建议（供圈定，未动手）

| 批 | 内容 | 性质 | 预估 |
|---|---|---|---|
| **A 判定语义** | margin 标注 raw 语义或按确认态重算；CLI 对齐 FEATURE_WINDOW 并声明口径；前端分位条配色对齐真实规则；状态标签单源化 | **需裁决**（margin 双算还是标注；CLI 要不要保持轻量） | 半天 |
| **B 口径统一** | `_rolling_rank` 复用生产实现；PANEL_LEGEND/前端标 rank_kind；funding 8h 默认三层收口；settled_only 日历越界修法；drop_unclosed 判据统一 | 大部分机械，settled_only 修法需选（扩表 vs 周末判定） | 半天 |
| **C Hermes/文案补齐** | 加密分支补近端 IV；心跳进 `<panel>`；OI 窗长常显；asof/stale 贯通；翻转预算改后端过滤 1h | 机械 | 半天 |
| **D 文档同步** | GAPS 三处自相矛盾、README 五处、SYSTEM_LOG 两处、calibrated-1 裁决记录补档、M1 规格、run_coupling_m2 说明 | 纯文档 | 半天 |
| **E 死代码清理** | 死常量/死函数/死导入删除；`rules` 列与 ref_daily 的去留**需裁决**（删列 vs 接入消费者）；采而不用列出清单裁决；counts()/Hermes 表清单补全 | 删除类需裁决数据去留 | 半天 |

**建议顺序**：D（文档，零风险）→ C（文案）→ B（口径）→ A（语义，含裁决）→ E（死物，含裁决）。
测试补位随批走：B 批必须补 settled_only 跨年测试与 `_rolling_rank` 对拍测试。

> **✅ 五批全部完成（2026-08-05 晚，用户批准"全部修改"）**：codex gpt-5.6-sol（xhigh）执行、
> Claude 监工逐批 diff 审核 + 实测 + 提交。提交链：批 D `9b2a194` → 批 C `275aae6` →
> 批 B `e057c5f` → 批 A `12d9b40` → 批 E `0e2afe8`。测试 69→93 全绿（+24 例回归）。
> 裁决记录：margin 保持 raw 计算只加标注（margin_basis:'raw'，Hermes 双态明示）；
> CLI 保持轻量但明示"原始态(无迟滞)"；已落库数据一律保留（rules 列接 opt-in 出口、
> ref_daily/breadth 在 vvvquery 露出、采而不用账本入档 GAPS）；meta 15 死键由监工亲删。
> codex 报告的"未动清单"三处均经复核确认合理（E1/E2 历史报告不回写、.bak 留待 E 批已删、
> stock_iv_term 表结构不加列）。

---

## 附录一：178 条判定全量清单

（✓=CONFIRMED ✎=CORRECTED；逐条 file:line/SQL 证据见 `docs/research/consistency_20260805/` 的 out1-14.md 与 verify_verdicts.json）

| 路线 | 级 | 类型 | 判 | 断言 | 修法 |
|---|---|---|---|---|---|
| t1 | P1 | 不一致 | ✓ | 前端六条分位条把 ATR/BBW 统一按 <0.15 挤压、>0.85 高波配色，实际规则是 BBW<0.15、ATR<0.30 共判挤压，高波只看 ATR | ATR 条挤压线用 0.30、BBW>0.85 不着 high_vol 色，历史图补 ATR 0.30 markLine |
| t1 | P1 | 干扰 | ✓ | 系统日志称『只有两个阈值真正决定状态』，把直接决定 squeeze/high_vol 的三个分位阈值排除在决定状态的集合之外 | 改写为『两个趋势阈值 + 三个波动分位阈值共 5 个决定 state；tilt_confirm 仅调置信度』 |
| t1 | P1 | 不一致 | ✓ | experiments.py 的 _rolling_rank 自称与生产 pct_rank 同口径，却用 <=（含自身）而生产用严格 < | _rolling_rank 改严格 < 或直接复用 utils.rolling_pct_rank，已出报告注明口径 |
| t1 | P1 | 不一致 | ✓ | CLI 路径（main.py）最多用 299 根已收盘数据算特征，未遵守生产/面板统一的 FEATURE_WINDOW=400 | main.py 显式 limit>=FEATURE_WINDOW+1 并只送最后 FEATURE_WINDOW 根 |
| t1 | P2 | 不一致 | ✎ | README 与 Hermes 的规则说明把确认表写成无条件 2/3/1，遗漏 v3.1 事件窗内 squeeze→trend 2→3 例外 | README 与 PANEL_LEGEND 静态文案补一句事件窗例外（最好从 _confirm_need 生成） |
| t1 | P2 | 不一致 | ✓ | calibrated-1 报告推荐 elig=0.35，运行时却用进入0.40/退出0.35，且代码把该报告称为自身推导链 | 在报告末尾追加人工裁决记录：最终采纳 elig=0.40/退出0.35 及理由 |
| t1 | P2 | 干扰 | ✓ | README 路线图仍把回测框架列为未完成，且描述的是已被 p3 修订废弃的『当期条件分布』目标 | 路线图该项改 [x] 并按 backtest.py 现状重写目标描述 |
| t1 | P1 | 不一致 | ✓ | GAPS_20260805.md 同时宣告 A1 已完成，又在 D2 与结尾把 A1 当待办且要求走已过期的 v2 升版 | 删『一句话优先级』旧段，D2 的搭车对象与版本号改为『下一 RULES_VERSION』 |
| t1 | P2 | 死代码 | ✓ | YEAR_DAYS=365.0 定义后全仓无任何读取，是死常量 | 删除；若期限年化本应用它，接入公式并补测试 |
| t1 | P2 | 死代码 | ✓ | REP_PAIRS_IDX=2 是未接线配置，代表对仍由手写逻辑决定 | 删除常量，或让 reps 构造真正按它截取并断言长度 |
| t1 | P2 | 死代码 | ✓ | experiments.py 导入 _pct 标注『口径参考』但从未调用，且掩盖了第3条的口径分叉 | 删死导入，_rolling_rank 复用 utils 的实现（与第3条同一修） |
| t2 | P1 | 不一致 | ✓ | confidence 并非统一的“子信号一致程度”，而是按状态各用不同强度公式，squeeze/high-vol 各只看一个分位 | README 改称“状态专属启发式强度分”并列出各态公式 |
| t2 | P2 | 不一致 | ✓ | README 迟滞说明漏掉 v3.1 事件门槛：财报窗内 squeeze→trend 需 3 根而非 2 根 | 迟滞段补一句 v3.1 事件窗内 squeeze→趋势 3 根 |
| t2 | P2 | 干扰 | ✓ | “BTC 1h 翻转 26→12”无样本窗/截止/版本，当前库按常见窗口无法复现 | 给该实测数补上日期/窗口/RULES_VERSION，或删除 |
| t2 | P1 | 不一致 | ✓ | README 仍要求影子转正时“清空重算”，实际版本谓词机制无删除动作、是原地 upsert 重算 | 三处“清空重算”统一改为“升版触发版本谓词原地重算” |
| t2 | P2 | 干扰 | ✓ | README 称美股盘中徽章“不含假日历”，实际已用 2025–2027 显式 NYSE 假日+半日市日历 | README 改为“含 2025–2027 显式日历，表外年份视为休市，每年 12 月须补表” |
| t2 | P2 | 干扰 | ✓ | README 宇宙描述停在首批 7 个美股，未交代当前 74 品种（其中 61 美股永续） | 注册表章节写明当前 74 品种及分类，“首批”降为历史注 |
| t2 | P1 | 干扰 | ✓ | README 把 CBOE iv30 写成个股 IV 的 L2 主能力，实际主线已是 moomoo，CBOE 仅影子/短史 | 重写该节为现行波动率栈：moomoo 主线 + CBOE 影子 + 期限曲线 + live 轨 |
| t2 | P1 | 不一致 | ✓ | README 的默认源顺序“Deribit→OKX→Binance”只是未登记加密品种的兜底链，不是 collector 当前默认路由 | 分开写“auto 注册表路由（现实）”与“未登记加密兜底链” |
| t2 | P2 | 不一致 | ✓ | README 称 K 线越攒越长“分位参照期随之变长”，实际特征窗 400、分位窗 250 均固定上限 | 架构图注区分 库内留存深度 / 400 计算窗 / 250 分位窗 |
| t2 | P2 | 干扰 | ✓ | 架构图只列 3 张表，实际单库已有 19 张业务表 | 图标注“节选”并附完整表清单 |
| t2 | P2 | 不一致 | ✓ | 架构图把 dashboard 画成纯读者，实际 chat 表由 dashboard 以 rw 连接直写同一 market.db | 图拆两条箭头：市场数据只读 + chat 表可写 |
| t2 | P2 | 不一致 | ✓ | README 称 Hermes 注入“全部特征”，实际只注入精选文本摘要，pivot/slope/dc/volz 等未进 prompt | 改称“精选面板摘要”或注入受限的完整结构化特征 |
| t2 | P2 | 不一致 | ✓ | “已知限制”仍写系统无迟滞，与前文及现行状态机直接相反 | 删该限制，改列“迟滞参数(2/3/+1)仍是未校准先验” |
| t2 | P2 | 干扰 | ✓ | “历史窗口约 300 根、之后分页拉长”已过时：计算窗 400，库内序列达数千根 | 分写库内深度/400 计算窗/250 分位窗；路线图只留 parquet 缓存为未完成 |
| t2 | P1 | 不一致 | ✓ | 路线图仍标回测框架未完成，且保留已被否决的“当期条件分布”主目标 | 勾选回测框架，主目标改为现行 proper-score 口径，阈值校准单列 |
| t2 | P1 | 不一致 | ✓ | 两处未来工作仍写“并入 v2 升版”，照做会从 v3.1 版本倒退 | 统一改为“递增 RULES_VERSION”，落地时再定具体号 |
| t3 | P1 | 不一致 | ✓ | 20260805:160「只有两个阈值真正决定状态」与实际五个状态边界不符，且回退了 0731 附录已订正的结论 | 改写为「五个状态边界（er_rank/∣dir∣/SQUEEZE_BBW/SQUEEZE_ATR/HIGHVOL_ATR）；tilt_confirm 只调置信度」 |
| t3 | P2 | 干扰 | ✓ | 「structure/volatility/volume 三支进判定」会让读者以为 volume 改变状态，实际 volume 只影响趋势态 confidence 与 rules 文本 | 改为「structure/volatility 决定 state；volume 只参与 confidence/rules」 |
| t3 | P2 | 不一致 | ✓ | 「6 个模块 634 行」口径不闭合：六个具名功能模块合计 604 行，634 是含 utils.py 的七文件总数；架构图 F2 已含 crsi、F3 又单列 cRSI | 写成「6 个功能模块 604 行 + utils 30 行」，删架构图重复的 F3 节点 |
| t3 | P1 | 不一致 | ✓ | 「平均置信度」一行实为按 raw_state 分组，却置于「确认态分布」表格语境中未声明口径切换 | 该行表头标注「按 raw_state 分组的原判置信度」或改按 state 重算 |
| t3 | P2 | 不一致 | ✓ | 12 文件/69 用例正确，但「265 条 assert」现算为 262 条 | 改为 262，并让测试统计由脚本/CI 生成 |
| t3 | P2 | 不一致 | ✓ | 快照截止 06:01 UTC 时 collector.log 实际已有 1,181 轮，文档写的 1,175 是约 35 分钟前的读数 | 统一 snapshot_at 后一次性取数，或给该数字补 ⚠️ 标记 |
| t3 | P2 | 干扰 | ✓ | 「单文件 SQLite」未限定口径：回测 trial ledger 实际写第二个 SQLite 库 backtest_ledger.sqlite3 | 改为「行情/状态单库 market.db；实验账本独立库 backtest_ledger.sqlite3」 |
| t3 | P2 | 干扰 | ✓ | 「全部写入幂等」过强：chat 表是普通 INSERT append，同一请求重放会产生新行 | 限定为「17 张采集/快照表幂等；chat 有意 append-only」 |
| t3 | P2 | 不一致 | ✓ | _migrate docstring 仍声称对旧审计行「一次性清空重算」，与 20260805:154/373「全表 DELETE 已拆除、只留考古注释」相抵触 | 更新 _migrate/connect docstring，区分 usvol 定点删除与已拆除的 regime 全表 purge |
| t3 | P2 | 死代码 | ✎ | ref_daily 持续日更 15,331 行但仓内无真实消费者，唯一读取函数 get_ref_daily 零调用 | 删 accessor 可行；表本身若删须同步改 agent.py 的表清单，或为其接入真实消费者 |
| t3 | P2 | 死代码 | ✓ | moomoo_iv.fetch_overview 零调用，已被 fetch_live 取代；其「RTH 复测待做」的说明也被 fetch_live 的实测结论推翻 | 删除 fetch_overview，或改为 fetch_live 的显式薄包装 |
| t3 | P2 | 死代码 | ✓ | data/vvv.db 是零字节、全仓零引用的数据库残留，会干扰单库/双库认知 | 确认无仓外进程写它后删除，运维文档列明仅有两个有效库 |
| t4 | P1 | 不一致 | ✓ | GAPS 已标 A1 完成（v3.1），结尾却仍把"实施 A1、走 v2 升版"列为唯一优先事项；过期 v2 指令散落三份 IV 文档 | 重写 GAPS"一句话优先级"并把所有前瞻升版指令改为"下一 RULES_VERSION"。另注意 dashboard.py:334 同款硬编码（见 missed_nearby） |
| t4 | P1 | 不一致 | ✓ | GAPS C1 仍承诺规则层强制过滤 known_at，但 v3.1 已明确消费 known_at=NULL 的历史财报行 | C1 改写为"当前接受先验，严格 as-of 触发条件见 C9（known_at 非空覆盖 ≥2 财报季）" |
| t4 | P1 | 不一致 | ✓ | 个股 IV rank 预注册门槛 252 交易日，代码实际 120 个观测即放行 | 裁决 252 或 120 为正式门槛并留痕；否则下一只新股上市即以 120 违反预注册出分位 |
| t4 | P1 | 不一致 | ✓ | 同名 calibrated-1：报告唯一"推荐"行 elig=0.35，代码实际 elig_enter=0.40 | 给 calibrated-1 报告补一节"最终人工裁决：enter 0.40 / exit 0.35（迟滞）"及依据数字 |
| t4 | P2 | 干扰 | ✓ | 耦合设计仍以 666 条边描述多重比较风险，扩容后显示宇宙已是 2,701 对 | 设计文档加 X2 修订章（core 池 C(50,2)=1,225 进矩阵、显示池 C(74,2)=2,701 仅展示），与 GAPS D6 对齐 |
| t4 | P2 | 不一致 | ✓ | X1 设计要求主题标签 valid_from/valid_to 区间，实际表结构与写入路径只有 valid_from | 文档明确改为"新快照隐式终止上一版本"语义，或补 valid_to 列；二选一留痕 |
| t4 | P1 | 不一致 | ✓ | M1 规格与代码注释声称计算/另存 β_ret、β_spread，pair_table 实际无任何 beta 输出 | 删掉两处"本层只算/另存"措辞并在设计文档标注 β 双轨延期；或真实现并补测试 |
| t4 | P2 | 死代码 | ✓ | moomoo_iv.fetch_overview() 已被 fetch_live() 取代，全仓只有定义、零调用方 | 删除，或抽 3303 解析公共段供 fetch_live 复用（两者各自解析同一协议返回体） |
| t4 | P2 | 干扰 | ✓ | 财报污染文档待办 1、2 已完成，仍以未完成清单呈现 | 待办 1/2 勾完成；待办 3 保留并把"须 v2"改成"下一 RULES_VERSION"（与第 1 条同批改） |
| t4 | P2 | 干扰 | ✓ | ORATS $49 条件在两份同日调研中相反，旧文档让读者误以为须持 Tradier 账户 | PERSTOCK:36 加一句"已被 PLATFORMS:50 横评更正：无需 Tradier"交叉引用即可 |
| t5 | P2 | 不一致 | ✓ | dashboard.py 文件头自称只读 SQLite，但助手聊天/清空历史从面板进程写 chat 表 | 文件头改为「市场数据只读；chat 表为唯一可写例外（见 storage.connect_rw_nomigrate）」 |
| t5 | P1 | 不一致 | ✓ | deriv.funding_interval_h 强调未知时绝不猜 8h，但面板无 meta 时默认 8.0 并据此年化 | meta 缺失时 interval_h/per_year 置 None，前端标「周期未知」 |
| t5 | P2 | 不一致 | ✓ | funding 首轮历史两处仍写≈333天，实际接口/库内只有≈168天 | deriv.py 文件头与 collector.py:667 日志统一为「接口上限 1000，实得约 168 天」 |
| t5 | P2 | 干扰 | ✓ | binance_opt_iv 标题与 collector 行内注释仍说只覆盖 XAU/XAG，实际已扩到 8 个标的 | 两处注释改为「XAU/XAG + BTC/ETH/SOL/BNB/XRP/DOGE 共 8 标的」 |
| t5 | P2 | 干扰 | ✓ | coupling.py 文件头仍称 M2 状态机是后续工作，但 M2 已实现并被面板实时调用 | 文件头改为 M1 已成/M2 已成（coupling_fsm）/仅 M4 为后续 |
| t5 | P2 | 干扰 | ✓ | coupling.py 的 composite_matrix docstring 残留第二份「38×38」，实际 74×74 动态 | 改为「N×N（N=当前有数据的注册成员数）」；同时见 missed_nearby——web/app.js:205 还有第三份 |
| t5 | P2 | 不一致 | ✓ | E1 离线实验 _rolling_rank 自称与正式 pct_rank 同口径，实际实验用 <=（含自身），正式用 < | E1 直接调用 features.utils 的共用实现或改 <，并在实验报告标注协议差异 |
| t5 | P2 | 不一致 | ✓ | confirm_states docstring 枚举的 candidate 结构漏掉实际返回且被 agent 消费的 gated 字段 | docstring 补 gated: bool（仅 squeeze→trend 受门槛时为真） |
| t5 | P1 | 不一致 | ✓ | cRSI docstring 返回契约漏掉 confirmed_i / confirmed_bars_ago 两个确认时点字段 | docstring 补两字段并写明回测/告警必须用 confirmed_* |
| t5 | P1 | 不一致 | ✓ | stock_iv_term docstring 称 ATM IV 是 call/put 均值，实际单腿缺失时静默退化为单腿值 | 要求两腿齐全才出值，或落库 n_legs 并在 docstring 写明退化语义 |
| t5 | P1 | 不一致 | ✓ | report.interpret 在 1h 缺失时仍会输出「三周期同向」 | h1 缺失走「两周期同向」文案或要求 h1 存在才进该分支 |
| t5 | P1 | 干扰 | ✓ | VVVhermes 注释把 Codex 参数过滤称「白名单」，实现是可被未来新增危险参数绕过的黑名单 | 注释如实改「拒绝列表」，或真改为允许参数白名单 |
| t5 | P2 | 干扰 | ✓ | 两处注释仍承诺「个股 IV 并入 RULES_VERSION v2」，但全局规则版本已是 v3.1 | 改为「进规则层须递增当时的 RULES_VERSION 并全量重算」，不写死版本号 |
| t5 | P2 | 不一致 | ✓ | usvol.py 对 CBOE CSV 更新时点同时写 T+1 与当日收盘后即更新，互斥 | 文件头采用 backfill_index_history 的实测口径并注明实测日期 |
| t5 | P2 | 死代码 | ✓ | moomoo_iv.fetch_overview 零调用方，其「仍须 RTH 复测」开放项已被同文件 fetch_live 实测推翻 | 删除 fetch_overview（3303 协议同款数据 fetch_live 已覆盖） |
| t5 | P2 | 死代码 | ✓ | get_ref_daily 与 get_breadth 两个读取函数零调用，持续写表但 getter 无消费者 | 删 getter 或在 docstring 标 reserved/no current consumer（breadth 侧注明影子字段配套） |
| t5 | P2 | 死代码 | ✓ | YEAR_DAYS 常量与 rolling_sign_rate 的 window_frac 参数从未参与任何计算 | 删除二者；window_frac 若是未完成能力先实现再暴露 |
| t5 | P2 | 干扰 | ✓ | breadth.py 未注明日期地保留「五周期分母同为 2659」，今日库内两槽位已是 2661/2664 | 给 2659 加探针日期，或只保留「分母须随值落库」的不变量表述 |
| t6 | P2 | 死代码 | ✓ | fetch_overview() 全仓零调用，生产已改走同一 moomoo 接口的 fetch_live() | 删除 fetch_overview（连同其已被推翻的 docstring，见第 17 条） |
| t6 | P2 | 死代码 | ✓ | 单品种 binance_opt_iv.snapshot() 零调用，生产只用批量版 snapshot_all() | 删除单品种包装，或落一个显式 probe CLI |
| t6 | P2 | 死代码 | ✓ | ref_daily 持续写入且已存 15,331 行，但唯一读取函数 get_ref_daily() 零调用，目前无程序化消费者 | getter 可删或标 future API；写入建议保留（设计即攒历史），但把消费时点登记进 GAPS |
| t6 | P2 | 死代码 | ✓ | get_breadth() 是零调用的预留读取 API，宽度管线只有采集写入 | getter 删除或加注释标 future API；两年评审点应登记在案而非靠 docstring 记忆 |
| t6 | P2 | 死代码 | ✓ | YEAR_DAYS 是零引用常量，期限计算直接用毫秒到天除数 | 删除该常量 |
| t6 | P2 | 死代码 | ✓ | 五个 import 零加载：dashboard.datetime、backfill_earnings.date、exp_event_gate.np、coupling_fsm.field、run_coupling_m2b.FSMParams | 删除五个 import（backfill_earnings 只删 date，保留其余三个） |
| t6 | P2 | 死代码 | ✓ | _live_iv_block() 的 settled_last 和 in30 两个形参从未在函数体读取 | 签名与调用点同步删除两参 |
| t6 | P2 | 死代码 | ✎ | collector.sync_moomoo_history() 内部函数 incr() 的 tag 参数无用途，调用方分别传入 "iv"、"optstat" | 删除 tag 形参及 "iv"/"pc" 两个实参 |
| t6 | P2 | 死代码 | ✓ | rolling_sign_rate() 的 window_frac 默认参数从未被读取，也无调用方传非默认值 | 删除参数 |
| t6 | P2 | 死代码 | ✓ | e1_estimator_race() 的 tf 参数始终被传入但函数完全不读取 | 删除形参与实参 |
| t6 | P2 | 死代码 | ✓ | run_experiment(..., note="") 的 note 参数不影响 payload/实验 ID，备注实际由 record_experiment() 单独处理 | 从 run_experiment 删 note，保留 record_experiment 的 |
| t6 | P2 | 死代码 | ✓ | pair_table() 中 n_rows = len(r) 是零读取死局部变量 | 删除赋值行 |
| t6 | P2 | 死代码 | ✓ | run_coupling_grid.py 留有三个互不生效残余：REP_PAIRS_IDX、reps、tc | 删除三处 |
| t6 | P2 | 死代码 | ✓ | run_coupling_m2b.power_test() 先算的小时/位置混合 delay 在读取前被第二套统一算法覆盖 | 删 :72-74，保留 bar 位置差算法 |
| t6 | P2 | 干扰 | ✓ | main.py 文件头仍称『市场状态系统 v1』，实际调用现行 v3.1 规则，易误判 CLI 用旧规则 | 删掉硬编码版本字样，指向 RULES_VERSION |
| t6 | P1 | 不一致 | ✓ | run_coupling_m2.py 用法说明宣称写入 prior-1，实际导入并写入 calibrated-1 | docstring 与 :49 的『先验』一并改为『按当前阈值代重放』 |
| t6 | P2 | 干扰 | ✓ | 已死 fetch_overview() 的 docstring 仍称 3303 等于昨结且『须在 RTH 复测』，同文件 fetch_live 已确认该接口盘中滚动 | 随第 1 条一并删除；如留史改为带日期的已推翻结论 |
| t6 | P2 | 死代码 | ✓ | 参考实现的 calibrate_t_amp() 和 continuous_series() 仓内零调用，连 __main__ 自检/示例也未覆盖 | 参考项目可不动；若要维持『可运行参考实现』口径，把两函数纳入 __main__ 示例 |
| t7 | P1 | 不一致 | ✓ | 五状态中文标签双源：规则端带「蓄势/趋势条件未齐」限定语，前端 SM 删掉了它们 | 前端标签统一消费后端 state_label（或 states_map），删除 SM 内第二份文案，颜色保留本地。 |
| t7 | P2 | 死代码 | ✓ | /api/dashboard 顶层 states_map 无任何仓内消费者，纯死负载 | 与发现1合并处置：让前端正式消费它，否则从响应删除。 |
| t7 | P2 | 死代码 | ✓ | .symlist .dot / .st / .disp 三条 CSS 规则对应元素已不存在 | 删 style.css:70,71,73 三条规则，保留 72 行 .sym。 |
| t7 | P1 | 不一致 | ✓ | 「翻转明细只列4h/1d」实为「全周期先截40条再前端丢1h」，旧 4h/1d 翻转会被近期 1h 噪音挤出，且「API/Hermes 可查全量」不实 | 后端按用途过滤再限额：面板请求排除 1h，Hermes/API 提供带 tf/limit 参数的查询；同步改 index.html:94 注释。 |
| t7 | P2 | 死代码 | ✓ | 实时个股IV响应中的 pc_volume_ratio 被计算传输但面板与 Hermes 均不读 | 在持仓/期权卡展示 Put/Call 成交比，或从 _live_iv_block 响应中删除该键。 |
| t7 | P2 | 死代码 | ✓ | _xopt_block 与 _live_iv_block 下发的两个 captured_at 均无消费者 | 要么用 captured_at 统一显示精确更新时刻，要么删除两处响应副本只留 age_min。 |
| t7 | P2 | 死代码 | ✓ | /api/coupling 至少 9 个叶字段（n_symbols/n_rows/global.dispersion/global.blocks/pair 的 rho_slow/c/dz/last_event/matrix.themes）仓内无人读取 | 先明确 /api/coupling 是否兼任研究调试接口：只服务 UI/Hermes 则裁掉 9 字段，保留则接入诊断视图。 |
| t7 | P2 | 死代码 | ✓ | collector.counts.deriv 每轮统计并下发，但面板与 Hermes 都不读 | 在采集器卡补一行「衍生品行数」（最低成本），不要按报告备选方案改 counts() 本体。 |
| t7 | P2 | 干扰 | ✓ | SYSTEM_LOG_20260805 漂移表称「app.js 注释已更新、只有 dashboard.py 没跟上」不实：三处代码注释（app.js:205/coupling.py:67/dashboard.py:1061）仍写 38×38 | 三处注释改为「按当前宇宙动态构造的 N×N 矩阵」，并更正 SYSTEM_LOG_20260805.md:318。 |
| t7 | P2 | 干扰 | ✓ | 折叠区把「26列」写成「26 项特征」，其中 4 列实为 TF/确认态/原始态/置信度标识列 | 改为「26 列（22 项指标＋4 项标识/输出）」，或由列定义数组动态生成 summary 文字。 |
| t8 | P2 | 不一致 | ✓ | storage.py 文件头“collector 写、dashboard 读”已非实际拓扑：19 张业务表含脚本写入、Hermes 读写、四张零读表 | 改文件头为真实角色描述，并生成机器可检验的表级 W/R 清单（SYSTEM_LOG 漂移表未含此条，非重复） |
| t8 | P2 | 死代码 | ✓ | storage.py 仅 get_ref_daily 和 get_breadth 两个存取函数零调用 | 删除或给出消费计划；“仅有两个”的全称断言经独立扫描成立 |
| t8 | P2 | 死代码 | ✓ | ref_daily 已存 15,331 行且每日续写，全仓零硬编码消费者；耦合层实际读 ohlcv | 接入或停更；注意 agent.py:458 把 ref_daily 列入 Hermes 只读 SQL 可查表，存在人工/LLM 动态查询通道——“零硬编码消费”的措辞因此精确且必要 |
| t8 | P2 | 死代码 | ✓ | bbo、universe_snapshot 所有列均无读者（含主键与全部属性列） | 暂停写入或设保留期；注意基础事实（无消费者+位置式 INSERT）已在 SYSTEM_LOG_20260805 剩余风险表 P3-7 登记（不在题设的漂移表/E 组内，故不判 KNOWN_DUP），本条增量为列级完备性 |
| t8 | P2 | 死代码 | ✓ | breadth 的 11 个指标列全部落库但零消费，读取函数本身也是死代码 | 按影子字段补消费者/转正日期，或删死 accessor；注意“只采不消费”是代码内声明的设计意图，属采而不用而非意外死代码 |
| t8 | P2 | 死代码 | ✓ | stock_option_stat 八个指标列不进 payload/判定/实验，生产读取仅 ts 水位 | 登记研究入口与保留期；注意采而不用是 docstring 明示的刻意设计（storage.py:673-678），非疏漏 |
| t8 | P2 | 死代码 | ✓ | regime_history.rules 236,830 行全有 JSON 但从未被任何读路径消费 | 接入 get_states_audit/vvvquery，或停写该列 |
| t8 | P2 | 死代码 | ✓ | deriv.oi_notional 65,224 非空值闲置，面板/回测只消费 oi/premium/taker_ratio/iv30/funding | 用作跨品种可比 OI 或停止落库；补充：dashboard.py:646 的 get_deriv(storage.py:561)整宽表读也带出 oi_notional 但仅用 iv30，读路径比报告多一条、同样未消费 |
| t8 | P2 | 死代码 | ✓ | stock_vol.underlying_price 47,185 行全非空，落库后无任何生产消费 | 接入 IV/标的同日对账或停止持久化 |
| t8 | P2 | 死代码 | ✓ | opt_iv_near.index_price 56 行全非空，从未进入近端 IV payload 或实验 | 进健康检查或从存储接口移除 |
| t8 | P2 | 死代码 | ✓ | stock_vol_live 的 hv_30d/vendor_iv_rank/vendor_iv_pct/call_volume/put_volume 五列满存零消费；pc_volume_ratio 不算死列 | 裁剪 _LIVE_COLS 或把 vendor rank/HV 接入对照审计 |
| t8 | P2 | 死代码 | ✓ | earnings.pub_type/period/known_at 均不被生产读取；known_at 23 个非空值不参与任何过滤 | known_at 留待严格 as-of；pub_type/period 明确用途或停采 |
| t8 | P2 | 死代码 | ✓ | meta 只有 15 个已知硬死键；新增死物是 status.symbols 字段；stock_iv_term_last='0' 不是死键 | 删 15 死键；status.symbols 停写或让健康页消费 |
| t8 | P2 | 干扰 | ✓ | counts() 只统计 4 张表，前端只展示 3 张（连已算的 deriv 都不显示） | 改名 core_counts 或动态覆盖全部采集表 |
| t8 | P2 | 干扰 | ✓ | Hermes 只读 SQL 提示只列 8 张表，遗漏 11 张，且列出的 ref_daily 是零硬编码消费表 | 提示改为示例表并由 sqlite_master 自动生成 |
| t9 | P2 | 干扰 | ✓ | 最新系统日志 SYSTEM_LOG_20260805.md:19 仍称 high_vol_chop 为「高波动无序」，与 classify.py 正式改名「高波动非趋势（趋势条件未齐）」矛盾 | 改系统日志:19 的五态列举为现行标签；旧日志保留旧称时加「当时术语」注记 |
| t9 | P2 | 死代码 | ✓ | dashboard payload 的 states_map 键全仓无消费者，前端独立维护硬编码 SM，状态中文名双源 | 前端改为消费 states_map（颜色留前端），或删死键并注明标签前端自治 |
| t9 | P2 | 不一致 | ✓ | moomoo 厂商聚合 iv 被 payload 与 Hermes 提升为严格「30天/iv30」，同名承载 moomoo 聚合 IV 与 CBOE 常数期限 IV30 两种概念 | 主线键改 stock_iv/vendor_iv，iv30 留给 CBOE；Hermes 措辞改「标的级聚合 IV（非严格 30d）」 |
| t9 | P2 | 不一致 | ✓ | opt_iv_near 会保存 <3d 的 nearest IV，历史 payload 丢弃 tenor_days 改名 iv3，前端曲线统一标「IV3/3d隐含」 | 历史 payload 逐点带 tenor_days/method；曲线改名「近端IV」，仅 interp∧3d 标 IV3 |
| t9 | P2 | 不一致 | ✓ | footer 宣称「时间均为 UTC」，但同页采集器日志尾用宿主本地时间（JST）无时区标识 | formatter 固定 UTC 加 Z 后缀（或标 JST），footer 加限定语 |
| t9 | P2 | 不一致 | ✎ | deriv.ts 同表承载墙钟采样、资金费结算、统计点、K 线开盘四种时刻语义，无统一契约 | 把 get_deriv_col docstring 的网格语义提升为 schema 注释/文档契约即可，不必急于拆表 |
| t9 | P2 | 不一致 | ✓ | 同为「接口无时间戳记本地采样刻」，breadth 用 captured_at 而近端 IV、盘中个股 IV 用 ts，观测时刻列命名无统一口径 | 在 schema 文档列出每表 ts 语义；新表遵循 ts=有效刻、captured_at=采集墙钟 |
| t9 | P2 | 死代码 | ✓ | dashboard 为近端 IV 与盘中个股 IV 输出的两个 captured_at payload 字段无前端/Hermes/测试消费者 | 删两死键（保留 age_min），或前端接入显示精确 as-of |
| t10 | P2 | 不一致 | ✓ | 耦合面板把 all247 写成“加密+商品”，实际还含国际股永续 SKHY | 文案改“24/7（加密+商品+国际股永续）”或由后端下发面板成员说明 |
| t10 | P2 | 不一致 | ✓ | 可见符号 ° 在矩阵表示观察池、在波动率卡表示未确权，页面无可见图例 | 两种语义用不同符号，并各加可见 tooltip/图例 |
| t10 | P1 | 不一致 | ✓ | 美股持仓卡把 moomoo 长史 IV 值/分位配上 CBOE 影子列的短史跨度 | iv30 三元组随 source 一并返回 span/n/rank_kind，meta 跟随最终选中 source |
| t10 | P2 | 干扰 | ✓ | 个股期限曲线出数后 UI/Hermes 会把实际到期天数固定写成 3d/9d/30d（dormant） | 显示实际 t3/t9/t30，目标桶只作括号说明；数据落库后升 P1 |
| t10 | P1 | 不一致 | ✓ | IV 分位可能是 504 日条件分位，PANEL_LEGEND 仍统一说 252 日，前端窗外只写“分位” | 图例与前端统一显示 rank_kind+窗口（同财报状态·504日 / 原始·252日） |
| t10 | P2 | 干扰 | ✓ | PANEL_LEGEND 迟滞说明漏掉 v3.1 事件窗内 squeeze→trend 加一根的例外 | 图例补事件门槛，最好由 _confirm_need 的结构化配置生成 |
| t10 | P1 | 不一致 | ✓ | 无 DVOL 品种（SOL）前端有近端 IV，Hermes <panel> 却输出 DVOL=None 且丢近端 IV 值/期限 | 无 DVOL 品种改写“近端IV=xx（实际x.xd）”，有 DVOL 时两层并列 |
| t10 | P1 | 不一致 | ✓ | 五条分管线心跳只进前端，未进 Hermes <panel>，数据健康问答盲区 | <panel> 注入各 lane 的 state/age/note，至少注入非 ok/idle 项 |
| t10 | P1 | 不一致 | ✎ | Hermes 无条件称个股 IV 为“昨结算”，丢弃后端 asof/age_days/stale | 改“最近结算 YYYY-MM-DD／N天前”，stale 注入健康警告 |
| t10 | P1 | 不一致 | ✎ | Hermes 丢弃指数 IV 与期限结构的 settled 状态，盘中未确权值被当正式数值 | 未确权值在文本中标“盘中延迟报价/未确权” |
| t10 | P2 | 不一致 | ✓ | vvvquery overview 给 Hermes 的表清单只列 8 张，实际库有 19 张业务表 | 由 sqlite_schema 动态生成，或标明“常用表举例” |
| t10 | P2 | 干扰 | ✓ | vvvquery states 自称“状态+审计”，实际只投影 5 个审计特征且不显示 rules/version/audit_version | 改称“状态+5项审计摘要”或加 --full/--json |
| t10 | P2 | 死代码 | ✓ | preview payload 的 bar_ts/age_sec 只有生产方，无任何消费者 | 删两字段，或补预览新鲜度展示后保留 |
| t10 | P2 | 干扰 | ✓ | 源码宣称“1h 翻转 Hermes/API 可查全量”，实际 API 上限 40 条、Hermes 注入上限 6 条 | 改“可查最近记录”，或提供按 TF 分页的 flips 接口 |
| t10 | P2 | 不一致 | ✎ | 商品（XAU/XAG）剪刀差标成“个股IV−RV”，把代理 GLD/SLV 说成商品自身个股 IV | spread_src 枚举扩为 self/index/proxy，商品统一显示“代理GLD IV−RV” |
| t11 | P1 | 不一致 | ✓ | test_usvol_authority 把周六 2026-08-01 当"新交易日"写入并断言保留，同一文件⑧节又声明"不得造出周末行"且脏值只验 isinstance(date)。 | D01 改为真实交易日；用可控时钟钉住周末脏值必须回退上一交易日，写入层拒绝非交易日。 |
| t11 | P1 | 不一致 | ✓ | 符号翻转测试的 docstring 承诺"全程无 decoupled"，实际断言允许途经 decoupled 且 :62 注释反称这是诚实记录。 | 先裁决 FSM 语义，再让测试名/docstring 与断言二选一对齐。 |
| t11 | P2 | 不一致 | ✓ | 文件头称"稳态零误报"，稳态测试只禁最终级 decoupled，误入 decoupling 仍全绿。 | 稳态样本加 `to_state!="decoupling"` 断言，或把头部改成"零确认脱耦"。 |
| t11 | P2 | 不一致 | ✓ | test_coupling.py 头部所称"EWMA 手算对照"不存在，实际是 4000 随机样本、容差 0.08 的总体相关收敛冒烟。 | 补短序列固定数值精确对拍，或改头部为"总体相关收敛测试"。 |
| t11 | P2 | 干扰 | ✓ | 财报条件分位测试 docstring 仍写"今天 IV=80、应落中间"，fixture 已改为 IV=70、同组最低、断言 rank==0.0。 | 按现 fixture 重写 docstring（条件组最低值 + rank_raw 污染对拍）。 |
| t11 | P2 | 干扰 | ✓ | 实时 IV 预览测试 :392 旧注释称无财报时预览分位应回退 None，现行断言与生产代码都要求回退全集≈0.127。 | 删除或改正 :392 注释后半句。 |
| t11 | P2 | 干扰 | ✓ | 两个半日市测试在 half is None 时直接 return，目标断言静默变通过。 | 先断言 2026-11-27 收 13:00，找不到半日市应 fail 或 pytest.skip 而非静默 return。 |
| t11 | P2 | 干扰 | ✓ | test_backtest_causality 头部只列"四件事"、test_stock_vol 头部只列"三件事"，实际分别 12/34 个用例。 | 更新文件头职责目录，或按主题拆分 test_stock_vol.py。 |
| t11 | P2 | 干扰 | ✓ | test_confirm_states 文件头仍无条件写"一般态 2 根"，遗漏 v3.1 事件窗内 squeeze→趋势 3 根的例外。 | 文件头补写 v3 事件门控例外。 |
| t11 | P2 | 干扰 | ✓ | test_pathgeom 已降格为确定性测试，:50 失败信息仍写"analyze 因果性失败"。 | :50 消息改为"analyze 确定性失败"。 |
| t11 | P2 | 不一致 | ✓ | 系统日志"265 条 assert"把 3 个 docstring 里的 assert 单词算进去了，真实静态 assert 语句 262 条。 | 日志改 262，计数改用 AST 或行首语句匹配。 |
| t11 | P2 | 死代码 | ✓ | 四组死脚手架：FakeConn 定义未实例化、FSMParams 导入未用、tempfile 导入未用、total 累加无读取。 | 删除四处；FakeConn 若要表达接口约束应真正传入 panel_returns。 |
| t11 | P2 | 干扰 | ✓ | 除已知 upsert_ohlcv 外，另有 12 个生产模块（agent/breadth/deriv/experiments/crsi/structure/utils/volume/instruments/report/main/vvvhermes）零直接单测引用。 | 在测试治理文档建立直接/间接/零覆盖矩阵，优先补 agent、deriv 清洗、breadth、CLI 白名单。 |
| t12 | P2 | 不一致 | ✓ | collector X1 注释写 37 品种（29 core+8 obs），硬编码 X1 段实为 38 个（30 core+8 obs） | 注释改 38/30+8 或删掉硬编码数字，改由代码打印计数 |
| t12 | P2 | 不一致 | ✓ | backfill_moomoo_iv.py 注释按 31 个标的估时，实际默认处理 63 个 | docstring 去固定数，运行时按 len(symbols) 报数并折算估时 |
| t12 | P2 | 不一致 | ✓ | probe_moomoo_iv.py:91 注释'一次拿全 31 个标的'，实际动态取全部 61 个 us_stock_perp | 注释去固定数字，打印 len(codes) |
| t12 | P2 | 干扰 | ✓ | probe_moomoo_iv 仍把'能否回溯≥3年/是否采纳'写成待决未知，但结论已落地生产 | docstring 顶部标注'历史探针，2026-08 已裁决采纳'，保留巡检功能 |
| t12 | P1 | 不一致 | ✓ | run_coupling_grid 文档写每组合 11 资格对，按现库实算为 37 | docstring 去固定数，运行时打印 len(pairs) 并按其折算路数/耗时 |
| t12 | P2 | 干扰 | ✓ | regime/instruments.py docstring 的 class 说明只列 crypto 与 us_stock_perp，遗漏已上线的 commodity 与 intl_stock_perp | docstring 补齐四类及各自交易时钟/功能边界 |
| t12 | P1 | 不一致 | ✓ | '注册表热读、改完即生效'未注明成员例外：新增 JSON 品种不会进入运行中或默认启动的 collector | 注释明确'成员非热读'，或让 collector 每轮从注册表安全刷新成员 |
| t12 | P2 | 不一致 | ✓ | requirements.txt 注释称每个测试文件保留直跑入口，实际 12 个文件仅 7 个有 __main__ | 注释改'部分文件支持直跑'或统一补 __main__ 入口 |
| t12 | P2 | 不一致 | ✓ | 根 requirements 装不出仓内标注'可执行'的参考实现所需的 SciPy | 在 regime-spectrum 内声明独立依赖，勿污染根 requirements |
| t12 | P2 | 不一致 | ✓ | moomoo 依赖注释要求最低 10.8.6808，但约束只写 >=10.8，表达不了该下限 | 改为 moomoo-api>=10.8.6808 |
| t12 | P2 | 死代码 | ✓ | REP_PAIRS_IDX 是零消费者的遗留常量 | 删除；如原意限制代表对数，接入 strong/edge 构造处 |
| t12 | P2 | 死代码 | ✓ | backfill_history._fetch_deribit_range 的 fetch_tf 赋值后被 `_ = fetch_tf` 显式丢弃，不参与请求 | 删 43 与 65 两行，行为零变化 |
| t13 | P1 | 不一致 | ✓ | 生产分位用严格 `<`，E1/E2 实验私有 `_rolling_rank` 用 `<=` 却声称同口径；并列值时 0.0 vs 1.0 | 删私有实现改调公共 rolling_pct_rank；如坚持 <= 须改名并写进实验协议版本 |
| t13 | P1 | 不一致 | ✓ | settled_only 把只覆盖 2025–2027 的 NYSE 日历复用于 2023 起的历史清洗，新建/全量回填时 2023–2024 工作日全被丢弃 | 历史合法性与实时 RTH fail-closed 拆成两个 API，历史侧只拒周末+已知假日 |
| t13 | P2 | 不一致 | ✓ | backfill_moomoo_iv 用宿主机 date.today()，而 collector/财报回填已统一 ET 日期，东京宿主每天约 13 小时相差一天 | 抽公共 et_today() 三处共用 |
| t13 | P1 | 不一致 | ✓ | fetch_ohlcv 的 drop_unclosed 不判断是否未收线而无条件删末行，与 fetch_binance_vol1h 的 ts+周期<=now 判据语义不同 | 统一为 bar_close_ms<=now 判据，或把「末行必为形成中」的契约下沉进各源适配器 |
| t13 | P1 | 不一致 | ✓ | CLI 把单根规则树原始态直接标为 state，面板展示 v3.1 迟滞确认态；两入口对「当前状态」语义不同，最新 180 序列中 24 个不一致 | CLI 标注「原始即时判定」或改读库内确认态；若 CLI 仍被用于盘中判定应升 P0 |
| t13 | P1 | 不一致 | ✓ | deriv.funding_interval_h 规定未知必须返回 None，面板却 get_meta(...,8.0) 默认 8h 并年化，前端再 `∣∣ 8` 回退 | 缺键时 interval 与年化都出 None，前端显示「周期未知」 |
| t13 | P2 | 不一致 | ✓ | Web 与 CLI 各自实现价格格式化，<0.01 时一个 toPrecision(4)、一个 .10f 去尾零，同价显示不同精度 | 冻结统一精度规范 + 跨语言固定样例测试 |
| t13 | P2 | 死代码 | ✓ | moomoo_iv.fetch_overview 是旧 3303 聚合读取，全仓无调用方，现行管线走 fetch_live——死代码 | 删除或移入 probe 脚本并标注非生产 API |
| t13 | P2 | 死代码 | ✓ | binance_opt_iv 的单品种 snapshot 及其专属 fetch_chain 构成零入口调用岛，已被 snapshot_all 取代 | 删除，或让 snapshot 包装 snapshot_all(symbols=[symbol]) 留作调试 |
| t13 | P2 | 死代码 | ✓ | ref_daily 持续写入但无固定应用消费者，唯一读取器 get_ref_daily 自身零引用；15,331 行/9 序列/2020-01-01~2026-08-04 未进任何耦合/回测/面板 | 明确标注冷存储/手工研究层，或接入具体实验消费者，否则停日更 |
| t14 | P2 | 不一致 | ✓ | BTC DVOL 值各出口一致为 34.0，但分位 payload/Hermes 为 0.003、前端四舍五入成 0.00 | 前端 DVOL 分位改 fmtN(…,3) 或统一显示为百分数，与 Hermes 口径一致 |
| t14 | P1 | 不一致 | ✓ | NVDA IV 正式分位实为 504 日窗同财报状态条件分位，Hermes 固定图例仍称普通 252 日分位 | 改 PANEL_LEGEND 为'优先504日同财报状态条件分位，样本不足回退252日原始分位'，前端把 rank_kind 译成中文标签 |
| t14 | P1 | 不一致 | ✓ | BTC 近端 IV(~3d) 从库到前端、collector 全通，但 Hermes 加密分支丢掉 IV/tenor/method，只留 RV3 与 spread3 | 加密 Hermes 行补输出 xopt 的 iv/tenor/method/n_expiries/age，兑现 _xopt_block 自己声明的消费方契约 |
| t14 | P2 | 干扰 | ✓ | RV30/RV3 可复算且各出口一致，但 README 仍写旧的'指数IV vs RV30 + 9D/3M'单卡描述 | README.md:74 改写为现行四层架构并写明 RV30=30×日收益、RV3=72×小时收益 |
| t14 | P2 | 不一致 | ✓ | 代码与活体文案把 IV−历史HV 直接称'方差风险溢价'，与仓内研究文档'IV−过去RV 不是严格 VRP'的结论相悖 | 活体出口统一改称'IV−HV 剪刀差'或'VRP proxy'，保留解读文案不变 |
| t14 | P2 | 干扰 | ✓ | 9D/30D 双速期限结构各出口一致，但 README 仍写旧的单一 9D/3M 口径 | README 改为'9D/30D 急性端 + 30D/3M 制度端'，ts_ratio 明标 legacy |
| t14 | P2 | 死代码 | ✓ | stock_option_stat 的 NVDA put/call 历史链（780 行）停在库内，dashboard/前端/Hermes/只读查询提示均无消费者 | 至少把表加进 vvvquery 表清单并标注'研究中、不进判定'，或由用户裁决是否停采 |
| t14 | P2 | 死代码 | ✓ | 盘中 stock_vol_live.pc_volume_ratio 进入 _live_iv_block payload，但前端与 Hermes 零引用，是死 payload 键 | 要么在 IV 实时区渲染'盘中P/C成交比'，要么从 live payload 删键 |
| t14 | P1 | 不一致 | ✓ | OI 分位计算一致，但前端标注约 25.7 天参照窗，Hermes 在跨度≥21 天后永久不提窗口长度 | Hermes 恒输出 spans.oi（如'近25.7日小时分位'），不要用 21 天闸门吞掉窗口信息 |
| t14 | P1 | 不一致 | ✓ | margin 按 raw_state 规则树计算，却与迟滞后的 confirmed state 并排展示；两态不同时方向文本自相矛盾 | payload 键改名 raw_margin 并在图例/前端标明'相对原始判定态的边界'；需要 confirmed 态 margin 则按 confirmed state 另算 |
## 附录二：核验者顺手发现的 34 条同区域遗漏（missed_nearby）

- [t1] README.md:195-199『已知限制（v1 刻意为之）』第2条『无状态迟滞』与第3条『历史窗口约 300 根』同样过时：非对称迟滞 2026-07-31 已实现（README:18-20 自己就在讲它），生产路径窗口已是 FEATURE_WINDOW=400 + 回填至 5000 根——与报告第7条同区域但未被列出（第3条对 CLI 路径反而仍成立，恰是报告第4条的另一面）
- [t1] regime/features/utils.py pct_rank 的 window 参数默认 250，而调用方与 SYSTEM_LOG 均以 250 为口径叙述；experiments._rolling_rank 除比较算子外对 NaN 的窗口语义也与生产不同（rolling 原序列 vs dropna 后 tail）——第3条修复时应一并对齐
- [t1] docs/COUPLING_CALIBRATED1_20260804.md 尾注『升版须人工复核 + 满额（20k 路）终验』与 GAPS E6『calibrated-1 从未跑过 20k 满额终验』互证：第6条修文档时应同时把『终验未跑』写进裁决记录，避免只补采纳值不补欠账
- [t2] 与 #17 同族的版本倒退引用还有两处代码注释未被报告列出：dashboard.py:334-336 “个股 IV 进规则层需并入 RULES_VERSION v2”、regime/storage.py:172-174 “将来 IV 进规则层（v2）”——修 README 时应一并改为“递增 RULES_VERSION”
- [t2] README.md:95 “每周期取约 300 根已收盘 K 线”写在“数据”通节，但它只对 main.py 直连接口路径成立；collector 路径经回填后读窗为 10000+399（collector.py:592）——报告 #15 只覆盖了“已知限制”一节的 300 根表述，未点名此处需区分两条路径
- [t2] README.md:22（a7/23 项，实际 a8/25 项）与路线图 :211 的 DATA_DEGRADED 项是本路线范围内的另两处漂移，报告未列——二者均已在 SYSTEM_LOG_20260805 漂移表入档，属 KNOWN_DUP，报告跳过合理但未声明
- [t3] regime/agent.py:457-459：VVVhermes 只读 SQL 接口的表清单停在 8 张（ohlcv/regime_history/deriv/vol1h/usvol/dvol/stock_vol/ref_daily），缺 08-04 后新增的 earnings/stock_option_stat/stock_vol_live/opt_iv_near/stock_iv_term/breadth 等——与「19 张业务表」现状漂移，属 E16 同族但未被漂移表收录
- [t3] SYSTEM_LOG_20260805.md:144 自身枚举即不闭合：13+2+2=17 张幂等表对不上同段「19 张业务表」，chat 与 meta 两张未被口径覆盖（与发现 8 相邻，报告只点了 chat）
- [t4] dashboard.py:334 docstring 同样硬编码"个股 IV 进规则层需并入 RULES_VERSION v2"——与第 1 条同族的过期 v2 指令，但在代码注释侧，报告只覆盖了三份文档，改文档时应成对修
- [t4] GAPS_20260805.md 自身内部矛盾：E9（:20）已明确"连带更正 B5：门槛早已越过，'约 10 月中自然达标'的记述错误"，但 B5 行（:48）原文未动——同一文档一处已判错、一处照旧陈述，读者按先后顺序读会先接受错误版本
- [t5] web/app.js:205 还有第三份「38×38 复合相关矩阵」注释（同函数 210 行已改口「74 品种」，头注释没跟上）——且 SYSTEM_LOG_20260805 漂移表括注「X2 后 app.js 的注释更新了」与实情不符，漂移清单本身在此处失准
- [t5] regime/breadth.py:5 文件头「我们 38 个品种里 30 个是 AI 链」也是 38 时代的陈旧数字（现 74 品种），与 #6 同族但另一处
- [t5] docs/GAPS_20260805.md C1（「v2 进规则层时强制只用 known_at」）与 D2（「IV 进特征层/规则层（v2 升版）」）、A5（「耦合层抑制条件属 v2」）存在与 #13 同款的硬编码 v2 版本号残留——规则版本已到 v3.1，这些文档承诺同样不可能以 v2 兑现
- [t6] scripts/run_coupling_m2.py:49 在报告文案里渲染『阈值代 `{THRESHOLD_VERSION}`（先验）』——常量已是 calibrated-1，『（先验）』括注与第 16 条 docstring 同源过时，修第 16 条时应一并改，报告只点了 docstring 未点这处输出文案
- [t7] _live_iv_block 还有 4 个报告未列的死叶字段：series（dashboard.py:466-467，每 symbol 每次刷新下发 ~120 个盘中点，是该块体积最大的死负载）、pre_iv（455）、in30_now（460）、preview（462）——app.js:791-793 与 agent.py:206-214 均只读 iv/chg/chg_pct/rank_preview；尤其 preview 字段旁注释写「前端必须据此标注未结算」，而前端实际是硬编码「实时(未结算)」文案、从未读该标志，注释承诺的契约并不存在。
- [t7] web/ 目录下 app.js.bak / index.html.bak / style.css.bak 三个备份文件仍在（会被静态服务出去）——此项与 GAPS_20260805.md E15 实质相同，属已知，不再单列。
- [t7] 报告发现8的处置建议有一处会引发回归：storage.counts() 除 dashboard 外另有 collector.py:812 每轮日志调用（含 deriv 计数并经 log_tail 显示在面板），「从 counts() 中移除」这一备选修法不可采。
- [t8] storage.py:561 get_deriv 以 _DERIV_COLS 整宽表 SELECT，dashboard.py:646 由此把 oi_notional 全列读入内存后只用 iv30——#8 的死列多一条实际读入路径（读而不用），报告只写了 757-760 的按列请求路径
- [t8] meta 键 usvol_csv_gate_v2：#13 的族清单未显式点名（可归入其『12 个单键族』，账目自洽但清单不完整）
- [t8] agent.py:458 将零消费表 ref_daily 写进 Hermes 提示，等于主动邀请 LLM 查询一张无人维护口径的表——#3 与 #15 各说了一半，两条合看才是完整风险：动态消费通道存在但通向死数据
- [t9] web/app.js:5-11 的 SM 标签已相对 classify.py STATES 再次漂移：squeeze 缺「（蓄势）」、high_vol_chop 缺「（趋势条件未齐）」括注——这是 #2 双源风险已兑现的实例，报告只说了双源未说现值已漂
- [t9] web/index.html:143 footer 数据源列举含 OKX，但 SYSTEM_LOG_20260805 明写 OKX/Hyperliquid 兜底「从未触发」，与 data.py 文件头漂移（已在漂移表）同族，footer 一侧未被登记
- [t9] dashboard.py:732 一处衍生细节：spread3 对 DOGE/XAU/XAG 实为 ~2.1-2.75d IV 减 3d RV 的跨期限差，标签「3dIV−RV3」——属 #4 的直接后果，报告建议里未点名这个具体读数
- [t10] preview payload 的 label 与 close 字段同样无消费者（dashboard.py:191、195 写入；app.js:336/436 用 SM[state].label 自查映射而非 preview.label，close 全仓零读取）——与第 13 条同源，可一并删除
- [t10] agent.py:47 PANEL_LEGEND 统一写“IV=Deribit DVOL 隐含波动率”，对无 DVOL、走币安近端 IV 的品种（SOL 等 6 币）该图例定义落空——与第 7 条同根但属图例层
- [t11] 两个半日市测试的候选日期表互不一致：tests/test_stock_vol.py:294 用 (2026-11-27, 2026-07-03, 2026-12-24)，:659 用 (2026-11-27, 2026-07-02, 2026-12-24)；且 2026-07-04 为周六、07-03 是补休全休日而非半日市，候选表本身含无效项——修第 7 条静默 return 时应一并统一并验证候选日。
- [t11] SYSTEM_LOG_20260805.md:214 同句宣称 GitHub Actions '68 次运行 100% 绿'，报告自查盲区已声明未核历史日志——该数字在本轮同样未被任何一方复核，属日志中紧邻的未验证活体数字。
- [t12] collector.py:49-51 的 X1 注释只描述前 38 个，但同一 DEFAULT_SYMBOLS 元组已并入 X2 共 74 个品种——修注释时应同步给出总量 74（=X1 38 + X2 36），否则读者会把整个元组当成 37/38 个
- [t12] 以上 12 条均不在 SYSTEM_LOG_20260805.md「文档与代码的漂移」11 处清单及 docs/GAPS_20260805.md E 组（E1-E16）内，无 KNOWN_DUP；E16 的 11 处漂移与本报告零重叠，说明治理件/脚本注释是漂移盘点的又一盲区，可考虑并入 E16 根因项
- [t13] 同区域第二处口径差（报告第 1 条漏）：experiments.py:51 `s.rolling(win).apply(...)` 默认 min_periods=win，前 249 根全 NaN；而 utils.rolling_pct_rank:26-29 从 2 个有效值起就出数、pct_rank 用 tail(window) 不足额也算——窗口不足语义三处各不相同，实验与生产的分位序列起点相差约 248 根
- [t13] dashboard.py:811 `per_year = 365*24.0/interval_h if interval_h else 3*365.0`：else 分支隐含 8h 假设且因上一行 `or 8.0` 兜底恒不可达——死分支，且与第 6 条同根（8h 伪默认写了两遍）
- [t13] meta 表中 74 个 funding_interval_at_<SYM> 时间戳键（共 148 行）无任何读取方消费其新鲜度——记录了「上次更新时刻」却没人检查过期，interval 改制（8h→4h）后若 fundingInfo 拉取失败会无限期沿用旧值且无告警
- [t14] dashboard.py:829-834 分位的"当前值"口径不统一：taker_rank 用小时重采样后的末值作当前值，而 oi_rank/premium_rank 用原始 5m 末值与小时参照集比——同一函数内两种口径，5m 噪声大时同一时刻两类分位的可比性不同（P2）
- [t14] regime/agent.py:44-68 PANEL_LEGEND 完全没有描述 3d 持仓前端层的字段（近端IV/RV3/spread3/个股期限3d9d30d）——正文已在输出这些数字（agent.py:188-189,252-253,269-271），图例却只解释 30d 层，与发现2同根因：固定图例落后于分层升级（P2）