---
slug: metals-squeeze-failed-release-reversal
title: 贵金属挤压后的失败释放反转
pattern: 4H挤压 → 1H单边冲击 → 4H拒绝跟随 → 1H反向确认 → 结构突破 → 4H趋势展开
aliases: ["贵金属那条", "黄金白银挤压反转", "失败释放"]
event_from: 2026-08-03
event_to: 2026-08-06
symbols: ["XAU-USDT", "XAG-USDT"]
trigger_regimes: ["squeeze", "high_vol_chop"]
trigger_classes: ["commodity"]
evidence_status: 观察性单事件，未完成历史回测
retrospective_path_clarity: HIGH
prospective_trade_edge_evidence: NONE
derivation_timing: post_hoc
status: active
superseded_by:
archive_reason:
created: 2026-08-06
updated: 2026-08-06
---

## 核心经验

> **低波挤压后的第一次释放不一定是真方向。真正值得关注的是：第一次单边冲击很强，却无法建立4H同向趋势；随后1H方向、效率、量流、波动贡献和结构接受共同反转。**
> [INFERRED, HIGH]

不能记成"急跌后抄底"，也不能记成"挤压天然看多"。

## 本次实际路径

1. **4H低波挤压**

   XAU、XAG的4H ATR/BBW降至低分位，方向分接近中性，量流一度偏空。[KNOWN, HIGH]

   含义：即将扩波的风险增加，但方向未知。
   裁决：**WAIT，双向准备。**

2. **1H向下高波冲击**

   8月3日附近：

   - XAU低点 **4027.74**，1H ATR分位 **0.864**、加速度 **1.369**、下行方差占比 **65.1%**、tilt **−0.448**
   - XAG低点 **56.68**，1H ATR分位 **0.880**、加速度 **1.208**、下行方差占比 **76.6%**、tilt **−0.444**

   [KNOWN, HIGH]

   含义：向下释放真实存在，但两者只进入1H高波震荡，**4H没有确认 `trend_down`**。

3. **失败释放候选**

   事件低点停止扩展，价格快速收回，1H逐渐脱离高波冲击，而4H仍未转空。[KNOWN/INFERRED, HIGH]

   含义：下跌可能未获跨周期接受。
   裁决：**仍然WAIT；"没有转空"不等于"已经转多"。**

4. **1H反向趋势证据**

   可复用的早期证据组合：

   - 形成更高低点；
   - `dir ≥ +0.30`
   - ER分位约 `≥0.60`
   - 波动加速度 `>1`
   - `tilt > +0.10`
   - 下行方差占比明显回落
   - 1H原始或确认态转为 `trend_up`

   [FRAME, MED；阈值尚未回测]

   本次XAG较早满足：8月4日11:00的1H `dir=+0.331`、ER **0.656**、加速度 **1.354**、tilt **+0.398**；随后确认 `trend_up`。[KNOWN, HIGH]

5. **结构接受**

   已收1H突破冲击前的4H结构高点，或者突破后回踩守住：

   - XAG关键旧高约 **59.37**
   - XAU关键旧高约 **4089.31**

   [KNOWN, HIGH]

   这是从"反弹候选"升级到"可以检查交易经济性"的关键一步。

6. **4H跨周期确认**

   8月5日附近，两者4H原始状态转为 `trend_up`：

   - XAU：dir **+0.381**、ER **0.712**、加速度 **1.253**、tilt **+0.188**
   - XAG：dir **+0.307**、ER **0.712**、加速度 **1.119**、tilt **+0.268**
   - 下行方差占比均降至约 **32%**

   [KNOWN, HIGH]

   含义：波动主导权由下跌转向上涨，反弹正式升级为跨周期趋势释放。
