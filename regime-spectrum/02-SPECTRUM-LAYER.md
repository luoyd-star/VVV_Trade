# 02 · 光谱层:从硬标签到隶属度向量

> 可选层。核心方法(01)输出一个硬标签;本层把同样的四个轴转成**五态隶属度向量 μ⃗**,得到状态的连续坐标与切换检测能力。
>
> **设计不变量**:在阈值处 `argmax(μ) ≡ classify(...)`。光谱层只在边界附近增加分辨率,不改变已验证内核的判断。移植时应把这条写成测试。

## 为什么要这一层

硬标签有两个问题:

1. **边界抖动**:`chop_freq` 在 8.19 和 8.21 之间抖,标签就在 swing/choppy 之间跳,但市场并没有变;
2. **丢失"正在切换"的信息**:一段路径可能 60% 像趋势、35% 像波段——硬标签只报"趋势",而"隶属度接近"这件事本身是有价值的信号(状态即将切换)。

隶属度向量解决这两点:它给每个状态一个 0~1 的归属值,并用 **margin(第一名与第二名之差)** 度量定位置信度。

## 五个状态(命名与 01 的对应)

| 光谱层 | 核心层 | 说明 |
|---|---|---|
| `trend_up` | trend_up | 同 |
| `trend_down` | trend_down | 同 |
| `range` | swing | 有序慢摆动 |
| `noisy` | choppy | 快噪声 |
| `quiet` | quiet | 窄幅 |

## 算法

### 1. 斜坡隶属元

把每个硬阈值替换成一段线性缓坡:

```python
def ramp(x, lo, hi):
    """x ≤ lo → 0;x ≥ hi → 1;之间线性。非有限值 → 0。"""
    if not isfinite(x): return 0.0
    if hi == lo:        return 1.0 if x >= hi else 0.0
    return min(1.0, max(0.0, (x - lo) / (hi - lo)))

def knee(t, frac=0.2):
    """阈值 t 的 ±20% 缓坡区间。"""
    return t * (1 - frac), t * (1 + frac)
```

`frac=0.2` 意味着缓坡宽度是阈值的 ±20%。**这不是自由参数**——它锚定在已验证的硬阈值上,只决定过渡带宽度,不移动决策边界(中点仍是原阈值)。

### 2. 各轴的隶属元

```python
snr_hi   = ramp(snr, *knee(T_SNR))          # 趋势位移主导度够
snr_lo   = 1 - snr_hi

tau_lo, tau_hi = knee(T_TAU)
tau_pos  = ramp( tau, tau_lo, tau_hi)       # 向上且干净
tau_neg  = ramp(-tau, tau_lo, tau_hi)       # 向下且干净

amp_lo, amp_hi = knee(t_amp)                # ← 用校准后的每标的 t_amp
amp_big   = ramp(amp, amp_lo, amp_hi)
amp_small = 1 - amp_big

freq_lo, freq_hi = knee(T_FREQ)
freq_fast = ramp(freq, freq_lo, freq_hi)    # 穿越快 = 噪声
freq_slow = 1 - freq_fast                   # 穿越慢 = 有序
```

### 3. 合成五态隶属度

```python
def G(*xs):                                  # 加权几何平均("合议"语义:任一元趋 0 则整体塌陷)
    xs = [max(1e-6, x) for x in xs]
    return exp(mean(log(xs)))

trend     = min(snr_hi, max(tau_pos, tau_neg))   # 对应 classify 的 AND
non_trend = 1 - trend

mu_up    = G(snr_hi, tau_pos) if tau_pos >= tau_neg else 0.0
mu_dn    = G(snr_hi, tau_neg) if tau_neg >  tau_pos else 0.0
mu_range = non_trend * amp_big   * freq_slow
mu_noisy = non_trend * amp_big   * freq_fast
mu_quiet = non_trend * amp_small

# 归一到和 = 1(是"相对定位",不是概率)
s   = sum(mu) or 1.0
mun = {k: v/s for k, v in mu.items()}
```

**为什么趋势用几何平均而其他用乘积**:趋势是"两个条件都要满足"的合议(几何平均对短板敏感,与 AND 语义一致);非趋势三态是同一个 `non_trend` 池按振幅/频率做的**互斥切分**(乘积形式保证三者之和 = non_trend)。

### 4. 派生量

```python
ranked = sorted(mun.items(), key=lambda kv: -kv[1])
top, second = ranked[0], ranked[1]
margin = top[1] - second[1]        # 定位置信度:小 = 正在两态之间滑动
```

**quality(质量标记)**——从非趋势成分判,用于风控:

```python
q = "normal"
if   mun["noisy"] >= max(mun["range"], mun["quiet"]) and mun["noisy"] > 0.15: q = "noisy"
elif mun["quiet"] >= max(mun["range"], mun["noisy"]) and mun["quiet"] > 0.15: q = "thin"
```

- `noisy` = 高频噪声成分显著 → 技术位不被尊重,建议回避;
- `thin` = 窄幅成分显著 → 流动性/波动不足;
- `normal` = 其余。

注意 `quality` **独立于主标签**:一段被判为 `trend_up` 的路径也可能带 `noisy` 质量标记(趋势夹杂高频噪声),这是有用的二级信息。

## 可选的二级证据轴(均线排列)

若你的系统里有均线,可以加两个**只做展示与加成、不改主判**的轴:

```python
# align ∈[-1,1]:EMA(21/55/100/200) 与周期序的负 Kendall τ
#   +1 = 完美多头排列(短期均线在最上),−1 = 完美空头排列,0 = 缠绕
emas  = [EMA(close, p)[-1] for p in (21, 55, 100, 200)]
kt, _ = kendalltau(emas, [1, 2, 3, 4])
align = -kt

# fan:开扇度%,缠绕 ≈ 0
fan = (max(emas) - min(emas)) / mean(emas) * 100
```

用法(**严格限制为加成**):

```python
al_up = ramp(align, 0.0, ALIGN_HI)          # ALIGN_HI = 0.8
mu_up = G(snr_hi, tau_pos) * (1.0 + 0.25 * al_up)
```

⚠️ **这里有一个我们踩过的坑**:最初写成 `mu_up = G(...) * (0.75 + 0.25*al_up)`,即把 align 当权重。后果是——当 align 缺失(均线未热身)时,它会**阻尼** trend 隶属度,导致在 `classify` 明确判 trend 的区域里 `argmax(μ)` 输出 range/quiet,**破坏了"阈值处复现 classify"的不变量**。修正方式:写成 `(1.0 + 0.25*al_up)` 的纯加成——缺失时乘 1.0,行为不变。

**移植建议**:如果你没有充分理由,**先不要加 align/fan**。四轴层已经自足;二级证据的拐点(ALIGN_HI=0.8、FAN_KNEE=3.5)在我们这边也是未校准的经验值。

## 不变量测试(必写)

```python
# 在阈值明确区(远离缓坡)随机采样 N 组 (tau, snr, amp, freq)
for _ in range(1000):
    label = classify(tau, snr, amp, freq, t_amp=t_amp)
    mu    = membership({...})
    top   = argmax(mu)
    assert MAP[top] == label      # MAP: range→swing, noisy→choppy, 其余同名
```

允许在缓坡带内(阈值 ±20%)出现分歧——那正是光谱层增加分辨率的地方。但**远离缓坡时必须 100% 一致**。

## 输出示例

```json
{
  "axes":   {"tau": -0.695, "snr": 6.94, "amp": 9.365, "freq": 12.67, "align": null, "fan": null},
  "mu":     {"trend_up": 0.0, "trend_down": 1.0, "range": 0.0, "noisy": 0.0, "quiet": 0.0},
  "quality": "normal",
  "margin":  1.0,
  "top":     "trend_down",
  "second":  "trend_up"
}
```

`margin = 1.0` 表示定位极其明确(纯下行趋势);实践中 `margin < 0.15` 值得标注"正在切换"。
