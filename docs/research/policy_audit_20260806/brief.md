# VVV_Trade policy 决策层专项审查 · 共享简报

审查对象：/Users/luoyingdong/Documents/VVV_Trade（当前目录即仓库根）**新建的 policy 层**。
只读审查。查库用 sqlite3 "file:data/market.db?mode=ro" "..."；不得改任何文件、不得重启进程。

## 这一层是什么
系统原本只能回答"现在是什么市场状态"（regime/结构/波动/持仓），这一层要回答
"**在这个状态该做什么**"——消费 `eric-policy.md`（仓库根，**必读原文**）这份
condition→action 交易政策，在关键位置配合关键信号给出判断建议。

新建代码（本次审查重点）：
- `regime/policy/levels.py`   关键位区间提取（六类来源，区间不是线）
- `regime/policy/location.py` **配合 regime 的位置判定**（同一价格在不同 regime 下语义不同）
- `regime/policy/volnote.py`  波动率与事件标注（policy 原文没有这个维度，是本系统的补充）
- `regime/policy/stopcheck.py` 止损宽度校验（GATE 1 + 3d IV 预期波动比较）
- `dashboard.py` 的 overview_payload / policy 块 / 路由拆分
- `web/overview.html` + `overview.js`（总览主页）、`web/index.html` + `app.js`（详情页政策卡）
- `regime/agent.py` 的 render_overview_context（Hermes 横截面注入）

## 用户裁决（设计的最高依据，与任何文档冲突时以此为准）
1. regime **直接复用现有五态**（trend_up/trend_down/range/squeeze/high_vol_chop），
   不做映射仲裁、不引入 turning_unconfirmed、**不改 classify.py**
2. **关键位置配合 regime**：同一价格在不同 regime 下是不是关键位、是支撑还是压力、
   可不可交易，答案不同
3. **key level 是价格区间不是价格线**
4. **EMA200 是关键位不参与 regime 判断**
5. signals **只做超买超卖，复用 cRSI**
6. **不做回测**（本阶段目标是建立明确的分析流程与判断方法，不是统计验证）
7. **4h 决策 + 1d 底座**（1d 缺失时降级保留 4h 判断，不排除品种）
8. 只处理库内 74 个币安永续合约品种

## 系统铁律
- 判定层与决策层分离：**绝不修改 classify.py 的五态定义或 RULES_VERSION 语义**
- 算不出返回 None，**绝不用 0/0.5/中性值伪装**
- 未收线数据不入判定；预览必须显式标注
- 阈值是版本化先验，须可校准、须标注来源
- **系统不下单**：输出止于"建议 + 理由"，执行由人；文案不得出现指令式措辞
- 时间戳 UTC 毫秒；跨源不混算分位

## 硬性要求
- 每条发现必须可独立复核：file:line / SQL / 实跑命令与输出
- 注释里的数字可能过期——**用今天的代码与库现算来对**
- 宁缺毋滥：没有证据的猜测不写
- **你的每条发现都会被另一名审查者逐条复核**，错误率会被记录
- 中文输出

## 输出格式（严格遵守）
# 路线N：<主题>
## 发现
| # | 严重度(P0/P1/P2) | 类型 | 断言（一句话） | 证据 | 建议修法 | 置信(高/中/低) |
严重度定义：P0=会给出错误的交易判断/违反铁律/policy 严重失真；P1=会误导使用者或维护者；P2=卫生
## 自查盲区
