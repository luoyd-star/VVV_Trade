#!/bin/zsh
# 每周复跑：P0 验收实验 + 梯队一 E1-E4。
# 幂等：数据没变则 experiment_id 不变、账本忽略重复；数据增长则生成新 id。
# 锁箱纪律由代码内置（>=2026-09-01 收线的数据不可见），脚本无须关心。
set -e
cd "$(dirname "$0")/.."
echo "== $(date -u '+%Y-%m-%d %H:%M') UTC 每周复跑 =="
.venv/bin/python scripts/run_backtest_p0.py --note "weekly rerun"
.venv/bin/python scripts/run_tier1_experiments.py
# A1 事件门槛的预登记复评（v3.1；窗内样本 ≥100 时自动给出维持/撤销建议）
.venv/bin/python scripts/exp_event_gate.py
echo "== 完成。报告在 docs/，账本在 data/backtest_ledger.sqlite3 =="
