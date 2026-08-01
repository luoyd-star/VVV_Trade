"""文本报告输出。表格列用英文缩写保证终端对齐，解读用中文。"""
from __future__ import annotations

from .classify import Regime

W = 86


def _fmt_price(p) -> str:
    if p is None:
        return "-"
    if p >= 1000:
        return f"{p:,.0f}"
    if p >= 1:
        return f"{p:,.2f}"
    if p >= 0.01:
        return f"{p:.4f}"
    return f"{p:.10f}".rstrip("0")  # 超低价币显示纯小数，避免科学计数法


def _flags(r: Regime) -> str:
    f = r.features
    out = []
    if f["volatility"]["squeeze"]:
        out.append("SQZ")
    if f["volatility"]["high_vol"]:
        out.append("HV")
    b = f["volume"]["breakout"]
    if b:
        arrow = "BO↑" if b["dir"] == "up" else "BO↓"
        out.append(f"{arrow}(v{b['vol_rank']:.2f})")
    return ",".join(out) or "-"


def interpret(regimes: dict) -> str:
    d1 = regimes.get("1d")
    h4 = regimes.get("4h")
    h1 = regimes.get("1h")
    if not (d1 and h4):
        only = " | ".join(f"{tf} {r.label}" for tf, r in regimes.items())
        return f"{only}。（周期不足，无法做多周期对照）"

    if d1.state.startswith("trend") and d1.state == h4.state and (not h1 or h1.state == d1.state):
        return f"三周期同向（{d1.label}），趋势环境明确，顺势信号质量最高。"
    if d1.state == "trend_up" and h4.state in ("range", "squeeze"):
        return "日线上行趋势 + 4小时整理：典型的趋势中继情景，关注 4 小时区间边界的放量方向选择。"
    if d1.state == "trend_down" and h4.state in ("range", "squeeze"):
        return "日线下行趋势 + 4小时整理：反弹整理或下跌中继，关注区间下沿是否放量失守。"
    if d1.state.startswith("trend") and h4.state.startswith("trend") and d1.state != h4.state:
        return f"日线（{d1.label}）与 4 小时（{h4.label}）方向相反：逆势回调进行中，日线近端摆动点是多空分界。"
    if d1.state == "squeeze":
        return (
            "日线级别波动率挤压：大行情酝酿期（方向未知）。重点盯日线近端摆动高/低的"
            "放量突破，挤压期内小周期信号多为噪音，不宜提前押方向。"
        )
    if d1.state == "range" and h4.state == "squeeze":
        return "日线震荡 + 4小时挤压：波动率蓄势末期，等待放量突破定方向，避免提前押注。"
    if "high_vol_chop" in (d1.state, h4.state):
        return "存在高波动无序状态：常见于顶底转换期，此时降低敞口、等待结构重建，比预测方向更重要。"
    parts = " | ".join(f"{tf} {regimes[tf].label}" for tf in regimes)
    return f"{parts}：以大周期为背景、小周期找位置。"


def _fmt_sources(sources) -> str:
    if isinstance(sources, str):
        return sources
    uniq = sorted(set(sources.values()))
    if len(uniq) == 1:
        return uniq[0]
    return "/".join(f"{tf}:{s}" for tf, s in sources.items())


def render(symbol: str, sources, regimes: dict, dfs: dict, iv: dict = None) -> str:
    ref_tf = "1d" if "1d" in dfs else next(iter(dfs))
    ref = dfs[ref_tf]
    last = float(ref["close"].iloc[-1])
    ts = ref["ts"].iloc[-1].strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "=" * W,
        f" {symbol} · 最新收盘 {_fmt_price(last)}（{ref_tf} @ {ts}，已收线） · 数据源 {_fmt_sources(sources)}",
        "=" * W,
        f" {'TF':<5}{'state':<15}{'conf':>5}{'dir':>7}{'ER%':>7}{'ATR%':>7}{'BBW%':>7}{'tilt':>7}   flags",
    ]
    for tf, r in regimes.items():
        f = r.features
        lines.append(
            f" {tf:<5}{r.state:<15}{r.confidence:>5.2f}"
            f"{f['structure']['direction']:>+7.2f}"
            f"{f['er_rank']:>7.2f}"
            f"{f['volatility']['atr_rank']:>7.2f}"
            f"{f['volatility']['bbw_rank']:>7.2f}"
            f"{f['volume']['updown_tilt_20']:>+7.2f}   {_flags(r)}"
        )
    lines.append("-" * W)
    for tf in ("1d", "4h"):
        if tf in regimes:
            s = regimes[tf].features["structure"]
            v = regimes[tf].features["volatility"]
            lines.append(
                f" {tf} 近端摆动高/低 {_fmt_price(s['swing_high'])} / {_fmt_price(s['swing_low'])}"
                f" · ATR {_fmt_price(v['atr'])}（{v['atr_pct_of_price']}%/根）"
                f" · 30根年化RV≈{v['rv30_annual_pct']}%"
            )
    if iv:
        line = f" 隐含波动率 DVOL {iv['dvol']}（近一年分位 {iv['dvol_rank']:.2f}）"
        if "1d" in regimes:
            rv = regimes["1d"].features["volatility"]["rv30_annual_pct"]
            line += (
                f" · IV−RV {iv['dvol'] - rv:+.1f}pt"
                "（正=期权定价的未来波动高于近期已实现）"
            )
        lines.append(line)
    lines.append("-" * W)
    lines.append(
        " 状态: " + " | ".join(f"{tf} {r.label}({r.confidence:.2f})" for tf, r in regimes.items())
    )
    lines.append(" 解读: " + interpret(regimes))
    lines.append(
        " 说明: dir 方向分[-1,+1] · ER% 趋势效率分位 · ATR%/BBW% 波动率分位 · tilt 近20根多空量差"
    )
    lines.append("")
    return "\n".join(lines)
