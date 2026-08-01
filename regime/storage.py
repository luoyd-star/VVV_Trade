"""SQLite 存储层：collector 写、dashboard 读，WAL 模式支持并发读写。

时间戳统一存 UTC 毫秒整数。数据库文件在 data/market.db。
"""
from __future__ import annotations

import os
import sqlite3

import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DATA_DIR, "market.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ohlcv(
  symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT,
  PRIMARY KEY(symbol, tf, ts)
);
CREATE TABLE IF NOT EXISTS dvol(
  currency TEXT NOT NULL, ts INTEGER NOT NULL, dvol REAL,
  PRIMARY KEY(currency, ts)
);
CREATE TABLE IF NOT EXISTS regime_history(
  symbol TEXT NOT NULL, tf TEXT NOT NULL, ts INTEGER NOT NULL,
  state TEXT, confidence REAL,
  PRIMARY KEY(symbol, tf, ts)
);
CREATE TABLE IF NOT EXISTS deriv(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  oi REAL, oi_notional REAL, funding REAL, premium REAL, taker_ratio REAL, iv30 REAL,
  PRIMARY KEY(symbol, ts)
);
CREATE TABLE IF NOT EXISTS usvol(
  idx TEXT NOT NULL, ts INTEGER NOT NULL, close REAL,
  PRIMARY KEY(idx, ts)
);
CREATE TABLE IF NOT EXISTS chat(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS live_bars(
  symbol TEXT NOT NULL, tf TEXT NOT NULL,
  ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL,
  fetched_at INTEGER,
  PRIMARY KEY(symbol, tf)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

_DERIV_COLS = ("oi", "oi_notional", "funding", "premium", "taker_ratio", "iv30")


def connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    """就地迁移：regime_history 增加审计列；旧的无审计行一次性清空重算
    （walk-forward 状态完全可由 K 线重算，不丢信息）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(regime_history)")}
    for col in ("features", "rules", "version", "raw_state"):
        if col not in cols:
            conn.execute(f"ALTER TABLE regime_history ADD COLUMN {col} TEXT")
    conn.execute("UPDATE regime_history SET raw_state=state WHERE raw_state IS NULL")
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(deriv)")}
    if "iv30" not in dcols:
        conn.execute("ALTER TABLE deriv ADD COLUMN iv30 REAL")
    conn.commit()
    # 审计特征集变更时递增 key 名触发一次性重算，保证所有行的 features 字段同构
    # v3：加入 pathgeom（freq/domp/tau）、margin、lag 影子字段（规则未变，RULES_VERSION 仍 v1）
    # v4：加入 atr_ds（hour-of-day 去季节化 ATR 分位，美股永续 1h/4h 专属影子字段）
    # ⚠ 未来再做 v4 式全量 purge 前必查：collector 重算深度为 get_ohlcv(limit=1200)，
    #   任一 (symbol,tf) 的 ohlcv 超过 1200 根后，全量 DELETE 将永久截断更老的状态行——
    #   届时应改为限窗 purge 或先提高重算深度（对抗校验发现的前瞻风险）。
    # v5：时间对齐批次——4h 残桶修复后 ohlcv 值变了、atr_ds 改按 ET 分桶、
    #     lag 字段从硬编码 15 换成按算子计算。三项都改了审计快照的口径。
    #     （atr_ds/lag 都是影子字段不参与判定，RULES_VERSION 仍 v1。）
    if get_meta(conn, "regime_audit_v5") is None:
        conn.execute("DELETE FROM regime_history")
        conn.commit()
        set_meta(conn, "regime_audit_v5", "1")
    # usvol 时间格统一为交易日 00:00 UTC：清掉早期用 time.time() 写入的盘中点位行
    # （日线格里混盘中点位会让 tail(365) 的"一年分位"悄悄退化成"最近几天分位"）。
    # 日线本身每天从 CSV 重拉，删掉即由下一轮补回，不丢信息。
    if get_meta(conn, "usvol_ts_aligned_v1") is None:
        conn.execute("DELETE FROM usvol WHERE ts % 86400000 <> 0")
        conn.execute("DELETE FROM meta WHERE key LIKE 'usvol_backfilled_%'")  # 已被 usvol_csv_* 取代
        conn.commit()
        set_meta(conn, "usvol_ts_aligned_v1", "1")
    # 短命的 usvol_csv_{name}（纯 24h 墙钟闸）已被 usvol_csv_at_/usvol_csv_max_ 取代：
    # 前者只记时间、判断不了"CSV 是否已确权到某个交易日"，会让官方收盘价确权靠运气。
    if get_meta(conn, "usvol_csv_gate_v2") is None:
        conn.execute(
            "DELETE FROM meta WHERE key LIKE 'usvol_csv_%'"
            " AND key NOT LIKE 'usvol_csv_at_%' AND key NOT LIKE 'usvol_csv_max_%'"
        )
        conn.commit()
        set_meta(conn, "usvol_csv_gate_v2", "1")


def ts_to_ms(series: pd.Series) -> list:
    return (series.astype("int64") // 10**6).tolist()


_TF_MS = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _assert_grid(conn, symbol: str, tf: str, ts_list, source: str) -> None:
    """拒绝把另一套时间网格写进同一个 (symbol, tf)。

    日界跨源不一致：Deribit 1d 在 08:00 UTC 收线，OKX/Binance 在 00:00 UTC。
    主键是 (symbol,tf,ts)，两套锚点**永不碰撞**——Deribit 宕一次机落到 OKX，
    就会往同一条 1d 序列里塞进约 300 根 00:00 行且不覆盖任何旧行，序列退化成
    8h/16h 交替的双网格，且不可逆（老行不会被清）。宁可报错进 errors 让人看见，
    也不要静默污染。
    """
    if tf not in _TF_MS or not ts_list:
        return
    anchors = {t % _TF_MS[tf] for t in ts_list}
    prev = conn.execute(
        "SELECT ts % ? FROM ohlcv WHERE symbol=? AND tf=? LIMIT 1",
        (_TF_MS[tf], symbol, tf),
    ).fetchone()
    if len(anchors) > 1 or (prev is not None and prev[0] not in anchors):
        raise RuntimeError(
            f"{symbol} {tf} 时间网格锚点冲突：新={sorted(anchors)} "
            f"存量={prev and prev[0]}（source={source}）——拒绝写入以免双网格污染"
        )


def upsert_ohlcv(conn, symbol: str, tf: str, df: pd.DataFrame, source: str) -> None:
    _assert_grid(conn, symbol, tf, ts_to_ms(df["ts"]), source)
    rows = [
        (symbol, tf, int(t), float(o), float(h), float(l), float(c), float(v), source)
        for t, o, h, l, c, v in zip(
            ts_to_ms(df["ts"]), df["open"], df["high"], df["low"], df["close"], df["volume"]
        )
    ]
    conn.executemany(
        "INSERT INTO ohlcv(symbol,tf,ts,open,high,low,close,volume,source)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(symbol,tf,ts) DO UPDATE SET open=excluded.open,high=excluded.high,"
        " low=excluded.low,close=excluded.close,volume=excluded.volume,source=excluded.source",
        rows,
    )
    conn.commit()


def get_ohlcv(conn, symbol: str, tf: str, limit: int = 1200) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT ts,open,high,low,close,volume FROM ohlcv"
        " WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
        conn,
        params=(symbol, tf, limit),
    )
    df = df.iloc[::-1].reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def last_source(conn, symbol: str, tf: str):
    row = conn.execute(
        "SELECT source FROM ohlcv WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT 1",
        (symbol, tf),
    ).fetchone()
    return row[0] if row else None


def upsert_dvol(conn, currency: str, df: pd.DataFrame) -> None:
    rows = [
        (currency, int(t), float(v)) for t, v in zip(ts_to_ms(df["ts"]), df["dvol"])
    ]
    conn.executemany(
        "INSERT INTO dvol(currency,ts,dvol) VALUES(?,?,?)"
        " ON CONFLICT(currency,ts) DO UPDATE SET dvol=excluded.dvol",
        rows,
    )
    conn.commit()


def get_dvol(conn, currency: str, limit: int = 730) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT ts,dvol FROM dvol WHERE currency=? ORDER BY ts DESC LIMIT ?",
        conn,
        params=(currency, limit),
    )
    df = df.iloc[::-1].reset_index(drop=True)
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df


def state_ts_set(conn, symbol: str, tf: str) -> set:
    rows = conn.execute(
        "SELECT ts FROM regime_history WHERE symbol=? AND tf=?", (symbol, tf)
    ).fetchall()
    return {r[0] for r in rows}


def upsert_states(conn, symbol: str, tf: str, rows) -> None:
    """rows: 可迭代的 (ts_ms, state, confidence, features_json, rules_json, version)。"""
    conn.executemany(
        "INSERT INTO regime_history(symbol,tf,ts,state,raw_state,confidence,features,rules,version)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(symbol,tf,ts) DO UPDATE SET state=excluded.state,"
        " raw_state=excluded.raw_state,"
        " confidence=excluded.confidence, features=excluded.features,"
        " rules=excluded.rules, version=excluded.version",
        [
            (symbol, tf, int(t), s, s, float(c), f, ru, v)
            for t, s, c, f, ru, v in rows
        ],
    )
    conn.commit()


def set_confirmed(conn, symbol: str, tf: str, pairs) -> None:
    """pairs: (confirmed_state, ts_ms)——把迟滞折叠后的确认态写回 state 列。"""
    conn.executemany(
        "UPDATE regime_history SET state=? WHERE symbol=? AND tf=? AND ts=?",
        [(s, symbol, tf, int(t)) for s, t in pairs],
    )
    conn.commit()


def get_states(conn, symbol: str, tf: str, limit: int = 600):
    rows = conn.execute(
        "SELECT ts,state,confidence,raw_state FROM regime_history"
        " WHERE symbol=? AND tf=? ORDER BY ts DESC LIMIT ?",
        (symbol, tf, limit),
    ).fetchall()
    return [
        {"ts": r[0], "state": r[1], "confidence": r[2], "raw_state": r[3] or r[1]}
        for r in reversed(rows)
    ]


def set_live_bar(conn, symbol: str, tf: str, row: dict) -> None:
    """保存形成中的最后一根 K 线（滚动预览用），每 (symbol, tf) 只留最新一份。"""
    conn.execute(
        "INSERT INTO live_bars(symbol,tf,ts,open,high,low,close,volume,fetched_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(symbol,tf) DO UPDATE SET ts=excluded.ts,open=excluded.open,"
        " high=excluded.high,low=excluded.low,close=excluded.close,"
        " volume=excluded.volume,fetched_at=excluded.fetched_at",
        (
            symbol, tf, int(row["ts"]), float(row["open"]), float(row["high"]),
            float(row["low"]), float(row["close"]), float(row["volume"]),
            int(row["fetched_at"]),
        ),
    )
    conn.commit()


def get_live_bar(conn, symbol: str, tf: str):
    r = conn.execute(
        "SELECT ts,open,high,low,close,volume,fetched_at FROM live_bars"
        " WHERE symbol=? AND tf=?",
        (symbol, tf),
    ).fetchone()
    if not r:
        return None
    keys = ("ts", "open", "high", "low", "close", "volume", "fetched_at")
    return dict(zip(keys, r))


def upsert_deriv(conn, symbol: str, rows) -> None:
    """rows: dict 列表，键含 ts（毫秒）与任意 _DERIV_COLS 子集；同 ts 行按列合并。"""
    params = [
        tuple([symbol, int(r["ts"])] + [r.get(c) for c in _DERIV_COLS]) for r in rows
    ]
    sets = ", ".join(f"{c}=COALESCE(excluded.{c}, {c})" for c in _DERIV_COLS)
    ph = ",".join("?" * (2 + len(_DERIV_COLS)))
    conn.executemany(
        f"INSERT INTO deriv(symbol,ts,{','.join(_DERIV_COLS)})"
        f" VALUES({ph})"
        f" ON CONFLICT(symbol,ts) DO UPDATE SET {sets}",
        params,
    )
    conn.commit()


def get_deriv(conn, symbol: str, limit: int = 4000) -> pd.DataFrame:
    df = pd.read_sql_query(
        f"SELECT ts,{','.join(_DERIV_COLS)} FROM deriv"
        " WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        conn,
        params=(symbol, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


def upsert_usvol(conn, idx: str, rows) -> None:
    """rows: (ts_ms, close) 可迭代。"""
    conn.executemany(
        "INSERT INTO usvol(idx,ts,close) VALUES(?,?,?)"
        " ON CONFLICT(idx,ts) DO UPDATE SET close=excluded.close",
        [(idx, int(t), float(c)) for t, c in rows],
    )
    conn.commit()


def get_usvol(conn, idx: str, limit: int = 800) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT ts,close FROM usvol WHERE idx=? ORDER BY ts DESC LIMIT ?",
        conn,
        params=(idx, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


def add_chat(conn, role: str, content: str) -> None:
    """VVVhermes 共享对话：面板与终端读写同一张表（单一全局会话流）。"""
    import time as _time

    conn.execute(
        "INSERT INTO chat(ts, role, content) VALUES(?,?,?)",
        (int(_time.time() * 1000), role, content),
    )
    conn.commit()


def get_chat(conn, limit: int = 60):
    rows = conn.execute(
        "SELECT id, ts, role, content FROM chat ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {"id": r[0], "ts": r[1], "role": r[2], "content": r[3]}
        for r in reversed(rows)
    ]


def clear_chat(conn) -> None:
    conn.execute("DELETE FROM chat")
    conn.commit()


def set_meta(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key,value) VALUES(?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def symbols(conn) -> list:
    rows = conn.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol").fetchall()
    return [r[0] for r in rows]


def counts(conn) -> dict:
    out = {}
    for table in ("ohlcv", "dvol", "regime_history", "deriv"):
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out
