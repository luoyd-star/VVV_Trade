"""regime-aware 关键位置判定。

同一条价格区间只有放进当前 regime 与来向后才有 policy 语义。本模块不产生
交易动作；``tradeable`` 只表示该位置是否是政策承认的候选机会位，信号与风控
门槛仍由后续决策层负责。
"""
from __future__ import annotations

from typing import Optional

import numpy as np


APPROACH_BARS = 3               # 起步值，待校准；识别“回踩至/反弹至”的最短路径窗
AT_ZONE_ATR = 0.50              # 起步值，待校准；离区间边界不超过此距离才算到位

VALID_REGIMES = {"trend_up", "trend_down", "range", "squeeze", "high_vol_chop"}
VALID_APPROACHES = {"from_above", "from_below", None}

_HIGH_KINDS = {"pivot_high", "prev_day_hi", "prev_week_hi"}
_LOW_KINDS = {"pivot_low", "prev_day_lo", "prev_week_lo"}
_FAST_EMAS = {"ema21", "ema55"}
_SLOW_EMAS = {"ema100", "ema200"}


def _finite_float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if np.isfinite(out) else None


def _unavailable(reason: str) -> dict:
    return {
        "at": None,
        "zone": None,
        "dist_atr": None,
        "role": None,
        "meaning": None,
        "tradeable": False,
        "role_flipped": None,
        "reason": reason,
        "degraded": [reason],
    }


def _middle() -> dict:
    return {
        "at": "middle_zone",
        "zone": None,
        "dist_atr": None,
        "role": None,
        "meaning": "middle_zone_veto",
        "tradeable": False,
        "role_flipped": False,
        "reason": "no_regime_key_level_nearby",
        "degraded": [],
    }


def infer_approach(prices, zone: dict, k: int = APPROACH_BARS) -> Optional[str]:
    """从最近 K 根收盘相对区间的净移动识别来向。

    起点必须明确位于区间一侧，终点还必须比起点更接近区间；这样横盘、远离
    关键位或从区间内部开始的路径都不会被冒充成“回踩/反弹”。
    """
    if k < APPROACH_BARS:
        return None
    try:
        values = np.asarray(prices, dtype=float)
    except (TypeError, ValueError):
        return None
    if len(values) < k:
        return None
    values = values[-k:]
    if not np.isfinite(values).all():
        return None
    lo = _finite_float(zone.get("lo") if isinstance(zone, dict) else None)
    hi = _finite_float(zone.get("hi") if isinstance(zone, dict) else None)
    if lo is None or hi is None or lo > hi:
        return None

    first, last = float(values[0]), float(values[-1])
    if first > hi and last < first:
        first_dist = first - hi
        last_dist = 0.0 if lo <= last <= hi else min(abs(last - lo), abs(last - hi))
        return "from_above" if last_dist < first_dist else None
    if first < lo and last > first:
        first_dist = lo - first
        last_dist = 0.0 if lo <= last <= hi else min(abs(last - lo), abs(last - hi))
        return "from_below" if last_dist < first_dist else None
    return None


def _zone_state(price: float, atr_value: float, zone: dict) -> Optional[dict]:
    lo = _finite_float(zone.get("lo"))
    hi = _finite_float(zone.get("hi"))
    if lo is None or hi is None or lo > hi:
        return None
    origin_role = zone.get("origin_role")
    if origin_role not in {"support", "resistance"}:
        return None
    if price > hi:
        role = "support"
        dist = (price - hi) / atr_value
    elif price < lo:
        role = "resistance"
        dist = (lo - price) / atr_value
    else:
        # 正在区间内尚不能宣告穿越，沿用建立时角色。
        role = origin_role
        dist = 0.0
    return {
        "role": role,
        "dist_atr": float(dist),
        "role_flipped": role != origin_role,
    }


def _eligible(regime: str, role: str, kinds: set[str], flipped: bool) -> bool:
    """用户裁决的 regime × 来源表；未列入的来源仍保留为 level，但不冒充机会位。"""
    if regime == "trend_up":
        if role == "support":
            return bool(kinds & (_FAST_EMAS | {"range_lo"}) or (kinds & _HIGH_KINDS and flipped))
        return bool(kinds & _HIGH_KINDS and not flipped)
    if regime == "trend_down":
        if role == "support":
            return bool(kinds & _SLOW_EMAS or (kinds & _LOW_KINDS and not flipped))
        return bool(kinds & (_FAST_EMAS | {"range_hi"}) or (kinds & _LOW_KINDS and flipped))
    if regime in {"range", "squeeze", "high_vol_chop"}:
        expected = "range_lo" if role == "support" else "range_hi"
        return expected in kinds
    return False


def _semantics(regime: str, role: str, kinds: set[str]) -> tuple[str, bool, Optional[str]]:
    if regime == "trend_up":
        if role == "support":
            return "pullback_long_opportunity", True, "from_above"
        return "reduce_long_not_open_short", False, None
    if regime == "trend_down":
        if role == "resistance":
            return "bounce_short_opportunity", True, "from_below"
        if kinds & _SLOW_EMAS:
            return "deep_long_requires_second_confirmation", True, None
        return "prior_low_support_not_tradeable", False, None
    if regime == "range":
        return "range_edge_opportunity", True, None
    if regime == "squeeze":
        return "wait_for_confirmed_breakout", False, None
    return "high_vol_reduce_frequency", False, None


def locate(price: float, atr: float, zones: list, regime: str,
           approach: Optional[str] = None, prices=None) -> dict:
    """判断价格命中的 regime 关键位及其当前角色和政策语义。

    **approach 必须逐 zone 推断**（2026-08-05 试算发现的 API 设计错误）：来向是
    「价格相对**某一个** zone 的移动路径」，而本函数会从多个候选 zone 里自己选一个
    命中——调用方无从预知谁被选中，传一个全局 approach 必然错配。实测后果是
    trend_up 的 23 个品种 tradeable 全为 0（回踩买点被误判 wrong_approach）。

    正确用法：传 ``prices``（收盘序列），本函数对每个候选 zone 各自 infer_approach。
    ``approach`` 参数仅为单 zone 场景与既有测试保留；两者都给时以 ``prices`` 为准。
    """
    price_f = _finite_float(price)
    atr_f = _finite_float(atr)
    if price_f is None:
        return _unavailable("no_price")
    if atr_f is None or atr_f <= 0:
        return _unavailable("no_atr")
    if regime not in VALID_REGIMES:
        return _unavailable("unsupported_regime")
    if zones is None:
        return _unavailable("no_zones")
    if not isinstance(zones, list):
        return _unavailable("invalid_zones")
    if len(zones) == 0:
        return _unavailable("no_zones")
    if prices is None and approach not in VALID_APPROACHES:
        return _unavailable("invalid_approach")

    candidates = []
    valid_zone_n = 0
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        state = _zone_state(price_f, atr_f, zone)
        if state is None:
            continue
        valid_zone_n += 1
        raw_kinds = zone.get("kinds", [])
        if isinstance(raw_kinds, str):
            raw_kinds = [raw_kinds]
        kinds = {str(kind) for kind in raw_kinds}
        if not _eligible(regime, state["role"], kinds, state["role_flipped"]):
            continue
        if state["dist_atr"] <= AT_ZONE_ATR:
            candidates.append((state["dist_atr"], -len(kinds), zone, state, kinds))

    if valid_zone_n == 0:
        return _unavailable("invalid_zones")
    if not candidates:
        return _middle()

    # 等距时优先共振来源更多的 zone；再保留输入顺序，避免无意义抖动。
    _, _, zone, state, kinds = min(candidates, key=lambda item: (item[0], item[1]))
    role = state["role"]
    meaning, tradeable, required_approach = _semantics(regime, role, kinds)
    # 来向针对**被选中的这个 zone** 现算（见 docstring：全局 approach 必然错配）
    zone_approach = infer_approach(prices, zone) if prices is not None else approach
    reason = None
    if tradeable and required_approach is not None and zone_approach != required_approach:
        tradeable = False
        reason = "wrong_approach"

    return {
        "at": "at_support" if role == "support" else "at_resistance",
        "zone": zone,
        "dist_atr": state["dist_atr"],
        "role": role,
        "meaning": meaning,
        "tradeable": tradeable,
        "role_flipped": state["role_flipped"],
        "approach": zone_approach,
        "reason": reason,
        "degraded": [],
    }
