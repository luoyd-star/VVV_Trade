"""VVVhermes：嵌在面板旁的研究助手，后端可插拔。（更名自 Hermes，与用户本机同名工具区分）

- provider 可选：anthropic（官方 SDK）/ openai（OpenAI 兼容接口）/ ollama（本地）/
  codex（本机官方 Codex CLI——用其订阅登录，本项目不经手任何 token）/
  mock（无模型自检，回显注入的面板上下文）。
- 配置在项目根 agent.json（已 gitignore；参考 agent.example.json）。
- API key 建议放环境变量，配置里用 api_key_env 指定变量名；不要把 key 提交进仓库。
- 每次对话自动把面板当前数据渲染成 <panel> 上下文注入 system——Hermes 因此"读得到面板"。
"""
from __future__ import annotations

import glob
import json
import math
import os
import subprocess
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "agent.json")

DEFAULTS = {
    "provider": "mock",
    "model": "claude-opus-5",
    "base_url": "",
    "api_key": "",
    "api_key_env": "ANTHROPIC_API_KEY",
    "max_tokens": 1024,
}

SYSTEM_PATH = os.path.join(ROOT, "hermes_system.md")

# 用户在 hermes_system.md 里完全掌控 Hermes 的人设与规则（每次提问热读，
# 改完即生效）；文件缺失或为空时才用这个内置默认。
DEFAULT_SYSTEM = """你是 VVVhermes，嵌在 VVV 市场状态面板旁的研究助手。
回答时优先引用注入面板数据中的具体数字。中文回答；工程风格，简洁直接；不确定就说不确定。
"""

CODEX_FALLBACK_BIN = "/Applications/ChatGPT.app/Contents/Resources/codex"

# 面板数据的格式说明属于"机制"，始终随 <panel> 一起注入，不占用用户的提示词文件。
PANEL_LEGEND = """<panel_legend>
下方 <panel> 是面板此刻的实时数据（每次提问自动注入）。字段说明：dir 方向分[-1,1]；
ER% 趋势效率分位；ATR%/BBW% 波动率分位(0-1，挤压需 BBW<0.15 且 ATR<0.30，
高波只看 ATR>0.85)；tilt 近20根多空量差[-1,1]；
cRSI 为周期自适应RSI，带位 0=下带 100=上带（可超界）；conf 是原始逐根判定的子信号一致度，
确认态没有独立置信度；摆动高/低是近端结构边界，突破量分位是突破当根成交量的历史分位；
VWAP偏离以 ATR 为单位，使用币安量窗，仅为展示层指标、不进规则。
DVOL=Deribit 30 天隐含波动率；近端IV=币安期权约 1-3 天口径，插值/单点、到期数与年龄
用于判断读数稳健性；RV30=30 个日收益的年化已实现波动率，RV3=72 个小时收益的年化已实现波动率，
3dIV−RV3 是 1-3 天持仓前端的同期限剪刀差；个股期限3d/9d/30d 是 moomoo ATM 期限曲线。
OI=永续未平仓量（张），OI 分位前的天数是小时样本实际参照跨度，Δ4h/Δ24h 是持仓变化；
Funding 为按品种结算周期的费率（显示值为下期预测、分位对照已结算分布），年化按实际结算周期换算；
Premium=标记价对指数价的基差，Taker买卖比>1 表示主动买盘占优——这组是持仓/杠杆维度。
状态为迟滞确认态（一般 2 根确认、恢复震荡 3 根、高波冲击立即；未来10日财报事件窗内
squeeze→趋势需 3 根）；[原始判定]为未折叠的逐根判定，[酝酿中]为尚未确认的候选切换，
[未收线预览]用形成中的K线试算、只作预警不作确认依据；近期状态翻转只列 4h/1d 最近记录。
加速度=快RV(12根)/慢RV(72根)，>1 表示波动率正在扩张；下行方差占比为近48根中
下跌收益贡献的方差比例，0.5 中性、越高越"跌出来的波动"。
路径几何（影子特征，不参与状态判定）：频率=去趋势残差每100根的均值穿越次数
（低≈慢摆动、高≈快噪声，可区分震荡的"可交易性"），主周期≈一个完整震荡的根数，
τ=Mann-Kendall 秩趋势[-1,1]（与 dir 交叉验证，两者背离时提示结构异常）；
margin=原始逐根判定态到最近可翻转状态边界的距离（<0.15 表示原始树处于边界过渡中，
与滞后确认的"酝酿中"互补；它按原始判定树计算，与当前确认态可能不同）。
这组读数滞后约60根（窗口120的一半），解读时注意。
美股永续专属：ATR%ds=按(小时,是否周末)桶去季节化后的 ATR 分位（影子字段，不参与判定）
——与 ATR% 分歧大说明当前读数主要是时段效应（盘中/盘外/周末）而非真实波动状态变化；
个股IV=该标的自身的聚合隐含波动率（moomoo 口径，非严格常数30天；2023-06 起约 3.1 年史），
分位优先使用 504 日同财报状态条件分位（rank_kind=cond），样本不足回退 252 日原始分位
（rank_kind=raw）——绝对值高低跨品种不可比，分位才可比；
指数IV=CBOE 板块指数（VXN=纳指100 / VIX），仅作长周期锚，**不与个股IV混算分位**
（两者口径不同源）；指数IV或期限比后的 ° 表示盘中延迟报价、尚未结算确权；
VRP=IV−HV（同源同口径的方差风险溢价）分离"贵"与"波动大"——
低ATR分位+高VRP=脆弱的安静（市场在买保险），高ATR分位+负VRP=已实现波动已超期权定价；
期限结构双速：9D/30D 抓急性冲击、30D/3M 抓制度切换，>1 为倒挂（已知失败模式：
能抓 2008/2020 式冲击、会错过 2022 式慢跌）；⚑财报标记表示该分位含日程驱动成分。
数据健康警告列出已知受影响读数；采集心跳固定列五路 lane，正常/迟滞/断流/盘外是各管线状态，
age 是最后落库年龄（OpenD 为网关探活、无落库年龄）；采集器间隔/错误数是整体循环摘要。
</panel_legend>"""


def system_is_custom() -> bool:
    try:
        with open(SYSTEM_PATH, encoding="utf-8") as f:
            return bool(f.read().strip())
    except OSError:
        return False


def load_system() -> str:
    try:
        with open(SYSTEM_PATH, encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    return DEFAULT_SYSTEM


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                loaded = json.load(f) or {}
            cfg.update({k: v for k, v in loaded.items() if not k.startswith("_")})
        except Exception as e:  # noqa: BLE001
            cfg["_config_error"] = str(e)
    return cfg


def _api_key(cfg: dict) -> str:
    if cfg.get("api_key"):
        return cfg["api_key"]
    env = cfg.get("api_key_env")
    return os.environ.get(env, "") if env else ""


def _pathgeom_str(
    f: dict,
    *,
    raw_state: str | None = None,
    confirmed_state: str | None = None,
) -> str:
    pg = f.get("pathgeom") or {}
    mg = f.get("margin") or {}
    parts = []
    if pg.get("chop_freq") is not None:
        dp = f" 主周期≈{pg['dom_period']}根" if pg.get("dom_period") is not None else ""
        parts.append(f"频率={pg['chop_freq']}/100根{dp} τ={pg.get('kendall_tau')}")
    if mg.get("margin") is not None:
        warn = "（<0.15 原始树边界过渡中）" if mg["margin"] < 0.15 else ""
        basis = "（相对原始态）"
        if raw_state and confirmed_state and raw_state != confirmed_state:
            basis = f"；margin 相对原始态 {raw_state} 而非当前确认态 {confirmed_state}"
        parts.append(f"margin={mg['margin']}({mg.get('nearest')}){warn}{basis}")
    return " ".join(parts) if parts else "路径几何=暂无(历史<120根)"


def render_context(p: dict) -> str:
    """把 build_dashboard 的载荷压缩成给模型看的文本。"""
    lines = [f"品种: {p.get('symbol')}（时间均为 UTC，K线均已收盘）"]
    inst = p.get("instrument") or {}
    if inst.get("class") == "us_stock_perp":
        name = inst.get("display") or ""
        if inst.get("market_open"):
            lines.append(
                f"品种类型: 美股永续（标的 {name} 盘中——已按 NYSE 假日/半日市日历校验）"
            )
        else:
            lines.append(
                f"品种类型: 美股永续（标的 {name} 正股休市中）——合约 24/7 交易但休市期波动塌陷，"
                "低波/挤压读数可能是休市假象而非真实蓄势，解读时必须声明这一点。"
            )
    issues = ((p.get("health") or {}).get("issues")) or []
    if issues:
        lines.append("⚠ 数据健康警告: " + "；".join(issues) + "——受影响周期的读数不可全信，回答时必须声明。")
    for tf, t in (p.get("tfs") or {}).items():
        f = t["features"]
        s, v, vol = f["structure"], f["volatility"], f["volume"]
        c = (t.get("crsi") or {}).get("last") or {}
        b = vol.get("breakout")
        cand = t.get("candidate")
        raw = t.get("raw_state")
        confirmed = t.get("state")
        states_map = p.get("states_map") or {}
        raw_label = states_map.get(raw, raw)
        confirmed_label = t.get("state_label") or states_map.get(confirmed, confirmed)
        raw_desc = (f"{raw_label}({raw})" if raw and raw_label != raw else raw)
        confirmed_desc = (
            f"{confirmed_label}({confirmed})"
            if confirmed and confirmed_label != confirmed else confirmed
        )
        parts = [
            f"[{tf}] 状态={t['state_label']}(原始判定conf {t['confidence']:.2f}——确认态无独立置信度)"
            + (f"[原始判定={raw}]" if raw and raw != t.get("state") else "")
            + (f"[酝酿中:{cand['state']} {cand['count']}/{cand['need']}"
               + ("（事件窗:未来10日有财报,确认门槛+1根）" if cand.get("gated")
                  else ("（事件窗内,此转换不受门槛）" if cand.get("event_win") else ""))
               + "]" if cand else "")
            + (
                f"[未收线预览:{t['preview']['state']}(conf {t['preview']['confidence']:.2f})]"
                if t.get("preview") else ""
            ),
            f"dir={s['direction']:+.2f} ER%={f['er_rank']:.2f}",
            f"ATR%={v['atr_rank']:.2f}"
            + (
                f"(去季节化{v['atr_rank_ds']:.2f})"
                if v.get("atr_rank_ds") is not None else ""
            )
            + f" BBW%={v['bbw_rank']:.2f} RV年化={v['rv30_annual_pct']}%",
            f"加速度={v.get('vol_accel')}(分位{v.get('vol_accel_rank')}) 下行方差占比={v.get('downside_share')}",
            _pathgeom_str(
                f,
                raw_state=raw_desc,
                confirmed_state=confirmed_desc if raw != confirmed else None,
            ),
            f"tilt={vol['updown_tilt_20']:+.2f}",
            f"摆动高/低={s['swing_high']}/{s['swing_low']}",
            f"cRSI={c.get('crsi')} 带位={c.get('pos')}% {c.get('zone') or ''}",
        ]
        vw = t.get("vwap") or {}
        if vw.get("dev") is not None:
            parts.append(
                f"VWAP偏离={vw['dev']:+.2f}ATR(币安量{vw.get('win_hours')}h窗"
                + (f",分位{vw['dev_rank']:.2f}" if vw.get("dev_rank") is not None else "")
                + ")——展示层指标,未进规则")
        if b:
            parts.append(f"突破{'↑' if b['dir'] == 'up' else '↓'}量分位{b['vol_rank']}")
        lines.append(" ".join(parts))
    dv = p.get("dvol")
    if dv:
        dvol_parts = []
        if dv.get("iv_last") is not None:
            dvol_txt = f"DVOL隐含={dv['iv_last']}"
            if dv.get("iv_rank") is not None:
                dvol_txt += f"(一年分位{dv['iv_rank']:.3f})"
            dvol_parts.append(dvol_txt)
        xopt = dv.get("xopt") or {}
        if xopt.get("iv") is not None:
            method = "插值" if xopt.get("method") == "interp" else "单点"
            xmeta = f"~{xopt['tenor_days']:.1f}d·{method}"
            if xopt.get("n_expiries") is not None:
                xmeta += f"·{xopt['n_expiries']}到期"
            if xopt.get("age_min") is not None:
                xmeta += f"·{xopt['age_min']:.0f}分前"
            dvol_parts.append(f"近端IV={xopt['iv']:.1f}({xmeta})")
        if dv.get("rv_last") is not None:
            dvol_parts.append(f"RV30={dv['rv_last']}")
        if dv.get("spread") is not None:
            dvol_parts.append(f"IV−RV={dv['spread']:+.1f}pt")
        # 3d 对照层（持仓前端）：近端IV−RV3 是"这笔 1-3 天仓的保险贵不贵"
        if dv.get("rv3_last") is not None:
            dvol_parts.append(f"RV3={dv['rv3_last']}")
        if dv.get("spread3") is not None:
            dvol_parts.append(f"3dIV−RV3={dv['spread3']:+.1f}pt(持仓期限口径)")
        if dvol_parts:
            lines.append("波动率: " + " ".join(dvol_parts))
    uv = p.get("usvol")
    if uv:
        # 个股 IV 主线（moomoo 3.1 年史，分位可用）；无回填时才退回 CBOE 短史影子值
        _iv = uv.get("iv")
        if _iv:
            _ed = _iv.get("earnings_days")
            _etxt = ("" if _ed is None else
                     f",⚑财报{'还有' if _ed > 0 else '已过'}{abs(_ed)}日"
                     f"(该分位含事件成分,非市场压力)" if _ed else ",⚑今日财报")
            # 分位已按"未来30天内有无财报"条件化；与原始差距大时并列给出
            _rr = _iv.get("rank_raw")
            _dv = ("" if _rr is None or _iv.get("rank") is None
                   or abs(_iv["rank"] - _rr) < 0.1 else f",未条件化时为{_rr}")
            _w30 = ",当前处于财报计价窗内(未来30日有财报)" if _iv.get("earn_in30") else ""
            # 盘中实时值单列——它是未结算的滚动值，与结算分位不是同一个量
            _lv = _iv.get("live") or {}
            _ltxt = ""
            if _lv.get("iv") is not None:
                _ltxt = (f" 【实时IV={_lv['iv']}"
                         + (f",今日{_lv['chg']:+.2f}({_lv['chg_pct']:+.1f}%)"
                            if _lv.get("chg") is not None else "")
                         + (f",预览分位{_lv['rank_preview']}"
                            if _lv.get("rank_preview") is not None else "")
                         + ",未结算仅供盘中参考】")
            # 标签跟着 rank_kind 走：cond=同财报状态内(2年窗)，raw=普通252日
            #（条件样本不足回退原始时也不得谎称"同财报状态内"——codex 审计）
            _rlabel = ("同财报状态内分位(2年窗)" if _iv.get("rank_kind") == "cond"
                       else "自身252日分位")
            _asof = _iv.get("asof")
            _asof_txt = (time.strftime("%Y-%m-%d", time.gmtime(_asof / 1000))
                         if isinstance(_asof, (int, float)) else "日期未知")
            _stale_txt = (f"·⚠{_iv['age_days']:.0f}天前"
                          if _iv.get("stale") and _iv.get("age_days") is not None else "")
            iv30_txt = (
                f"个股IV(最近结算{_asof_txt}{_stale_txt})={_iv['last']}"
                + (f"({_rlabel}{_iv['rank']}{_dv}{_w30}{_etxt})"
                   if _iv.get("rank") is not None
                   else f"(样本仅{_iv['n']}日,分位不足{_etxt})")
                + _ltxt
            )
        elif uv.get("iv30_last") is not None:
            iv30_txt = f"个股iv30={uv['iv30_last']}(CBOE自采{uv['iv30_days']}天,历史短勿看分位)"
        else:
            iv30_txt = "个股IV=未回填"
        # VRP=IV−HV（同源同口径）：分离"贵"与"波动大"。低ATR分位+高VRP=脆弱的安静；
        # 高ATR分位+负VRP=已实现波动超过期权定价
        _vrp = ("" if not _iv or _iv.get("vrp") is None else
                f" VRP={_iv['vrp']:+.1f}pt(分位{_iv['vrp_rank']})")
        # 期限结构双速：快端抓急性冲击、慢端抓制度切换。文献已知失败模式——
        # 倒挂能抓 2008/2020 式冲击、会错过 2022 式慢跌——本身即可用信息
        _t = uv.get("term") or {}
        if _t.get("fast") and _t.get("slow"):
            _f, _s = _t["fast"], _t["slow"]
            _fm = "°" if _f.get("settled") is False else ""
            _sm = "°" if _s.get("settled") is False else ""
            _inv = ("(全曲线倒挂:急性且已传导到制度端)" if _t.get("both_inverted")
                    else "(快端倒挂:急性冲击)" if _f["inverted"]
                    else "(慢端倒挂:制度端承压)" if _s["inverted"] else "")
            ts_txt = (f" 期限结构 9D/30D{_fm}={_f['ratio']}(分位{_f['rank']})"
                      f" 30D/3M{_sm}={_s['ratio']}(分位{_s['rank']}){_inv}")
        elif uv.get("ts_ratio") is not None:
            ts_txt = f" 期限结构9D/3M={uv['ts_ratio']}"
        else:
            ts_txt = ""
        # 个股期限曲线（3d/9d/30d ATM）：3d 是 1-3 天持仓窗的直接定价；
        # 3d>30d 倒挂=近期有已知事件或急性压力；无历史暂无分位
        _ts2 = uv.get("term_stock") or {}
        if _ts2.get("iv3") is not None:
            ts_txt += (f" 个股期限3d/9d/30d={_ts2['iv3']}/{_ts2.get('iv9')}/{_ts2.get('iv30')}"
                       + ("(倒挂:近期定价高于制度层)" if _ts2.get("inverted") else ""))
        ts_txt = _vrp + ts_txt
        lines.append(
            ("商品波动率" if uv.get("proxy") else "美股波动率") + ": "
            # 商品无指数锚；iv 来自代理标的（GLD/SLV）须标明
            + (f"{uv['index']}{'°' if uv.get('index_settled') is False else ''}="
               f"{uv['index_last']}(一年分位{uv['index_rank']}) "
               if uv.get("index") else "")
            + f"RV30={uv['rv_last']}"
            # 标签须跟着 spread_src 走：值来自个股 IV 却标"指数IV−RV"会误导读者
            + (f" {'个股' if uv.get('spread_src') == 'stock' else '指数'}IV−RV="
               f"{uv['spread']:+.1f}pt" if uv.get("spread") is not None else "")
            + (f" [IV为代理{uv['proxy']}期权口径(30天)]" if uv.get("proxy") else "")
            + f" {iv30_txt}{ts_txt}"
            + (f" 币安近端IV={uv['xopt']['iv']}(期限~{uv['xopt']['tenor_days']}天,"
               f"24/7报价,与30天口径不可比)" if uv.get("xopt") else "")
            # 3d 对照层（持仓前端）：美股=期限曲线iv3−RV3，商品=近端IV−RV3
            + (f" RV3={uv['rv3_last']}" if uv.get("rv3_last") is not None else "")
            + (f" 3dIV−RV3={uv['spread3']:+.1f}pt(持仓期限口径)"
               if uv.get("spread3") is not None else "")
        )
    dr = p.get("deriv")
    if dr:
        def _f(v, fmt="{:.2f}"):
            return fmt.format(v) if v is not None else "—"
        try:
            _interval = float(dr.get("funding_interval_h"))
            interval_txt = (f"{_interval:g}h"
                            if math.isfinite(_interval) and _interval > 0 else "未知周期")
        except (TypeError, ValueError):
            interval_txt = "未知周期"
        chg4 = _f((math.exp(dr["oi_change_4h"]) - 1) * 100 if dr["oi_change_4h"] is not None else None, "{:+.2f}")
        chg24 = _f((math.exp(dr["oi_change_24h"]) - 1) * 100 if dr["oi_change_24h"] is not None else None, "{:+.2f}")
        oi_days = _f((dr.get("spans") or {}).get("oi"), "{:.1f}")
        lines.append(
            f"持仓(Binance永续): OI={_f(dr['oi'], '{:.0f}')}张(近{oi_days}日小时分位{_f(dr['oi_rank'])}) "
            f"Δ4h={chg4}% Δ24h={chg24}% "
            f"Funding预测={_f(dr['funding_pct'], '{:.4f}')}%/{interval_txt}"
            f"(年化{_f(dr['funding_annual_pct'], '{:.1f}')}%) "
            f"上期结算={_f(dr.get('funding_settled_pct'), '{:.4f}')}%(分位{_f(dr['funding_rank'])}) "
            f"Premium={_f(dr['premium_pct'], '{:.4f}')}%(分位{_f(dr['premium_rank'])}) "
            f"Taker买卖比={_f(dr['taker_ratio'], '{:.3f}')}(分位{_f(dr['taker_rank'])})"
        )
    flips = p.get("flips") or []
    if flips:
        lines.append("近期状态翻转: " + "; ".join(
            f"{x['tf']} {x['from']}→{x['to']}" for x in flips[:6]))
    heartbeat = p.get("heartbeat") or []
    if heartbeat:
        state_labels = {"ok": "正常", "warn": "迟滞", "bad": "断流", "idle": "盘外"}
        hb_parts = []
        for lane in heartbeat:
            age = (f"{lane['age_min']:.1f}分前" if lane.get("age_min") is not None
                   else "无落库年龄")
            state = state_labels.get(lane.get("state"), lane.get("state") or "未知")
            note = f"·{lane['note']}" if lane.get("note") else ""
            hb_parts.append(f"{lane.get('key')}={state}/{age}{note}")
        lines.append("采集心跳(五路): " + "; ".join(hb_parts))
    col = p.get("collector") or {}
    lines.append(
        f"采集器: 间隔{col.get('interval')}s 最近错误{len(col.get('errors') or [])}个"
    )
    return "\n".join(lines)


def system_brief() -> str:
    """自动生成的系统能力简报——Hermes 的系统认知随升级自动更新。

    设计原则：**不做手工能力清单**（会漂移——README 三处过时的教训）。
    每段都取自权威源：版本号=代码常量、品种与数据深度=库、最近升级=git log、
    文档=目录实际列表。任何一段失败都静默省略，简报绝不拖垮对话。
    hermes_system.md 仍完全归用户掌控，本简报独立注入、明确标注机器生成。
    """
    parts = ["<system_brief>（机器生成，每问装配，随升级自动更新）"]
    try:
        from .classify import AUDIT_VERSION, RULES_VERSION
        line = f"版本: 规则 {RULES_VERSION} / 审计快照 {AUDIT_VERSION}"
        try:
            from .backtest import LOCKBOX_START
            line += f"；回测框架已建（锁箱 {LOCKBOX_START} 起前向累积，账本 data/backtest_ledger.sqlite3）"
        except Exception:  # noqa: BLE001
            pass
        parts.append(line)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import storage
        conn = storage.connect_ro()
        try:
            syms = sorted(storage.symbols(conn))
            n_states, = conn.execute("SELECT COUNT(*) FROM regime_history").fetchone()
            spans = conn.execute(
                "SELECT tf, COUNT(*), MIN(ts) FROM ohlcv GROUP BY tf ORDER BY tf"
            ).fetchall()
            span_txt = " ".join(
                f"{tf}:{n}根(最早{time.strftime('%Y-%m', time.gmtime(lo / 1000))})"
                for tf, n, lo in spans)
            n_vol, = conn.execute("SELECT COUNT(*) FROM vol1h").fetchone()
            parts.append(f"品种({len(syms)}): {', '.join(syms)}")
            parts.append(f"数据: {span_txt}；状态行 {n_states}；"
                         f"VWAP量流(币安1h) {n_vol} 行")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        docs = sorted(f for f in os.listdir(os.path.join(ROOT, "docs"))
                      if f.endswith(".md"))
        logs = sorted(os.path.basename(p)
                      for p in glob.glob(os.path.join(ROOT, "SYSTEM_LOG_*.md")))
        parts.append("权威文档: " + ", ".join(logs[-2:]) + "（系统全量审计）；"
                     "docs/: " + ", ".join(docs[-10:]))
    except Exception:  # noqa: BLE001
        pass
    try:
        log = subprocess.check_output(
            ["git", "log", "--oneline", "-8"], text=True, cwd=ROOT, timeout=5)
        parts.append("最近升级（git log 标题行）:\n" + log.strip())
    except Exception:  # noqa: BLE001
        pass
    parts.append(
        "以上及仓库代码/文档才是系统能力的权威描述。你运行在本项目目录的只读沙箱内，"
        "可以直接查阅上述文件（含 SYSTEM_LOG、docs/ 下的回测/实验/预研报告）获取细节；"
        "回答涉及系统能力、版本、数据范围的问题时以此为准，不要凭旧印象。")
    parts.append("</system_brief>")
    return "\n".join(parts)


_TFMS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def overview_brief() -> str:
    """全品种状态概览——修复"站在 AAPL 页看不见 QQQ/SPY"的单品种上下文盲区。

    直接读 regime_history 各序列最新行（审计快照自带特征，零重算），
    每问装配 ~2KB。<panel> 仍是当前品种的全量细节；这里给的是横截面。
    任何失败静默省略，绝不拖垮对话。
    """
    try:
        from . import storage
        conn = storage.connect_ro()
        try:
            rows = conn.execute(
                "SELECT r.symbol, r.tf, r.ts, r.state, r.raw_state, r.confidence,"
                " r.features FROM regime_history r JOIN ("
                "  SELECT symbol, tf, MAX(ts) mts FROM regime_history GROUP BY symbol, tf"
                " ) m ON r.symbol=m.symbol AND r.tf=m.tf AND r.ts=m.mts"
                " ORDER BY r.symbol, r.tf"
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return ""
        now = time.time() * 1000
        by_sym: dict = {}
        for sym, tf, ts, st, raw, conf, feats in rows:
            try:
                f = json.loads(feats) if feats else {}
            except Exception:  # noqa: BLE001
                f = {}
            raw = raw or st
            step = _TFMS.get(tf, 0)
            stale = step and (now - (ts + step)) > 1.5 * step
            bits = st
            if raw != st:
                bits += f"(原始:{raw})"
            bits += f" c{conf:.2f}" if conf is not None else ""
            atr_r = f.get("atr_rank")
            if atr_r is not None:
                bits += f" atr%{atr_r:.2f}"
            if f.get("warmup"):
                bits += " ⚠预热"
            if stale:
                bits += " ⚠陈旧"
            by_sym.setdefault(sym, {})[tf] = bits
        lines = ["<all_symbols_snapshot>（全品种最新确认态横截面，每问装配；",
                 "state(原始:xx)=确认态与原始判定分歧中；c=原始判定conf；atr%=ATR分位）"]
        for sym in sorted(by_sym):
            tfs = by_sym[sym]
            lines.append(sym + " | " + " | ".join(
                f"{tf}: {tfs[tf]}" for tf in ("1d", "4h", "1h") if tf in tfs))
        try:
            # 个股隐波横截面（moomoo 口径，3.1 年史）：让 Hermes 能横向比"谁的隐波
            # 相对自己历史贵/便宜"——这是 VXN 代理时代给不了的。分位为空=样本不足。
            conn2 = storage.connect_ro()
            try:
                ivs = storage.stock_vol_latest_ranks(conn2, "moomoo")
            finally:
                conn2.close()
            if ivs:
                # kind: cond=财报条件分位(与面板同口径)，raw(°标)=普通252日
                seg = " ".join(
                    f"{s.split('-')[0]}:{v:.0f}"
                    + (f"%{r:.2f}{'°' if k == 'raw' else ''}" if r is not None
                       else f"(n{n})")
                    for s, (v, r, n, k) in sorted(ivs.items()))
                lines.append("个股隐波(IV，%后为财报条件分位[2年窗,与面板同口径]，"
                             f"°=无财报品种的252日原始分位；moomoo口径不与VIX混比): {seg}")
        except Exception:  # noqa: BLE001
            pass
        try:
            # 耦合雷达一行摘要（M3 诊断层；运行时导入避免环依赖，失败静默省略）
            from dashboard import coupling_payload
            cp = coupling_payload()
            if cp.get("pairs"):
                seg = " ".join(
                    f"{p['a'].split('-')[0]}×{p['b'].split('-')[0]}:{p['state']}"
                    f"({p['rho_fast']:+.2f})" for p in cp["pairs"])
                lines.append(f"耦合雷达(阈值代 {cp['threshold_version']}，诊断非信号): {seg}")
            for b in cp.get("blocks") or []:
                lines.append(
                    f"块级: {b['block_a']}×{b['block_b']} {b['votes']}/3票 {b['state']}"
                    f"（中位ρ {b['median_rho_slow']}）")
        except Exception:  # noqa: BLE001
            pass
        lines.append(
            "以上仅横截面摘要。当前品种的全量细节在 <panel>。要查其他品种的深度"
            "数据，用只读查询接口（已实测你的沙箱可运行）：\n"
            "  .venv/bin/python scripts/vvvquery.py panel QQQ-USDT   "
            "# 任意品种的全量面板文本（与 <panel> 同口径）\n"
            "  .venv/bin/python scripts/vvvquery.py states BTC-USDT 4h 20  "
            "# 状态+审计特征历史\n"
            "  .venv/bin/python scripts/vvvquery.py sql \"SELECT ...\"  "
            "# 只读 SQL（表：ohlcv/regime_history/deriv/vol1h/usvol/dvol/"
            "stock_vol/ref_daily）\n"
            "  个股隐波历史查 stock_vol，**必须带 source 条件**（口径不同源不可混算）：\n"
            "    SELECT ts,iv,hv FROM stock_vol WHERE symbol='NVDA-USDT' "
            "AND source='moomoo' ORDER BY ts DESC LIMIT 60\n"
            "跨品种对比、历史回看类问题，先跑接口再回答，不要凭横截面猜细节。")
        lines.append("</all_symbols_snapshot>")
        return "\n".join(lines)
    except Exception:  # noqa: BLE001
        return ""


def chat(payload: dict, messages: list) -> dict:
    cfg = load_config()
    context = render_context(payload)
    ov = overview_brief()
    system = (
        load_system() + "\n\n" + system_brief()
        + (("\n\n" + ov) if ov else "")
        + "\n\n" + PANEL_LEGEND + "\n<panel>\n" + context + "\n</panel>"
    )
    msgs = [
        {"role": m["role"], "content": str(m.get("content", ""))[:8000]}
        for m in messages
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ][-20:]
    # 裁剪可能砍在 turn 中间（U1 被裁掉、A1 留下）：开头的孤立 assistant 会让
    # 模型把上一轮回答当成对当前问题的既有立场。掐头到首个 user 为止。
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    if not msgs or msgs[-1]["role"] != "user":
        return {"error": "最后一条消息必须是用户消息"}

    provider = cfg.get("provider", "mock")
    try:
        if provider == "mock":
            reply = _mock(context)
        elif provider == "anthropic":
            reply = _anthropic(cfg, system, msgs)
        elif provider == "openai":
            reply = _openai(cfg, system, msgs)
        elif provider == "ollama":
            reply = _ollama(cfg, system, msgs)
        elif provider == "codex":
            reply = _codex(cfg, system, msgs)
        else:
            return {"error": f"未知 provider: {provider}（可选 anthropic/openai/ollama/codex/mock）"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{provider} 调用失败: {e}"}
    return {"reply": reply, "provider": provider, "model": cfg.get("model", "")}


def _mock(context: str) -> str:
    return (
        "【mock 模式——链路自检】我已读到面板注入的实时上下文：\n\n"
        + context
        + "\n\n要接入真实模型：复制 agent.example.json 为 agent.json，"
        "选择 provider（anthropic / openai 兼容 / ollama）并配置模型与 key，"
        "然后刷新页面即可（provider 可选 anthropic / openai 兼容 / ollama / codex）。"
    )


# codex_args 里绝不允许出现的参数：它们能关掉沙箱、改工作目录或扩大可写范围。
# 聊天后端跑的是被面板数据注入的 prompt，必须钉死在只读沙箱里。
_CODEX_FORBIDDEN = (
    "--dangerously-bypass-approvals-and-sandbox",
    "--full-auto", "--yolo",
    "-s", "--sandbox",
    "-C", "--cd",
    "-a", "--ask-for-approval",
)


def _safe_codex_args(args) -> list:
    """过滤用户 codex_args：拒绝任何能突破只读沙箱或改变工作目录的参数。"""
    out, skip = [], False
    for raw in args:
        a = str(raw)
        if skip:                      # 上一个被拒参数的取值，一并丢弃
            skip = False
            continue
        head = a.split("=", 1)[0]
        if head in _CODEX_FORBIDDEN or head.startswith("--sandbox"):
            skip = "=" not in a and head in ("-s", "--sandbox", "-C", "--cd",
                                             "-a", "--ask-for-approval")
            continue
        out.append(a)
    return out


def _codex_bin(cfg: dict) -> str:
    import shutil

    if cfg.get("codex_bin"):
        return cfg["codex_bin"]
    found = shutil.which("codex")
    if found:
        return found
    if os.path.exists(CODEX_FALLBACK_BIN):
        return CODEX_FALLBACK_BIN
    raise RuntimeError(
        "未找到 codex CLI：请安装并登录 Codex，或在 agent.json 用 codex_bin 指定路径"
    )


def _codex(cfg: dict, system: str, msgs: list) -> str:
    """走本机官方 Codex CLI（codex exec）。

    订阅认证由 CLI 自己完成（~/.codex/auth.json），本项目不读取、不传输任何 token。
    exec 是单轮执行，多轮对话通过把历史内联进提示词实现。
    """
    import subprocess
    import tempfile

    bin_ = _codex_bin(cfg)
    lines = []
    for m in msgs[:-1]:
        lines.append(("用户: " if m["role"] == "user" else "VVVhermes: ") + m["content"])
    prompt = system
    if lines:
        prompt += "\n\n<之前的对话>\n" + "\n".join(lines) + "\n</之前的对话>"
    prompt += "\n\n用户的新问题：" + msgs[-1]["content"] + "\n直接输出给用户的回答文本。"

    fd, out_path = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    cmd = [bin_, "exec", "--skip-git-repo-check", "-o", out_path]
    if cfg.get("model"):
        cmd += ["-m", str(cfg["model"])]
    cmd += _safe_codex_args(cfg.get("codex_args") or [])
    # 只读沙箱：放末尾让同名 -s/--sandbox 以本值为准；但**顺序不足以保证安全**——
    # --dangerously-bypass-approvals-and-sandbox 是独立开关，无论放哪都会关掉沙箱。
    # 这里实现的是显式危险参数拒绝列表；未知参数仍会放行，不是允许参数白名单。
    cmd += ["-s", "read-only"]
    cmd.append(prompt)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=int(cfg.get("timeout_sec", 300)),
            cwd=ROOT,
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip()[-800:]
            raise RuntimeError(f"codex exec 退出码 {proc.returncode}：{tail}")
        text = ""
        try:
            with open(out_path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            pass
        return text or (proc.stdout or "").strip()[-4000:] or "（空回复）"
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _anthropic(cfg: dict, system: str, msgs: list) -> str:
    try:
        import anthropic
    except ImportError as e:
        raise RuntimeError("未安装 anthropic SDK：.venv/bin/pip install anthropic") from e
    kwargs = {}
    key = _api_key(cfg)
    if key:
        kwargs["api_key"] = key
    if cfg.get("base_url"):
        kwargs["base_url"] = cfg["base_url"]
    client = anthropic.Anthropic(timeout=120.0, **kwargs)
    model = cfg.get("model") or "claude-opus-5"
    create = client.messages.create
    extra = {}
    if model.startswith(("claude-opus-5", "claude-fable-5")):
        # 这两档模型带安全分类器，默认开启服务端回退（拒绝时自动换模型重试）
        create = client.beta.messages.create
        extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
    resp = create(
        model=model,
        max_tokens=int(cfg.get("max_tokens", 1024)),
        system=system,
        messages=msgs,
        **extra,
    )
    if resp.stop_reason == "refusal":
        return "（模型的安全分类器拒绝了此请求）"
    return "".join(b.text for b in resp.content if b.type == "text") or "（空回复）"


def _openai(cfg: dict, system: str, msgs: list) -> str:
    base = (cfg.get("base_url") or "https://openrouter.ai/api/v1").rstrip("/")
    # fail closed：openai 分支必须显式配置自己的 key 来源。全局默认的
    # api_key_env 指向 ANTHROPIC_API_KEY——不拦的话，只切 provider 不改配置，
    # Anthropic 的密钥就会被 Bearer 头发给 OpenRouter/任意第三方 base_url。
    if not cfg.get("api_key") and (cfg.get("api_key_env") or "").upper().startswith("ANTHROPIC"):
        raise RuntimeError(
            "openai provider 需要显式配置 api_key 或 api_key_env"
            "（拒绝把 ANTHROPIC_API_KEY 发给非 Anthropic 端点）"
        )
    key = _api_key(cfg)
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": cfg.get("model"),
            "max_tokens": int(cfg.get("max_tokens", 1024)),
            "messages": [{"role": "system", "content": system}] + msgs,
        },
        timeout=int(cfg.get("timeout_sec", 120)),
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _ollama(cfg: dict, system: str, msgs: list) -> str:
    base = (cfg.get("base_url") or "http://127.0.0.1:11434").rstrip("/")
    r = requests.post(
        f"{base}/api/chat",
        json={
            "model": cfg.get("model") or "hermes3",
            "messages": [{"role": "system", "content": system}] + msgs,
            "stream": False,
        },
        timeout=300,
    )
    r.raise_for_status()
    return r.json()["message"]["content"]
