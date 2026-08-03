"""VWAP 偏离（币安量源）的窗口对齐与守门测试。

钉四件事：
1. 窗口语义 (close-W, close]：整根 1h 量 bar 落在窗内才计入，边界不越界；
2. 锚不对齐也正确：08:00 日界的 1d bar 在 00:00 锚的 1h 流上聚合无相位问题；
3. 覆盖率守门：窗内缺 1h 超过 20% → NaN（宁缺毋滥，不给部分窗的假值）；
4. quote/volume 精确 VWAP：结果等于手工 Σq/Σv，非典型价近似。
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime.features.vwap import rolling_vwap, vwap_payload  # noqa: E402

H = 3_600_000


def test_window_semantics_and_exact_vwap():
    # 1h 量流：ts=0..9h，每根 volume=1，quote=price（price=100+i → 每小时 VWAP=100+i）
    vts = [i * H for i in range(10)]
    vol = [1.0] * 10
    qvol = [100.0 + i for i in range(10)]
    # 4h bar 收线于 8h：窗 (4h, 8h] 应含 ts=4,5,6,7 的四根（ts=8h 的那根收线在 9h，出窗）
    vw = rolling_vwap([8 * H], vts, vol, qvol, 4)
    assert abs(vw[0] - (104 + 105 + 106 + 107) / 4) < 1e-9
    # 完整覆盖的另一位置：收线 4h → (0,4h] 含 ts=0..3
    vw2 = rolling_vwap([4 * H], vts, vol, qvol, 4)
    assert abs(vw2[0] - (100 + 101 + 102 + 103) / 4) < 1e-9


def test_anchor_mismatch_daily_on_offset_grid():
    # 08:00 锚的 1d bar（收线于次日 08:00），1h 流从 00:00 起连续 72 根
    vts = [i * H for i in range(72)]
    vol = [1.0] * 72
    qvol = [float(i) for i in range(72)]  # 每小时 VWAP=i
    close = 32 * H  # 次日 08:00 收线，窗 24h → (8h, 32h] 含 ts=8..31
    vw = rolling_vwap([close], vts, vol, qvol, 24)
    assert abs(vw[0] - np.mean(range(8, 32))) < 1e-9


def test_coverage_gate():
    # 24h 窗只给 18 根（75% < 80%）→ NaN
    vts = [i * H for i in range(18)]
    vw = rolling_vwap([24 * H], vts, [1.0] * 18, [100.0] * 18, 24)
    assert np.isnan(vw[0])
    # 补到 20 根（83%）→ 有值
    vts2 = [i * H for i in range(20)]
    vw2 = rolling_vwap([24 * H], vts2, [1.0] * 20, [100.0] * 20, 24)
    assert np.isfinite(vw2[0])


def test_dev_and_rank_gate():
    n = 300
    close = np.full(n, 110.0)
    atr = np.full(n, 5.0)
    vwap = np.full(n, 100.0)
    dev, last, rank = vwap_payload(close, atr, vwap)
    assert abs(last - 2.0) < 1e-9  # (110-100)/5
    assert rank is not None
    # 有效样本不足 RANK_MIN → rank 必须是 None，不许用 0.5 伪装中性
    vwap2 = np.full(n, np.nan)
    vwap2[-10:] = 100.0
    _, last2, rank2 = vwap_payload(close, atr, vwap2)
    assert last2 is not None and rank2 is None
