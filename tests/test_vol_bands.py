"""30d/3d 波动率 EMA、滚动经验 σ 带与结构化指标契约。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import dashboard


DAY_MS = 86_400_000


def _pairs(n: int, *, gap_ms: int = DAY_MS, values=None):
    values = values if values is not None else [float(i + 1) for i in range(n)]
    return [[i * gap_ms, float(values[i])] for i in range(n)]


def _independent_ema(values, span: int):
    """不调用 pandas/生产 helper 的递推实现，用来抓 EMA 被换成 SMA。"""
    alpha = 2.0 / (span + 1.0)
    out = [float(values[0])]
    for value in values[1:]:
        out.append(alpha * float(value) + (1.0 - alpha) * out[-1])
    return out


@pytest.mark.parametrize("win", dashboard.VOL_EMA_WINDOWS)
def test_ema_matches_independent_implementation_and_requires_full_window(monkeypatch, win):
    """刚好/少一根/多一根三条边界都不能输出受初值支配的预热段。"""
    monkeypatch.setattr(dashboard, "VOL_EMA_WINDOWS", (win,))
    key = f"ema{win}"

    assert dashboard._vol_moving_averages(_pairs(win - 1))[key] == []

    exact = dashboard._vol_moving_averages(_pairs(win))[key]
    expected = _independent_ema(range(1, win + 1), win)
    assert exact == [[(win - 1) * DAY_MS, round(expected[-1], 2)]]

    extra = dashboard._vol_moving_averages(_pairs(win + 1))[key]
    expected = _independent_ema(range(1, win + 2), win)
    assert extra == [
        [(win - 1) * DAY_MS, round(expected[-2], 2)],
        [win * DAY_MS, round(expected[-1], 2)],
    ]


def test_ema_field_names_have_no_legacy_ma_keys():
    output = dashboard._vol_moving_averages(_pairs(200))
    assert set(output) == {"ema20", "ema60", "ema200"}
    assert not any(key.startswith("ma") for key in output)


def test_bands_center_is_rolling_ema_and_coverage_is_pointwise_empirical(monkeypatch):
    """中轴、带宽和覆盖率均独立复算；静态均值或正态常数都会让本测试变红。"""
    win = 20
    monkeypatch.setattr(dashboard, "VOL_EMA_WINDOWS", (win,))
    values = [
        25 + i * 0.18 + (16 if i % 31 == 0 else 0) - (7 if i % 47 == 0 else 0)
        for i in range(90)
    ]
    pairs = _pairs(len(values), values=values)
    bands = dashboard._vol_bands(pairs, win)

    assert bands is not None
    assert bands["win"] == win
    assert bands["basis"] == f"ema{win}_rolling_sd"
    assert bands["n"] == len(values) - win + 1
    centers = _independent_ema(values, win)
    expected_center = [
        [i * DAY_MS, round(centers[i], 2)] for i in range(win - 1, len(values))
    ]
    assert bands["center"] == expected_center
    assert bands["center"] == dashboard._vol_moving_averages(pairs)[f"ema{win}"]
    assert len({point[1] for point in bands["center"]}) > 1

    inside1 = inside2 = 0
    for i in range(win - 1, len(values)):
        sd = float(np.std(values[i - win + 1:i + 1], ddof=1))
        inside1 += int(centers[i] - sd <= values[i] <= centers[i] + sd)
        inside2 += int(centers[i] - 2 * sd <= values[i] <= centers[i] + 2 * sd)
        out_i = i - win + 1
        assert bands["u1"][out_i][1] == round(centers[i] + sd, 2)
        assert bands["l2"][out_i][1] == round(centers[i] - 2 * sd, 2)
    n = len(values) - win + 1
    assert bands["coverage1"] == round(inside1 / n, 3)
    assert bands["coverage2"] == round(inside2 / n, 3)
    assert (bands["coverage1"], bands["coverage2"]) != (0.683, 0.955)

    sd_now = float(np.std(values[-win:], ddof=1))
    assert bands["now"]["value"] == round(values[-1], 2)
    assert bands["now"]["z"] == round((values[-1] - centers[-1]) / sd_now, 3)


def test_band_requires_exact_200_points_and_keeps_only_one_and_two_sigma():
    assert dashboard._vol_bands(_pairs(199)) is None
    exact = dashboard._vol_bands(_pairs(200))
    assert exact is not None
    assert exact["win"] == 200 and exact["n"] == 1
    assert all(len(exact[key]) == 1 for key in ("center", "u1", "l1", "u2", "l2"))
    assert "u3" not in exact and "l3" not in exact and "levels" not in exact


def test_window_span_desc_comes_from_timestamps_not_a_hardcoded_cadence():
    half_hour = dashboard._vol_window_span_desc(_pairs(60, gap_ms=30 * 60_000))
    hourly = dashboard._vol_window_span_desc(_pairs(60, gap_ms=60 * 60_000))
    assert half_hour == "200 点 ≈ 4.2 天"
    assert hourly == "200 点 ≈ 8.3 天"


def _stub_crypto_payload(monkeypatch, *, iv3_n: int):
    ts = pd.date_range("2025-01-01", periods=250, freq="D", tz="UTC")
    dvol = pd.DataFrame({"ts": ts, "dvol": np.arange(30.0, 280.0)})
    xh = pd.DataFrame({
        "ts": [i * 30 * 60_000 for i in range(iv3_n)],
        "iv": [40.0 + i / 10 for i in range(iv3_n)],
    })
    monkeypatch.setattr(dashboard, "DVOL_RANK_WIN", 5)
    monkeypatch.setattr(dashboard.instruments, "get", lambda symbol: {"class": "crypto"})
    monkeypatch.setattr(dashboard, "_xopt_block", lambda conn, symbol: None)
    monkeypatch.setattr(dashboard, "_rv3_pairs", lambda conn, symbol: ([], None))
    monkeypatch.setattr(dashboard.storage, "get_dvol", lambda *args, **kwargs: dvol)
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        dashboard.storage, "get_opt_iv_near", lambda *args, **kwargs: xh,
    )
    return dashboard._dvol_payload(None, "BTC-USDT")


def test_dvol_band_window_is_200_and_independent_of_rank_window(monkeypatch):
    payload = _stub_crypto_payload(monkeypatch, iv3_n=60)
    assert payload["view_points"] == 5 and payload["iv_rank_win"] == 5
    assert payload["bands"]["win"] == dashboard.VOL_BAND_WIN
    assert payload["bands"]["basis"] == "ema200_rolling_sd"
    assert set(payload["ema"]) == {"ema20", "ema60", "ema200"}
    assert "ma" not in payload and "band_win" not in payload and "band_basis" not in payload


def test_dvol_iv3_overlay_grows_automatically_at_200_points(monkeypatch):
    short = _stub_crypto_payload(monkeypatch, iv3_n=60)
    assert short["iv3_ema"]["ema200"] == []
    assert short["iv3_bands"] is None
    assert short["window_span_desc"] is None

    grown = _stub_crypto_payload(monkeypatch, iv3_n=250)
    assert len(grown["iv3_ema"]["ema200"]) == 51
    assert grown["iv3_bands"] is not None
    assert len(grown["iv3_bands"]["center"]) == 51
    assert grown["iv3_bands"]["center"] == grown["iv3_ema"]["ema200"]
    assert grown["window_span_desc"] == "200 点 ≈ 4.2 天"


def test_usvol_iv3_overlay_grows_without_a_3d_special_case(monkeypatch):
    pairs = _pairs(250)
    usvol = pd.DataFrame({"ts": [p[0] for p in pairs], "close": np.arange(20.0, 270.0)})
    iv3 = pd.DataFrame({
        "ts": [i * 60 * 60_000 for i in range(250)],
        "iv3": [50.0 + i / 20 for i in range(250)],
    })
    monkeypatch.setattr(
        dashboard.instruments, "get",
        lambda symbol: {"class": "us_stock_perp", "vol_index": "VXN"},
    )
    monkeypatch.setattr(dashboard.storage, "get_usvol", lambda *args, **kwargs: usvol)
    monkeypatch.setattr(dashboard.storage, "get_meta", lambda *args, **kwargs: pairs[-1][0])
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(dashboard.storage, "get_deriv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(dashboard.storage, "get_stock_iv_term", lambda *args, **kwargs: iv3)
    monkeypatch.setattr(dashboard, "_term_structure", lambda conn: (None, None))
    monkeypatch.setattr(dashboard, "_rv3_pairs", lambda conn, symbol: ([], None))
    monkeypatch.setattr(dashboard, "_stock_iv_block", lambda conn, symbol: {
        "last": 250.0,
        "series": pairs,
        "live": None,
        "rank": 0.5,
        "rank_raw": 0.5,
        "rank_kind": "raw",
        "n": 250,
        "vrp": None,
        "earnings_days": None,
        "earn_in30": False,
    })

    payload = dashboard._usvol_payload(None, "NVDA-USDT")
    assert payload["bands"]["win"] == dashboard.VOL_BAND_WIN
    assert payload["iv3_bands"] is not None
    assert payload["iv3_bands"]["center"] == payload["iv3_ema"]["ema200"]
    assert len(payload["iv3_ema"]["ema200"]) == 51
    assert payload["window_span_desc"] == "200 点 ≈ 8.3 天"


def test_usvol_metrics_cover_every_field_from_the_legacy_line():
    payload = {
        "iv": {
            "last": 61.0,
            "asof": 1_722_470_400_000,
            "age_days": 1.0,
            "stale": False,
            "rank": 0.90,
            "rank_raw": 0.63,
            "rank_kind": "cond",
            "earn_in30": False,
            "earnings_days": None,
            "n": 781,
            "vrp": -6.3,
            "vrp_rank": 0.25,
            "live": {
                "iv": 62.7,
                "chg": 1.7,
                "chg_pct": 2.7,
                "rank_preview": 0.92,
            },
        },
        "proxy": None,
        "rv_last": 70.7,
        "spread": -9.7,
        "spread_src": "stock",
        "index": "VXN",
        "index_last": 24.7,
        "index_rank": 0.65,
        "index_settled": False,
        "term": {
            "fast": {"ratio": 0.87, "rank": 0.21, "settled": False,
                     "inverted": False, "n": 4243},
            "slow": {"ratio": 0.83, "rank": 0.20, "settled": True,
                     "inverted": False, "n": 4243},
            "both_inverted": False,
        },
        "ts_ratio": None,
        "term_stock": None,
        "xopt": None,
    }

    rows = dashboard._usvol_metrics(payload)
    by_label = {row["label"]: row for row in rows}
    assert set(by_label) == {
        "实时IV", "结算IV", "VRP", "RV30", "个股IV−RV", "VXN",
        "期限 9D/30D", "期限 30D/3M",
    }
    assert (by_label["实时IV"]["value"], by_label["实时IV"]["chg"],
            by_label["实时IV"]["chg_pct"], by_label["实时IV"]["rank"]) == (
                62.7, 1.7, 2.7, 0.92,
            )
    assert by_label["实时IV"]["settled"] is False
    assert by_label["结算IV"] == dashboard._metric(
        "结算IV", 61.0, rank=0.90, rank_kind="cond",
        rank_note="同财报状态·504日", raw_rank=0.63, note=None, settled=True,
        asof=1_722_470_400_000, age_days=1.0, stale=False, n=781,
    )
    assert (by_label["VRP"]["value"], by_label["VRP"]["rank"]) == (-6.3, 0.25)
    assert by_label["RV30"]["value"] == 70.7
    assert by_label["个股IV−RV"]["value"] == -9.7
    assert (by_label["VXN"]["value"], by_label["VXN"]["rank"],
            by_label["VXN"]["note"], by_label["VXN"]["settled"]) == (
                24.7, 0.65, "锚", False,
            )
    assert (by_label["期限 9D/30D"]["value"], by_label["期限 9D/30D"]["rank"],
            by_label["期限 9D/30D"]["settled"]) == (0.87, 0.21, False)
    assert (by_label["期限 30D/3M"]["value"], by_label["期限 30D/3M"]["rank"],
            by_label["期限 30D/3M"]["settled"]) == (0.83, 0.20, True)


def test_dvol_metrics_cover_dvol_rv_spread_and_near_iv():
    payload = {
        "iv_last": 34.7,
        "iv_rank": 0.123,
        "rv_last": 28.3,
        "spread": 6.4,
        "xopt": {
            "iv": 25.8,
            "tenor_days": 2.8,
            "method": "interp",
            "n_expiries": 2,
            "age_min": 8,
        },
    }

    rows = dashboard._dvol_metrics(payload)
    by_label = {row["label"]: row for row in rows}
    assert set(by_label) == {"DVOL", "RV30", "IV−RV", "近端IV"}
    assert (by_label["DVOL"]["value"], by_label["DVOL"]["rank"],
            by_label["DVOL"]["rank_note"]) == (34.7, 0.123, f"{dashboard.DVOL_RANK_WIN}日")
    assert by_label["RV30"]["value"] == 28.3
    assert (by_label["IV−RV"]["value"], by_label["IV−RV"]["unit"]) == (6.4, "pt")
    assert (by_label["近端IV"]["value"], by_label["近端IV"]["tenor_days"],
            by_label["近端IV"]["method"], by_label["近端IV"]["n_expiries"],
            by_label["近端IV"]["age_min"]) == (25.8, 2.8, "interp", 2, 8)


def test_band_position_and_empirical_coverage_are_metrics_not_chart_labels():
    bands = {
        "win": 200,
        "n": 51,
        "coverage1": 0.685,
        "coverage2": 0.941,
        "now": {"value": 62.7, "z": 1.42, "pos": "between_u1_u2"},
    }
    rows = dashboard._vol_band_metrics(bands)
    by_label = {row["label"]: row for row in rows}

    assert set(by_label) == {
        "EMA200 带位置", "±1σ 实测覆盖", "±2σ 实测覆盖",
    }
    assert by_label["EMA200 带位置"]["value"] == "+1σ～+2σ"
    assert by_label["EMA200 带位置"]["pos"] == "between_u1_u2"
    assert "z=+1.42" in by_label["EMA200 带位置"]["note"]
    assert (by_label["±1σ 实测覆盖"]["value"],
            by_label["±1σ 实测覆盖"]["unit"]) == (68.5, "%")
    assert by_label["±2σ 实测覆盖"]["value"] == 94.1


def test_grown_3d_band_metrics_keep_the_point_window_span_visible():
    pairs = _pairs(250, gap_ms=30 * 60_000)
    bands = dashboard._vol_bands(pairs)
    rows = dashboard._vol_band_metrics(
        bands, prefix="3d ",
        span_desc=dashboard._vol_window_span_desc(pairs),
    )
    by_label = {row["label"]: row for row in rows}
    assert by_label["3d EMA200 带位置"]["value"] in {
        "+2σ 上方", "+1σ～+2σ", "±1σ 内", "−2σ～−1σ", "−2σ 下方",
    }
    assert "200 点 ≈ 4.2 天" in by_label["3d EMA200 带位置"]["note"]
