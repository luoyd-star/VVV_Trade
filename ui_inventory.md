# 详情页现有元素清点（监工实测，2026-08-07 视口 1280×900）

**页面总高 3588px ≈ 4 屏。重排时以下元素一个都不许丢。**
（宽×高，top 为文档坐标）

## 顶栏（top 15-50）
hbStrip 188×42 心跳条 · mktBadge 85×38 · fresh 88×38 · countdown 35×31 ·
hermesToggle 94×31 · symSel/symBtn 220×29 品种切换 · clock 67×28 ·
symName 70×15 · symDot 8×8

## Hermes 侧栏（aside#hermes 350×849，top 73）
hermesClear · hermesClose · hermesMeta · hermesSym · hermesChips 349×81 ·
hermesMsgs 349×591 · hermesText 255×54 · hermesSend 54×54

## 结论区（top 89-540）
banner 882×38 · stateCards 882×191 · volRanks 572×66 · alerts 286×158

## 政策判断（top 591-1130）
policyMeta · policySignal · policyConclusion ·
policyZoneTable 848×356 / policyZoneRows ·
policyApproach 247×19 · policyVolNotes 247×56 · policyStop 247×37 ·
policyDegraded 848×8

## 价格结构（top 1180-1942，左 474 宽）
tfPicker 131×24 · priceMeta 331×34 · priceLegend 474×69 · priceChart 474×640

## 3d 波动率（top 1180-1461，右 324 宽）
iv3Meta 229×30 · iv3Metrics 324×0 · iv3Chart 324×236

## 持仓与杠杆（top 1509-1960，右 324 宽）
derivMeta 203×30 · derivInfo 324×159 · derivChart 324×240

## 30d 波动率（top 2009-2305，全宽 848）
dvolMeta 58×15 · dvolMetrics 848×28 · dvolChart 848×236

## 状态时间线（top 2352-2534）
stripLegend 486×15 · strips 848×153

## 翻转表（top 2546-3696）
flipsSummary 848×24 · **flipTable 560×1126（极高）**

## 耦合（top 2616-3678）
coupBox 848×23 · coupMeta 168×15 · coupBody 848×307 · **coupMatrix 848×720**

## 特征明细与采集器（top 2670-3305）
detail 882×45 · **featTable 1461×127（宽度溢出容器 882）** ·
colStat 97×23 · colInfo 200×287 · colCountsSummary · colCountsAll 200×210 ·
colLog 200×210

## ATR/BBW（top 3368-3588，全宽 848）
volRankChart 848×220

## 已知问题
- featTable 宽 1461 > 容器 882 —— 横向溢出
- flipTable 高 1126、coupMatrix 高 720 —— 单表撑掉一屏多
- 波动率被拆两处（3d 在 1180、30d 在 2009），中间夹着持仓卡
- ATR/BBW 是①定状态的证据，却在最底部第 3.7 屏
