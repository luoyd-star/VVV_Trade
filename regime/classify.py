"""状态分类：把三组特征合成为一个可读的市场状态。

规则完全显式（无黑盒），所有阈值集中在 THRESHOLDS——这些是先验起点，
之后应该用回测来校准，而不是拍脑袋微调。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .features.pathgeom import path_features
from .features.structure import efficiency_ratio_series, structure_features
from .features.utils import pct_rank
from .features.volatility import volatility_features
from .features.volume import volume_features

BARS_PER_YEAR = {"1h": 24 * 365, "4h": 6 * 365, "1d": 365}

# 特征计算窗口：walk-forward 入库与面板实时展示**必须用同一个值**，
# 否则历史攒够后同一根 bar 会给出两个 atr_rank（分位与去季节化因子都对窗长敏感）。
FEATURE_WINDOW = 400

# 各算子的群延迟（≈窗口一半，单位=根）。原先只写一个硬编码的 15，
# 而实际上 EMA50 斜率 25、vol_accel 慢腿 36、Donchian 50、EMA200 100、
# 分位窗 250→125 全都大于 15——把最慢的一条明写出来，别给出没算过的数。
_OPERATOR_LAG = {
    "er30": 15, "atr14": 7, "bb20": 10, "ema50_slope": 25,
    "downside48": 24, "vol_accel72": 36, "donchian100": 50,
    "ema200": 100, "pct_rank250": 125, "pathgeom120": 60,
}
LAG_BARS = {
    "slowest": max(_OPERATOR_LAG.values()),   # 125（pct_rank 250 的群延迟）
    "by_operator": _OPERATOR_LAG,
    "pathgeom": _OPERATOR_LAG["pathgeom120"],
    "confirm_bars_needed": 3,                 # 恢复震荡需 3 根；额外滞后 = need-1
}

# 规则版本号：THRESHOLDS 或 classify 规则结构一旦改动必须递增——
# 不同版本产生的 regime_history 行混在一起会悄悄污染回测统计。
# v2（2026-08-02）：direction 加权在 EMA200 缺席（<200 根）时改为权重重归一。
#   v1 按 0 计入，|direction| 上限降到 0.90 却仍比同一个 0.30 阈值——
#   同样的结构，短历史品种被系统性压低了趋势判定概率（50.8% 的行受影响）。
#   阈值三件套（0.60/0.30/0.10）与规则树结构未动，仍待回测校准。
RULES_VERSION = "v3"   # v3（2026-08-05）：A1 事件感知确认门槛——事件窗内 squeeze→趋势 +1 根
                       # 测量层 classify() 未变，只动确认层；升版触发全量重算（版本谓词）

# 审计特征集版本：features JSON 的键集或口径一变就递增（规则可以不变）。
# 两个版本都进入 state_ts_set 的跳过谓词：任一不匹配的行视为"缺失"、
# 下一轮被 upsert 原地重算——升版自动重算，不再需要全表 DELETE。
# 代际史：v2 补四列 / v3 pathgeom+margin / v4 atr_ds / v5 时间对齐批 /
#         a6 加 src 键 / a7 direction 重归一改了 dir 口径 + src→src_round 改名 /
#         a8 健康位入库（win/warmup）——回测按数据质量分层的前置
# 契约：features 的**键集或任一字段口径**变化都必须升这里（口径变了不升版，
#       同一个 a6 会同时标识两种语义的 dir——回测按版本分桶就失效了）
AUDIT_VERSION = "a8"

# 分位窗口 250 + 指标暖机 ~30：可用历史少于此，250 根分位的参照期缩水，
# 分位读数系统性偏移。面板的整序列 warmup 角标与逐行审计的 warmup 位共用此值。
WARMUP_BARS = 280

THRESHOLDS = {
    "er_rank_trend": 0.60,    # ER 分位高于此才算"有趋势效率"
    "direction_trend": 0.30,  # 方向分绝对值高于此才认趋势
    "tilt_confirm": 0.10,     # 多空量差站队阈值
}

# 键是历史契约（入库、前端、Hermes 都认它），不动；中文标签只描述规则真正
# 检验过的东西。"high_vol_chop" 的规则只是 ATR 分位 >0.85 且未过趋势判定——
# 它没有证明"无序"（实测该态的 chop_freq 与 range 态只差 2.1%），也没有证明
# "方向未证"（实测 45.2% 的该态行方向腿 |dir|>=0.30 已通过，只是 ER 腿没过）。
# 所以标签只说"趋势条件未齐"——这是规则字面上唯一成立的陈述。
STATES = {
    "trend_up": "趋势上行",
    "trend_down": "趋势下行",
    "range": "震荡",
    "squeeze": "低波动挤压（蓄势）",
    "high_vol_chop": "高波动非趋势（趋势条件未齐）",
}

# squeeze / high_vol 的判定字面量镜像自 features/volatility.py（那边是硬编码），
# 两处必须同步改动。margin 计算需要引用这些边界。
SQUEEZE_BBW = 0.15
SQUEEZE_ATR = 0.30
HIGHVOL_ATR = 0.85


def _boundary_margin(state: str, d: float, er_rank: float, vol: dict) -> dict:
    """到最近**能实际翻转输出**的决策边界的归一化距离（regime-spectrum #2 改造版）。

    分层短路树（trend > squeeze > high_vol > range）下不能对所有边界裸取最小距离
    ——处于 trend 态时 bbw_rank 逼近 0.15 根本改变不了输出。因此按当前态只枚举
    "可达翻转"：AND 条件的进入距离取未满足项的最大缺口（须全部跨越），
    OR 式破坏取最小盈余（任一失守即破）。各轴同为 0-1 量纲（分位轴天然 0-1，
    |direction| 也在 0-1），距离直接可比。margin<0.15 视为"边界过渡中"。
    仅作展示与审计（影子字段），不参与判定。
    """
    ad = abs(d)
    er_t = THRESHOLDS["er_rank_trend"]
    d_t = THRESHOLDS["direction_trend"]
    bbw, atr = vol["bbw_rank"], vol["atr_rank"]
    to_trend = max(max(0.0, er_t - er_rank), max(0.0, d_t - ad))
    if state in ("trend_up", "trend_down"):
        dists = {"exit_trend": min(er_rank - er_t, ad - d_t)}
    elif state == "squeeze":
        dists = {
            "to_trend": to_trend,
            "exit_squeeze": min(SQUEEZE_BBW - bbw, SQUEEZE_ATR - atr),
        }
    elif state == "high_vol_chop":
        dists = {"to_trend": to_trend, "to_range": atr - HIGHVOL_ATR}
    else:  # range
        dists = {
            "to_trend": to_trend,
            "to_squeeze": max(max(0.0, bbw - SQUEEZE_BBW), max(0.0, atr - SQUEEZE_ATR)),
            "to_chop": max(0.0, HIGHVOL_ATR - atr),
        }
    nearest, dist = min(dists.items(), key=lambda kv: kv[1])
    return {"margin": round(max(0.0, float(dist)), 3), "nearest": nearest}


@dataclass
class Regime:
    state: str
    confidence: float  # 0..1，子信号的一致程度
    features: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)  # {"triggered": [...], "unmet": [...]}

    @property
    def label(self) -> str:
        return STATES[self.state]


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def classify(struct: dict, vol: dict, volu: dict, er_rank: float) -> Regime:
    d = struct["direction"]
    trending = (
        er_rank >= THRESHOLDS["er_rank_trend"]
        and abs(d) >= THRESHOLDS["direction_trend"]
    )

    # 逐条规则求值——命中与未满足清单随快照入库，是回测与审计的原料
    checks = {
        f"er_rank>={THRESHOLDS['er_rank_trend']}": er_rank >= THRESHOLDS["er_rank_trend"],
        f"abs(dir)>={THRESHOLDS['direction_trend']}": abs(d) >= THRESHOLDS["direction_trend"],
        "squeeze(bbw<0.15&atr<0.30)": bool(vol["squeeze"]),
        "high_vol(atr>0.85)": bool(vol["high_vol"]),
        f"tilt_confirm(|tilt|>{THRESHOLDS['tilt_confirm']})":
            abs(volu["updown_tilt_20"]) > THRESHOLDS["tilt_confirm"],
    }
    rules = {
        "triggered": [k for k, v in checks.items() if v],
        "unmet": [k for k, v in checks.items() if not v],
    }

    if trending:
        state = "trend_up" if d > 0 else "trend_down"
        tilt = volu["updown_tilt_20"] * (1 if d > 0 else -1)  # 量能是否站在趋势一边
        t = THRESHOLDS["tilt_confirm"]
        vol_confirm = 1.0 if tilt > t else (0.5 if tilt > -t else 0.0)
        conf = _clip01(0.45 * abs(d) + 0.35 * er_rank + 0.20 * vol_confirm)
    elif vol["squeeze"]:
        state = "squeeze"
        conf = _clip01(1.0 - vol["bbw_rank"])
    elif vol["high_vol"]:
        state = "high_vol_chop"
        conf = _clip01(vol["atr_rank"])
    else:
        state = "range"
        conf = _clip01(0.5 * (1.0 - er_rank) + 0.5 * (1.0 - abs(d)))

    features = {"structure": struct, "volatility": vol, "volume": volu}
    try:
        features["margin"] = _boundary_margin(state, d, er_rank, vol)
    except Exception:  # noqa: BLE001  影子字段绝不允许拖垮主判定
        features["margin"] = None
    return Regime(state, round(conf, 2), features, rules=rules)


def _confirm_need(state: str, from_state: str = None, event_win: bool = False) -> int:
    """非对称迟滞：冲击态立即；恢复震荡（≈NORMAL）要 3 根；其余 2 根。

    v3 事件门槛（A1）：**事件窗内（未来 10 日历日有财报）从 squeeze 进趋势态 +1 根**。
    依据：① 外部实证——压缩态+财报窗未来 10 日扩张概率 60.6% vs 21.4%（2.83×，
    15/17 品种同向），事件驱动的扩张多为双向脉冲而非趋势诞生；② 本地诊断
    （2026-08-05，docs/A1_EVENT_GATE_20260805.md）——事件窗内 squeeze→趋势确认的
    10 根失败率 83.8% vs 77.2%（1h）、80% vs 72%（4h），真趋势率 16.2% vs 22.8%，
    方向一致但 z≈0.9 未达显著（样本仅 2 个财报周期）——故门槛取**最小强度 +1 根**，
    属版本化先验；预登记复评：窗内转换样本 ≥100 时由每周复跑重估。
    冲击态（high_vol_chop）不受门槛：对波动冲击的响应速度不能慢。
    """
    if state == "high_vol_chop":
        return 1
    need = 3 if state == "range" else 2
    if (event_win and from_state == "squeeze"
            and state in ("trend_up", "trend_down")):
        need += 1
    return need


def confirm_states(raw_states, event_win=None):
    """把逐根 raw 状态序列折叠成确认态序列（非对称迟滞）。

    event_win：与 raw_states 等长的 bool 列表（该 bar 起 10 日历日内有财报），
    None = 无事件数据（加密/商品），行为与 v2 完全一致。
    返回 (confirmed_list, candidate)：candidate 是序列末端酝酿中的切换
    {"state", "count", "need", "event_win"}，尚未达到确认根数时非 None。
    """
    confirmed = []
    cur = None
    pending, count = None, 0
    last_win = False
    for i, raw in enumerate(raw_states):
        last_win = bool(event_win[i]) if event_win is not None else False
        if cur is None:
            cur = raw
        elif raw == cur:
            pending, count = None, 0
        else:
            if raw == pending:
                count += 1
            else:
                pending, count = raw, 1
            if count >= _confirm_need(raw, from_state=cur, event_win=last_win):
                cur = raw
                pending, count = None, 0
        confirmed.append(cur)
    candidate = None
    if pending is not None:
        candidate = {
            "state": pending, "count": count,
            "need": _confirm_need(pending, from_state=cur, event_win=last_win),
            "event_win": last_win,
        }
    return confirmed, candidate


def rolling_states_missing(
    df,
    timeframe: str,
    existing_ts_ms,
    min_bars: int = 90,
    window: int = FEATURE_WINDOW,
    session_aware: bool = False,
    source: str = None,
):
    """逐根 K 线做 walk-forward 状态分类，只计算 existing_ts_ms 里没有的时间戳。

    每根 K 线的状态只使用该根及之前的数据（窗口截断到最近 window 根），
    没有未来函数，与日后回测的口径一致。
    返回 [(ts_ms, state, confidence, features_json, rules_json, version, audit_version), ...]
    ——特征值与规则命中随行入库（审计化快照），回测无需重算特征、也不会混用规则版本。
    RULES_VERSION 或 AUDIT_VERSION 任一升版，旧行会被 state_ts_set 的谓词判为缺失、
    下一轮自动原地重算（upsert 覆盖）——升版即重算，无需手工迁移。
    """
    if len(df) < min_bars:
        return []
    ts_ms = (df["ts"].astype("int64") // 10**6).tolist()
    out = []
    for i in range(min_bars, len(df) + 1):
        t = ts_ms[i - 1]
        if t in existing_ts_ms:
            continue
        sub = df.iloc[max(0, i - window): i]
        r = analyze_timeframe(sub, timeframe, session_aware=session_aware)
        f = r.features
        audit = {
            "dir": f["structure"]["direction"],
            "pivot": f["structure"]["pivot_dir"],
            "slope": f["structure"]["ema_slope"],
            "dc": f["structure"]["donchian_pos"],
            "er": f["er"],
            "er_rank": f["er_rank"],
            "atr_rank": f["volatility"]["atr_rank"],
            "atr_ds": f["volatility"].get("atr_rank_ds"),
            "bbw_rank": f["volatility"]["bbw_rank"],
            "rv": f["volatility"]["rv30_annual_pct"],
            "accel": f["volatility"]["vol_accel"],
            "dshare": f["volatility"]["downside_share"],
            "tilt": f["volume"]["updown_tilt_20"],
            "volz": f["volume"]["vol_z20"],
            "sq": f["volatility"]["squeeze"],
            "hv": f["volatility"]["high_vol"],
            # 影子字段（regime-spectrum 采纳批次一）：仅记录，不参与判定
            "freq": (f.get("pathgeom") or {}).get("chop_freq"),
            "domp": (f.get("pathgeom") or {}).get("dom_period"),
            "tau": (f.get("pathgeom") or {}).get("kendall_tau"),
            "margin": (f.get("margin") or {}).get("margin"),
            "m_near": (f.get("margin") or {}).get("nearest"),
            "lag": (f.get("lag_bars") or {}).get("slowest"),
            # 本轮采集源。**注意语义边界**：这是写入这一行时 collector 本轮的
            # fetch 来源，不是该状态 400 根计算窗口内每根 K 线的来源——历史重算
            # 时窗口里可能混着旧源的行。作为取证线索够用（ohlcv.source 会被
            # 换源 last-write-wins 覆盖，快照不留就彻底没了），但别当成逐根 provenance。
            "src_round": source,
            # a8 健康位：win = 本行计算窗实际根数（min(i, window)，<250+14 时各
            # 分位的参照期缩水）；warmup = 该行时点可用历史 < WARMUP_BARS。
            # i 以库内序列首根起算——采集端取窗 10000+399 根远超现有任何序列，
            # 所以 i 就是该 ts 时点的真实可用历史；若未来历史超过取窗上限，
            # 最老的行会看到被截断的 i（届时 warmup 恒 False，win 恒满，无害）。
            "win": len(sub),
            "warmup": bool(i < WARMUP_BARS),
        }
        out.append(
            (
                t,
                r.state,
                r.confidence,
                json.dumps(audit, ensure_ascii=False),
                json.dumps(r.rules, ensure_ascii=False),
                RULES_VERSION,
                AUDIT_VERSION,
            )
        )
    return out


def analyze_timeframe(df, timeframe: str, session_aware: bool = False) -> Regime:
    """session_aware=True（美股永续的 1h/4h）时额外计算去季节化 ATR 分位影子字段。"""
    struct = structure_features(df)
    vol = volatility_features(
        df,
        bars_per_year=BARS_PER_YEAR[timeframe],
        session_aware=session_aware and timeframe in ("1h", "4h"),
    )
    volu = volume_features(df)

    er_s = efficiency_ratio_series(df["close"], n=30)
    er_rank = pct_rank(er_s, 250)

    regime = classify(struct, vol, volu, er_rank)
    regime.features["er"] = round(float(er_s.iloc[-1]), 3)
    regime.features["er_rank"] = round(er_rank, 3)
    # 路径几何（影子特征，不参与判定）与滞后声明（regime-spectrum 采纳 #1/#4/#7）
    try:
        regime.features["pathgeom"] = path_features(df["close"])
    except Exception:  # noqa: BLE001  影子字段绝不允许拖垮主判定
        regime.features["pathgeom"] = None
    regime.features["lag_bars"] = LAG_BARS
    return regime
