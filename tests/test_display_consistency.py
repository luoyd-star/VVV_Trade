"""批 C 显示层口径回归：Hermes、dashboard payload 与前端文案必须同源。"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

import dashboard
from regime.agent import PANEL_LEGEND, render_context
from regime.classify import Regime
from regime.report import interpret, render


ROOT = Path(__file__).resolve().parents[1]


def _base_payload(**extra):
    payload = {"symbol": "TEST-USDT", "tfs": {}, "collector": {}}
    payload.update(extra)
    return payload


def _display_features():
    return {
        "structure": {"direction": 0.1, "swing_high": 102.0, "swing_low": 98.0},
        "er_rank": 0.4,
        "volatility": {
            "atr_rank": 0.2,
            "bbw_rank": 0.1,
            "rv30_annual_pct": 25.0,
            "vol_accel": 1.0,
            "vol_accel_rank": 0.5,
            "downside_share": 0.5,
            "squeeze": True,
            "high_vol": False,
        },
        "volume": {"updown_tilt_20": 0.0, "breakout": None},
        "pathgeom": {},
        "margin": {"margin": 0.02, "nearest": "to_trend"},
        "margin_basis": "raw",
    }


def test_crypto_context_includes_near_iv_and_suppresses_missing_dvol():
    text = render_context(_base_payload(dvol={
        "iv_last": None,
        "iv_rank": None,
        "rv_last": 38.2,
        "spread": None,
        "xopt": {
            "iv": 44.48,
            "tenor_days": 3.0,
            "method": "interp",
            "n_expiries": 3,
            "age_min": 7,
        },
        "rv3_last": 34.1,
        "spread3": 10.4,
    }))

    vol_line = next(line for line in text.splitlines() if line.startswith("波动率:"))
    assert vol_line.startswith("波动率: 近端IV=44.5(~3.0d·插值·3到期·7分前)")
    assert "RV3=34.1" in vol_line and "3dIV−RV3=+10.4pt" in vol_line
    assert "DVOL" not in vol_line


def test_dvol_rank_and_nearest_method_use_shared_precision():
    text = render_context(_base_payload(dvol={
        "iv_last": 34.0,
        "iv_rank": 0.003,
        "rv_last": 30.0,
        "spread": 4.0,
        "xopt": {
            "iv": 29.68,
            "tenor_days": 2.5,
            "method": "nearest",
            "n_expiries": 1,
            "age_min": 12,
        },
        "rv3_last": 28.8,
        "spread3": 0.9,
    }))

    assert "一年分位0.003" in text
    assert "近端IV=29.7(~2.5d·单点·1到期·12分前)" in text


def test_context_includes_oi_span_and_all_heartbeat_lanes():
    lanes = [
        {"key": "近端IV", "state": "ok", "age_min": 4.2, "note": "币安期权"},
        {"key": "个股IV", "state": "idle", "age_min": 60.0, "note": "RTH"},
        {"key": "期限曲线", "state": "warn", "age_min": 95.0, "note": "小时频"},
        {"key": "衍生品", "state": "bad", "age_min": 35.0, "note": "5分频"},
        {"key": "OpenD", "state": "ok", "age_min": None, "note": "网关探活"},
    ]
    text = render_context(_base_payload(
        deriv={
            "oi": 1000.0,
            "oi_rank": 0.83,
            "oi_change_4h": 0.01,
            "oi_change_24h": 0.02,
            "funding_pct": 0.01,
            "funding_interval_h": 8,
            "funding_annual_pct": 10.0,
            "funding_settled_pct": 0.009,
            "funding_rank": 0.5,
            "premium_pct": 0.02,
            "premium_rank": 0.6,
            "taker_ratio": 1.1,
            "taker_rank": 0.7,
            "spans": {"oi": 25.7},
            "warmup": False,
        },
        heartbeat=lanes,
    ))

    assert "OI=1000张(近25.7日小时分位0.83)" in text
    heartbeat_line = next(line for line in text.splitlines() if line.startswith("采集心跳"))
    assert all(lane["key"] in heartbeat_line for lane in lanes)
    assert "近端IV=正常/4.2分前" in heartbeat_line
    assert "期限曲线=迟滞/95.0分前" in heartbeat_line
    assert "衍生品=断流/35.0分前" in heartbeat_line
    assert "OpenD=正常/无落库年龄" in heartbeat_line


def test_unknown_funding_interval_is_visible_and_never_falls_back_to_8h():
    text = render_context(_base_payload(deriv={
        "oi": 1000.0,
        "oi_rank": None,
        "oi_change_4h": None,
        "oi_change_24h": None,
        "funding_pct": 0.01,
        "funding_interval_h": None,
        "funding_annual_pct": None,
        "funding_settled_pct": None,
        "funding_rank": None,
        "premium_pct": None,
        "premium_rank": None,
        "taker_ratio": None,
        "taker_rank": None,
        "spans": {"oi": 0.0},
    }))

    assert "Funding预测=0.0100%/未知周期(年化—%)" in text
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert "funding_interval_h || 8" not in app
    assert "未知周期" in app


def test_vol_tail_uses_calendar_days_across_sampling_frequencies():
    """同一时间边界应按日历天截，不能把稀疏交易日序列误切成最后 N 个点。"""
    day_ms = 86_400_000
    daily = [[i * day_ms, i] for i in range(8)]
    trading_days = [[i * day_ms, i] for i in (0, 1, 4, 7)]

    assert [p[0] for p in dashboard._tail_days(daily, days=3)] == [
        i * day_ms for i in (4, 5, 6, 7)
    ]
    assert [p[0] for p in dashboard._tail_days(trading_days, days=3)] == [
        i * day_ms for i in (4, 7)
    ]


def test_vol_tail_default_covers_shared_calendar_window():
    """默认参数必须跟随三年数据窗，不能静默退回旧的较短窗口。"""
    day_ms = 86_400_000
    last_day = dashboard.VOL_DATA_DAYS + 5
    pairs = [[i * day_ms, i] for i in range(last_day + 1)]

    tail = dashboard._tail_days(pairs)
    cutoff = pairs[-1][0] - dashboard.VOL_DATA_DAYS * day_ms

    assert tail[-1][0] - tail[0][0] <= dashboard.VOL_DATA_DAYS * day_ms
    assert tail[0][0] >= cutoff
    assert tail[0][0] == cutoff


def test_vol_payload_view_points_and_fetch_limits_follow_shared_constants(monkeypatch):
    """两类 payload 的默认窗跟随各自分位窗，日频上游须覆盖完整数据窗。"""
    ts = pd.date_range("2026-01-01", periods=70, freq="D", tz="UTC")
    d1 = pd.DataFrame({"ts": ts, "close": [100.0 + i for i in range(len(ts))]})
    calls = {}

    monkeypatch.setattr(dashboard.instruments, "get", lambda symbol: {"class": "crypto"})
    monkeypatch.setattr(dashboard, "_xopt_block", lambda conn, symbol: None)
    monkeypatch.setattr(dashboard, "_rv3_pairs", lambda conn, symbol: ([], None))
    monkeypatch.setattr(
        dashboard.storage, "get_dvol",
        lambda conn, base, limit: (
            calls.__setitem__("dvol", limit)
            or pd.DataFrame({"ts": ts, "dvol": [30.0 + i / 10 for i in range(len(ts))]})
        ),
    )
    monkeypatch.setattr(
        dashboard.storage, "get_ohlcv",
        lambda conn, symbol, tf, limit: calls.__setitem__("crypto_ohlcv", limit) or d1,
    )
    monkeypatch.setattr(
        dashboard.storage, "get_opt_iv_near",
        lambda conn, symbol, limit: pd.DataFrame(),
    )

    crypto_view_points = 41
    monkeypatch.setattr(dashboard, "DVOL_RANK_WIN", crypto_view_points)
    crypto = dashboard._dvol_payload(None, "BTC-USDT")
    assert crypto["view_points"] == crypto_view_points
    assert crypto["iv_rank_win"] == crypto_view_points
    assert set(crypto["ema"]) == {"ema20", "ema60", "ema200"}
    assert crypto["ema"]["ema20"] and crypto["ema"]["ema60"]
    assert crypto["ema"]["ema200"] == [] and crypto["bands"] is None
    assert "ma" not in crypto and "band_win" not in crypto and "band_basis" not in crypto
    assert crypto["iv3_ema"]["ema200"] == [] and crypto["iv3_bands"] is None
    assert dashboard.VOL_DATA_FETCH_LIMIT > dashboard.VOL_DATA_DAYS
    assert calls["dvol"] == dashboard.VOL_DATA_FETCH_LIMIT
    assert calls["crypto_ohlcv"] == dashboard.VOL_DATA_FETCH_LIMIT

    us_ts = [int(t.timestamp() * 1000) for t in ts]
    monkeypatch.setattr(
        dashboard.instruments, "get",
        lambda symbol: {"class": "us_stock_perp", "vol_index": "VIX"},
    )
    monkeypatch.setattr(
        dashboard.storage, "get_usvol",
        lambda conn, idx, limit: (
            calls.__setitem__("usvol", limit)
            or pd.DataFrame({"ts": us_ts, "close": [18.0 + i / 10 for i in range(len(ts))]})
        ),
    )
    monkeypatch.setattr(
        dashboard.storage, "get_ohlcv",
        lambda conn, symbol, tf, limit: calls.__setitem__("us_ohlcv", limit) or d1,
    )
    monkeypatch.setattr(dashboard.storage, "get_meta", lambda *args, **kwargs: us_ts[-1])
    monkeypatch.setattr(dashboard.storage, "get_deriv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        dashboard.storage, "get_stock_iv_term", lambda *args, **kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(dashboard, "_stock_iv_block", lambda conn, symbol: None)
    monkeypatch.setattr(dashboard, "_term_structure", lambda conn: (None, None))

    us_view_points = 37
    monkeypatch.setattr(dashboard, "IV_RANK_WIN", us_view_points)
    us = dashboard._usvol_payload(None, "NVDA-USDT")
    assert us["view_points"] == us_view_points
    assert set(us["ema"]) == {"ema20", "ema60", "ema200"}
    assert all(not line for line in us["ema"].values()) and us["bands"] is None
    assert "ma" not in us and "band_win" not in us and "band_basis" not in us
    assert us["iv3_ema"]["ema200"] == [] and us["iv3_bands"] is None
    # usvol 的 limit 是交易日行数；用日历天数多取是安全方向，且须至少覆盖三年交易日。
    assert calls["usvol"] == dashboard.VOL_DATA_FETCH_LIMIT
    # 美股永续 RV30 含周末，仍按日历天数据窗取数。
    assert calls["us_ohlcv"] == dashboard.VOL_DATA_FETCH_LIMIT


def test_stock_iv_fetch_limit_covers_three_trading_years(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        dashboard.storage, "get_stock_vol",
        lambda conn, symbol, source, limit: calls.__setitem__("stock_iv", limit)
        or pd.DataFrame(),
    )

    assert dashboard._stock_iv_block(None, "NVDA-USDT") is None
    assert calls["stock_iv"] >= 3 * dashboard.IV_RANK_WIN


def test_vol30_frontend_uses_primary_iv_timestamp_for_shared_zoom():
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    helper = app.split("function vol30ZoomStart", 1)[1].split("function renderDvol", 1)[0]
    dvol = app.split("function renderDvol", 1)[1].split("const IV3_WIN_MIN_MS", 1)[0]
    usvol = app.split("function renderUsvol", 1)[1].split("function renderDeriv", 1)[0]

    assert "series.length >= viewPoints" in helper
    assert "series[series.length - viewPoints][0]" in helper
    assert "vol30ZoomStart([d.iv, d.rv], d.view_points)" in dvol
    assert "vol30ZoomStart([iv && iv.series, uv.series, uv.rv], uv.view_points)" in usvol
    for block in (dvol, usvol):
        assert "bottom: zs == null ? 20 : 32" in block
        assert "dataZoom: zs == null ? []" in block
        assert "type: 'inside'" in block and "type: 'slider'" in block


def test_all_volatility_zoom_controls_apply_start_value():
    """inside 与 slider 都必须消费起始时间戳，否则默认窗只算了却没有生效。"""
    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    blocks = {
        "30d 加密卡": app.split("function renderDvol", 1)[1].split(
            "const IV3_WIN_MIN_MS", 1
        )[0],
        "3d 持仓卡": app.split("function renderIv3", 1)[1].split(
            "function renderUsvol", 1
        )[0],
        "30d 美股卡": app.split("function renderUsvol", 1)[1].split(
            "function renderDeriv", 1
        )[0],
    }

    for name, block in blocks.items():
        zoom = block.split("dataZoom:", 1)[1].split("series:", 1)[0]
        inside = next(line for line in zoom.splitlines() if "type: 'inside'" in line)
        slider = next(line for line in zoom.splitlines() if "type: 'slider'" in line)
        assert "startValue: zs" in inside, f"{name} 的 inside 未应用默认窗"
        assert "startValue: zs" in slider, f"{name} 的 slider 未应用默认窗"


def test_stock_iv_freshness_and_unsettled_marks_reach_context():
    asof = int(datetime(2026, 7, 20, tzinfo=timezone.utc).timestamp() * 1000)
    text = render_context(_base_payload(usvol={
        "iv": {
            "last": 47.3,
            "asof": asof,
            "age_days": 14.2,
            "stale": True,
            "rank": 0.092,
            "rank_raw": 0.611,
            "rank_kind": "cond",
            "earn_in30": False,
            "earnings_days": None,
            "live": None,
            "n": 780,
            "vrp": None,
        },
        "index": "VIX",
        "index_last": 18.0,
        "index_rank": 0.2,
        "index_settled": False,
        "rv_last": 42.0,
        "spread": None,
        "proxy": None,
        "term": {
            "fast": {"ratio": 0.9, "rank": 0.3, "inverted": False, "settled": False},
            "slow": {"ratio": 0.8, "rank": 0.2, "inverted": False, "settled": False},
            "both_inverted": False,
        },
        "term_stock": None,
        "ts_ratio": None,
        "xopt": None,
        "rv3_last": None,
        "spread3": None,
    }))

    assert "个股IV(最近结算2026-07-20·⚠14天前)=47.3" in text
    assert "昨结算" not in text
    assert "VIX°=18.0" in text
    assert "9D/30D°=0.9" in text and "30D/3M°=0.8" in text


def test_iv30_metadata_follows_selected_moomoo_source(monkeypatch):
    monkeypatch.setattr(dashboard, "_stock_iv_block", lambda conn, symbol: {
        "last": 47.3,
        "rank": 0.092,
        "source": "moomoo",
        "days": 1135.0,
        "n": 780,
        "rank_kind": "cond",
    })

    fields = dashboard._iv30_fields(
        None,
        "NVDA-USDT",
        99.9,
        pd.DataFrame({"iv30": [99.9]}),
        {"iv30": 4.6},
    )

    assert fields == {
        "iv30": 47.3,
        "iv30_rank": 0.092,
        "iv30_src": "moomoo",
        "iv30_span_days": 1135.0,
        "iv30_n": 780,
        "iv30_rank_kind": "cond",
    }


def test_flips_exclude_1h_before_forty_row_budget(monkeypatch):
    calls = []

    def fake_states(conn, symbol, tf, limit):
        calls.append(tf)
        if tf != "4h":
            return []
        return [
            {"ts": i, "state": "range" if i % 2 == 0 else "trend_up", "confidence": 0.8}
            for i in range(50)
        ]

    monkeypatch.setattr(dashboard.storage, "get_states", fake_states)
    flips = dashboard._flips(None, "BTC-USDT", ["1h", "4h", "1d"])

    assert "1h" not in calls
    assert len(flips) == 40
    assert {flip["tf"] for flip in flips} == {"4h"}


def test_interpret_declares_missing_period_instead_of_three_period_alignment():
    regimes = {
        "1d": Regime("trend_up", 0.8),
        "4h": Regime("trend_up", 0.7),
    }
    text = interpret(regimes)

    assert text.startswith("缺 1h 周期；")
    assert "两周期同向" in text
    assert "三周期同向" not in text


def test_margin_basis_and_diverged_states_are_explicit_in_hermes_context():
    text = render_context(_base_payload(
        states_map={"range": "震荡", "trend_up": "趋势上行"},
        tfs={"1h": {
            "state": "trend_up",
            "state_label": "趋势上行",
            "raw_state": "range",
            "confidence": 0.7,
            "features": _display_features(),
            "crsi": {"last": {}},
        }},
    ))

    assert "margin 相对原始态 震荡(range) 而非当前确认态 趋势上行(trend_up)" in text
    assert "原始树边界过渡中" in text


def test_cli_report_labels_raw_single_tree_basis_and_panel_difference():
    features = _display_features()
    regime = Regime("squeeze", 0.8, features)
    frame = pd.DataFrame({
        "ts": [pd.Timestamp("2026-08-05T00:00:00Z")],
        "close": [100.0],
    })

    text = render(
        "TEST-USDT",
        {"1h": "demo"},
        {"1h": regime},
        {"1h": frame},
    )

    assert "原始态(单根规则树，无迟滞确认)" in text
    assert "面板展示逐根历史经非对称迟滞后的确认态" in text
    assert "轻量对照通道" in text
    assert "raw_state" in text


def test_legend_and_frontend_copy_keep_display_contracts_explicit():
    assert "504 日同财报状态条件分位" in PANEL_LEGEND
    assert "样本不足回退 252 日原始分位" in PANEL_LEGEND
    assert "squeeze→趋势需 3 根" in PANEL_LEGEND
    assert "挤压需 BBW<0.15 且 ATR<0.30" in PANEL_LEGEND
    assert "高波只看 ATR>0.85" in PANEL_LEGEND
    assert "采集心跳固定列五路 lane" in PANEL_LEGEND
    assert "近端IV" in PANEL_LEGEND and "RV3" in PANEL_LEGEND and "3dIV−RV3" in PANEL_LEGEND

    app = (ROOT / "web/app.js").read_text(encoding="utf-8")
    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    # 30d 副文本已改成 payload metrics 表；分位精度由统一表格 formatter 消费，
    # 不再要求旧长字符串里的 d.iv_rank 直接插值仍存在。
    assert "renderVolMetrics(d.metrics" in app
    assert "条件分位" in app
    assert ".filter((f) => f.tf !== '1h')" not in app
    assert "Hermes/API 可查全量" not in app + index

    dashboard_src = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert 'display_features["margin_basis"] = "raw"' in dashboard_src
    assert "原始树边界" in app

    metrics_block = app.split("function renderVolMetrics", 1)[1].split(
        "function volMaSeries", 1
    )[0]
    assert "document.createElement" in metrics_block
    assert ".textContent" in metrics_block
    assert "innerHTML" not in metrics_block

    iv3_block = app.split("function renderIv3", 1)[1].split(
        "function renderUsvol", 1
    )[0]
    assert "o.iv3_ema" in iv3_block and "o.iv3_bands" in iv3_block
    assert "o.window_span_desc" in iv3_block
    # 后端位置枚举是 payload 契约；未知码直出会把 above_u2 暴露给用户。
    for pos in ("above_u2", "between_u1_u2", "in_1", "between_l2_l1", "below_l2"):
        assert f"{pos}:" in app

    sm_block = app.split("const SM = {", 1)[1].split("};", 1)[0]
    assert "label:" not in sm_block, "状态中文名不得在前端颜色表中保留第二份"
    assert "S.data.states_map" in app
    assert "低波动挤压（蓄势）" not in sm_block
    assert "高波动非趋势（趋势条件未齐）" not in sm_block

    assert "squeezeAt: 0.30, highVolAt: 0.85" in app
    assert "squeezeAt: 0.15, highVolAt: null" in app
    assert "highVolAt != null && val > highVolAt" in app
    assert "yAxis: 0.30, label: { formatter: '挤压ATR' }" in app
    assert "yAxis: 0.15, label: { formatter: '挤压BBW' }" in app
    assert "高波仅 ATR&gt;0.85" in app
    assert "ATR &lt;0.30 与 BBW &lt;0.15 共判挤压" in index


def test_hermes_time_awareness():
    """Hermes 时间感知三件套：当前时刻行 / 历史消息时间前缀 / 时间跳变横幅。

    用户实际踩到的坑（2026-08-05）：一小时前的对话读数被模型当成现状——
    此前历史重放只带 role+content，system 里也没有"现在几点"。
    """
    from regime import agent

    # ① _age_txt 分档与负值防御
    now = 1_800_000_000_000
    assert agent._age_txt(now - 30_000, now) == "刚刚"
    assert agent._age_txt(now - 74 * 60_000, now) == "74分前"
    assert agent._age_txt(now - 5 * 3_600_000, now) == "5.0小时前"
    assert agent._age_txt(now - 3 * 86_400_000, now) == "3.0天前"
    assert agent._age_txt(now + 60_000, now) == "刚刚", "未来时间戳（时钟漂移）不得输出'未来'"

    # ② 时间前缀格式：[MM-DD HH:MM UTC·距今]
    tag = agent._time_tag(now - 74 * 60_000, now)
    assert tag.startswith("[") and "UTC·74分前] " in tag

    # ③ render_context 首行是当前时刻（模型的"现在"锚点）
    ctx = agent.render_context({"symbol": "TEST-USDT"})
    assert ctx.startswith("当前时刻: ") and "UTC" in ctx.split("\n")[0]

    # ④ PANEL_LEGEND 声明时间语义（历史读数只在其时点有效）
    assert "时间语义" in agent.PANEL_LEGEND and "历史" in agent.PANEL_LEGEND


def test_hermes_gap_notice_and_prefix_wiring(monkeypatch):
    """chat() 组装：历史消息带前缀、本轮提问不带；间隔超阈值出横幅、未超不出。"""
    from regime import agent

    captured = {}

    def fake_provider(cfg, system, msgs):
        captured["system"] = system
        captured["msgs"] = msgs
        return "ok"

    monkeypatch.setattr(agent, "_anthropic", fake_provider)
    monkeypatch.setattr(agent, "load_config",
                        lambda: dict(agent.DEFAULTS, provider="anthropic"))
    monkeypatch.setattr(agent, "overview_brief", lambda: "")
    monkeypatch.setattr(agent, "system_brief", lambda: "brief")

    import time as _t
    now_ms = int(_t.time() * 1000)
    old = now_ms - 60 * 60_000   # 1 小时前 → 超过 CHAT_GAP_NOTICE_MIN
    out = agent.chat({"symbol": "TEST-USDT"}, [
        {"role": "user", "content": "老问题", "ts": old},
        {"role": "assistant", "content": "老回答", "ts": old + 5_000},
        {"role": "user", "content": "新问题"},
    ])
    assert out.get("reply") == "ok", out
    assert captured["msgs"][0]["content"].startswith("["), "历史 user 须带时间前缀"
    assert captured["msgs"][1]["content"].startswith("["), "历史 assistant 须带时间前缀"
    assert not captured["msgs"][2]["content"].startswith("["), "本轮提问不带前缀（它就是现在）"
    assert "距上一轮对话已过" in captured["system"], "超阈值须出时间跳变横幅"

    # 间隔很近：不出横幅
    recent = now_ms - 2 * 60_000
    agent.chat({"symbol": "TEST-USDT"}, [
        {"role": "user", "content": "刚问过", "ts": recent},
        {"role": "assistant", "content": "刚答过", "ts": recent + 5_000},
        {"role": "user", "content": "接着问"},
    ])
    assert "距上一轮对话已过" not in captured["system"], "间隔未超阈值不得出横幅"
