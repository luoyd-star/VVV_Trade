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
    assert "fmtN(d.iv_rank, 3)" in app
    assert "条件分位" in app
    assert ".filter((f) => f.tf !== '1h')" not in app
    assert "Hermes/API 可查全量" not in app + index

    dashboard_src = (ROOT / "dashboard.py").read_text(encoding="utf-8")
    assert 'display_features["margin_basis"] = "raw"' in dashboard_src
    assert "原始树边界" in app

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
