"""setup 稀疏落库、生命周期分组、版本谓词与 walk-forward 因果回归。"""
from __future__ import annotations

import os
import sqlite3
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector  # noqa: E402
from regime import storage
from regime.policy import assemble, tracking


STEP = tracking.BAR_4H_MS
T0 = 1_700_006_400_000


def row(
    offset: int,
    *,
    symbol="TEST-USDT",
    at="at_support",
    regime="trend_up",
    pos=40.0,
    crsi=None,
    main_ok=False,
    observed=None,
):
    item = {
        "symbol": symbol,
        "ts": T0 + offset * STEP,
        "at": at,
        "regime_4h": regime,
        "crsi_4h": pos if crsi is None else crsi,
        "crsi_pos_4h": pos,
        "main_ok": main_ok,
    }
    if observed is not None:
        item["observed_through_ts"] = T0 + observed * STEP
    return item


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([row(0), row(1)], 1),                         # 连续
        ([row(0), row(2)], 1),                         # 中断一根仍同一次
        ([row(0), row(3)], 2),                         # 中断两根切开
        ([row(0), row(1, at="at_resistance")], 2),    # 位置角色变化
        ([row(0), row(1, regime="range")], 2),        # 4h regime 变化
    ],
)
def test_runs_grouping(rows, expected):
    assert len(tracking.runs(rows)) == expected


def test_gap_trend_has_all_three_directions():
    converging = tracking.runs([row(0, pos=50), row(1, pos=40), row(2, pos=30)])[0]
    diverging = tracking.runs([row(0, pos=20), row(1, pos=30), row(2, pos=40)])[0]
    flat = tracking.runs([row(0, pos=30), row(1, pos=30.5), row(2, pos=30.2)])[0]

    assert converging["gap_trend"] == "收敛中"
    assert diverging["gap_trend"] == "发散中"
    assert flat["gap_trend"] == "横盘"


def test_resistance_gap_moves_toward_100_band_position():
    run = tracking.runs([
        row(0, at="at_resistance", pos=60),
        row(1, at="at_resistance", pos=75),
    ])[0]
    assert run["gap_now"] == 25.0
    assert run["gap_trend"] == "收敛中"


def test_status_has_all_four_lifecycle_values():
    brewing = tracking.runs([row(0, main_ok=False)])[0]
    ripe = tracking.runs([row(0, main_ok=True)])[0]
    matured = tracking.runs([
        row(0, main_ok=True), row(1, main_ok=False),
    ])[0]
    expired = tracking.runs([
        row(0, main_ok=False, observed=2),
    ])[0]

    assert brewing["status"] == "brewing"
    assert ripe["status"] == "ripe"
    assert matured["status"] == "matured"
    assert expired["status"] == "expired"
    assert matured["ever_main_ok"] is True


def test_empty_and_single_row_inputs():
    assert tracking.runs([]) == []
    result = tracking.runs([row(0, crsi=55.0, pos=42.0)])
    assert len(result) == 1
    assert result[0]["bars"] == 1
    assert result[0]["setup_bars"] == 1
    assert result[0]["crsi_first"] == result[0]["crsi_last"] == 55.0
    assert result[0]["gap_trend"] == "横盘"


def _frame(start: int, count: int, step_ms: int, close_base=100.0) -> pd.DataFrame:
    ts = [start + index * step_ms for index in range(count)]
    closes = [close_base + index for index in range(count)]
    return pd.DataFrame({
        "ts": ts,
        "open": closes,
        "high": [value + 1 for value in closes],
        "low": [value - 1 for value in closes],
        "close": closes,
        "volume": [10.0] * count,
    })


def _causal_inputs():
    base = T0
    frames = {
        "4h": _frame(base, 8, STEP),
        "1h": _frame(base - 8 * 3_600_000, 40, 3_600_000, 50.0),
        "1d": _frame(base - 4 * 86_400_000, 6, 86_400_000, 80.0),
    }
    regimes = {
        "4h": [{"ts": base + i * STEP, "state": "trend_up"} for i in range(8)],
        "1h": [{"ts": base - 8 * 3_600_000, "state": "range"}],
        "1d": [{"ts": base - 4 * 86_400_000, "state": "range"}],
    }
    return frames, regimes


def _fake_assemble(symbol, *, ohlcv_by_tf, regime_by_tf, instrument,
                   asof_ms, vol1h=None, vol_input=None):
    del symbol, instrument, vol1h, vol_input
    # 这条断言是故障注入的保险丝：walk_forward 若把全量未来 frame 交进来，
    # 第一根历史目标就会立即越界，而不是依赖最终 state 恰好翻转才发现。
    for tf, frame in ohlcv_by_tf.items():
        if not len(frame):
            continue
        last = int(frame["ts"].iloc[-1])
        assert last + assemble.TF_SEC[tf] * 1000 <= asof_ms
    last_4h = float(ohlcv_by_tf["4h"]["close"].iloc[-1])
    return {
        "versions": {"assemble": assemble.ASSEMBLE_VERSION},
        "regime_4h": regime_by_tf["4h"],
        "regime_1d": regime_by_tf["1d"],
        "location": {
            "at": "at_support",
            "zone": {"lo": 99.0, "hi": 101.0, "kinds": ["ema21"]},
            "dist_atr": 0.0,
            "approach": "from_above",
            "tradeable": True,
            "meaning": "pullback_long_opportunity",
            "role_flipped": False,
        },
        "crsi_by_tf": {
            tf: {"crsi": last_4h, "pos": last_4h, "zone": "带内"}
            for tf in ("4h", "1d", "1h")
        },
        "resonance": {"main_ok": False, "score": 0, "grade": None},
        "signal_ok": False,
        "play": "S4（位置到了信号没到）",
        "price": last_4h,
        "atr": 2.0,
    }


def test_walk_forward_never_passes_future_data_and_prefix_matches_full():
    frames, regimes = _causal_inputs()
    targets = [int(value) for value in frames["4h"]["ts"].iloc[2:5]]
    processed_full, full = tracking.walk_forward(
        "TEST-USDT",
        ohlcv_by_tf=frames,
        regime_by_tf=regimes,
        instrument={"class": "crypto"},
        target_ts=targets,
        assemble_fn=_fake_assemble,
    )

    cutoff = targets[-1] + STEP
    prefix_frames = {
        tf: frame[
            pd.to_numeric(frame["ts"], errors="raise")
            + assemble.TF_SEC[tf] * 1000 <= cutoff
        ].reset_index(drop=True)
        for tf, frame in frames.items()
    }
    processed_prefix, prefix = tracking.walk_forward(
        "TEST-USDT",
        ohlcv_by_tf=prefix_frames,
        regime_by_tf=regimes,
        instrument={"class": "crypto"},
        target_ts=targets,
        assemble_fn=_fake_assemble,
    )

    assert processed_full == processed_prefix == targets
    assert full == prefix


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)
    return conn


def test_assemble_version_change_makes_old_setup_row_missing():
    conn = _mem_conn()
    payload = row(0)
    payload.update({
        "regime_1d": "range", "zone_lo": 99.0, "zone_hi": 101.0,
        "zone_kinds": ["ema21"], "dist_atr": 0.0, "approach": "from_above",
        "tradeable": True, "meaning": "pullback_long_opportunity",
        "role_flipped": False, "crsi_1d": 50.0, "crsi_1h": 50.0,
        "crsi_pos_1d": 50.0, "crsi_pos_1h": 50.0, "score": 0,
        "grade": None, "play": "S4", "price": 100.0, "atr": 2.0,
        "rules_version": "rules1", "assemble_version": "asm1",
    })
    storage.replace_setup_scan(
        conn, "TEST-USDT", [T0], [payload],
        rules_version="rules1", assemble_version="asm1",
    )

    assert storage.setup_ts_set(conn, "TEST-USDT", "rules1", "asm1") == {T0}
    assert storage.setup_ts_set(conn, "TEST-USDT", "rules1", "asm2") == set()


def test_sparse_scan_watermark_remembers_middle_and_replaces_old_setup():
    conn = _mem_conn()
    payload = row(0)
    payload.update({
        "regime_1d": "range", "zone_lo": 99.0, "zone_hi": 101.0,
        "zone_kinds": [], "rules_version": "rules1", "assemble_version": "asm1",
    })
    storage.replace_setup_scan(
        conn, "TEST-USDT", [T0], [payload],
        rules_version="rules1", assemble_version="asm1",
    )
    # 同一个 ts 在新装配下变成 middle：processed 仍推进，但 setup 行必须消失。
    storage.replace_setup_scan(
        conn, "TEST-USDT", [T0], [],
        rules_version="rules1", assemble_version="asm1",
    )

    assert storage.setup_ts_set(conn, "TEST-USDT", "rules1", "asm1") == set()
    assert storage.setup_scan_watermark(conn, "TEST-USDT", "rules1", "asm1") == T0


def test_scan_watermark_can_advance_past_current_version_row_without_deleting_it():
    conn = _mem_conn()
    payload = row(0)
    payload.update({
        "regime_1d": "range", "zone_lo": 99.0, "zone_hi": 101.0,
        "zone_kinds": [], "rules_version": "rules1", "assemble_version": "asm1",
    })
    storage.replace_setup_scan(
        conn, "TEST-USDT", [T0], [payload],
        rules_version="rules1", assemble_version="asm1",
    )
    storage.replace_setup_scan(
        conn, "TEST-USDT", [], [],
        rules_version="rules1", assemble_version="asm1",
        scanned_through=T0 + STEP,
    )

    assert storage.setup_ts_set(conn, "TEST-USDT", "rules1", "asm1") == {T0}
    assert storage.setup_scan_watermark(conn, "TEST-USDT", "rules1", "asm1") == T0 + STEP


def test_collector_uses_version_predicate_for_missing_targets(monkeypatch):
    frame = _frame(T0, 2, STEP)
    frame["ts"] = pd.to_datetime(frame["ts"], unit="ms", utc=True)
    monkeypatch.setattr(storage, "get_ohlcv", lambda *args, **kwargs: frame.copy())
    monkeypatch.setattr(storage, "get_states", lambda *args, **kwargs: [])
    monkeypatch.setattr(storage, "get_vol1h", lambda *args, **kwargs: pd.DataFrame())
    monkeypatch.setattr(storage, "setup_scan_watermark", lambda *args, **kwargs: None)
    predicate_calls = []

    def existing(*args):
        predicate_calls.append(args[1:])
        return {T0}

    monkeypatch.setattr(storage, "setup_ts_set", existing)
    seen = {}

    def walk(symbol, **kwargs):
        seen["walk_targets"] = kwargs["target_ts"]
        return list(kwargs["target_ts"]), []

    monkeypatch.setattr(tracking, "walk_forward", walk)

    def replace(conn, symbol, processed, rows, **kwargs):
        del conn, symbol, rows
        seen["processed"] = processed
        seen["scanned_through"] = kwargs["scanned_through"]
        return 0

    monkeypatch.setattr(storage, "replace_setup_scan", replace)

    scanned, written = collector.sync_setup_history(object(), "TEST-USDT")

    assert predicate_calls == [
        ("TEST-USDT", tracking.RULES_GENERATION, assemble.ASSEMBLE_VERSION),
    ]
    assert seen == {
        "walk_targets": [T0 + STEP],
        "processed": [T0 + STEP],
        "scanned_through": T0 + STEP,
    }
    assert (scanned, written) == (2, 0)
