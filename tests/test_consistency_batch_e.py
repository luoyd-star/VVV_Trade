"""批 E 死物处置回归：审计 rules 出口、动态目录/计数与查询提示。"""
from __future__ import annotations

import json
import sqlite3

import collector
from regime import storage
from scripts import vvvquery


def _mem_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)
    storage._migrate(conn)
    return conn


def test_collector_status_stops_repeating_symbol_array():
    payload = collector._status_payload(300, 12.34, [f"e{i}" for i in range(12)])

    assert payload == {
        "interval": 300,
        "cycle_sec": 12.3,
        "errors": [f"e{i}" for i in range(2, 12)],
    }
    assert "symbols" not in payload


def test_get_states_audit_rules_are_explicit_opt_in():
    conn = _mem_conn()
    conn.execute(
        "INSERT INTO regime_history("
        "symbol,tf,ts,state,raw_state,confidence,features,rules,version,audit_version"
        ") VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("BTC-USDT", "1h", 1, "trend_up", "trend_up", 0.8,
         json.dumps({"warmup": False}), json.dumps({"er": ">=0.4"}), "v3.1", "a8"),
    )

    lean = storage.get_states_audit(conn, "BTC-USDT", "1h")
    rich = storage.get_states_audit(conn, "BTC-USDT", "1h", include_rules=True)

    assert "rules" not in lean[0]
    assert rich[0]["rules"] == {"er": ">=0.4"}


def test_counts_and_table_directory_follow_sqlite_master():
    conn = _mem_conn()
    conn.execute("CREATE TABLE future_pipe(id INTEGER)")
    conn.executemany("INSERT INTO future_pipe(id) VALUES(?)", [(1,), (2,)])

    names = storage.table_names(conn)
    result = storage.counts(conn)

    assert "sqlite_sequence" not in names
    assert set(result) == set(names)
    assert result["future_pipe"] == 2
    assert storage.table_columns(conn, "regime_history")[-1] == "audit_version"


def test_vvvquery_tables_exposes_reserved_data_outlets(monkeypatch, capsys):
    conn = _mem_conn()
    monkeypatch.setattr(vvvquery.storage, "connect_ro", lambda: conn)

    assert vvvquery.cmd_tables() == 0
    out = capsys.readouterr().out

    assert "regime_history" in out and "rules 是逐根判定规则命中的 JSON 账本" in out
    assert "ref_daily" in out and "参考历史层" in out
    assert "breadth" in out and "宽度影子" in out
