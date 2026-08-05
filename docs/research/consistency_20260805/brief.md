# VVV_Trade 一致性与死代码专项审查 · 共享简报

你在只读审查 /Users/luoyingdong/Documents/VVV_Trade（当前目录即仓库根）。

## 系统一句话
规则式市场状态识别系统：74 品种（加密/美股永续/贵金属/国际，币安 fapi 为主源）、
1h/4h/1d 三周期、五状态 + 非对称迟滞确认（RULES_VERSION v3.1 / AUDIT_VERSION a8）、
SQLite 单库（data/market.db，只读连接串 file:data/market.db?mode=ro）、
采集守护 collector.py（每 300s 一轮）+ 面板 dashboard.py（127.0.0.1:8787）+ VVVhermes 助手。
项目四天内经历了大量升级（72 个提交：宇宙 10→38→74、个股 IV 换源 moomoo、
波动率栈四层、v2→v3.1、回测框架、耦合系统暂停），**升级快、文档与旧代码没跟上**，
这正是本次审查的主题。

## 你的任务主题（用户原话）
「系统的一致性问题：对于同一个事情在不同的文件呈现的不太一样。
特别是系统经过了很多次的升级，有一些 dead code 以及干扰内容。」

三类目标，定义如下：
1. **不一致**：同一事实在两处呈现不同——数字、版本号、阈值、行为描述、术语、口径、
   品种数、表结构描述。两处都要给 file:line。
2. **死代码/死物**：未被任何调用方引用的函数/常量/类/分支/文件/CSS 类/DOM id/
   数据库表/列/meta 键/配置项。必须附零引用的证明（你用的 grep 命令与输出摘要）。
3. **干扰内容**：会误导后来读者的东西——过期注释、过期示例、失效 TODO、
   与现实相反的 docstring、陈旧的实测数字。

## 已知清单（先读，别重复报）
`SYSTEM_LOG_20260805.md` 的「文档与代码的漂移」一节已录 11 处（README a7/23项、
ci.yml 六个测试、.gitignore 9MB、structure.py 50.8%、classify.py 2.1%/45.2%、
dashboard 38×38、data.py 兜底描述、stock_iv_term docstring 跨源示例、
features/__init__ 死 __all__、E1 六种估计器、DATA_DEGRADED）。
`docs/GAPS_20260805.md` E 组也录了 dead 项（E12/E15/E16）与 bbo/universe_snapshot 无消费者、
meta 死键 regime_audit_purged/v2..v5、data.py DEFAULT_SOURCES 死常量、web/*.bak。
**这些算已知。你要么找新的，要么对已知项给出更精确/更正的证据——原样复述不算产出。**

## 硬性要求
- 只读。不得写任何文件、不得改库。查库用：sqlite3 "file:data/market.db?mode=ro" "SELECT ..."
- 每条发现必须可独立复核：两侧证据都到 file:line；死代码给 grep 证明；库事实给 SQL。
- 注释里的数字可能过期——**用今天的代码/库现算来对**，别拿一处注释去"证实"另一处注释。
- 宁缺毋滥：没有证据的猜测不写。你写的每条都会被另一名审查者逐条复核，
  错误率会被记录。
- 中文输出。

## 输出格式（严格遵守）
# 路线N：<主题>
## 发现
| # | 类型(不一致/死代码/干扰) | 断言（一句话） | 证据A (file:line + 内容) | 证据B (file:line / grep / SQL) | 建议处置 | 置信(高/中/低) |
## 自查盲区
（你没来得及覆盖或没有把握的区域，1-3 条）
