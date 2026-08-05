# 路线1：政策矩阵与信号闸

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P0 | GATE 4 失效 | 总览把“位置成立但信号未共振”的 WAIT 标的计入“机会位”，当前 15 个机会中 13 个不满足信号门槛。 | Policy 明定无信号→WAIT、所有开仓须位置+信号共振：[eric-policy.md:26](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:26)、[eric-policy.md:115](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:115)。分层却只检查 `tradeable`：[dashboard.py:1438](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1438)。实跑 `dashboard.overview_payload()` 输出 `opportunity=15`，其中只有 `JPM-USDT/XBI-USDT signal_ok=True`，其余 13 个均为 `False`，仍显示 `S3 …做空（位置到了信号没到）`。 | 将“位置候选”和“满足政策门槛的机会”分开；`opportunity` 至少要求 `location.tradeable is True && signal_ok is True`，否则归入 `near/WAIT`。 | 高 |
| 2 | P0 | 信号伪阳性 | 完全横盘的价格序列会被 cRSI 判成超买，从而让区间压力位通过信号闸。 | cRSI 在 `up=down=0` 时先命中 `down==0` 并给 RSI=100：[crsi.py:94](/Users/luoyingdong/Documents/VVV_Trade/regime/features/crsi.py:94)；政策直接按 zone 判信号：[dashboard.py:1056](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1056)。实跑 90 根价格恒为 100：`flat90 {'crsi':100.0,'db':99.97,'ub':100.0,'pos':101.9,'zone':'超买区'}`，随后 `_signal_ok('at_resistance','超买区') => True`。 | 在 policy 信号适配层对 `up=down=0`、近期价格零方差、带宽退化设置 `signal_ok=None` 并显式 degraded；不要用“超买”替代不可计算。 | 高 |
| 3 | P1 | S13 保真度 | S13 要求“第二次超卖/确认”，当前实现只有当根 cRSI 状态，却会匹配 S13 剧本。 | 原文要求第二次确认：[eric-policy.md:77](/Users/luoyingdong/Documents/VVV_Trade/eric-policy.md:77)。代码只判断当前 zone：[dashboard.py:1056](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1056)，随后匹配 S13：[dashboard.py:1079](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1079)。实跑：`single_crsi True`，紧接着输出 `S13 深跌逆势做多（需二次确认·半仓）`。 | 增加“首次进入超卖→离开→再次进入/第二次确认”的状态字段；未能确认时保持 WAIT，并把 `second_confirmation=None/False` 暴露给前端与 Hermes。 | 高 |

## 自查盲区

按用户裁决未做回测，也未评价 cRSI 阈值的统计有效性；这里只验证逻辑闸是否忠实执行。

# 路线2：关键区间与路径语义

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P0 | 方向不确定性伪装 | 支撑、压力来源合并成同一区间后，代码用多数票/输入顺序强行选角色；同一价格、同一 regime 可仅因列表顺序而得到相反方向。 | 合并角色逻辑：[levels.py:119](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:119)、[levels.py:125](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:125)；区间内直接沿用该角色：[location.py:110](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/location.py:110)。实跑两个完全相同的 `[99,101]` 区间：`merge_zones([range_lo,range_hi],4)`→`at_support/tradeable=True`；反序→`at_resistance/tradeable=True`。现库试算还发现 34 个 zone 同时含固定高点类和低点类来源。 | 聚类保留每个来源的角色；混合角色且价格仍在区间内时返回 `role=None/ambiguous_role`，或按角色拆成两个重叠 zone，绝不能投票生成方向。 | 高 |
| 2 | P0 | Veto 未传播 | `locate` 已因错误来向否决交易，但详情页仍输出做多剧本并继续计算多头止损。 | 路径否决在 [location.py:213](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/location.py:213)；剧本与止损装配没有检查 `tradeable/reason`：[dashboard.py:1221](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1221)、[dashboard.py:1233](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1233)。实跑从下方进入 EMA21 支撑：`location={tradeable:False, approach:'from_below', reason:'wrong_approach'}`，但仍输出 `play='S4 趋势回踩做多'` 和多头 `stop_check`。 | 统一建立 `decision_gate`；只要 `location.tradeable is not True`，结论必须是 WAIT/不满足路径，且不得生成方向性 play、side 或止损建议。 | 高 |

## 自查盲区

没有校准 `MERGE_ATR/MAX_ZONE_ATR/APPROACH_BARS`；混合角色问题不依赖阈值大小，任何允许异角色聚类的阈值都会触发。

# 路线3：新鲜度与降级传播

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P0 | 陈旧数据 | 总览没有详情页的 1.5×TF stale 闸，也没有校验 regime ts 与价格 bar ts 一致，陈旧历史可被包装成刚更新的机会。 | 详情页有 stale 判定：[dashboard.py:210](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:210)；总览扫描 [dashboard.py:1323](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1323) 不检查年龄，`updated_at` 只是扫描墙钟：[dashboard.py:1496](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1496)。合成 2020 年 K 线实跑仍得到 `S3 区间下沿做多`，分层结果 `opportunity=1, unavailable=0`。采集端又会先提交 OHLCV、后提交状态：[collector.py:595](/Users/luoyingdong/Documents/VVV_Trade/collector.py:595)、[collector.py:654](/Users/luoyingdong/Documents/VVV_Trade/collector.py:654)，存在短暂错时窗口。 | 输出 `bar_open_ts/bar_close_ts/state_ts/asof`；要求收线年龄≤1.5×TF 且 `state_ts==bar_open_ts`，否则进入 unavailable；`updated_at` 与行情 `asof` 分列。 | 高 |
| 2 | P1 | Warmup 静默 | 90–279 根的预热 regime 会直接进入总览政策判断，没有任何 warmup/degraded 标记。 | 总览只检查非空 OHLCV：[dashboard.py:1323](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1323)，没有复用 `WARMUP_BARS=280`。SQL：`WITH b AS (...) SELECT SUM(n BETWEEN 90 AND 279)...` 输出 `9`；其中当前 `APP-USDT(158)、GEV-USDT(158)、XBI-USDT(163)` 已进入 opportunity，XBI 还显示 `signal_ok=True`。 | 读取最新 state 的 `features.warmup` 或直接按 bars 计算；warmup 品种保留 4h 判断，但显式标注并不得进入“完整机会”层。 | 高 |
| 3 | P1 | 静默降级 | POC/vol1h、IV 及波动输入异常在总览中会被吞掉，用户无法区分“没有风险提示”和“风险算不出”。 | `extract_levels` 会返回 `poc_missing`：[levels.py:379](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:379)，但总览在 zones 可用时不保留 `levels.degraded`；波动异常直接变成空 notes：[dashboard.py:1365](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1365)。实跑：90 根 + 无 vol1h 得到 `['ema100_history_insufficient','ema200_history_insufficient','poc_missing']`，扫描 item 中没有 degraded；IV 全缺输出 `vol_notes=[]、check_stop_vs_iv=None`。 | 每个 item 增加 `degraded` 和 `risk_status=ok/unavailable`；局部缺失不排除品种，但必须传播至总览、详情和 Hermes。 | 高 |

## 自查盲区

现库检查时 72 条 4h 序列收线年龄均约 27 分钟、当前 stale 数为 0；陈旧问题由合法合成输入复现，未在现库等待真实断流。

# 路线4：版本化先验与审计元数据

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P0 | 铁律违反 | location、stopcheck、volnote 的阈值没有版本标识，唯一的 `LEVELS_VERSION` 也在 dashboard 装配时被丢弃。 | 阈值见 [location.py:14](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/location.py:14)、[stopcheck.py:12](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/stopcheck.py:12)、[volnote.py:12](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/volnote.py:12)；levels 虽有 `lv1`：[levels.py:18](/Users/luoyingdong/Documents/VVV_Trade/regime/policy/levels.py:18)，但 payload 基础结构未带版本：[dashboard.py:1142](/Users/luoyingdong/Documents/VVV_Trade/dashboard.py:1142)。实跑 `build_dashboard('BTC-USDT')['policy']` 与 `overview_payload()`：均无任何 `version` 字段。 | 建立独立于 `RULES_VERSION` 的 `POLICY_VERSION/LEVELS_VERSION/LOCATION_VERSION/VOL_VERSION/STOP_VERSION`，附阈值快照、来源和 `eric-policy v2026-07-11`；传播到 API、UI、Hermes。 | 高 |

## 自查盲区

没有检查外部发布流程是否另有版本登记；仓库代码、API payload 和 UI 中未找到可消费的对应标识。

# 路线5：前端路由与时区

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P1 | 路由拆分 | 总览链接携带 `?symbol=目标`，详情页却完全不读取该参数，点击任意机会会默认打开字母序第一个品种。 | 总览生成 `/symbol?symbol=...`：[overview.js:35](/Users/luoyingdong/Documents/VVV_Trade/web/overview.js:35)；详情初始化只有 `S.symbol = S.symbol || S.symbols[0]`：[app.js:72](/Users/luoyingdong/Documents/VVV_Trade/web/app.js:72)，全文件无 `URLSearchParams/searchParams`。 | `loadSymbols()` 后解析并校验 URL symbol；切换品种时同步 `history.replaceState`，未知 symbol 显式报错。 | 高 |
| 2 | P1 | 时区显示 | 新总览和详情页固定显示 UTC，没有按路线9口径提供 JST 展示。 | 总览更新时间和时钟使用 `getUTCHours()` 并标 UTC：[overview.js:205](/Users/luoyingdong/Documents/VVV_Trade/web/overview.js:205)、[overview.js:363](/Users/luoyingdong/Documents/VVV_Trade/web/overview.js:363)；详情同样固定 UTC：[app.js:45](/Users/luoyingdong/Documents/VVV_Trade/web/app.js:45)。ET 财报日计算则正确使用 `America/New_York`：[storage.py:929](/Users/luoyingdong/Documents/VVV_Trade/regime/storage.py:929)。 | API 继续传 UTC 毫秒；展示层用 `Asia/Tokyo` 格式化并明确 `JST`，必要时 tooltip 同时给 UTC。 | 高 |

## 自查盲区

未启动或重启服务、未做浏览器端交互；路由结论来自确定性的 JS 初始化路径。

# 路线6：Hermes 注入与文案安全

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P0 | 系统铁律冲突 | 实际加载的 Hermes system 明确要求“给出当下最优行动、不要再等等”，与 WAIT 常态及“系统不下单、不得指令式措辞”直接冲突。 | [hermes_system.md:7](/Users/luoyingdong/Documents/VVV_Trade/hermes_system.md:7) 至 [hermes_system.md:17](/Users/luoyingdong/Documents/VVV_Trade/hermes_system.md:17)；该文件由 `load_system()` 注入所有 provider：[agent.py:665](/Users/luoyingdong/Documents/VVV_Trade/regime/agent.py:665)。实跑确认：`custom=True`、`has_action_rule=True`、`has_wait_conflict=True`。 | 在自定义提示词之后追加不可覆盖的 policy guard：只输出“建议+理由”；任一 policy gate 未满足必须明确 WAIT；禁止命令式开仓/平仓/仓位指令。 | 高 |
| 2 | P1 | 上下文丢失 | overview API 有 unavailable reason，但 Hermes 横截面只注入数量，无法说明哪些品种为何被排除。 | API 当前不可用为 `GS-USDT/SMH-USDT:4h_ohlcv_missing`；[agent.py:389](/Users/luoyingdong/Documents/VVV_Trade/regime/agent.py:389) 只展开机会和风险。实跑 `render_overview_context`：`不可用 2 个`，但 `HAS_GS_REASON False False`。 | 注入 unavailable 明细及所有局部 degraded；数量较大时至少按 reason 聚合并列出 symbol。 | 高 |

## 自查盲区

未调用真实模型，因此没有把提示词冲突转化成一条真实交易回答；缺陷在 system 装配层已确定存在。

# 路线9：失败模式与降级路径

1. 异常输入实跑结果：

   - `ATR=0`：`atr=None, zones=None, degraded=['no_atr']`，不崩。
   - `价格=NaN`：`atr=2.0, zones=None, degraded=['no_price']`，不生成位置。
   - `zones=[]`：`at/zone/role/meaning=None, reason='no_zones'`。
   - 未知 regime：`reason='unsupported_regime'`。
   - cRSI 全 NaN：末值五项均为 `None`；详情标 `crsi_unavailable`，总览将品种放入 unavailable。
   - OHLCV 90 根：可生成 ATR/zones，但明确返回 `ema100_history_insufficient、ema200_history_insufficient`；这些降级在总览被静默丢弃，见路线3。
   - vol1h 完全缺失：其余 levels 保留，返回 `poc_missing`；总览静默。
   - IV 全缺：`vol_notes=[]、check_stop_vs_iv=None`；没有伪造数值，但总览无法区分缺失与“无风险”。

2. degraded 传播：

   - 显式：ATR/价格/zones/regime/cRSI 的致命缺失；详情页 levels/location/stop 缺失。
   - 静默：overview 的部分 levels 缺失、warmup、vol1h/POC 缺失、IV 缺失及 `_overview_vol_inputs` 异常。
   - 1d 缺失按裁决保留 4h，当前 72 个 4h 品种中 37 个无 1d；字段表现为 `regime_1d:null`，没有静默排除。

3. unavailable 列表：

   - 现库 74 个成员全部有归宿：`15 opportunity + 28 near + 29 middle + 2 unavailable = 74`；risk 为可重叠维度。
   - 两个不可用品种及原因完整：`GS-USDT/SMH-USDT → 4h_ohlcv_missing`。
   - 单品种异常由 `measurement_error:<Exception>` 兜底，没有发现整品种静默丢失；Hermes 丢失 reason 另见路线6。

4. 新鲜度：

   - SQL 现算：72 条现有 4h 序列收线年龄约 27 分钟，当前 `stale_over_1_5tf=0`。
   - 详情页有 1.5×TF 闸；总览无对应机制，合成 2020 数据仍进入 opportunity，见路线3。
   - policy 只读取已收线 `ohlcv`，未消费 `live_bars`；未发现未收线预览混入政策判断。

5. 时区：

   - SQLite `ts` 为 UTC 毫秒整数；现库未发现非法 ts/价格。
   - 前日/前周关键位按 UTC 日历分桶；财报 proximity 使用 ET 日历日，口径正确。
   - overview、详情和 Hermes 显示均为 UTC，不是路线要求的 JST，见路线5。

## 自查盲区

完整 pytest 因只读沙箱没有可用临时目录而未能启动；改用无落盘直接函数实跑覆盖上述输入。Python AST 校验输出 `python_ast_ok 6`，两份 JS 的 `node --check` 均退出 0。全程未改文件、未重启进程。