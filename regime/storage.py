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
    """写者连接（collector 专用）：建表 + 迁移。

    迁移里有 ALTER / 全量 UPDATE / 代际清空重算这类重武器，**只允许 collector
    这一个进程持有扳机**。面板等只读消费者一律用 connect_ro()——曾实测：
    面板每次请求都跑迁移时，审计代际一升版，谁先 connect 谁执行 purge，
    一条 GET /api/dashboard 就能把 regime_history 清成 0 行。
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    return conn


def connect_ro() -> sqlite3.Connection:
    """只读连接：不建表、不迁移，数据库层面拒绝一切写入。

    库还不存在时直接抛错——先起 collector 是部署顺序的一部分，
    面板在库出现前返回 500 比静默建一个空库诚实。
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=15)
    return conn


def connect_rw_nomigrate() -> sqlite3.Connection:
    """可写但不迁移：面板的 chat 表读写专用（面板不该有迁移扳机）。

    mode=rw：只打开已存在的库，绝不新建——否则库尚未由 collector 建立时，
    一次 POST /api/agent/chat 会静默留下一个无表空库文件，把"先起 collector"
    的部署顺序语义悄悄破坏掉。
    """
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=rw", uri=True, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _migrate(conn) -> None:
    """就地迁移：regime_history 增加审计列；旧的无审计行一次性清空重算
    （walk-forward 状态完全可由 K 线重算，不丢信息）。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(regime_history)")}
    for col in ("features", "rules", "version", "raw_state", "audit_version"):
        if col not in cols:
            conn.execute(f"ALTER TABLE regime_history ADD COLUMN {col} TEXT")
    conn.execute("UPDATE regime_history SET raw_state=state WHERE raw_state IS NULL")
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(deriv)")}
    if "iv30" not in dcols:
        conn.execute("ALTER TABLE deriv ADD COLUMN iv30 REAL")
    conn.commit()
    # 历史上这里有一套"regime_audit_vN 键不存在就 DELETE FROM regime_history"的
    # 代际清空机制（v2 补列 / v3 pathgeom / v4 atr_ds / v5 时间对齐批），已被
    # state_ts_set 的版本谓词取代：RULES_VERSION / AUDIT_VERSION 升版时旧行被判
    # 为缺失、下一轮 upsert 原地重算。谓词方案没有删除动作——旧机制的两个雷
    # （超出重算窗口的历史被永久截断；任何持有迁移的连接都能触发清库）同时拆除。
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


def _invalidate_if_revised(conn, symbol: str, tf: str, rows) -> None:
    """写入前对账：同 ts 的行若 OHLCV 任一值有变，删掉该 ts 起的全部状态历史。

    这是对「K 线被修订/换源覆盖，但状态按 ts 已存在而跳过重算」这个机制的
    结构性关闭——比 bar_hash 更直接：不是记录污染，是让污染无法存在。
    删除后 collector 同轮的 rolling_states_missing 会自然把缺的 ts 补算回来。
    容差取相对 1e-9：同源重复写入的浮点必然逐位相等，只有真修订才会触发。
    """
    if not rows:
        return
    all_ts = [r[2] for r in rows]
    ts_lo, ts_hi = min(all_ts), max(all_ts)
    old = {
        r[0]: r[1:]
        for r in conn.execute(
            "SELECT ts,open,high,low,close,volume FROM ohlcv"
            " WHERE symbol=? AND tf=? AND ts BETWEEN ? AND ?",
            (symbol, tf, ts_lo, ts_hi),
        )
    }
    first_changed = None
    for r in rows:
        prev = old.get(r[2])
        if prev is None:
            continue
        for a, b in zip(prev, r[3:8]):
            if a is None or (a != a):  # 库内历史遗留 NULL/NaN：一律视为已修订
                first_changed = r[2] if first_changed is None else min(first_changed, r[2])
                break
            if abs(a - b) > 1e-9 * max(1.0, abs(a), abs(b)):
                first_changed = r[2] if first_changed is None else min(first_changed, r[2])
                break
    if first_changed is not None:
        n = conn.execute(
            "DELETE FROM regime_history WHERE symbol=? AND tf=? AND ts>=?",
            (symbol, tf, first_changed),
        ).rowcount
        conn.commit()
        if n:
            __import__("logging").getLogger("storage").warning(
                "%s %s 检测到 K 线修订（首个变更 ts=%d）→ 已失效 %d 行状态，本轮将重算",
                symbol, tf, first_changed, n,
            )


def upsert_ohlcv(conn, symbol: str, tf: str, df: pd.DataFrame, source: str) -> None:
    # NaN 拒收：NaN 会以 SQL NULL 落库，下一轮对账时 None 与 float 相减抛
    # TypeError，把该 (symbol,tf) 永久炸死在 cycle 的 try 里。宁可这一轮报错。
    if df[["open", "high", "low", "close", "volume"]].isna().any().any():
        raise ValueError(f"{symbol} {tf} 行情含 NaN，拒绝入库（source={source}）")
    _assert_grid(conn, symbol, tf, ts_to_ms(df["ts"]), source)
    rows = [
        (symbol, tf, int(t), float(o), float(h), float(l), float(c), float(v), source)
        for t, o, h, l, c, v in zip(
            ts_to_ms(df["ts"]), df["open"], df["high"], df["low"], df["close"], df["volume"]
        )
    ]
    _invalidate_if_revised(conn, symbol, tf, rows)
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


def state_ts_set(conn, symbol: str, tf: str, version: str = None,
                 audit_version: str = None) -> set:
    """已算过的 ts 集合。版本谓词是这套系统"升版自动重算"的机关：

    只有 version 与 audit_version **都匹配当前代码**的行才算"已存在"；
    旧版本行被视为缺失 → rolling_states_missing 会重算 → upsert 原地覆盖。
    这取代了旧的"regime_audit_vN 键不存在就 DELETE 全表"迁移——那套机制
    要求所有历史都在重算窗口内（超窗即永久截断），且任何持有迁移扳机的
    连接（曾包括面板）都可能触发清库。谓词方案没有删除动作，天然无此风险。
    """
    if version is None:
        rows = conn.execute(
            "SELECT ts FROM regime_history WHERE symbol=? AND tf=?", (symbol, tf)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts FROM regime_history WHERE symbol=? AND tf=?"
            " AND version=? AND audit_version=?",
            (symbol, tf, version, audit_version),
        ).fetchall()
    return {r[0] for r in rows}


def upsert_states(conn, symbol: str, tf: str, rows) -> None:
    """rows: (ts_ms, state, confidence, features_json, rules_json, version, audit_version)。"""
    conn.executemany(
        "INSERT INTO regime_history(symbol,tf,ts,state,raw_state,confidence,features,rules,version,audit_version)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(symbol,tf,ts) DO UPDATE SET state=excluded.state,"
        " raw_state=excluded.raw_state,"
        " confidence=excluded.confidence, features=excluded.features,"
        " rules=excluded.rules, version=excluded.version,"
        " audit_version=excluded.audit_version",
        [
            (symbol, tf, int(t), s, s, float(c), f, ru, v, av)
            for t, s, c, f, ru, v, av in rows
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


def get_deriv_col(conn, symbol: str, col: str, limit: int = 6000,
                  hourly_grid: bool = False) -> pd.DataFrame:
    """单指标窗口：只取该列非空的行，各指标窗口互不挤占。

    背景：deriv 是稀疏宽表（funding 8h 结算格 / OI·premium·taker 1h 回填格 /
    快照 5m 墙钟格 / iv30 30 分钟格全在一张表里）。按行整表取窗（LIMIT 4000）
    会让高频的 5m 快照把低频的 funding 历史挤出窗口——每 5 分钟一行时
    4000 行 ≈ 13.9 天，168 天的 funding 结算史两周内被淘汰殆尽。
    """
    if col not in _DERIV_COLS:
        raise ValueError(f"未知 deriv 列: {col}")
    # hourly_grid：只取落在整点网格上的行（±60s 双向容差——fundingTime 实测有
    # 1001ms 级抖动，且理论上可早可晚）。这一步必须在 SQL 层做：LIMIT 若对
    # 全部非空行计数，每 5 分钟一行的快照会把网格行重新挤出窗口——
    # "结算样本枯竭"就只是被推迟而不是被消除。
    grid = " AND MIN(ts % 3600000, 3600000 - ts % 3600000) < 60000" if hourly_grid else ""
    df = pd.read_sql_query(
        f"SELECT ts,{col} FROM deriv WHERE symbol=? AND {col} IS NOT NULL{grid}"
        " ORDER BY ts DESC LIMIT ?",
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
