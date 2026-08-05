# 路线1：policy 原文保真度

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1.1 | P0 | 矩阵漏项 | `trend_up` 的结构前低支撑和 `trend_down` 的结构前高压力被资格表排除，核心 S4/S1 会被误判成 `middle_zone`。 | 原文 [eric-policy.md:60](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:60)、[eric-policy.md:72](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:72) 明列结构前低/前高；[location.py:123](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/location.py:123) 的上升支撑未包含 `_LOW_KINDS`，[location.py:130](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/location.py:130) 的下降压力未包含 `_HIGH_KINDS`。实跑：`locate(...pivot_low..., "trend_up")` 和 `locate(...pivot_high..., "trend_down")` 均返回 `middle_zone/no_regime_key_level_nearby`。 | 至少将未翻转的 `pivot_low` 纳入上升支撑、未翻转的 `pivot_high` 纳入下降压力；日/周前高低是否同等采用应单独裁决，不要顺带放宽。补两个原文镜像测试。 | 高 |
| 1.2 | P0 | S13 失真 | S13 把第一次 cRSI 超卖直接标成信号满足，还把 `1/2–1/3 + 强制/优先 DCA` 压缩成“半仓”。 | 原文 [eric-policy.md:77](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:77)、[eric-policy.md:135](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:135) 要求“第二次超卖、不接第一刀、1/2–1/3、DCA”；[dashboard.py:1056](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1056) 只判断当前 cRSI 区域，[dashboard.py:1079](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1079) 输出“需二次确认·半仓”。实跑：`_signal_ok("at_support","超卖区") -> True`，随后直接得到 `S13 深跌逆势做多（需二次确认·半仓）`。 | 未实现超卖 episode 序数前，S13 的 `signal_ok` 必须为 `None/未实现`，不得为 True；实现后记录“第一次离开超卖再二次进入”等可审计状态。文案恢复 `1/2–1/3 + DCA`，不输出单一“半仓”。 | 高 |

## 自查盲区

未把尚不存在的账户、持仓和 DCA 状态子系统一概判为缺陷；本路线只报告已经输出了错误矩阵语义的分支。

# 路线2：机会分层与统一 GATE

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 2.1 | P0 | GATE 4 | 总览以位置层 `tradeable` 直接进入“机会位”，未要求 cRSI 共振；当前 15 个“机会”中 13 个信号未到。 | [dashboard.py:1438](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1438) 只检查 `item["tradeable"]`；原文 [eric-policy.md:115](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:115) 要求位置与信号同时满足。只读实跑输出：`counts opportunity=15`，`signal_ok=True 2 / False 13`；APP、ARM、EWY 等 13 个均显示“位置到了信号没到”。 | 将 `opportunity` 条件改为 `tradeable is True and signal_ok is True`；`tradeable=True, signal_ok=False/None` 统一进入“位置候选/等待信号”，不要叫机会。 | 高 |
| 2.2 | P1 | GATE 1/3 | 两个真正位置+cRSI 共振的品种止损均被本层判为 `too_tight`，且 RR 完全未算，却仍出现在“机会位”并展示做空剧本。 | 原文 [eric-policy.md:106](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:106) 规定任一门槛不过降为 WAIT；[dashboard.py:1233](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1233) 只把止损结果放详情，[dashboard.py:1438](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1438) 分层不消费它，也没有 GATE 3。实跑：JPM `ratio=0.190, too_tight`，XBI `ratio=0.306, too_tight`，两者仍为 `S3 区间上沿做空` 机会。 | 在完整 GATE 未实现前，将输出明确命名为“位置/信号候选”；若保留“机会/剧本”语义，则必须将 stop verdict、目标位、RR 和各 GATE 三态送入分层，任一失败为 WAIT、任一缺失为不可判定。 | 高 |

## 自查盲区

没有把 `too_tight` 自动等同于应放宽止损；正确处理也可能是放弃候选或降低仓位，但不能继续宣称统一门槛已经满足。

# 路线3：关键位区间与来源语义

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 3.1 | P1 | 来源口径 | `poc` 实际是“把每根 1h 的全部成交量放到该小时 VWAP 桶后的众数”，不是真正的成交价 Volume Profile POC，且输出没有方法标识。 | [levels.py:247](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:247) 用 `quote_vol/volume` 得到每小时单一价格，[levels.py:253](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:253) 将整根小时量放入单桶并求最大量。它随后参与合并区间 [levels.py:379](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:379)，阈值扰动已改变 2–4 个品种的位置签名。 | 改名为 `hourly_vwap_volume_mode`，或在 zone 中输出 `method`/`resolution=1h`；若继续称 POC，必须明确“近似 POC”，不得让消费者当逐成交价 POC。 | 高 |
| 3.2 | P1 | 角色压缩 | 支撑与压力成员合并后用多数票、平票用较低成员决定单一 `origin_role`，会丢失 contested 语义。 | [levels.py:119](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:119) 至 [levels.py:131](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:131)。现库复算：72 品种共 92 个混合角色区间，3 个正被当前位置命中；其中 CRWD 命中区同时含 `poc:support` 与 `range_hi/prev_day_hi:resistance`，却被压成 `resistance`。 | 保留 `member_roles`，混合角色输出 `origin_role="contested"`；位置层应按 regime、来源及来向解决 contested，而不是用价格排序平票。 | 高 |

## 自查盲区

库内没有逐笔成交数据，因此没有量化近似 POC 与真实成交价 POC 的误差，只确认了当前算法的真实口径。

# 路线4：位置来向与详情剧本

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 4.1 | P1 | 条件组合 | `_policy_play` 不消费 `location.tradeable/reason`，所以来向门槛失败仍显示 S5 剧本。 | [dashboard.py:1221](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1221) 调用 `_policy_play` 时只传 regime、位置、信号和翻转；[dashboard.py:1067](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1067) 也没有来向参数。现库 DIS、EWJ 均为 `tradeable=False, reason=wrong_approach, approach=None`，但详情仍显示 `S5 突破回踩加仓（位置到了信号没到）`。 | `play` 生成前先要求 `location.tradeable is True`；门槛失败输出 `WAIT（来向不成立）` 或纯测量说明，不能把原因错误归结为“只差信号”。 | 高 |

## 自查盲区

今天两个实例的 cRSI 都未共振，因此尚未观察到“信号已到但来向错误”的线上实例；代码签名证明一旦发生仍会错误返回 S5。

# 路线5：波动率与事件标注

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 5.1 | P1 | 期限口径 | policy 层把所有“1–3 天近端 IV”都标成 `IV3/3日`，但当前部分数据实际是 1.65 或 2.19 天的单到期 nearest 值。 | `_xopt_block` 明确返回 `tenor_days/method/n_expiries` [dashboard.py:249](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:249)，但 `_overview_vol_inputs` 只保留 `near["iv"]` [dashboard.py:1288](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1288)，[volnote.py:42](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/volnote.py:42) 固定显示 IV3。只读 SQL：DOGE `1.65, nearest, n=1`；XAU/XAG `2.19, nearest, n=1`；BTC/ETH 为 `3.0, interp`。 | 将 tenor、方法、到期数、年龄送进 policy payload；文案按实际期限写“近端 IV(1.65d)”；由非 3d 值外推 3 日波动时显式标为模型换算，并提示单到期不稳健。 | 高 |

## 自查盲区

未复核外部期权源的 IV 计算公式；发现只针对数据库中已落库的期限与展示文案不一致。

# 路线6：止损校验

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 6.1 | P2 | 死护栏 | 当前生产组合下 `MAX_STOP_DIST_ATR=3.0` 不可达，不能实际承担注释所称的宽止损拒绝保护。 | [stopcheck.py:77](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/stopcheck.py:77) 检查 3 ATR；但位置至区间最多 `AT_ZONE_ATR 0.5`，区间宽最多 `MAX_ZONE_ATR 1.5`，外缓冲 `0.25`，故上界为 `0.5+1.5+0.25=2.25 ATR`。实扰动：改 4.0 无变化；改 2.0 才从 17 个结构止损降为 16。 | 删除死护栏，或把它定义为显式组合约束并加断言：`MAX_STOP_DIST_ATR <= AT_ZONE_ATR + MAX_ZONE_ATR + ZONE_BUFFER_ATR`；更重要的是不要用无原文依据的最大宽度替代风险定仓。 | 高 |

## 自查盲区

没有对 ratio 阈值做收益回测，遵守“不做回测”裁决；这里只审计可达性、数学口径和当前横截面敏感性。

# 路线7：时间语义与数据新鲜度

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 7.1 | P1 | 时间戳/陈旧 | 总览条目不带 4h bar 时间或 `settled`，页面只显示重新生成时间；数据断流时旧判断会被不断盖上新的“更新时间”。 | `_scan_overview_symbol` 的 item [dashboard.py:1372](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1372) 无时间字段；`updated_at` 使用当前 `time.time()` [dashboard.py:1496](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1496)，前端只展示它 [overview.js:205](/Users/luoyingdong/Documents/VVV_Trade/web/overview.js:205)。实跑 item keys：`has_bar_ts=False, has_settled=False`；总览 `updated_at=2026-08-05 16:32:47Z`，实际末根 4h 为 `open 12:00Z / close 16:00Z`。 | 每项输出 `bar_ts_ms`、`bar_close_ts_ms`、`settled=true`、`age_sec/stale`；总览分层对 stale 项降至 unavailable/陈旧区。页面同时显示“计算时间”和“数据截至”。 | 高 |

## 自查盲区

当前 72 个有 4h 数据的品种均新鲜，因此没有人为停止 collector 验证陈旧路径；按要求未重启或干预进程。

# 路线8：总览 UI 与 Hermes 语义

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 8.1 | P1 | 风险统计 | “风险提示”计数把普通预期波动说明也算风险，当前至少 9 个品种仅因存在一条中性预期波动文案而进入红色风险区。 | [dashboard.py:1429](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1429) 只要 `_vol_notes` 非空即进入 risk；[volnote.py:42](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/volnote.py:42) 对任何有效 IV 都生成预期波动。现库：`risk_rows=57`，其中 `rows_only_expected=9`。 | `vol_notes` 返回结构化 `{kind,severity,text}`；预期波动放“波动参考”，仅高分位、期限倒挂、财报窗等 warning 进入风险计数。 | 高 |

## 自查盲区

未启动或重启服务做浏览器视觉 QA；UI 结论来自后端 payload、HTML/JS 静态调用链和纯函数实跑。

# 路线9：版本纪律与回归保护

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 9.1 | P0 | 铁律/版本 | 32 个决策与标注阈值没有可消费的阈值版本或参数快照；唯一的 `LEVELS_VERSION="lv1"` 也未进入详情或总览 payload。 | [levels.py:18](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:18) 只有固定字符串；详情 schema [dashboard.py:1142](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1142) 和总览 schema [dashboard.py:1372](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1372) 均无版本。实跑：`policy_keys=[atr,crsi,...,zones]`，`version_keys=[]`。这直接违反“阈值是版本化先验、须标注来源”。 | 建立 `POLICY_SPEC_VERSION / POLICY_THRESHOLDS_VERSION / LEVELS_VERSION / LOCATION_VERSION / VOLNOTE_VERSION / STOPCHECK_VERSION`；参数字典做规范 JSON+digest，每个 payload 输出版本、参数摘要、ATR 口径和 policy 原文版本。变更决策阈值必须强制升版。 | 高 |
| 9.2 | P1 | 参数漂移 | 同一“3日持仓”在三个模块重复定义，改动一个不会同步另外两个，能产生事件窗、预期波动和止损 verdict 三套期限。 | [volnote.py:12](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/volnote.py:12) `HOLDING_DAYS=3`、[volnote.py:15](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/volnote.py:15) 财报窗 3、[stopcheck.py:14](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/stopcheck.py:14) 又定义 3；`DAYS_PER_YEAR` 也重复。实扰动：1 日时预期波动均值由 6.28% 降至 3.63%，止损 verdict 由 11 个全 `too_tight` 变成 9 个 `too_tight`+2 个 `tight`。 | 建立单一 `PolicyHorizon` 输入；持仓日数、财报窗和 IV 比较共享它。年度日数只定义一次并输出 convention。 | 高 |
| 9.3 | P2 | 测试缺口 | 现有 65 个 policy 测试全部通过，却没有覆盖“结构前低/前高核心矩阵”“tradeable 真但 signal 假不得进机会”“S13 第二次超卖”三个关键反例。 | 实跑：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -s -p no:cacheprovider tests/test_policy_*.py tests/test_dashboard_overview.py`，输出 `65 passed`。位置参数表 [test_policy_location.py:47](/Users/luoyingdong/Documents/VVV_Trade/tests/test_policy_location.py:47) 缺上述核心 pivot 组合；分区测试 [test_dashboard_overview.py:87](/Users/luoyingdong/Documents/VVV_Trade/tests/test_dashboard_overview.py:87) 没有 `tradeable=True, signal_ok=False`。 | 将上述三个反例加为失败优先的契约测试；再加“每个 policy 原文矩阵行至少一个正例和一个否决例”的覆盖表。 | 高 |

## 自查盲区

只读环境不允许 pytest 建临时捕获文件，改用 `-s -p no:cacheprovider` 后 65 项通过；未生成 coverage 文件。`git status --short` 为空，未修改 `classify.py` 或其他文件。

# 路线10：阈值审计（全部新引入的常量）

现库扰动基线：74 品种；`candidate=15`、真正 `位置+cRSI=2`、`near=28`、`middle=29`、`unavailable=2`。以下均为同一快照、一次只改一个常量、恢复后再改下一项；“changed”指品种的 `(位置、tradeable、signal、命中来源)` 签名改变数。校准建议全部采用前向 shadow、人工标注和稳定性监测，不建议本阶段做 PnL 回测。

| 常量（当前值） | 来源 | 真实数据扰动与敏感性 | 是否应版本化 | 建议校准方式 |
|---|---|---|---|---|
| `ATR_PERIOD=14` | 现有 `volatility.atr` 默认值继承；SMA 而非 Wilder | 10：candidate 14、changed 17；20：candidate 13、changed 16。高敏感，但非拍脑袋。 | 是；同时记录 `sma14` 口径 | 固定复用现有 ATR，跨实现迁移先做 SMA/Wilder 换算；前向检查位置稳定率。 |
| `PIVOT_K=4` | 现有 `swing_pivots` 默认值继承 | 3：changed 14；6：candidate 12、changed 16。中高敏感。 | 是 | 用人工确认的“可见结构拐点”前向样本选择，不以收益调参。 |
| `PIVOTS_PER_SIDE=4` | 纯拍脑袋 | 2：candidate 16、changed 16；6：candidate 17、共振 3、changed 9。中高敏感。 | 是 | 以区间数量、重复/陈旧枢轴比例和人工可读性校准。 |
| `EMA_PERIODS=(21,55,100,200)` | policy 原文直接给定 | 改常见 `(20,50,100,200)` 仅 changed 1；删除 EMA200 changed 7。低敏感但属于规格。 | 是，随 policy spec | 不做数值校准；只随 policy 原文版本变更。 |
| `RANGE_WINDOW=60` | **纯拍脑袋且高敏感 ⚠** | 40：candidate 18、共振 3、changed 20；80：candidate 10、changed 22。 | 是 | 前向人工标注“可辨识区间起止”，报告 40/60/80 的稳定性交集。 |
| `RANGE_HIGH_Q=0.90` | **纯拍脑袋且高敏感 ⚠** | 0.85：candidate 11、共振 3、changed 25；0.95：candidate 13、共振 3、changed 16。 | 是 | 上下沿分别校准；以触碰/突破后的结构解释一致率做 shadow 评审。 |
| `RANGE_LOW_Q=0.10` | 纯拍脑袋 | 0.05：changed 4；0.15：changed 6；当前低敏感且明显不对称于 HIGH_Q。 | 是 | 单独检查下沿样本，禁止只因“与 0.90 对称”固定。 |
| `POC_WINDOW_1H=240` | **纯拍脑袋且有高耦合风险 ⚠** | 168：POC 72、changed 4；336：调用方只取 260h，POC 72→0、changed 3。 | 是，并加跨常量约束 | 要求 `VOL1H_HOURS >= POC_WINDOW_1H`；先明确是近似 POC，再按前向稳定性选择窗。 |
| `POC_BUCKET_ATR=0.25` | 纯拍脑袋 | 0.15：changed 2；0.50：candidate 16、changed 3。当前低敏感。 | 是 | 监测桶切换频率、跨轮抖动和区间宽度，避免基于收益调参。 |
| `POC_MIN_COVERAGE=0.80` | 纯拍脑袋 | 0.70/0.90：当前均 changed 0、POC 72。当前低敏感。 | 是 | 用真实缺口注入验证；coverage 同时检查连续小时而非只看行数。 |
| `ZONE_HALF_ATR=0.25` | 纯拍脑袋 | 0.15：candidate 16、changed 8；0.40：changed 14。中敏感。 | 是 | 以“影线触碰是否应算到位”的前向人工标签校准。 |
| `MERGE_ATR=0.50` | **纯拍脑袋且高敏感 ⚠** | 0.30：candidate 14、changed 22；0.80：changed 4。 | 是 | 联合监控 confluence、混合角色率、宽度与抖动；不得单独调。 |
| `MAX_ZONE_ATR=1.50` | **纯拍脑袋且高敏感 ⚠** | 1.0：candidate 13、changed 27；2.0：candidate 17、changed 20。 | 是 | 先限定可解释最大区间，再以前向人工审阅决定；与 merge/half 联合升版。 |
| `APPROACH_BARS=3` | 纯拍脑袋 | 2：candidate 17、changed 2；5：changed 0。今天低敏感。 | 是 | 记录每次命中前的路径长度分布；按 4h 决策尺度人工选择。 |
| `AT_ZONE_ATR=0.50` | **纯拍脑袋且机会数敏感 ⚠** | 0.25：candidate 13、changed 7；0.75：candidate 18、共振 3、changed 7。 | 是 | 用“肉眼是否已到区间”前向标注；必须绑定 SMA-ATR 口径。 |
| `HOLDING_DAYS=3` | 继承既有 3d IV/RV3 展示口径；非 policy 明文 | 1 日：预期波动均值 3.63%；3 日 6.28%；5 日 8.12%。数值高敏感。 | 是，最好改为输入 | 从用户计划持仓期读取；无法取得时标为假设，不应藏在常量。 |
| `DAYS_PER_YEAR=365`（volnote） | 既有自然日年化口径/数学约定 | 252：均值 7.57%；366：6.28%。对 252 选择敏感。 | 是，记录 convention | 永续与期权坚持自然日 365；不得拿交易日 252 混用。 |
| `IV30_HIGH_RANK=0.85` | **纯拍脑袋；借用了现有 ATR 高波 0.85 的数字但统计量不同；高敏感 ⚠** | 0.75：高位提示 38；0.85：28；0.95：11。 | 是，并按来源分版本 | DVOL、个股 IV 各自独立设门；前向统计提示频率与人工风险判读。 |
| `EARNINGS_WINDOW_DAYS=3` | 由 3 日持仓假设派生，非 policy 原文 | 1 日：持仓窗警告仍 7；5 日：7→9，near 5→3。当前低中敏感。 | 是，或取消独立常量 | 直接等于计划持仓天数，避免与 HOLDING_DAYS 漂移。 |
| `EARNINGS_NEAR_DAYS=10` | 继承现有 earnings/event horizon | 7 日：未来 near 4；10 日：5；14 日：仍 5。当前低敏感。 | 是（标注层版本） | 按用户希望的预告提前量配置；与持仓窗分开显示。 |
| `ZONE_BUFFER_ATR=0.25` | policy 只规定“结构区外”，数值纯拍脑袋 | 0.10：平均止损 2.78%；0.25：3.15%；0.50：3.77%；verdict 当前不变。中等数值敏感。 | 是 | 用前向 MAE/结构失效人工复核；在验证前只作参考，不作开仓许可。 |
| `MAX_STOP_DIST_ATR=3.0` | 纯拍脑袋 | 2.0：结构止损 17→16；4.0：无变化；当前 3.0 为死护栏。 | 是，若保留 | 先修组合可达性；policy 没有最大 ATR 止损原文，不应擅自变成 veto。 |
| `HOLDING_DAYS_DEFAULT=3` | 继承既有 3d 口径 | 1 日：9 too_tight、2 tight；3/5 日：11 个全 too_tight。中高敏感。 | 是，改为共享输入 | 与 volnote/财报窗共享 `PolicyHorizon`。 |
| `DAYS_PER_YEAR=365`（stopcheck） | 既有自然日口径 | 252：平均 ratio 0.342；365：0.411；366：0.412；当前 verdict 未变。 | 是，共享定义 | 与 volnote 共用一个 convention 常量。 |
| `TOO_TIGHT_RATIO=1.0` | **纯拍脑袋且在阈值附近会高敏感 ⚠** | 降至 0.5：7 too_tight、4 tight；升至 1.25：11 个全 too_tight。 | 是 | 在无验证前只输出连续 ratio，不给离散“严重”结论；积累前向人工标签后再切档。 |
| `TIGHT_RATIO=1.5` | 纯拍脑袋 | 1.25/2.0：当前均无变化，因为 11 个 ratio 全低于 0.682。当前低敏感。 | 是 | 同上；先保留连续值，避免无依据的档位精度。 |
| `OVERVIEW_TTL_SEC=60` | 纯工程先验，与前端 60 秒刷新配对 | 模拟查询时点 0/45/90 秒：TTL30 扫描 3 次，TTL60 扫描 2 次，TTL120 扫描 1 次。影响陈旧上界，不改交易分类。 | 运行配置版本即可 | 绑定 collector cadence、扫描耗时和 source age；UI显示缓存年龄。 |
| `OVERVIEW_OHLCV_LIMIT=400` | 工程派生：覆盖 EMA200、cRSI 和关键位窗 | 250：changed 1；600：changed 0。当前低敏感。 | 是（测量配置） | 加断言 `limit >= max(EMA_PERIODS)+warmup`；用稳定性监控而非收益调参。 |
| `OVERVIEW_VOL1H_HOURS=260` | 工程先验：POC240加余量 | 200/320：当前 changed 0、POC 均 72；但 POC 窗升到336会全灭。 | 是，且与 POC 窗联合 | 建立显式约束并按缺口率决定余量。 |
| `OVERVIEW_TERM_FRESH_SEC=7200` | 继承现有期限曲线 2h 新鲜度闸 | 1800：有效 iv3 仅 8、倒挂 0；7200/14400：iv3 48、倒挂 30。**高敏感，但有采集频率依据。** | 是（数据质量版本） | 按实际采集频率和延迟分布设为若干轮，并输出 age/method。 |
| `OVERVIEW_DVOL_RANK_WIN=365` | 继承现有 DVOL 一年窗 | 180：BTC/ETH rank 0.000/0.006；365 约0.003/0.003；730 同约0.003/0.003。当前低敏感。 | 是，标明来源 | 保持 DVOL 独立分位；按可用历史与制度周期选择，不与个股 IV 共窗暗示同口径。 |
| `OVERVIEW_DVOL_RANK_MIN=120` | 从现有 IV 最小样本纪律借用，DVOL 数值本身无直接来源 | 60/300：当前 BTC/ETH 均仍有 rank，结果不变；现有样本 370。当前低敏感。 | 是 | 用分位抽样误差或 bootstrap 置信宽度确定最小样本，但不据收益挑门槛。 |

## 自查盲区

- 敏感性是“当前横截面判断稳定性”，不是策略收益验证；严格遵守“不做回测”。
- 当前真正共振机会只有 2 个，少量增减会显示成较大的相对变化，不能据此宣布统计优劣。
- `TERM_FRESH_SEC` 的扰动跨过了当前约 0.75 小时的数据年龄，因此表现为明显阶跃；这是新鲜度闸的预期行为，不等同于阈值错误。
- 全部数据库命令均使用只读连接；未修改文件、未重启进程。