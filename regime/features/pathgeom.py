"""路径几何特征（regime-spectrum 评审采纳 #1/#7）：频率轴 + Kendall τ 交叉轴。

保真细节（对抗校验后的约定，勿随意更改）：
- 去趋势用**一次线性拟合**（带截距 OLS，np.polyfit），不是 EMA——EMA 是滞后
  自适应均线，残差围绕它的穿越率系统性偏高，会使 t_freq≈8.2 初值失准。
  与 regime-spectrum 正文的 Theil-Sen 相比残差形状略有差异（venv 无 scipy 的
  现实取舍），引用其 t_freq 标定值时应知悉此偏差。
- 穿越计数以**残差均值**为轴，且剔除恰好落在轴上的点（regime-spectrum 参考
  实现口径 s=s[s!=0]）。注：带截距 OLS 的残差均值恒≈0，此实现中"均值轴"与
  其 README 说的"零轴"实为同一条轴；保留按均值计算是为对 Theil-Sen 类
  非零均值残差保持口径稳健。
- 窗口 120：高于其文档下限 100（"太短则 τ 与频率估计不稳"），低于推荐窗以保留
  响应速度；读数滞后约 window/2 根，以 lag 字段显式声明（采纳 #4 的理念）。
- 数据不足或含 NaN 时**全部输出 None**（regime-spectrum 05-B1：未知不得静默
  塌成中性/False）。
- Kendall τ 用 numpy 手写（venv 为 Python 3.9 无 scipy）；τ-a 定义（不修平局），
  收盘价平局极少，偏差可忽略。

影子期约定：本模块输出只入审计快照与面板展示，**不参与 classify 判定**；
任何一项进规则之日必须递增 RULES_VERSION 并清空重算涉改品种。
"""
from __future__ import annotations

import numpy as np

WINDOW = 120


def kendall_tau(x) -> float:
    """Mann-Kendall τ-a：S = Σ sign(x_j - x_i), i<j；τ = S / C(n,2)。不修平局。"""
    x = np.asarray(x, dtype=float)
    n = len(x)
    s = 0
    for i in range(n - 1):
        s += int(np.sign(x[i + 1:] - x[i]).sum())
    return s / (n * (n - 1) / 2.0)


def path_features(close, window: int = WINDOW) -> dict:
    """返回 {chop_freq, dom_period, kendall_tau, lag}；历史不足/含 NaN 时值为 None。

    chop_freq：线性去趋势残差对残差均值的穿越次数，按每 100 根归一。
    dom_period：主导周期估计 ≈ 2*window/穿越数（一个完整震荡周期穿越均值两次）。
    kendall_tau：Mann-Kendall 秩趋势 [-1,1]，对插针免疫，与合成 direction 交叉验证。
    lag：本组读数的近似滞后（≈window/2 根）——窗口统计天然滞后，显式声明给下游。
    """
    if window < 100:
        # regime-spectrum 文档下限："太短则 τ 与频率估计不稳"；且过小窗口会触发
        # 除零/退化拟合——这是编程错误而非数据状况，直接拒绝。
        raise ValueError(f"pathgeom window 须 >= 100，收到 {window}")
    out = {"chop_freq": None, "dom_period": None, "kendall_tau": None, "lag": None}
    x = np.asarray(close, dtype=float)
    if len(x) < window:
        return out
    x = x[-window:]
    if not np.isfinite(x).all():
        return out
    n = len(x)

    t = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(t, x, 1)
    resid = x - (slope * t + intercept)
    s = np.sign(resid - resid.mean())
    s = s[s != 0]  # 剔除恰好落轴的点（参考实现口径）
    crossings = int(np.count_nonzero(s[1:] != s[:-1])) if len(s) > 1 else 0
    chop_freq = crossings * 100.0 / n
    dom_period = (2.0 * n / crossings) if crossings > 0 else None

    tau = kendall_tau(x)

    return {
        "chop_freq": round(chop_freq, 2),
        "dom_period": round(dom_period, 1) if dom_period else None,
        "kendall_tau": round(float(tau), 3),
        "lag": window // 2,
    }
