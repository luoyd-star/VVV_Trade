"""VVVhermes：嵌在面板旁的研究助手，后端可插拔。（更名自 Hermes，与用户本机同名工具区分）

- provider 可选：anthropic（官方 SDK）/ openai（OpenAI 兼容接口）/ ollama（本地）/
  codex（本机官方 Codex CLI——用其订阅登录，本项目不经手任何 token）/
  mock（无模型自检，回显注入的面板上下文）。
- 配置在项目根 agent.json（已 gitignore；参考 agent.example.json）。
- API key 建议放环境变量，配置里用 api_key_env 指定变量名；不要把 key 提交进仓库。
- 每次对话自动把面板当前数据渲染成 <panel> 上下文注入 system——Hermes 因此"读得到面板"。
"""
from __future__ import annotations

import json
import math
import os

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
ER% 趋势效率分位；ATR%/BBW% 波动率分位(0-1，低=挤压 高=高波)；tilt 近20根多空量差[-1,1]；
cRSI 为周期自适应RSI，带位 0=下带 100=上带（可超界）；IV=Deribit DVOL 隐含波动率，
RV=已实现波动率，IV−RV 为波动率风险溢价；OI=永续未平仓量（张），Funding 为按品种结算周期的费率（显示值为下期预测、分位对照已结算分布），
Premium=标记价对指数价的基差，Taker买卖比>1 表示主动买盘占优——这组是持仓/杠杆维度。
状态为迟滞确认态（一般 2 根确认、恢复震荡 3 根、高波冲击立即）；[原始判定]为未折叠的逐根判定，
[酝酿中]为尚未确认的候选切换，[未收线预览]用形成中的K线试算、只作预警不作确认依据。
加速度=快RV(12根)/慢RV(72根)，>1 表示波动率正在扩张；下行方差占比为近48根中
下跌收益贡献的方差比例，0.5 中性、越高越"跌出来的波动"。
路径几何（影子特征，不参与状态判定）：频率=去趋势残差每100根的均值穿越次数
（低≈慢摆动、高≈快噪声，可区分震荡的"可交易性"），主周期≈一个完整震荡的根数，
τ=Mann-Kendall 秩趋势[-1,1]（与 dir 交叉验证，两者背离时提示结构异常）；
margin=到最近可翻转状态边界的距离（<0.15 表示状态处于边界过渡中，是领先预警，
与滞后确认的"酝酿中"互补）。这组读数滞后约60根（窗口120的一半），解读时注意。
美股永续专属：ATR%ds=按(小时,是否周末)桶去季节化后的 ATR 分位（影子字段，不参与判定）
——与 ATR% 分歧大说明当前读数主要是时段效应（盘中/盘外/周末）而非真实波动状态变化；
指数IV=CBOE 板块波动率指数（个股映射 VXN=纳指100、SPY 映射 VIX），个股iv30 为
CBOE 延迟报价自采（历史短则分位不可用），期限结构9D/3M>1 表示近端恐慌（倒挂）。
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


def _pathgeom_str(f: dict) -> str:
    pg = f.get("pathgeom") or {}
    mg = f.get("margin") or {}
    parts = []
    if pg.get("chop_freq") is not None:
        dp = f" 主周期≈{pg['dom_period']}根" if pg.get("dom_period") is not None else ""
        parts.append(f"频率={pg['chop_freq']}/100根{dp} τ={pg.get('kendall_tau')}")
    if mg.get("margin") is not None:
        warn = "（<0.15 边界过渡中）" if mg["margin"] < 0.15 else ""
        parts.append(f"margin={mg['margin']}({mg.get('nearest')}){warn}")
    return " ".join(parts) if parts else "路径几何=暂无(历史<120根)"


def render_context(p: dict) -> str:
    """把 build_dashboard 的载荷压缩成给模型看的文本。"""
    lines = [f"品种: {p.get('symbol')}（时间均为 UTC，K线均已收盘）"]
    inst = p.get("instrument") or {}
    if inst.get("class") == "us_stock_perp":
        name = inst.get("display") or ""
        if inst.get("market_open"):
            lines.append(
                f"品种类型: 美股永续（标的 {name} 按工作日时钟推断为盘中 9:30-16:00 ET；"
                "未校验节假日——若今日为美股假日则此判断错误）"
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
        parts = [
            f"[{tf}] 状态={t['state_label']}(原始判定conf {t['confidence']:.2f}——确认态无独立置信度)"
            + (f"[原始判定={raw}]" if raw and raw != t.get("state") else "")
            + (f"[酝酿中:{cand['state']} {cand['count']}/{cand['need']}]" if cand else "")
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
            _pathgeom_str(f),
            f"tilt={vol['updown_tilt_20']:+.2f}",
            f"摆动高/低={s['swing_high']}/{s['swing_low']}",
            f"cRSI={c.get('crsi')} 带位={c.get('pos')}% {c.get('zone') or ''}",
        ]
        if b:
            parts.append(f"突破{'↑' if b['dir'] == 'up' else '↓'}量分位{b['vol_rank']}")
        lines.append(" ".join(parts))
    dv = p.get("dvol")
    if dv:
        lines.append(
            f"波动率: DVOL隐含={dv['iv_last']}(一年分位{dv['iv_rank']}) "
            f"RV30={dv['rv_last']}"
            + (f" IV−RV={dv['spread']:+.1f}pt" if dv.get("spread") is not None else "")
        )
    uv = p.get("usvol")
    if uv:
        _ivr = (p.get("deriv") or {}).get("iv30_rank")
        iv30_txt = (
            (f"个股iv30={uv['iv30_last']}(自采{uv['iv30_days']}天"
             + (f",分位{_ivr}" if _ivr is not None else ",历史短勿看分位") + ")")
            if uv.get("iv30_last") is not None else "个股iv30=采集中"
        )
        ts_txt = (
            f" 期限结构9D/3M={uv['ts_ratio']}" + ("(倒挂,近端恐慌)" if uv["ts_ratio"] > 1 else "")
            if uv.get("ts_ratio") is not None else ""
        )
        lines.append(
            f"美股波动率: {uv['index']}={uv['index_last']}(一年分位{uv['index_rank']}) "
            f"RV30={uv['rv_last']}"
            + (f" 指数IV−RV={uv['spread']:+.1f}pt" if uv.get("spread") is not None else "")
            + f" {iv30_txt}{ts_txt}"
        )
    dr = p.get("deriv")
    if dr:
        def _f(v, fmt="{:.2f}"):
            return fmt.format(v) if v is not None else "—"
        chg4 = _f((math.exp(dr["oi_change_4h"]) - 1) * 100 if dr["oi_change_4h"] is not None else None, "{:+.2f}")
        chg24 = _f((math.exp(dr["oi_change_24h"]) - 1) * 100 if dr["oi_change_24h"] is not None else None, "{:+.2f}")
        lines.append(
            f"持仓(Binance永续): OI={_f(dr['oi'], '{:.0f}')}张(分位{_f(dr['oi_rank'])}) "
            f"Δ4h={chg4}% Δ24h={chg24}% "
            f"Funding预测={_f(dr['funding_pct'], '{:.4f}')}%/{dr.get('funding_interval_h') or 8:g}h"
            f"(年化{_f(dr['funding_annual_pct'], '{:.1f}')}%) "
            f"上期结算={_f(dr.get('funding_settled_pct'), '{:.4f}')}%(分位{_f(dr['funding_rank'])}) "
            f"Premium={_f(dr['premium_pct'], '{:.4f}')}%(分位{_f(dr['premium_rank'])}) "
            f"Taker买卖比={_f(dr['taker_ratio'], '{:.3f}')}(分位{_f(dr['taker_rank'])})"
            + ("（持仓历史<21天，分位仅供参考）" if dr.get("warmup") else "")
        )
    flips = p.get("flips") or []
    if flips:
        lines.append("近期状态翻转: " + "; ".join(
            f"{x['tf']} {x['from']}→{x['to']}" for x in flips[:6]))
    col = p.get("collector") or {}
    lines.append(
        f"采集器: 间隔{col.get('interval')}s 最近错误{len(col.get('errors') or [])}个"
    )
    return "\n".join(lines)


def chat(payload: dict, messages: list) -> dict:
    cfg = load_config()
    context = render_context(payload)
    system = (
        load_system() + "\n\n" + PANEL_LEGEND + "\n<panel>\n" + context + "\n</panel>"
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
    # 真正的防线是上面的 _safe_codex_args 白名单。
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
