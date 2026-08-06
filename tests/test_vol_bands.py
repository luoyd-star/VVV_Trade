"""30d 波动率均线、经验 σ 带与结构化指标契约。"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import dashboard
from regime.features.utils import pct_rank


DAY_MS = 86_400_000


def _pairs(n: int):
    return [[i * DAY_MS, float(i + 1)] for i in range(n)]


@pytest.mark.parametrize("win", dashboard.VOL_MA_WINDOWS)
def test_moving_average_requires_the_exact_full_window(monkeypatch, win):
    """刚好/少一根/多一根三条边界都不能静默换成短窗。"""
    monkeypatch.setattr(dashboard, "VOL_MA_WINDOWS", (win,))
    key = f"ma{win}"

    assert dashboard._vol_moving_averages(_pairs(win - 1))[key] == []

    exact = dashboard._vol_moving_averages(_pairs(win))[key]
    assert exact == [[(win - 1) * DAY_MS, round((win + 1) / 2, 2)]]

    extra = dashboard._vol_moving_averages(_pairs(win + 1))[key]
    assert len(extra) == 2
    assert extra[-1] == [win * DAY_MS, round((win + 3) / 2, 2)]


def test_band_coverage_and_upper_percentile_are_empirical(monkeypatch):
    """用独立 numpy 复算，避免把正态理论覆盖率误写进 payload。"""
    monkeypatch.setattr(dashboard, "VOL_BAND_MIN", 5)
    values = pd.Series([7, 8, 9, 10, 11, 12, 13, 18, 27, 45, 46, 80], dtype=float)
    win = 10
    ref = values.tail(win).to_numpy()
    bands = dashboard._vol_bands(values, win)

    assert bands is not None
    assert bands["win"] == win and bands["basis"] == "raw" and bands["n"] == win
    mean = float(np.mean(ref))
    sd = float(np.std(ref, ddof=1))
    assert bands["mean"] == round(mean, 2)
    assert bands["sd"] == round(sd, 2)

    for level in bands["levels"]:
        lo, hi = mean - level["k"] * sd, mean + level["k"] * sd
        expected_coverage = round(float(np.count_nonzero((ref >= lo) & (ref <= hi)) / win), 3)
        expected_hi_pct = round(float(np.count_nonzero(ref <= hi) / win), 3)
        assert level["coverage"] == expected_coverage
        assert level["hi_pct"] == expected_hi_pct

        # pct_rank 用严格小于；把探针放到 hi 的下一浮点数并去掉探针自身的分母，
        # 即得到同一窗口上的“不超过 hi”经验 CDF。
        probe = pd.Series([*ref, np.nextafter(hi, math.inf)])
        via_pct_rank = pct_rank(probe, len(probe)) * len(probe) / win
        assert level["hi_pct"] == round(via_pct_rank, 3)


def test_bands_are_none_below_calibrated_sample_floor(monkeypatch):
    monkeypatch.setattr(dashboard, "VOL_BAND_MIN", 6)
    assert dashboard._vol_bands([1, 2, 3, 4, 5], win=20) is None


def test_dvol_band_window_follows_rank_constant(monkeypatch):
    ts = pd.date_range("2026-01-01", periods=10, freq="D", tz="UTC")
    dvol = pd.DataFrame({"ts": ts, "dvol": np.arange(10.0, 20.0)})
    monkeypatch.setattr(dashboard, "DVOL_RANK_WIN", 5)
    monkeypatch.setattr(dashboard, "VOL_BAND_MIN", 2)
    monkeypatch.setattr(dashboard.instruments, "get", lambda symbol: {"class": "crypto"})
    monkeypatch.setattr(dashboard, "_xopt_block", lambda conn, symbol: None)
    monkeypatch.setattr(dashboard, "_rv3_pairs", lambda conn, symbol: ([], None))
    monkeypatch.setattr(dashboard.storage, "get_dvol", lambda *args, **kwargs: dvol)
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        dashboard.storage, "get_opt_iv_near", lambda *args, **kwargs: pd.DataFrame(),
    )

    payload = dashboard._dvol_payload(None, "BTC-USDT")
    assert payload["band_win"] == 5
    assert payload["band_basis"] == "raw"
    assert payload["bands"]["win"] == 5
    assert payload["bands"]["mean"] == 17.0  # 只认最后 5 根，不得偷用整条序列


def test_usvol_band_window_follows_rank_constant(monkeypatch):
    pairs = _pairs(10)
    usvol = pd.DataFrame({"ts": [p[0] for p in pairs], "close": np.arange(20.0, 30.0)})
    monkeypatch.setattr(dashboard, "IV_RANK_WIN", 4)
    monkeypatch.setattr(dashboard, "VOL_BAND_MIN", 2)
    monkeypatch.setattr(
        dashboard.instruments, "get",
        lambda symbol: {"class": "us_stock_perp", "vol_index": "VXN"},
    )
    monkeypatch.setattr(dashboard.storage, "get_usvol", lambda *args, **kwargs: usvol)
    monkeypatch.setattr(dashboard.storage, "get_meta", lambda *args, **kwargs: pairs[-1][0])
    monkeypatch.setattr(dashboard.storage, "get_ohlcv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(dashboard.storage, "get_deriv", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(
        dashboard.storage, "get_stock_iv_term", lambda *args, **kwargs: pd.DataFrame(),
    )
    monkeypatch.setattr(dashboard, "_term_structure", lambda conn: (None, None))
    monkeypatch.setattr(dashboard, "_rv3_pairs", lambda conn, symbol: ([], None))
    monkeypatch.setattr(dashboard, "_stock_iv_block", lambda conn, symbol: {
        "last": 10.0,
        "series": pairs,
        "live": None,
        "rank": 0.5,
        "rank_raw": 0.5,
        "rank_kind": "raw",
        "n": 10,
        "vrp": None,
        "earnings_days": None,
        "earn_in30": False,
    })

    payload = dashboard._usvol_payload(None, "NVDA-USDT")
    assert payload["band_win"] == 4
    assert payload["band_basis"] == "raw"
    assert payload["bands"]["win"] == 4
    assert payload["bands"]["mean"] == 8.5  # 最后四根为 7,8,9,10


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

