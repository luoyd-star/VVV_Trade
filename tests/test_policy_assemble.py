"""Policy 纯装配回归，以及 A1 重构前后全宇宙对拍入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd

import dashboard
from regime import storage
from regime.policy import assemble


POLICY_KEYS = {
    "versions", "tf", "regime_4h", "regime_1d", "price", "atr", "location",
    "crsi", "crsi_by_tf", "signal_ok", "signal_tf", "resonance",
    "regime_conflict", "play", "zones", "vol_notes", "vol_meta", "stop_check",
    "degraded",
}


def _frame(ts, closes) -> pd.DataFrame:
    closes = [float(value) for value in closes]
    return pd.DataFrame({
        "ts": ts,
        "open": closes,
        "high": [value + 1.0 for value in closes],
        "low": [value - 1.0 for value in closes],
        "close": closes,
        "volume": [10.0] * len(closes),
    })


def _ohlcv_inputs() -> tuple[dict, int]:
    end_4h = pd.Timestamp("2026-08-01T08:00:00Z")
    frame_4h = _frame(
        pd.date_range(end=end_4h, periods=3, freq="4h"),
        [105.0, 103.0, 100.0],
    )
    frame_1d = _frame(
        pd.date_range(end="2026-07-31T00:00:00Z", periods=100, freq="1d"),
        [100.0] * 100,
    )
    frame_1h = _frame(
        pd.date_range(end="2026-08-01T11:00:00Z", periods=100, freq="1h"),
        [100.0] * 100,
    )
    asof_ms = int((end_4h + pd.Timedelta(hours=4)).timestamp() * 1000)
    return {"4h": frame_4h, "1d": frame_1d, "1h": frame_1h}, asof_ms


def _support_levels(*args, **kwargs) -> dict:
    del args, kwargs
    return {
        "atr": 4.0,
        "zones": [{
            "lo": 99.0,
            "hi": 101.0,
            "mid": 100.0,
            "kinds": ["ema21"],
            "touches": 2,
            "last_touch_bars": 0,
            "origin_role": "support",
            "width_atr": 0.5,
        }],
        "degraded": [],
    }


def _oversold_crsi(frame) -> dict:
    assert len(frame)
    return {"last": {"crsi": 20.0, "pos": -2.0, "zone": "超卖区"}}


def _build(**overrides) -> dict:
    frames, asof_ms = _ohlcv_inputs()
    kwargs = {
        "ohlcv_by_tf": frames,
        "regime_by_tf": {"4h": "trend_up", "1d": "range"},
        "instrument": {"class": "crypto"},
        "asof_ms": asof_ms,
        "vol1h": pd.DataFrame(columns=["ts", "volume", "quote_vol"]),
        "vol_input": {"iv3": 50.0, "tenor_days": 3.0},
        "_levels_fn": _support_levels,
        "_crsi_fn": _oversold_crsi,
    }
    kwargs.update(overrides)
    return assemble.build("TEST-USDT", **kwargs)


def test_build_is_pure_and_support_branch_is_assembled_without_sqlite(monkeypatch):
    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("pure policy assembly touched SQLite")

    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(storage, "connect_ro", forbidden)
    monkeypatch.setattr(storage, "get_ohlcv", forbidden)

    payload = _build()

    assert set(payload) == POLICY_KEYS
    assert payload["location"]["at"] == "at_support"
    assert payload["location"]["approach"] == "from_above"
    assert payload["signal_ok"] is True
    assert payload["play"] == "S4 趋势回踩做多"


def test_missing_ohlcv_keeps_stable_shape_and_explicit_degradation():
    payload = _build(
        ohlcv_by_tf={},
        regime_by_tf={"4h": "trend_up"},
        vol1h=None,
        vol_input=None,
    )

    assert set(payload) == POLICY_KEYS
    assert payload["location"] is None and payload["zones"] == []
    assert payload["crsi_by_tf"] == {
        tf: {"crsi": None, "pos": None, "zone": None}
        for tf in assemble.POLICY_RESONANCE_TFS
    }
    assert payload["degraded"] == ["regime_1d_missing", "4h_ohlcv_missing"]


def test_missing_regimes_degrade_instead_of_guessing_a_policy_cell():
    payload = _build(regime_by_tf={})

    assert payload["regime_4h"] is None and payload["regime_1d"] is None
    assert payload["location"]["at"] is None
    assert payload["location"]["reason"] == "unsupported_regime"
    assert payload["play"] is None
    assert {"regime_4h_missing", "regime_1d_missing", "unsupported_regime"}.issubset(
        payload["degraded"]
    )


def test_optional_inputs_none_keep_shape_and_mark_degradation():
    payload = _build(vol1h=None, vol_input=None)

    assert set(payload) == POLICY_KEYS
    assert payload["vol_notes"] == []
    assert payload["vol_meta"] is None
    assert payload["stop_check"] is None
    assert "vol1h_unavailable" in payload["degraded"]
    assert "vol_inputs_unavailable" in payload["degraded"]
    assert "iv3_missing_for_stop_check" in payload["degraded"]


def test_asof_clips_future_ohlcv_before_levels_and_signal_calculation():
    frames, asof_ms = _ohlcv_inputs()
    future = _frame(
        [pd.Timestamp("2026-08-01T12:00:00Z")],
        [999.0],
    )
    frames["4h"] = pd.concat([frames["4h"], future], ignore_index=True)
    seen = {}

    def levels(frame, **kwargs):
        del kwargs
        seen["levels_last"] = frame["ts"].iloc[-1]
        seen["levels_close"] = frame["close"].iloc[-1]
        return _support_levels()

    def crsi(frame):
        seen.setdefault("crsi_lasts", []).append(frame["ts"].iloc[-1])
        return _oversold_crsi(frame)

    payload = _build(
        ohlcv_by_tf=frames,
        asof_ms=asof_ms,
        _levels_fn=levels,
        _crsi_fn=crsi,
    )

    assert payload["price"] == 100.0
    assert seen["levels_close"] == 100.0
    assert seen["levels_last"] == pd.Timestamp("2026-08-01T08:00:00Z")
    assert max(seen["crsi_lasts"]) <= pd.Timestamp("2026-08-01T11:00:00Z")


def test_versions_include_assemble_generation():
    payload = _build()

    assert assemble.ASSEMBLE_VERSION == "asm1"
    assert payload["versions"] == {
        "levels": "lv1",
        "location": "loc1",
        "stopcheck": "stop1",
        "volnote": "vol1",
        "assemble": "asm1",
    }


def _snapshot(db_path: Path, output: Path) -> None:
    """固定数据库快照上生成全部详情 policy，供重构前后逐字节比较。"""
    original_path = storage.DB_PATH
    storage.DB_PATH = str(db_path)
    try:
        conn = storage.connect_ro()
        try:
            payloads = {}
            for symbol in storage.symbols(conn):
                tfs_payload = {}
                for tf in dashboard.TIMEFRAMES:
                    payload = dashboard._tf_payload(conn, symbol, tf)
                    if payload:
                        tfs_payload[tf] = payload
                payloads[symbol] = dashboard._deep_clean(
                    dashboard._build_policy_payload(conn, symbol, tfs_payload)
                )
        finally:
            conn.close()
    finally:
        storage.DB_PATH = original_path

    encoded = json.dumps(
        payloads,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    output.write_bytes(encoded)
    print(
        f"snapshot symbols={len(payloads)} bytes={len(encoded)} "
        f"sha256={hashlib.sha256(encoded).hexdigest()} output={output}"
    )


def _compare(before_path: Path, after_path: Path) -> None:
    before = json.loads(before_path.read_text(encoding="utf-8"))
    after = json.loads(after_path.read_text(encoding="utf-8"))
    for payload in after.values():
        # A1 明确允许的唯一差异；删除后再重编码，做真正的逐字节比较。
        (payload.get("versions") or {}).pop("assemble", None)
    before_bytes = json.dumps(
        before,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    after_bytes = json.dumps(
        after,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    changed = sorted(
        symbol for symbol in before.keys() | after.keys()
        if before.get(symbol) != after.get(symbol)
    )
    print(
        f"compare symbols_before={len(before)} symbols_after={len(after)} "
        f"byte_equal={before_bytes == after_bytes} changed={changed}"
    )
    if before_bytes != after_bytes:
        raise SystemExit(1)


def _main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("db", type=Path)
    snapshot_parser.add_argument("output", type=Path)
    compare_parser = subparsers.add_parser("compare")
    compare_parser.add_argument("before", type=Path)
    compare_parser.add_argument("after", type=Path)
    args = parser.parse_args()
    if args.command == "snapshot":
        _snapshot(args.db, args.output)
    else:
        _compare(args.before, args.after)


if __name__ == "__main__":
    _main()
