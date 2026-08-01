"""
04-REFERENCE-IMPL.py — Regime 光谱识别法 · 单文件参考实现 v1.0

依赖: numpy, pandas, scipy    运行: python 04-REFERENCE-IMPL.py  (跑自检用例)
规格: 见 01-CORE-METHOD.md / 02-SPECTRUM-LAYER.md / 03-CALIBRATION.md

本文件是规格的可执行版本, 与文档逐条对应。周期无关: 输入任意等间隔收盘序列即可。
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from scipy import stats

# ── 常数(01 §1.2, §3) ────────────────────────────────────────────────
SCALES = (4, 8, 16, 32, 64, 128)      # 多尺度分解的平滑窗(细→粗)
COARSE_MIN = 32                        # 粗带分界: ≥此尺度算"慢摆动"
T_SNR, T_TAU, T_AMP, T_FREQ = 4.0, 0.3, 1.15, 8.2   # t_amp 必须按标的校准, 见 03


# ── 第一部分: 多尺度分解(01 §1) ──────────────────────────────────────
def _smooth(x, w, causal):
    s = pd.Series(x)
    if causal:
        return s.rolling(w, min_periods=1).mean().values
    return s.rolling(w, min_periods=1, center=True).mean().values


def multiscale_decompose(y, scales=SCALES, causal=True):
    """à-trous 平滑金字塔。返回 (D, trend, smooths)。
    恒等式: y = trend + sum(D)  —— 精确, 与平滑器无关。"""
    y = np.asarray(y, float)
    smooths = [y]
    for w in scales:
        smooths.append(_smooth(smooths[-1], w, causal))
    trend = smooths[-1]
    D = np.vstack([smooths[k] - smooths[k + 1] for k in range(len(scales))])
    return D, trend, smooths


# ── 第二部分: 四轴 + 分类(01 §2, §3) ─────────────────────────────────
def _mk_tau(y):
    """Mann-Kendall τ: 全点对单调性 ∈[-1,1]。"""
    y = np.asarray(y, float)
    if len(y) < 3:
        return 0.0
    t, _ = stats.kendalltau(np.arange(len(y)), y)
    return 0.0 if np.isnan(t) else float(t)


def _zero_crossings(v):
    """序列穿越自身均值的次数。"""
    s = np.sign(v - v.mean())
    s = s[s != 0]
    return int((np.diff(s) != 0).sum()) if len(s) > 1 else 0


def classify(tau, snr, chop_amp, chop_freq,
             t_snr=T_SNR, t_tau=T_TAU, t_amp=T_AMP, t_freq=T_FREQ) -> str:
    """决策树(01 §3)。t_amp 应传入该标的校准值。"""
    if snr >= t_snr and abs(tau) >= t_tau:
        return "trend_up" if tau > 0 else "trend_down"
    if chop_amp >= t_amp:
        return "swing" if chop_freq <= t_freq else "choppy"
    return "quiet"


def describe_path(y, scales=SCALES, causal=True, t_amp=None) -> dict:
    """核心函数: 一段价格 → 连续多轴画像 + 薄标签。
    causal=True 用于实时(只用过去信息); False 用于事后刻画(含未来信息, 禁用于实盘)。"""
    y = np.asarray(y, float)
    T = len(y)
    x = np.arange(T)
    pm = y.mean()

    # 多尺度能量谱
    D, trend, _ = multiscale_decompose(y, scales, causal)
    e_bands = np.array([np.sum(d ** 2) for d in D])
    e_trend = float(np.sum((trend - trend.mean()) ** 2))
    total = e_bands.sum() + e_trend + 1e-12
    spectrum = {str(s): round(float(e / total), 3) for s, e in zip(scales, e_bands)}
    spectrum["trend"] = round(e_trend / total, 3)

    # 方向/趋势轴
    tau = _mk_tau(y)
    er = abs(y[-1] - y[0]) / (np.abs(np.diff(y)).sum() + 1e-12)
    ts = stats.theilslopes(y, x)                    # (slope, intercept, lo, hi)
    tsline = ts[1] + ts[0] * x
    resid = y - tsline
    net_pct = abs(tsline[-1] - tsline[0]) / pm * 100
    chop_amp = float(np.sqrt(np.mean(resid ** 2)) / pm * 100)
    snr = net_pct / (chop_amp + 1e-9)

    # 震荡轴
    nz = _zero_crossings(resid)
    chop_freq = round(100 * nz / T, 2)
    dom_period = round(2 * T / nz, 1) if nz > 0 else None

    ta = T_AMP if t_amp is None else float(t_amp)
    return {
        "dir": int(np.sign(tau)), "tau": round(tau, 3), "ER": round(float(er), 3),
        "net_pct": round(float(net_pct), 2), "snr": round(float(snr), 2),
        "chop_amp": round(chop_amp, 3), "chop_freq": chop_freq, "dom_period": dom_period,
        "spectrum": spectrum,
        "regime": classify(tau, snr, chop_amp, chop_freq, t_amp=ta),
    }


def spectrum_summary(spectrum, coarse_min=COARSE_MIN) -> dict:
    """能量谱 → 人读摘要(01 §4)。"""
    det = {int(k): v for k, v in spectrum.items() if k != "trend"}
    det_tot = sum(det.values()) + 1e-12
    return {"trend_share": spectrum.get("trend", 0.0),
            "dom_band": max(det, key=det.get) if det else None,
            "coarse_share": round(sum(v for k, v in det.items() if k >= coarse_min) / det_tot, 3)}


# ── 第三部分: 光谱层(02) ─────────────────────────────────────────────
def _ramp(x, lo, hi):
    if not np.isfinite(x):
        return 0.0
    if hi == lo:
        return 1.0 if x >= hi else 0.0
    return float(min(1.0, max(0.0, (x - lo) / (hi - lo))))


def _knee(t, frac=0.2):
    return t * (1 - frac), t * (1 + frac)


def membership(dp, t_amp=None) -> dict:
    """四轴 → 五态隶属度 μ⃗ + margin/quality(02)。
    不变量: 远离缓坡时 argmax(mu) 与 classify() 一致。"""
    tau = float(dp.get("tau", np.nan))
    snr = float(dp.get("snr", np.nan))
    amp = float(dp.get("chop_amp", np.nan))
    freq = float(dp.get("chop_freq", np.nan))

    snr_hi = _ramp(snr, *_knee(T_SNR))
    tau_lo, tau_hi = _knee(T_TAU)
    tau_pos = _ramp(tau, tau_lo, tau_hi)
    tau_neg = _ramp(-tau, tau_lo, tau_hi)
    amp_lo, amp_hi = _knee(T_AMP if t_amp is None else float(t_amp))
    amp_big = _ramp(amp, amp_lo, amp_hi)
    amp_small = 1 - amp_big
    freq_fast = _ramp(freq, *_knee(T_FREQ))
    freq_slow = 1 - freq_fast

    def G(*xs):                                    # 加权几何平均(合议语义)
        xs = [max(1e-6, v) for v in xs]
        return float(np.exp(np.mean(np.log(xs))))

    trend = min(snr_hi, max(tau_pos, tau_neg))
    non_trend = 1 - trend
    mu = {
        "trend_up":   G(snr_hi, tau_pos) if tau_pos >= tau_neg else 0.0,
        "trend_down": G(snr_hi, tau_neg) if tau_neg > tau_pos else 0.0,
        "range":      non_trend * amp_big * freq_slow,
        "noisy":      non_trend * amp_big * freq_fast,
        "quiet":      non_trend * amp_small,
    }
    s = sum(mu.values()) or 1.0
    mun = {k: round(v / s, 4) for k, v in mu.items()}
    ranked = sorted(mun.items(), key=lambda kv: -kv[1])
    q = "normal"
    if mun["noisy"] >= max(mun["range"], mun["quiet"]) and mun["noisy"] > 0.15:
        q = "noisy"
    elif mun["quiet"] >= max(mun["range"], mun["noisy"]) and mun["quiet"] > 0.15:
        q = "thin"
    return {"mu": mun, "top": ranked[0][0], "second": ranked[1][0],
            "margin": round(ranked[0][1] - ranked[1][1], 4), "quality": q}


# ── 第四部分: 校准(03) ───────────────────────────────────────────────
def calibrate_t_amp(closes, window, p_star=0.298, causal=True, min_windows=10):
    """按 03 的分位对齐法标定单个标的的 t_amp。
    closes: 该标的历史收盘(越长越好); window: 必须等于生产 lookback。
    返回 (t_amp 或 None, 诊断 dict)。窗口数不足 → (None, ...) 由调用方回退全局值。"""
    y = np.asarray(closes, float)
    amps = []
    for i in range(0, len(y) - window + 1, window):        # 非重叠
        seg = y[i:i + window]
        x = np.arange(len(seg))
        ts = stats.theilslopes(seg, x)
        resid = seg - (ts[1] + ts[0] * x)
        amps.append(float(np.sqrt(np.mean(resid ** 2)) / seg.mean() * 100))
    diag = {"n_windows": len(amps), "p_star": p_star, "window": window,
            "median_amp": round(float(np.median(amps)), 4) if amps else None}
    if len(amps) < min_windows:
        diag["status"] = "pending_insufficient_history"
        return None, diag
    diag["status"] = "ok"
    return round(float(np.quantile(amps, p_star)), 4), diag


# ── 第五部分: 逐根序列化(01 §5) ──────────────────────────────────────
def continuous_series(df, lookback=192, causal=True, shift=1, stride=2, t_amp=None):
    """逐根连续轴 + regime。df 需含 'close' 列。
    纪律: causal=True + shift(1) 缺一不可(否则同根泄漏, 不可用于实盘)。"""
    close = df["close"].to_numpy(float)
    n = len(df)
    cols = ["tau", "snr", "net_pct", "chop_amp", "chop_freq", "trend_share"]
    rows = np.full((n, len(cols)), np.nan)
    reg = np.array([None] * n, dtype=object)
    for i in range(lookback, n, stride):
        y = close[max(0, i - lookback + 1):i + 1]
        d = describe_path(y, causal=causal, t_amp=t_amp)
        rows[i] = [d["tau"], d["snr"], d["net_pct"], d["chop_amp"], d["chop_freq"],
                   d["spectrum"].get("trend", np.nan)]
        reg[i] = d["regime"]
    out = pd.DataFrame(rows, columns=cols, index=df.index)
    out["regime"] = reg
    out = out.ffill().shift(shift)
    out["lag_bars"] = lookback // 2                        # 声明滞后
    return out


# ── 自检用例(01 §6) ─────────────────────────────────────────────────
def _selftest():
    rng = np.random.default_rng(0)
    T = 200
    t = np.arange(T)
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")

    # 1) 重建恒等式
    y = 100 + np.cumsum(rng.normal(0, 0.5, T))
    D, trend, _ = multiscale_decompose(y, causal=True)
    err = float(np.max(np.abs(y - (trend + D.sum(axis=0)))))
    check("重建恒等式 y = trend + ΣD", err < 1e-9, f"max_err={err:.2e}")

    # 2) 谱和为 1
    d = describe_path(y, causal=True)
    tot = sum(d["spectrum"].values())
    check("能量谱和 ≈ 1", abs(tot - 1.0) < 0.01, f"sum={tot:.3f}")

    # 3) 直线 → trend_up
    d = describe_path(100 + 0.1 * t, causal=True)
    check("直线 → trend_up", d["regime"] == "trend_up", f'τ={d["tau"]}, snr={d["snr"]}')

    # 4) 慢正弦(大振幅) → swing
    y_sw = 100 + 6 * np.sin(2 * np.pi * t / 90)
    d = describe_path(y_sw, causal=True)
    check("慢正弦 → swing", d["regime"] == "swing",
          f'amp={d["chop_amp"]}, freq={d["chop_freq"]}')

    # 5) 快噪声(大振幅) → choppy
    y_ch = 100 + 4 * np.sin(2 * np.pi * t / 6) + rng.normal(0, 1.0, T)
    d = describe_path(y_ch, causal=True)
    check("快噪声 → choppy", d["regime"] == "choppy",
          f'amp={d["chop_amp"]}, freq={d["chop_freq"]}')

    # 6) 极小波动 → quiet
    y_q = 100 + rng.normal(0, 0.02, T)
    d = describe_path(y_q, causal=True)
    check("极小波动 → quiet", d["regime"] == "quiet", f'amp={d["chop_amp"]}')

    # 7) 光谱层不变量: 远离缓坡时 argmax(mu) == classify
    MAP = {"range": "swing", "noisy": "choppy"}
    bad = 0
    for _ in range(500):
        fake = {"tau": rng.uniform(-1, 1), "snr": rng.uniform(0, 12),
                "chop_amp": rng.uniform(0, 5), "chop_freq": rng.uniform(0, 25)}
        # 只测远离所有缓坡(±20%)的样本
        near = (abs(fake["snr"] - T_SNR) < T_SNR * 0.25 or
                abs(abs(fake["tau"]) - T_TAU) < T_TAU * 0.25 or
                abs(fake["chop_amp"] - T_AMP) < T_AMP * 0.25 or
                abs(fake["chop_freq"] - T_FREQ) < T_FREQ * 0.25)
        if near:
            continue
        lab = classify(fake["tau"], fake["snr"], fake["chop_amp"], fake["chop_freq"])
        top = membership(fake)["top"]
        if MAP.get(top, top) != lab:
            bad += 1
    check("光谱层不变量 argmax(μ)==classify", bad == 0, f"分歧={bad}/500")

    # 8) 因果性: 追加未来 bar 不改变历史读数
    base = 100 + np.cumsum(rng.normal(0, 0.4, 300))
    a = describe_path(base[:200], causal=True)
    b = describe_path(base[:200], causal=True)      # 同输入必同输出
    check("确定性(同输入同输出)", a == b)
    win_now = base[100:200]
    ext = np.concatenate([base[:200], rng.normal(100, 5, 50)])
    win_after = ext[100:200]
    check("因果性(历史窗不受未来影响)", np.array_equal(win_now, win_after))

    print("\n自检:", "全部通过 ✓" if ok else "有失败项 ✗")
    return ok


if __name__ == "__main__":
    print("Regime 光谱识别法 · 参考实现自检\n")
    _selftest()
    print("\n示例输出(随机游走 200 根):")
    rng = np.random.default_rng(7)
    y = 100 + np.cumsum(rng.normal(0, 0.6, 200))
    d = describe_path(y, causal=True)
    print("  轴:", {k: d[k] for k in ("tau", "snr", "chop_amp", "chop_freq", "net_pct")})
    print("  谱:", d["spectrum"])
    print("  摘要:", spectrum_summary(d["spectrum"]))
    print("  标签:", d["regime"])
    print("  隶属:", membership(d))
