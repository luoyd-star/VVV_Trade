# 路线6：前端两页（overview + 详情页）

## 发现

| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | P0 | Policy 失真 / 信息架构 | 信号未共振时系统没有降级为 WAIT，反而把 13 个品种放进首页“机会位”并展示做空剧本。 | `eric-policy.md:106,115` 规定任一开仓门槛未过即 WAIT；`dashboard.py:1087-1089` 在 `signal_ok=False` 时仍返回原剧本；`dashboard.py:1438-1441` 仅按 `tradeable` 分组，未检查信号；`web/overview.js:100-102`、`web/app.js:431-438` 将其作为建议结论展示。实跑：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c 'import dashboard; p=dashboard.overview_payload(); o=p["opportunity"]; print(len(o),sum(x["signal_ok"] is False for x in o),[(x["symbol"],x["play"]) for x in o[:3]])'` → `15 13`，前三项均为 `S3 区间上沿做空（位置到了信号没到）`。 | 输出结构化的 `action=WAIT`、`candidate_play`、`reason=信号未共振`；首页“机会位”至少要求 `tradeable is True and signal_ok is True`，其余移入“接近关键位/观望”。 | 高 |
| 2 | P0 | 文案 / 铁律 | 两页用户可见剧本直接包含“做多、做空、加仓”，违反“系统不下单、文案不得出现指令式措辞”的铁律。 | `dashboard.py:1075-1085` 生成“S5 突破回踩加仓”“S4 趋势回踩做多”“S1/S3…做空”；`web/overview.js:100-102` 与 `web/app.js:431-433` 原样展示。命令 `grep -RInE '买入\|做空\|立即\|做多\|加仓' web dashboard.py` 命中上述 5 条；当前信号已共振实例输出为 `JPM-USDT · S3 区间上沿做空`。 | 改为非执行性描述，例如“S3 区间上沿 · 空头条件候选”“S4 趋势支撑 · 多头条件候选”“S5 突破回踩 · 追加观察点”；动作统一显示“建议关注/继续等待”。 | 高 |
| 3 | P1 | 交互诱导 | overview 与详情页都持续展示逐秒 UTC 时钟和“刷新 Ns”倒计时，形成用户明确要求避免的秒表式催促设计。 | `web/overview.html:71-74`、`web/overview.js:362-367`；`web/index.html:47-54`、`web/app.js:1345-1353`。两个页面均每秒更新 `刷新 ${left}s`，到零立即刷新。 | 保留静默自动刷新，只展示数据更新时间或分钟级新鲜度；移除秒级倒计时，必要时仅在请求失败时提供中性的手动重试。 | 中 |
| 4 | P1 | 深链 / 输入边界 | `/symbol?symbol=XXX` 只做字符正则、不做 74 品种白名单校验，未知品种会被当成“数据库为空或历史不足”并误导用户运行采集器。 | `web/index.html:227-230` 只校验字符；`web/app.js:75-76` 保留任意非空深链值；`dashboard.py:1617-1619` 未像聊天接口一样校验品种；`web/app.js:285-288` 将空 `tfs` 解释为需运行采集器。SQL：`SELECT COUNT(*) FROM (SELECT DISTINCT symbol FROM ohlcv) WHERE symbol='NOTREAL';` → `0`；实跑 `dashboard.build_dashboard("NOTREAL")` → `symbol=NOTREAL, tfs=[], policy.degraded=['regime_4h_missing','regime_1d_missing','4h_ohlcv_missing']`。 | `/api/dashboard` 先按 `storage.symbols(conn)` 白名单校验，未知品种返回 400；前端加载品种表后同步校验，选择安全默认值或显示“未知品种”，并用 `history.replaceState` 规范化 URL。 | 高 |
| 5 | P1 | 中文文案 / 术语口径 | 新页面把内部枚举和错误码直接暴露给用户，未满足“全部用户可见中文”，且与既有中文状态口径不一致。 | `web/index.html:92,98` 显示 `eligible`、`APPROACH`；`web/app.js:428-429,453,457,495` 显示原始 regime、来源键及降级码；`web/overview.js:168` 原样显示不可用原因。当前实跑输出：`overview.unavailable=[…'4h_ohlcv_missing']`；JPM 详情为 `range`、`['pivot_high','prev_week_hi','range_hi']`、`['regime_1d_missing']`。 | 建立集中式中文映射：`range→震荡`、`pivot_high→结构前高`、`prev_week_hi→前周高`、`range_hi→区间上沿`、`regime_1d_missing→1d 状态缺失`；标题改为“政策适用”“来向路径”，内部码只留在 API/日志。 | 高 |

## 自查盲区

- `127.0.0.1:8787` 当前未监听，浏览器运行环境也返回无可用浏览器；遵守“不得重启进程”，未能进行真实桌面/移动端截图、悬停和点击 QA。
- 心跳仅验证了当前 payload 的 5 条管线及两页共同使用 `.hb/.lane` 样式契约，未能现场验证 tooltip 和响应式折行。
- XSS 采用静态数据流审查：overview 全部服务端文本使用 `textContent`/DOM 节点，新增详情 policy 的 `innerHTML` 动态字段均经过 `esc()`；未发现新增遗漏，但无法做浏览器内恶意 payload 注入验证。