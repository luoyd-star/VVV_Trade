#!/usr/bin/env python3
"""usvol 权威分层与时间格的确定性回归测试。

用法: .venv/bin/python tests/test_usvol_authority.py

守的是一个真实发生过的 bug：CSV 官方收盘价先写、延迟报价后写且同一主键
无条件覆盖（last-write-wins），导致官方值每轮都被盘中报价盖回去；采集器
若错过收盘窗口，该交易日就永久停在盘中值（实测偏差可达 1.8 个 VIX 点）。

用注入的假数据源跑 collector.sync_vol_index，全程内存库，不碰真库与网络。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collector import sync_vol_index  # noqa: E402
from regime import storage  # noqa: E402
from regime.usvol import _quote_trading_day, day_ms  # noqa: E402

D30, D31, D01 = day_ms(date(2026, 7, 30)), day_ms(date(2026, 7, 31)), day_ms(date(2026, 8, 1))


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(storage._SCHEMA)
    return conn


def close_of(conn, ts):
    row = conn.execute("SELECT close FROM usvol WHERE idx='VIX' AND ts=?", (ts,)).fetchone()
    return row[0] if row else None


def run(conn, csv_rows, quote, now, calls=None):
    def fetch_csv(_name):
        if calls is not None:
            calls.append(now)
        if isinstance(csv_rows, Exception):
            raise csv_rows
        return csv_rows

    def fetch_quote(_name):
        if isinstance(quote, Exception):
            raise quote
        return quote

    return sync_vol_index(
        conn, "VIX", fetch_csv=fetch_csv, fetch_quote=fetch_quote, now=now
    )


def main() -> None:
    T0 = 1_785_000_000.0  # 任意基准墙钟

    # ① 官方收盘价不得被延迟报价盖回（本测试存在的理由）
    conn = fresh_db()
    run(conn, [(D30, 18.0), (D31, 15.99)], {"ts": D31, "close": 99.0}, T0)
    got = close_of(conn, D31)
    print(f"① 官方不被盖回   07-31 库值={got}（报价 99.0 必须被拒）")
    assert got == 15.99, f"官方收盘价被报价覆盖了：{got}"

    # ② CSV 未覆盖的当日，报价照常补进来（临时值不能丢）
    run(conn, [(D30, 18.0), (D31, 15.99)], {"ts": D01, "close": 16.4}, T0 + 60)
    print(f"② 未覆盖日补值   08-01 库值={close_of(conn, D01)}")
    assert close_of(conn, D01) == 16.4
    assert close_of(conn, D31) == 15.99, "补当日不应动到已确权的前一日"

    # ③ 进入新交易日会触发补确权（不靠 24h 墙钟碰运气），随后官方值胜出
    calls = []
    run(conn, [(D31, 15.99), (D01, 16.1)], {"ts": D01, "close": 16.4}, T0 + 7200, calls)
    print(f"③ 补确权         CSV 重拉={len(calls)} 次 → 08-01 库值={close_of(conn, D01)}")
    assert len(calls) == 1, "报价进入未覆盖交易日后应立即重拉 CSV"
    assert close_of(conn, D01) == 16.1, "确权后当日应为官方收盘价"

    # ④ 1 小时重试下限：刚拉过就不再重拉（防刷 600KB×5）
    calls = []
    run(conn, [(D31, 15.99)], {"ts": D01, "close": 16.9}, T0 + 7200 + 600, calls)
    print(f"④ 重试下限       10 分钟后 CSV 重拉={len(calls)} 次（应为 0）")
    assert len(calls) == 0
    assert close_of(conn, D01) == 16.1, "闸关着时不得让报价覆盖已确权的当日"

    # ⑤ 故障隔离：CSV 挂了，报价仍要写进未覆盖的交易日
    conn2 = fresh_db()
    storage.set_meta(conn2, "usvol_csv_max_VIX", str(D30))
    storage.set_meta(conn2, "usvol_csv_at_VIX", str(T0))
    msg, errs = run(conn2, RuntimeError("CDN 503"), {"ts": D31, "close": 17.2}, T0 + 90_000)
    print(f"⑤ 故障隔离       CSV 失败 errs={len(errs)} → 07-31 库值={close_of(conn2, D31)}")
    assert close_of(conn2, D31) == 17.2, "CSV 失败不该拖累报价路径"
    assert len(errs) == 1 and "csv" in errs[0]

    # ⑥ 反向隔离：报价挂了，CSV 仍要落地
    conn3 = fresh_db()
    msg, errs = run(conn3, [(D30, 18.0), (D31, 15.99)], RuntimeError("超时"), T0)
    print(f"⑥ 反向隔离       报价失败 errs={len(errs)} → 07-31 库值={close_of(conn3, D31)}")
    assert close_of(conn3, D31) == 15.99
    assert len(errs) == 1 and "quote" in errs[0]

    # ⑦ 时间格：写进库的 ts 一律对齐交易日 00:00 UTC
    bad = conn.execute("SELECT count(*) FROM usvol WHERE ts % 86400000 <> 0").fetchone()[0]
    print(f"⑦ 时间格         未对齐行={bad}")
    assert bad == 0

    # ⑧ 交易日推断：周末/盘后拉到的是上一交易日，不得造出周末行
    assert _quote_trading_day({"last_trade_time": "2026-07-31T16:15:01"}) == date(2026, 7, 31)
    assert _quote_trading_day({"last_trade_time": "2026-07-31T15:15:01.233000-05:00"}) == date(2026, 7, 31)
    fallback = _quote_trading_day({"last_trade_time": "垃圾"})
    print(f"⑧ 交易日推断     ET 时刻/CT 带偏移均归 07-31；脏值回退={fallback}")
    assert isinstance(fallback, date)

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()


def test_all():
    """pytest 入口：main() 内的 assert 即断言，任何一条失败即测试失败。"""
    main()
