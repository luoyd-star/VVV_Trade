# 01 · 核心方法规格

> 本文档自足:只要有 numpy/scipy,照此可完整重建。所有公式给出精确定义,不留"大概这样"的余地。

## 0. 输入与符号

```
y = [y_0, y_1, ..., y_{T-1}]    # 一段收盘价,等间隔,无缺口,升序(y_0 最早)
T = len(y)                       # 窗口长度(根数)
x = [0, 1, ..., T-1]             # 时间序号
```

**周期无关**:y 可以是任何周期的收盘价序列。方法只关心"这 T 根的形状",不关心每根代表多久。

**唯一硬要求**:等间隔且无缺口。若数据有停牌/休市的空洞,先决定是**剔除**那些 bar(让序列在"交易时间轴"上连续)还是**保留**——两种做法都可以,但必须全程一致(校准、生产、回测同一口径)。

**长度建议**:T 至少 100,推荐 150~256。太短则 τ 与频率估计不稳;太长则滞后过大。

---

## 1. 多尺度分解(à-trous 平滑金字塔)

这一步产出**能量谱**,是"光谱"一词的来源。

### 1.1 平滑算子

```python
def smooth(x, w, causal):
    s = pd.Series(x)
    if causal:
        return s.rolling(w, min_periods=1).mean().values          # 尾部对齐(实盘)
    return s.rolling(w, min_periods=1, center=True).mean().values # 居中(复盘,无相位滞后)
```

- `min_periods=1`:序列开头不产生 NaN(用可得的部分做均值)。
- `causal=True` 用于实时/实盘(只用过去信息,有滞后);`causal=False` 用于事后刻画一段已完成路径(无滞后但**含未来信息,绝不能用于实盘**)。

### 1.2 逐层平滑

尺度序列(细→粗):

```
SCALES = (4, 8, 16, 32, 64, 128)
```

```
smooths[0] = y
smooths[k+1] = smooth(smooths[k], SCALES[k], causal)     # 注意:对上一层的结果再平滑(级联)
trend = smooths[-1]                                       # 最粗层 = 大级别漂移
D[k] = smooths[k] − smooths[k+1]                          # 第 k 层的细节/震荡分量
```

**重建恒等式(可用于自检)**:

```
y ≡ trend + Σ_k D[k]        # 精确成立(望远镜求和),与平滑器选择无关
```

实现时应断言 `max|y − (trend + ΣD)| < 1e-9`。这是本方法与"特征表"的根本区别:它是**路径表示**,信息无损。

### 1.3 能量谱

```
E_k     = Σ_t D[k][t]²                         # 第 k 层能量
E_trend = Σ_t (trend[t] − mean(trend))²        # 趋势层能量(去均值,只算波动部分)
total   = Σ_k E_k + E_trend + 1e-12

spectrum = { "4": E_0/total, "8": E_1/total, ..., "128": E_5/total, "trend": E_trend/total }
```

`spectrum` 各项之和 = 1。**读法**:

- `trend` 占比高 → 能量集中在大级别漂移 → 趋势特征;
- 某个细带(4/8)占比高 → 能量在快摆动 → 噪声特征;
- 粗带(32/64/128)占比高 → 慢摆动 → 波段特征。

### 1.4 尺度序列如何按周期调整

`SCALES` 的物理含义 = 平滑窗覆盖的**根数**。若你的周期不同,有两种选择:

| 做法 | 说明 |
|---|---|
| **A. 不动**(推荐起步) | 尺度是相对窗口 T 的,`(4,8,16,32,64,128)` 配 T=192 覆盖了从"很快"到"接近整窗"的完整倍频程。换周期时形状语义不变,只是物理时长跟着变。 |
| B. 按物理时长对齐 | 若你要求"频带对应固定物理时长"(如 1h/4h/1d),按 `scale = 目标时长 / 单根时长` 重算,再取最接近的 2 的幂。**注意:改了尺度就必须重新校准阈值。** |

`COARSE_MIN = 32`:用于区分"慢摆动"与"快噪声"的分界尺度(见 `spectrum_summary`)。同样,改尺度序列时需同步调整。

---

## 2. 四个连续轴

这四个轴是分类的输入,也是比标签更有信息量的输出——**建议下游同时消费连续轴,不要只取标签**。

### 2.1 τ(Mann-Kendall 相关系数)—— 方向有多单调

```python
from scipy import stats
tau, _ = stats.kendalltau(np.arange(T), y)
tau = 0.0 if np.isnan(tau) else float(tau)
```

- 定义:所有点对 (i,j), i<j 中,`y_j > y_i` 的对数减去 `y_j < y_i` 的对数,除以总对数。
- 范围 ∈[−1, 1]。+1 = 严格单调上升(每一步都涨),−1 = 严格单调下降,0 = 无单调倾向。
- **为什么不用端点收益**:端点收益无法区分"一路稳涨 10%"和"暴跌 20% 后暴涨 30% 净涨 10%"。τ 考察全部点对,是**路径主导**的度量。
- **为什么不用线性回归 R²**:τ 是秩相关,对异常值稳健(单根插针不会污染方向判断)。

### 2.2 Theil-Sen 趋势线 → net_pct 与 chop_amp

```python
res = stats.theilslopes(y, x)          # 返回 (slope, intercept, lo_slope, hi_slope)
slope, intercept = res[0], res[1]
tsline = intercept + slope * x         # 稳健趋势线
resid  = y - tsline                    # 围绕趋势线的残差 = "震荡"
pm     = mean(y)                       # 用于归一成百分比

net_pct  = |tsline[-1] − tsline[0]| / pm × 100      # 趋势线净位移(%)
chop_amp = sqrt(mean(resid²)) / pm × 100            # 震荡的 RMS 振幅(%)
```

- **Theil-Sen** = 所有点对斜率的中位数,是稳健回归(击穿点 29%),比 OLS 抗异常值。
- `net_pct` 是"这段路径沿趋势方向走了多远",**用趋势线端点而非价格端点**——同样是为了抗噪。
- `chop_amp` 是"围绕这条趋势线上下摆动多大",单位是价格的百分比,因此**跨标的可比**。
- 除以 `mean(y)` 而不是首值/末值:避免端点异常放大或缩小整段读数。

### 2.3 SNR —— 趋势主导度

```
snr = net_pct / (chop_amp + 1e-9)
```

这是本方法的**核心量**:趋势位移与震荡幅度之比。

- SNR 大 = 走出去的距离远超上下摆的幅度 = 趋势特征明确;
- SNR 小 = 摆动幅度和净位移差不多 = 在原地折腾。
- 它是**无量纲**的,因此原则上跨标的可比(但见 03,实际分布仍有标的差异)。

### 2.4 chop_freq 与 dom_period —— 震荡有多快

```python
def zero_crossings(v):
    s = np.sign(v - v.mean())
    s = s[s != 0]                                   # 剔除恰好等于均值的点
    return int((np.diff(s) != 0).sum()) if len(s) > 1 else 0

nz         = zero_crossings(resid)                  # 残差穿越其均值的次数
chop_freq  = 100 * nz / T                           # 每 100 根穿越次数
dom_period = 2 * T / nz  if nz > 0 else None        # 主导震荡周期(根)
```

- `chop_freq` 归一到"每 100 根",因此**不同窗口长度之间可比**。
- `dom_period ≈ 2T/nz`:一个完整震荡周期包含两次穿越,故乘 2。
- **这个轴是本方法区别于常规方法的关键**:它让"慢波段"和"快噪声"可分——两者的 `chop_amp` 可以完全相同,但一个每 100 根穿越 4 次,一个穿越 20 次,交易含义天差地别。

### 2.5 辅助轴:ER(路径效率)

```
ER = |y[-1] − y[0]| / (Σ|diff(y)| + 1e-12)      # ∈[0,1]
```

净位移 / 总路程。1 = 直线,趋近 0 = 来回折腾。**不参与分类**,但作为展示和交叉验证有用(ER 与 SNR 应大致同向;背离时说明趋势线拟合有异常)。

---

## 3. 分类决策树

```python
def classify(tau, snr, chop_amp, chop_freq,
             t_snr=4.0, t_tau=0.3, t_amp=1.15, t_freq=8.2):
    if snr >= t_snr and abs(tau) >= t_tau:
        return "trend_up" if tau > 0 else "trend_down"
    if chop_amp >= t_amp:
        return "swing" if chop_freq <= t_freq else "choppy"
    return "quiet"
```

### 五个状态的语义

| 状态 | 含义 | 典型交易含义(仅供参考,非建议) |
|---|---|---|
| `trend_up` | 趋势位移压过震荡,方向单调向上 | 顺势环境;逆势均值回复危险 |
| `trend_down` | 同上,向下 | 同上 |
| `swing` | 非趋势,但振幅足够且摆动**慢** | 有序波段;区间两端有意义 |
| `choppy` | 非趋势,振幅足够但摆动**快** | 高频噪声;技术位不被尊重,信噪比低 |
| `quiet` | 非趋势,且振幅小 | 窄幅/低波动;等突破 |

### 决策树的设计逻辑(为什么是这个顺序)

1. **先判趋势,且用 AND**:`SNR ≥ t_snr` **且** `|τ| ≥ t_tau`——位移压过震荡还不够,方向还得单调。这两个条件互补:SNR 防"大幅震荡碰巧端点差很远",τ 防"缓慢但极不规则的漂移"。
2. **非趋势先按振幅分**:振幅太小的一切摆动都无意义(`quiet`),不必再谈频率。
3. **有振幅的才谈频率**:这一层是本方法的核心增量——不把"没趋势"笼统归一类。

### 阈值的地位(必读)

| 阈值 | 默认值 | 性质 |
|---|---|---|
| `t_snr` | 4.0 | 跨标的稳定(SNR 无量纲),**可直接沿用** |
| `t_tau` | 0.3 | 跨标的稳定(τ 有界),**可直接沿用** |
| `t_freq` | 8.2 | 跨标的较稳定(已归一到每 100 根),**可直接沿用** |
| **`t_amp`** | 1.15 | **强标的依赖 + 强周期依赖 —— 必须校准,见 03** |

`t_amp` 是唯一必须校准的参数,原因:`chop_amp` 是"振幅百分比",而不同标的的波动率、不同周期的每根振幅,量级相差可达十倍以上。直接用别人的 `t_amp` 会导致所有标的挤在 `quiet` 或全部挤在 `choppy`。

---

## 4. 完整输出(describe_path)

```python
{
  # 方向/趋势轴
  "dir":        int(sign(tau)),        # -1 / 0 / 1
  "tau":        round(tau, 3),
  "ER":         round(ER, 3),
  "net_pct":    round(net_pct, 2),
  "snr":        round(snr, 2),
  # 震荡轴
  "chop_amp":   round(chop_amp, 3),
  "chop_freq":  round(chop_freq, 2),
  "dom_period": round(dom_period, 1) or None,
  # 路径表示
  "spectrum":   {"4":…, "8":…, …, "trend":…},
  # 薄读数
  "regime":     classify(...)
}
```

### 光谱摘要(可选,便于人读)

```python
def spectrum_summary(spectrum, coarse_min=32):
    det = {int(k): v for k, v in spectrum.items() if k != "trend"}
    det_tot = sum(det.values()) + 1e-12
    return {
      "trend_share":  spectrum.get("trend", 0.0),                 # 趋势带能量占比
      "dom_band":     max(det, key=det.get),                      # 震荡中能量最大的频带
      "coarse_share": sum(v for k,v in det.items() if k >= coarse_min) / det_tot,
                                                                   # 慢摆动占全部震荡的比例
    }
```

`coarse_share` 高 = 震荡以慢摆动为主(接近 swing 性格);低 = 以快噪声为主(接近 choppy 性格)。它与 `chop_freq` 是同一件事的两个视角,可互相印证。

---

## 5. 逐根序列化(生产用法)

单点调用给出"最近 T 根"的画像。要得到**每一根的状态序列**,标准做法:

```python
LOOKBACK = 192          # 窗口长度,按你的周期与需求定
STRIDE   = 2            # 每 STRIDE 根算一次,中间 ffill(describe_path 较贵)
SHIFT    = 1            # 输出整体后移一根

for i in range(LOOKBACK, n, STRIDE):
    y = close[max(0, i-LOOKBACK+1) : i+1]
    d = describe_path(y, causal=True)     # 实盘必须 causal=True
    out[i] = d
out = out.ffill().shift(SHIFT)            # ← 两步都不可省
```

**三条纪律(违反任何一条,结果不可用于实盘):**

1. `causal=True` —— 居中平滑会把未来信息带进当前根;
2. `.shift(1)` —— 第 i 根的读数只能用 ≤ i−1 的信息,否则同根泄漏;
3. 声明滞后 —— 窗口 T 的读数,有效滞后约 **T/2 根**。它描述的是"刚过去这段",不是当下瞬间。

---

## 6. 自检清单(移植后必做)

| 检查 | 期望 |
|---|---|
| 重建恒等式 | `max|y − (trend + ΣD)| < 1e-9` |
| 谱和 | `sum(spectrum.values()) ≈ 1.0` |
| 合成直线(y = 100 + 0.1·x) | `regime = trend_up`,τ ≈ 1.0,chop_amp ≈ 0 |
| 合成正弦(慢,周期 ≈ T/3) | `regime = swing`(振幅够时),chop_freq 小 |
| 合成白噪(高频) | `regime = choppy`(振幅够时),chop_freq 大 |
| 合成常数 + 极小噪声 | `regime = quiet` |
| 随机游走 | 各状态都会出现;**趋势占比应显著低于 50%**,若你的数据上趋势占比 >50%,说明 `t_amp`/`t_snr` 标定有问题 |
| 因果性 | 同一根的 causal 读数,在追加未来 bar 后**不改变** |

`04-REFERENCE-IMPL.py` 内含前六项的可执行版本。
