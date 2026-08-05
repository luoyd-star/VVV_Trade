"""结构止损与近端隐含波动的宽度校验。

本模块只消费调用方已经完成的位置测量、关键位和 IV 输入，不读取数据库，也
不改变 policy 剧本。缺少结构位或 IV 时返回 ``None``，不制造替代值。
"""
from __future__ import annotations

import math
from typing import Optional


ZONE_BUFFER_ATR = 0.25          # 起步值，待校准；结构区外预留的失效缓冲
MAX_STOP_DIST_ATR = 3.0         # 起步值，待校准；拒绝与 4h 命中结构明显错配的止损
HOLDING_DAYS_DEFAULT = 3.0      # 起步值，待校准；与 1-3 天持仓前端口径对齐
DAYS_PER_YEAR = 365.0           # 起步值，待校准；IV 年化换算沿用自然日口径
TOO_TIGHT_RATIO = 1.0           # 起步值，待校准；低于一倍预期波动为严重偏紧
TIGHT_RATIO = 1.5               # 起步值，待校准；低于一点五倍预期波动为偏紧


def _finite(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_zone(left: dict, right: dict) -> bool:
    """按价格边界核对 locate 命中的 zone 确实来自调用方的完整列表。"""
    for key in ("lo", "hi"):
        a = _finite(left.get(key))
        b = _finite(right.get(key))
        if a is None or b is None or not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9):
            return False
    return True


def find_structural_stop(location: dict, zones: list, price: float,
                         atr: float, side: str) -> Optional[dict]:
    """找命中关键区之外的结构性失效位。

    多头止损测量在命中支撑区下沿以下 ``ZONE_BUFFER_ATR``；空头反之。位置没有
    命中相应角色、zone 不在完整列表中、止损落在价格错误一侧，或距离超过
    ``MAX_STOP_DIST_ATR`` 时均返回 ``None``。
    """
    if side not in {"long", "short"} or not isinstance(location, dict):
        return None
    if not isinstance(zones, list) or not zones:
        return None
    price_f = _finite(price)
    atr_f = _finite(atr)
    if price_f is None or price_f <= 0 or atr_f is None or atr_f <= 0:
        return None

    expected_at = "at_support" if side == "long" else "at_resistance"
    expected_role = "support" if side == "long" else "resistance"
    if location.get("at") != expected_at or location.get("role") != expected_role:
        return None
    hit = location.get("zone")
    if not isinstance(hit, dict):
        return None
    matched = next(
        (zone for zone in zones if isinstance(zone, dict) and _same_zone(hit, zone)),
        None,
    )
    if matched is None:
        return None

    edge_key = "lo" if side == "long" else "hi"
    edge = _finite(matched.get(edge_key))
    if edge is None:
        return None
    direction = -1.0 if side == "long" else 1.0
    stop_price = edge + direction * ZONE_BUFFER_ATR * atr_f
    stop_dist = abs(price_f - stop_price)
    correct_side = stop_price < price_f if side == "long" else stop_price > price_f
    if not correct_side or stop_price <= 0 or stop_dist / atr_f > MAX_STOP_DIST_ATR:
        return None

    return {
        "side": side,
        "stop_price": float(stop_price),
        "stop_dist_pct": float(stop_dist / price_f * 100.0),
    }


def check_stop_vs_iv(stop_dist_pct: float, iv3: Optional[float],
                     holding_days: float = HOLDING_DAYS_DEFAULT) -> Optional[dict]:
    """比较止损距离与 IV3 隐含的计划持仓期预期波动。"""
    stop_f = _finite(stop_dist_pct)
    iv_f = _finite(iv3)
    days_f = _finite(holding_days)
    if (stop_f is None or stop_f <= 0 or iv_f is None or iv_f <= 0
            or days_f is None or days_f <= 0):
        return None

    expected_move_pct = iv_f * math.sqrt(days_f / DAYS_PER_YEAR)
    if not math.isfinite(expected_move_pct) or expected_move_pct <= 0:
        return None
    ratio = stop_f / expected_move_pct
    if ratio < TOO_TIGHT_RATIO:
        verdict = "too_tight"
        note = "止损在 1 倍预期波动内，正常波动就会打掉"
    elif ratio < TIGHT_RATIO:
        verdict = "tight"
        note = "止损偏紧"
    else:
        verdict = "ok"
        note = "止损宽度相对预期波动充足"
    return {
        "expected_move_pct": float(expected_move_pct),
        "ratio": float(ratio),
        "verdict": verdict,
        "note": note,
    }
