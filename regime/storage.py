"""SQLite 存储层：collector 与维护脚本写业务表；dashboard/VVVhermes 读业务表、只写 chat；
backtest、experiments 与 coupling 只读 market.db。WAL 模式支持并发读写。

时间戳统一存 UTC 毫秒整数。数据库文件在 data/market.db。
"""
from __future__ import annotations

import json
import os
import sqlite3

import numpy as np
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
  oi REAL, oi_notional REAL, funding REAL, premium REAL, taker_ratio REAL, iv30 REAL, kind TEXT,
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
CREATE TABLE IF NOT EXISTS vol1h(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  volume REAL, quote_vol REAL,
  PRIMARY KEY(symbol, ts)
);
CREATE TABLE IF NOT EXISTS universe_snapshot(
  snapped_at INTEGER NOT NULL, symbol TEXT NOT NULL,
  pool TEXT, theme TEXT, class TEXT, valid_from TEXT,
  PRIMARY KEY(snapped_at, symbol)
);
CREATE TABLE IF NOT EXISTS ref_daily(
  series TEXT NOT NULL, ts INTEGER NOT NULL,
  close REAL, source TEXT,
  PRIMARY KEY(series, ts)
);
CREATE TABLE IF NOT EXISTS bbo(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  bid REAL, bid_qty REAL, ask REAL, ask_qty REAL,
  PRIMARY KEY(symbol, ts)
);
CREATE TABLE IF NOT EXISTS stock_vol(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL, source TEXT NOT NULL,
  iv REAL, hv REAL, underlying_price REAL,
  PRIMARY KEY(symbol, ts, source)
);
CREATE TABLE IF NOT EXISTS stock_option_stat(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL, source TEXT NOT NULL,
  option_volume REAL, call_volume REAL, put_volume REAL, pc_volume_ratio REAL,
  option_oi REAL, call_oi REAL, put_oi REAL, pc_oi_ratio REAL,
  PRIMARY KEY(symbol, ts, source)
);
CREATE TABLE IF NOT EXISTS stock_iv_term(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  iv3 REAL, t3 INTEGER, iv9 REAL, t9 INTEGER, iv30 REAL, t30 INTEGER,
  PRIMARY KEY(symbol, ts)
);
CREATE TABLE IF NOT EXISTS opt_iv_near(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL,
  iv REAL, tenor_days REAL, method TEXT, n_expiries INTEGER, index_price REAL,
  PRIMARY KEY(symbol, ts)
);
CREATE TABLE IF NOT EXISTS stock_vol_live(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL, source TEXT NOT NULL,
  iv REAL, pre_iv REAL, hv_30d REAL,
  vendor_iv_rank REAL, vendor_iv_pct REAL,
  call_volume REAL, put_volume REAL, pc_volume_ratio REAL,
  PRIMARY KEY(symbol, ts, source)
);
CREATE TABLE IF NOT EXISTS earnings(
  symbol TEXT NOT NULL, ts INTEGER NOT NULL, source TEXT NOT NULL,
  pub_type TEXT, period TEXT, known_at INTEGER,
  PRIMARY KEY(symbol, ts, source)
);
CREATE TABLE IF NOT EXISTS breadth(
  ts INTEGER NOT NULL, slot TEXT NOT NULL, source TEXT NOT NULL,
  denom INTEGER,
  up_1d INTEGER, up_20d INTEGER, up_60d INTEGER, up_250d INTEGER, up_ytd INTEGER,
  tail_up INTEGER, tail_dn INTEGER, flat INTEGER, total INTEGER,
  captured_at INTEGER,
  PRIMARY KEY(ts, slot, source)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

# kind: funding 行的显式语义标记（settled=已结算 / pred=快照预测）。靠时间戳猜
# 结算行不可靠——墙钟快照会撞进整点 ±60s 窗口（实测 523 个网格样本混入 18 个）。
_DERIV_COLS = ("oi", "oi_notional", "funding", "premium", "taker_ratio", "iv30", "kind")


def connect() -> sqlite3.Connection:
    """写者连接（collector 专用）：建表 + 迁移。

    迁移里有 ALTER / 兼容性 UPDATE / usvol 定点清理，**只允许 collector 这一个进程
    持有扳机**。面板等只读消费者一律用 connect_ro()。历史上曾有审计代际全表 purge，
    现已拆除并由版本谓词原地重算取代。
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
    """就地迁移 schema/兼容列，并保留 usvol 的一次性定点清理。

    regime_history 不做代际全表删除；版本不匹配的行由 state_ts_set 谓词视为缺失，
    后续在涉改序列上 upsert 原地重算。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(regime_history)")}
    for col in ("features", "rules", "version", "raw_state", "audit_version"):
        if col not in cols:
            conn.execute(f"ALTER TABLE regime_history ADD COLUMN {col} TEXT")
    conn.execute("UPDATE regime_history SET raw_state=state WHERE raw_state IS NULL")
    dcols = {r[1] for r in conn.execute("PRAGMA table_info(deriv)")}
    if "iv30" not in dcols:
        conn.execute("ALTER TABLE deriv ADD COLUMN iv30 REAL")
    if "kind" not in dcols:
        conn.execute("ALTER TABLE deriv ADD COLUMN kind TEXT")
    # earnings 的 point-in-time 缺口（codex 审计）：无 known_at 则无法证明历史 in30
    # 是当时可知的。此列只对**新写入**行有效；既有回填行 known_at 为 NULL，
    # 语义=事后回填、非 point-in-time。当前 v3.1 事件门槛仍消费这些 NULL 历史行，
    # 明确接受先验；known_at 非空覆盖 ≥2 财报季后，才在下一次 RULES_VERSION 升版时
    # 切换严格 as-of。显示层条件分位维持现状（见 GAPS C1/C9）。
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(earnings)")}
    if "known_at" not in ecols:
        conn.execute("ALTER TABLE earnings ADD COLUMN known_at INTEGER")
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
    # 结构性修订：往历史**中间**补进此前缺失的 K 线，同样会让其后的状态失真
    # （那些状态是在缺这一根的序列上算出来的）。只有纯尾部追加可以跳过失效。
    old_max = conn.execute(
        "SELECT MAX(ts) FROM ohlcv WHERE symbol=? AND tf=?", (symbol, tf)
    ).fetchone()[0]
    first_changed = None
    for r in rows:
        prev = old.get(r[2])
        if prev is None:
            if old_max is not None and r[2] < old_max:
                first_changed = r[2] if first_changed is None else min(first_changed, r[2])
            continue
        for a, b in zip(prev, r[3:8]):
            if a is None or not (np.isfinite(a) and np.isfinite(b)):
                # 任一侧非有限（历史遗留 NULL/NaN/Inf）：比较无意义，一律判为已修订
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
    _vals = df[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if not np.isfinite(_vals).all():
        # NaN 与 ±Inf 一并拒收：非有限值落库后，下一轮修订对账的
        # abs(a-b) 对 Inf 恒为 nan（比较为 False），旧状态永远不会被失效
        raise ValueError(f"{symbol} {tf} 行情含 NaN/Inf，拒绝入库（source={source}）")
    # 源粘性（审计 4 路独立发现的潜伏 P0）：ohlcv 主键不含 source，主源宕机时
    # 兜底源会把最近 ~300 根覆盖成另一交易所口径、更早历史仍是旧源——分位窗与
    # 状态窗变成拼接序列。宁缺毋滥：序列已有既定源时，**拒绝**异源写入
    #（一天没数据可接受，混口径序列不可接受）。换源必须走显式迁移（删序列重建）。
    prev = conn.execute(
        "SELECT source FROM ohlcv WHERE symbol=? AND tf=? AND source IS NOT NULL"
        " ORDER BY ts DESC LIMIT 1", (symbol, tf),
    ).fetchone()
    if prev and prev[0] and prev[0] != source:
        raise ValueError(
            f"{symbol} {tf} 拒绝混源写入：序列既定源={prev[0]}，本次={source}"
            "（源粘性纪律；换源须显式迁移）"
        )
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


def upsert_states(conn, symbol: str, tf: str, rows, commit: bool = True) -> None:
    """rows: (ts_ms, state, confidence, features_json, rules_json, version, audit_version)。

    commit=False 供 collector 把"写 raw + 迟滞确认"合成单事务——两次独立提交之间
    的中间态会让面板把未确认的 raw 冒充确认态（审计 3 路独立发现），崩溃则残留到下一轮。"""
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
    if commit:
        conn.commit()


def set_confirmed(conn, symbol: str, tf: str, pairs, commit: bool = True) -> None:
    """pairs: (confirmed_state, ts_ms)——把迟滞折叠后的确认态写回 state 列。"""
    conn.executemany(
        "UPDATE regime_history SET state=? WHERE symbol=? AND tf=? AND ts=?",
        [(s, symbol, tf, int(t)) for s, t in pairs],
    )
    if commit:
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


def upsert_vol1h(conn, symbol: str, rows) -> int:
    """币安 1h 量流（VWAP 专用量源，与 OHLCV 主源解耦）。

    rows: [(ts_ms, volume, quote_vol), ...]。quote_vol/volume 即该小时的
    精确 VWAP（币安 klines 原生字段，非典型价近似）。ts 为开盘时刻（口径同 ohlcv）。
    """
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO vol1h(symbol,ts,volume,quote_vol) VALUES (?,?,?,?)",
            [(symbol, int(t), float(v), float(q)) for t, v, q in rows],
        )
    return len(rows)


def get_vol1h(conn, symbol: str, since_ms: int = 0) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT ts, volume, quote_vol FROM vol1h WHERE symbol=? AND ts>=? ORDER BY ts",
        (symbol, int(since_ms)),
    ).fetchall()
    return pd.DataFrame(rows, columns=["ts", "volume", "quote_vol"])


def vol1h_watermark(conn, symbol: str):
    row = conn.execute("SELECT MAX(ts) FROM vol1h WHERE symbol=?", (symbol,)).fetchone()
    return row[0]


def upsert_ref_daily(conn, series: str, rows, source: str) -> int:
    """底层参考日线（耦合研究历史层，2020-2026 回填 + 日更）。

    ts = 交易日 00:00 UTC 毫秒（日期键）。时点语义因源而异（美股类 close 是
    该日 16:00 ET，BTC 是该日 UTC 日线收盘）——对齐留给研究层按 C8 协议做，
    存储层只存原生语义，source 字段留痕。
    """
    if not rows:
        return 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO ref_daily(series,ts,close,source) VALUES (?,?,?,?)",
            [(series, int(t), float(c), source) for t, c in rows if c is not None],
        )
    return len(rows)


def get_ref_daily(conn, series: str, since_ms: int = 0) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT ts, close FROM ref_daily WHERE series=? AND ts>=? ORDER BY ts",
        (series, int(since_ms)),
    ).fetchall()
    return pd.DataFrame(rows, columns=["ts", "close"])


def get_states_audit(conn, symbol: str, tf: str, include_rules: bool = False):
    """回测用全量状态行（含审计快照）。按 ts 升序；JSON 列解析为对象。

    与 get_states 分开：面板只要末端窗口的轻量列，回测要全历史 + 健康位
    （a8 起 features 里有 win/warmup）。版本过滤交给调用方——回测按
    (version, audit_version) 分桶是硬纪律，这里原样带出。

    rules 是逐根判定规则命中的审计账本，只有显式 include_rules=True 才增选并
    返回；默认不选，避免全历史调用无意放大查询与 Python payload。
    """
    fields = "ts,state,raw_state,confidence,features,version,audit_version"
    if include_rules:
        fields += ",rules"
    rows = conn.execute(
        f"SELECT {fields} FROM regime_history"
        " WHERE symbol=? AND tf=? ORDER BY ts",
        (symbol, tf),
    ).fetchall()
    out = []
    for r in rows:
        try:
            feats = json.loads(r[4]) if r[4] else {}
        except (TypeError, ValueError):
            feats = {}
        item = {
            "ts": r[0], "state": r[1], "raw_state": r[2] or r[1],
            "confidence": r[3], "features": feats,
            "version": r[5], "audit_version": r[6],
        }
        if include_rules:
            try:
                item["rules"] = json.loads(r[7]) if r[7] else {}
            except (TypeError, ValueError):
                item["rules"] = {}
        out.append(item)
    return out


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
    # settled：kind='settled' 显式命中；kind 为 NULL 的旧行退回 **8h 结算格**判定
    # （只是升级旧库后、首次每日补采前的过渡通道——补采会把全部真结算行重写为
    # kind='settled'，此后这个分支不再命中任何行。不能用 1h 格：墙钟快照撞进
    # 整点 ±60s 的旧污染行会被重新放进来）
    grid = (
        " AND (kind='settled'"
        " OR (kind IS NULL AND MIN(ts % 28800000, 28800000 - ts % 28800000) < 60000))"
        if hourly_grid else ""
    )
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


def upsert_stock_vol(conn, symbol: str, source: str, rows) -> None:
    """个股波动率日频序列。rows: dict(ts, iv, hv, underlying_price) 可迭代。

    **source 进主键**是刻意的：不同源（moomoo 期权链聚合 / CBOE iv30 / ORATS iv30d）
    算法不同，绝对值有系统性偏差。分列存放让"混拼历史算分位"在结构上不可能发生，
    读取侧必须显式指定口径——见 get_stock_vol。
    """
    conn.executemany(
        "INSERT INTO stock_vol(symbol,ts,source,iv,hv,underlying_price) VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(symbol,ts,source) DO UPDATE SET"
        " iv=COALESCE(excluded.iv,iv), hv=COALESCE(excluded.hv,hv),"
        " underlying_price=COALESCE(excluded.underlying_price,underlying_price)",
        [
            (symbol, int(r["ts"]), source,
             _f(r.get("iv")), _f(r.get("hv")), _f(r.get("underlying_price")))
            for r in rows
        ],
    )
    conn.commit()


def _f(v):
    """数值清洗：None/NaN/±inf 一律 None——inf 进分位分母会吞掉整个分布。"""
    import math

    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def get_stock_vol(conn, symbol: str, source: str, limit: int = 1500) -> pd.DataFrame:
    """单一口径内的日频序列（升序）。source 必填——不给默认值是为了逼调用方表态。"""
    df = pd.read_sql_query(
        "SELECT ts,iv,hv,underlying_price FROM stock_vol WHERE symbol=? AND source=?"
        " ORDER BY ts DESC LIMIT ?",
        conn,
        params=(symbol, source, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


_OPTSTAT_COLS = ("option_volume", "call_volume", "put_volume", "pc_volume_ratio",
                 "option_oi", "call_oi", "put_oi", "pc_oi_ratio")


def upsert_stock_option_stat(conn, symbol: str, source: str, rows) -> None:
    """个股期权流日频序列（put/call 成交比与持仓比等）。

    **纯采集，不参与任何判定**——put/call 是被过度使用的指标，其样本外证据
    正在调研中；先把易逝的历史留下来，价值判断交给回测。与 stock_vol 同样
    source 进主键、COALESCE 合并（当日 OI 为 T-1 延迟，先写成交后补持仓）。
    """
    cols = ",".join(_OPTSTAT_COLS)
    ph = ",".join("?" * len(_OPTSTAT_COLS))
    sets = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in _OPTSTAT_COLS)
    conn.executemany(
        f"INSERT INTO stock_option_stat(symbol,ts,source,{cols}) VALUES(?,?,?,{ph})"
        f" ON CONFLICT(symbol,ts,source) DO UPDATE SET {sets}",
        [(symbol, int(r["ts"]), source, *(_f(r.get(c)) for c in _OPTSTAT_COLS))
         for r in rows],
    )
    conn.commit()


def get_stock_option_stat(conn, symbol: str, source: str, limit: int = 1500) -> pd.DataFrame:
    """单一口径内的期权流序列（升序）。source 必填。"""
    df = pd.read_sql_query(
        f"SELECT ts,{','.join(_OPTSTAT_COLS)} FROM stock_option_stat"
        " WHERE symbol=? AND source=? ORDER BY ts DESC LIMIT ?",
        conn,
        params=(symbol, source, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


_BREADTH_COLS = ("denom", "up_1d", "up_20d", "up_60d", "up_250d", "up_ytd",
                 "tail_up", "tail_dn", "flat", "total", "captured_at")


def upsert_breadth(conn, ts: int, slot: str, source: str, row: dict) -> None:
    """市场宽度快照（**影子字段**，只采不消费，攒够约 2 年再评审转正）。

    slot 进主键：09:45 与 15:59 的采样口径不同（隔夜跳空后的盘中 vs 近收盘），
    混入同一序列会让分位失去意义。
    """
    cols = ",".join(_BREADTH_COLS)
    ph = ",".join("?" * len(_BREADTH_COLS))
    sets = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in _BREADTH_COLS)
    conn.execute(
        f"INSERT INTO breadth(ts,slot,source,{cols}) VALUES(?,?,?,{ph})"
        f" ON CONFLICT(ts,slot,source) DO UPDATE SET {sets}",
        (int(ts), slot, source, *(row.get(c) for c in _BREADTH_COLS)),
    )
    conn.commit()


def get_breadth(conn, slot: str, source: str, limit: int = 800) -> pd.DataFrame:
    df = pd.read_sql_query(
        f"SELECT ts,{','.join(_BREADTH_COLS)} FROM breadth"
        " WHERE slot=? AND source=? ORDER BY ts DESC LIMIT ?",
        conn, params=(slot, source, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


_TERM_COLS = ("iv3", "t3", "iv9", "t9", "iv30", "t30")


def upsert_stock_iv_term(conn, rows) -> None:
    """个股 IV 期限曲线快照（3d/9d/30d ATM）。实际期限随值落库（t3/t9/t30）。"""
    cols = ",".join(_TERM_COLS)
    ph = ",".join("?" * len(_TERM_COLS))
    sets = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in _TERM_COLS)
    conn.executemany(
        f"INSERT INTO stock_iv_term(symbol,ts,{cols}) VALUES(?,?,{ph})"
        f" ON CONFLICT(symbol,ts) DO UPDATE SET {sets}",
        [(r["symbol"], int(r["ts"]), *(_f(r.get(c)) for c in _TERM_COLS))
         for r in rows],
    )
    conn.commit()


def get_stock_iv_term(conn, symbol: str, limit: int = 500) -> pd.DataFrame:
    df = pd.read_sql_query(
        f"SELECT ts,{','.join(_TERM_COLS)} FROM stock_iv_term"
        " WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        conn, params=(symbol, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


def upsert_opt_iv_near(conn, row: dict) -> None:
    """币安期权近端 IV 快照。tenor_days 必须随值落库——不同期限的 IV 不是同一个量。"""
    conn.execute(
        "INSERT INTO opt_iv_near(symbol,ts,iv,tenor_days,method,n_expiries,index_price)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(symbol,ts) DO UPDATE SET"
        " iv=excluded.iv, tenor_days=excluded.tenor_days, method=excluded.method,"
        " n_expiries=excluded.n_expiries, index_price=excluded.index_price",
        (row["symbol"], int(row["ts"]), _f(row.get("iv")), _f(row.get("tenor_days")),
         row.get("method"), row.get("n_expiries"), _f(row.get("index_price"))),
    )
    conn.commit()


def get_opt_iv_near(conn, symbol: str, limit: int = 500) -> pd.DataFrame:
    df = pd.read_sql_query(
        "SELECT ts,iv,tenor_days,method,n_expiries,index_price FROM opt_iv_near"
        " WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        conn, params=(symbol, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


_LIVE_COLS = ("iv", "pre_iv", "hv_30d", "vendor_iv_rank", "vendor_iv_pct",
              "call_volume", "put_volume", "pc_volume_ratio")


def upsert_stock_vol_live(conn, source: str, rows) -> None:
    """盘中实时 IV 快照。rows: dict(symbol, ts, ...)。

    **与 stock_vol 分表是刻意的**，同 live_bars 与 ohlcv 的关系：
    盘中值还在滚动，混进 stock_vol 会让分位分母含一个当天还会变的数
    （同一天算两次分位得数不同）。分位永远只在结算序列上算，
    实时值只做预览展示——见 dashboard._stock_iv_block 的 live 段。
    """
    cols = ",".join(_LIVE_COLS)
    ph = ",".join("?" * len(_LIVE_COLS))
    sets = ",".join(f"{c}=COALESCE(excluded.{c},{c})" for c in _LIVE_COLS)
    conn.executemany(
        f"INSERT INTO stock_vol_live(symbol,ts,source,{cols}) VALUES(?,?,?,{ph})"
        f" ON CONFLICT(symbol,ts,source) DO UPDATE SET {sets}",
        [(r["symbol"], int(r["ts"]), source, *(_f(r.get(c)) for c in _LIVE_COLS))
         for r in rows],
    )
    conn.commit()


def get_stock_vol_live(conn, symbol: str, source: str, limit: int = 200) -> pd.DataFrame:
    """盘中快照序列（升序）。limit 默认只取最近 200 个点（一个交易日约 78 个）。"""
    df = pd.read_sql_query(
        f"SELECT ts,{','.join(_LIVE_COLS)} FROM stock_vol_live"
        " WHERE symbol=? AND source=? ORDER BY ts DESC LIMIT ?",
        conn, params=(symbol, source, limit),
    )
    return df.iloc[::-1].reset_index(drop=True)


def upsert_earnings(conn, source: str, rows) -> None:
    """财报日历。rows: dict(symbol, ts, pub_type, period)。

    存在的理由不是预测财报，而是**给 IV 分位标注事件邻近度**：实测 NVDA 财报
    事前峰→事后谷崩塌 38%，占分位分母 9.9%（docs/EARNINGS_IV_CONTAMINATION_20260804.md）。
    分母不清洗（财报是该股 IV 分布的结构性部分），但当下读数须标注。
    """
    import time as _time

    now = int(_time.time() * 1000)
    conn.executemany(
        "INSERT INTO earnings(symbol,ts,source,pub_type,period,known_at)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(symbol,ts,source) DO UPDATE SET"
        " pub_type=COALESCE(excluded.pub_type,pub_type),"
        " period=COALESCE(excluded.period,period),"
        # known_at 取首见时刻，重写不得刷新——否则 point-in-time 语义被破坏
        " known_at=COALESCE(known_at,excluded.known_at)",
        [(r["symbol"], int(r["ts"]), source, r.get("pub_type"), r.get("period"), now)
         for r in rows],
    )
    conn.commit()


def earnings_event_windows(conn, symbol: str, ts_list, horizon_days: int = 10,
                           source: str = "moomoo") -> list:
    """逐 bar 判定：该时刻起 horizon_days 个日历日内是否有财报。→ list[bool]。

    给确认层的事件门槛用（RULES_VERSION v3）。**point-in-time 说明**：
    对 ≤10 日的前瞻窗，"财报日期在 t 时刻已知"是极强先验——财报日期通常提前
    数周至数月公布，最后 10 天内改期极罕见。earnings.known_at 对新写入行记录
    首见时刻；历史回填行为 NULL（事后回填），本函数不按 known_at 过滤——
    这是**记录在案的先验**而非疏漏（docs/AUDIT_CODEX_20260805.md 处置项）。
    若日后要求严格 as-of，收紧点只在此一处。
    """
    rows = conn.execute(
        "SELECT ts FROM earnings WHERE symbol=? AND source=?", (symbol, source),
    ).fetchall()
    if not rows:
        return [False] * len(ts_list)
    import numpy as np
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    # **必须按 ET 日历日比较，不能按 UTC 毫秒**（v3.1 修复，6 路审计独立发现）：
    # 财报 ts 锚在其日期 00:00 UTC，毫秒严格比较 `ed > bar_ts` 会把**财报当天整天**
    # 排除在窗外——恰是开盘跳空/流动性最差、最需要门槛的一天；且固定 240h 窗口
    # 在跨 DST 时与 ET 日历差漂移 1 小时。日差语义：0 ≤ (财报日 − bar 的 ET 日) ≤ horizon，
    # 含第 0 天（盘后财报发布前的当日 RTH 正在窗内）。
    et = ZoneInfo("America/New_York")
    e_days = np.array(
        [_dt.fromtimestamp(r[0] / 1000, tz=_tz.utc).date().toordinal() for r in rows],
        dtype="int64",
    )
    b_days = np.array(
        [_dt.fromtimestamp(int(t) / 1000, tz=et).date().toordinal() for t in ts_list],
        dtype="int64",
    )
    diff = e_days[None, :] - b_days[:, None]
    return ((diff >= 0) & (diff <= int(horizon_days))).any(axis=1).tolist()


def prune_stale_future_earnings(conn, source: str, symbols, start_ts: int,
                                end_ts: int, fresh_rows) -> int:
    """未来窗口内以最新日历为权威：删掉本批品种在窗内、但不在最新日历里的行。

    codex 审计：财报从 8-20 改到 8-21 时旧行永久残留，继续参与邻近度与条件分位。
    只清**未来**（start_ts 应 ≥ 今天）——历史行是既成事实永不触碰；
    对未改动的行不做删+插（保住 known_at 的 point-in-time 语义）。

    **fail-safe 边界**（第二轮审计，3 路独立发现）：接口"成功但静默空缺"时不得
    把有效日历当改期删除——① fresh 为空整体跳过；② 只对 **fresh 里出现过的品种**
    做清理（品种级 fail-open 保护：厂商漏了某品种，就不动它的旧行）。
    代价是"某品种唯一未来财报被撤销"的旧行要等它再次出现在日历里才清——
    宁可残留，不可误删（删除不可恢复）。
    """
    if not fresh_rows:
        return 0
    keep = {(r["symbol"], int(r["ts"])) for r in fresh_rows}
    fresh_syms = {r["symbol"] for r in fresh_rows}
    prunable = [s for s in symbols if s in fresh_syms]
    if not prunable:
        return 0
    stale = [
        (s, t) for s, t in conn.execute(
            f"SELECT symbol, ts FROM earnings WHERE source=? AND ts BETWEEN ? AND ?"
            f" AND symbol IN ({','.join('?' * len(prunable))})",
            (source, int(start_ts), int(end_ts), *prunable),
        ).fetchall()
        if (s, int(t)) not in keep
    ]
    for s, t in stale:
        conn.execute("DELETE FROM earnings WHERE symbol=? AND ts=? AND source=?",
                     (s, int(t), source))
    if stale:
        conn.commit()
    return len(stale)


def earnings_proximity(conn, symbol: str, now_ms: int, horizon: int = 10,
                       source: str = "moomoo") -> dict | None:
    """距最近财报的**日历天数**：{"days": ±N, "ts": ...}，超出 horizon 返回 None。

    正数=还有 N 天，负数=已过 N 天。

    **必须比日期而不是比时间戳**：财报 ts 锚在其日期的 00:00 UTC，而 now_ms 是当下
    时刻，两者相减再除 86400000 会得到分数日——当日 14:00 UTC 查今天的财报会算出
    −0.58 并四舍五入成"已过 1 天"，整条标注系统性偏移一日（实测 AMD/APP/CRCL 三例全错）。
    "今天"取 **ET 日历日**，因为财报日期本身是美东交易日历上的日期。
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    # 必须按 **ET 日历差**选最近事件，不能按 UTC 毫秒距离选——codex 抓到的边界：
    # ET 23:30（=UTC 次日 03:30）时按毫秒距离会选中明天的财报（days=+1），
    # 而今天的财报（days=0）才是最近的。行数每品种 ≤ 二十来条，全取内存算。
    rows = conn.execute(
        "SELECT ts FROM earnings WHERE symbol=? AND source=?", (symbol, source),
    ).fetchall()
    if not rows:
        return None
    today = _dt.fromtimestamp(now_ms / 1000, tz=ZoneInfo("America/New_York")).date()
    cands = [
        ((_dt.fromtimestamp(r[0] / 1000, tz=_tz.utc).date() - today).days, int(r[0]))
        for r in rows
    ]
    best = min(cands, key=lambda x: (abs(x[0]), x[0]))
    if abs(best[0]) > horizon:
        return None
    return {"days": int(best[0]), "ts": best[1]}


def stock_vol_latest_ranks(conn, source: str, win: int = 252, min_n: int = 120,
                           cond_win: int = 504, earn_window_d: int = 30,
                           cond_min: int = 60) -> dict:
    """全品种最新 IV 与分位：{symbol: (iv, rank|None, n, kind)}。两次查询算完。

    给 Hermes 横截面用。**分位与面板同口径**：有财报记录且同状态样本足够时用
    财报条件分位（2 年窗、同 in30 状态比），否则退回普通 win 日分位——
    codex 审计抓到横截面绕过条件化，NVDA 面板 0.12 而横截面 0.66，
    把刚消除的财报污染重新端给 Hermes 做跨品种比较。kind 标注口径。
    """
    import numpy as np

    df = pd.read_sql_query(
        "SELECT symbol, ts, iv FROM stock_vol WHERE source=? AND iv IS NOT NULL"
        " ORDER BY symbol, ts",
        conn,
        params=(source,),
    )
    ea = pd.read_sql_query(
        "SELECT symbol, ts FROM earnings WHERE source=? ORDER BY symbol, ts",
        conn, params=(source,),
    )
    emap = {sym: g["ts"].to_numpy(dtype="int64") for sym, g in ea.groupby("symbol")}
    out = {}
    for sym, g in df.groupby("symbol"):
        n = len(g)
        last = float(g["iv"].iloc[-1])
        if n < min_n:
            out[sym] = (round(last, 2), None, n, "raw")
            continue
        rank = None
        kind = "raw"
        ed = emap.get(sym)
        if ed is not None and len(ed):
            sw = g.tail(cond_win)
            ts = sw["ts"].to_numpy(dtype="int64")[:, None]
            ahead = (ed[None, :] - ts) / 86_400_000.0
            days_to = np.where(ahead >= 0, ahead, np.inf).min(axis=1)
            in30 = days_to <= earn_window_d
            peer = sw["iv"].to_numpy()[:-1][in30[:-1] == bool(in30[-1])]
            if len(peer) >= cond_min:
                rank = round(float((peer < last).mean()), 3)
                kind = "cond"
        if rank is None:
            w = g["iv"].tail(win)
            rank = round(float((w < last).mean()), 3)
        out[sym] = (round(last, 2), rank, n, kind)
    return out


def stock_vol_coverage(conn, source: str) -> pd.DataFrame:
    """每个品种在该口径下的覆盖情况（行数/起止），供面板与运维体检用。"""
    return pd.read_sql_query(
        "SELECT symbol, COUNT(iv) n, MIN(ts) t0, MAX(ts) t1 FROM stock_vol"
        " WHERE source=? AND iv IS NOT NULL GROUP BY symbol ORDER BY symbol",
        conn,
        params=(source,),
    )


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


def table_names(conn) -> list[str]:
    """现库业务表目录；SQLite 内部自增表 sqlite_sequence 不对外暴露。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type='table' AND name<>'sqlite_sequence' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_columns(conn, table: str) -> list[str]:
    """返回目录内一张表的字段；拒绝目录外名字后再拼接标识符。"""
    if table not in set(table_names(conn)):
        raise ValueError(f"unknown table: {table}")
    quoted = '"' + table.replace('"', '""') + '"'
    return [r[1] for r in conn.execute(f"PRAGMA table_info({quoted})").fetchall()]


def counts(conn) -> dict:
    """动态统计全部业务表；新管线建表后无需再维护手写清单。"""
    out = {}
    for table in table_names(conn):
        quoted = '"' + table.replace('"', '""') + '"'
        out[table] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return out
