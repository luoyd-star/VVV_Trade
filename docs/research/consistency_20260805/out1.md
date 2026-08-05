# 路线1：常量与阈值的多处呈现

## 发现

| # | 类型(不一致/死代码/干扰) | 断言（一句话） | 证据A (file:line + 内容) | 证据B (file:line / grep / SQL) | 建议处置 | 置信(高/中/低) |
|---|---|---|---|---|---|---|
| 1 | 不一致 | 前端把 ATR 与 BBW 共用 `<0.15` 挤压、`>0.85` 高波配色，实际规则是 BBW `<0.15`、ATR `<0.30`，且高波只看 ATR。 | `web/app.js:399-417` 注释及实现对六条 ATR/BBW 分位统一用 `val < 0.15`、`val > 0.85`；`web/app.js:648-657` 又在 ATR 曲线上标“挤压 0.15”。 | `regime/features/volatility.py:129-130`：`squeeze = bbw_rank < 0.15 and atr_rank < 0.30`；`high_vol = atr_rank > 0.85`。 | ATR 条使用 0.30 挤压线；BBW 条只显示 0.15，不把 BBW>0.85 着成 high_vol；历史图补 ATR 0.30 线并明确两条轴的规则语义。 | 高 |
| 2 | 干扰 | 最新系统日志称“只有两个阈值真正决定状态”，遗漏了直接决定 squeeze/high_vol 的另外三个分位阈值。 | `SYSTEM_LOG_20260805.md:160`：“只有两个阈值真正决定状态：er_rank 0.60 与 direction 0.30”。 | `regime/features/volatility.py:129-130` 生成两个判态布尔；`regime/classify.py:166-173` 直接按 `vol["squeeze"]`、`vol["high_vol"]` 选择状态。实际共有五个 state 阈值；0.10 才是仅改置信度。 | 改为“两个趋势阈值 + 三个波动状态阈值决定 state；tilt_confirm 只改 confidence”。 | 高 |
| 3 | 不一致 | 实验层声称与生产 `pct_rank` 同口径，却用 `<=` 而生产用严格 `<`。 | `regime/experiments.py:47-51`：docstring 称同口径，计算为 `(x <= x[-1]).mean()`。 | `regime/features/utils.py:8-13`：生产实现为 `(s < s.iloc[-1]).mean()`；无重复值的满 250 窗中实验值固定比生产值高 `1/250=0.004`，有并列值时差更大。 | `_rolling_rank` 改为严格 `<`，或直接复用 `rolling_pct_rank`；现有 E1/E2 报告注明口径并重算。 | 高 |
| 4 | 不一致 | CLI 路径最多用 299 根已收盘数据计算特征，未遵守生产与面板统一的 `FEATURE_WINDOW=400`。 | `regime/classify.py:19-21` 定义并要求 walk-forward 与面板共用 `FEATURE_WINDOW=400`。 | `main.py:62-64` 不传 limit、直接分析返回值；`regime/data.py:372-376` 默认 `limit=300`，`:399` 再丢最后一根。SQL：`SELECT COUNT(*) ... HAVING COUNT(*)>=400` 显示当前 **126/180** 个 symbol×tf 组已有至少 400 根（1h 71、4h 48、1d 7），分叉已是现实而非未来风险。 | `main.py` 显式请求至少 `FEATURE_WINDOW+1` 根，并只把最后 `FEATURE_WINDOW` 根送入分析。 | 高 |
| 5 | 不一致 | README 与 Hermes 的规则说明仍把确认表写成无条件的 2/3/1，遗漏 v3.1 财报窗内 squeeze→trend 的 2→3 例外。 | `README.md:18-20`：“一般状态连续2根”；`regime/agent.py:50-51` 的实时 `PANEL_LEGEND` 同样只写一般2、range3、high-vol1。 | `regime/classify.py:184-202`：`event_win and from_state=="squeeze" and trend` 时 `need += 1`；`collector.py:611-623` 已在生产传入事件窗。 | 从 `_confirm_need` 的规则生成 README/Hermes 文案，至少补“事件窗内 squeeze→trend 需3根”。 | 高 |
| 6 | 不一致 | `calibrated-1` 唯一报告推荐 `elig=0.35`，运行时却采用进入0.40/退出0.35，同时代码还把该报告称为自身推导链。 | `docs/COUPLING_CALIBRATED1_20260804.md:43`：推荐 `elig: 0.35, d_enter: 2.0, delta: 0.35`。 | `regime/coupling_fsm.py:22,29-32` 声称取自该报告并获批；`:61-62` 实际为 `elig_exit=0.35 / elig_enter=0.40`。 | 在报告追加人工裁决记录，明确最终选择 0.40/0.35 及原因；报告的“推荐”不得继续指向未采用组合。 | 高 |
| 7 | 干扰 | README 仍把回测框架列为未完成，并描述已被 p3 废弃的“当期条件分布”目标。 | `README.md:204-205`：`[ ] 回测框架`，主目标为“各状态下的风控背景条件分布”。 | `regime/backtest.py:1-6` 已实现“t 对 t+1..t+H 的预测信息”；`:40-58` 已冻结 HORIZONS、MIN_*、MAX_SHIFTS、FEE_BPS；`:347-418` 是完整实验入口。 | 路线图改为已完成 p3 框架，并逐字同步当前未来窗与门槛。 | 高 |
| 8 | 不一致 | `GAPS_20260805.md` 同时宣告 A1 已完成，又在结尾把 A1 当作尚待执行且要求升到已过期的 v2。 | `docs/GAPS_20260805.md:34`：A1“已完成（RULES_VERSION v3.1）”。 | 同文件 `:69` 仍写“IV进规则层（v2升版），A1一起做”，`:77-79` 又称“若只做一件事：A1…须走v2升版”；`regime/classify.py:44` 当前已是 v3.1。只读 SQL `SELECT version,audit_version,COUNT(*) ...` → 唯一版本桶 `v3.1/a8`，236,830 行。 | 删除已完成任务的旧优先级段；未来升版写“下一 RULES_VERSION”，不要预写已存在的 v2。 | 高 |
| 9 | 死代码 | `YEAR_DAYS=365.0` 定义后全仓无任何读取。 | `regime/binance_opt_iv.py:37`：`YEAR_DAYS = 365.0`。 | 命令：`grep -RIn --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude-dir=.pytest_cache --exclude-dir=__pycache__ 'YEAR_DAYS' .`；输出仅上述定义1行。 | 删除；若原意是期限年化，则应接入公式并补测试，不能保留装饰性常量。 | 高 |
| 10 | 死代码 | 耦合网格的 `REP_PAIRS_IDX=2` 是未接线配置，代表对仍由手写列表决定。 | `scripts/run_coupling_grid.py:38`：`REP_PAIRS_IDX = 2`。 | 命令：`grep -RIn --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude-dir=.pytest_cache --exclude-dir=__pycache__ 'REP_PAIRS_IDX' .`；仅定义1行；实际 `scripts/run_coupling_grid.py:103-106` 直接构造 `reps=[strong, edge]`。 | 删除常量，或让代表对截取/校验逻辑真正消费它。 | 高 |
| 11 | 死代码 | 实验层导入 `_pct` 并标成“口径参考”，但从未调用；它反而掩盖了第3项的真实实现分叉。 | `regime/experiments.py:107`：`from .features.utils import pct_rank as _pct  # noqa: F401 口径参考`。 | 命令：`grep -RInw --exclude-dir=.git --exclude-dir=.venv --exclude-dir=data --exclude-dir=.pytest_cache --exclude-dir=__pycache__ --include='*.py' '_pct' .`；仅该导入1行。 | 删除死导入，并让 `_rolling_rank` 真正复用公共实现。 | 高 |

## 自查盲区

- 未启动 dashboard 做浏览器级视觉验证；前端结论来自静态 JS/HTML 与后端 payload 契约。
- `regime-spectrum/` 按参考材料而非生产规范处理；未逐个审查其独立阈值。
- 裸数字只围绕题目指定常量族做了语义追踪，未穷举所有无名称数值。