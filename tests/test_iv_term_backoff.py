#!/usr/bin/env python3
"""IV 期限链的固定分组与失败退避。

用法: .venv/bin/pytest tests/test_iv_term_backoff.py

守两个真实发生过的问题：
1. 建不成的品种每轮继续占名额——2026-08-07 实测每轮 16 个槽位里有 4~6 个在重试
   PANW/EWY/EWJ/APP，成功率因此只有 58%，当天近 30 个品种拿不到期限曲线。
2. 退避本身的反噬：撞账号级限频时**整批一起失败**，若无脑记账，一次限频就会把
   当轮品种全部拉黑一天，比不做退避更糟。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import stock_iv_term as t  # noqa: E402

SYMS = [f"S{i}-USDT" for i in range(20)]


# ---------- 固定分组 ----------

def test_groups_are_deterministic_and_partition_all_symbols():
    """同一品种永远落在同一组；所有组并起来恰好是全集，不重不漏。"""
    seen = []
    for r in range(t.BUILD_GROUPS):
        seen += t.build_group(SYMS, r)
    assert sorted(seen) == sorted(SYMS), "分组必须恰好覆盖全集"
    assert len(seen) == len(set(seen)), "同一品种不得落入两组"
    # 确定性：同一轮次多次调用结果一致；轮次 +BUILD_GROUPS 回到同一组
    assert t.build_group(SYMS, 3) == t.build_group(SYMS, 3)
    assert t.build_group(SYMS, 3) == t.build_group(SYMS, 3 + t.BUILD_GROUPS)


def test_group_size_is_balanced():
    sizes = [len(t.build_group(SYMS, r)) for r in range(t.BUILD_GROUPS)]
    assert max(sizes) - min(sizes) <= 1, f"分组不均衡: {sizes}"


def test_empty_symbols_is_safe():
    assert t.build_group([], 0) == []


# ---------- 失败退避 ----------

def test_fail_is_counted_once_per_symbol_not_per_target():
    """同一品种三个期限都失败只算一次——否则计数三倍速膨胀、误伤偶发失败。"""
    lines = ["PANW-USDT/3d:链(-1)", "PANW-USDT/9d:链(-1)", "PANW-USDT/30d:链(-1)"]
    out = t.count_fails({}, lines, ["PANW-USDT", "OK-USDT"], built_now=1)
    assert out == {"PANW-USDT": 1}


def test_whole_batch_failure_is_treated_as_systemic_and_counts_nothing():
    """整批全灭 = 限频，不许记任何品种的账（这条防的是退避本身的反噬）。"""
    lines = [f"{s}:无现价" for s in SYMS[:5]]
    out = t.count_fails({}, lines, SYMS[:5], built_now=0)
    assert out == {}, "整批失败时记账会让一次限频拉黑一整轮品种"


def test_partial_failure_counts_only_the_failed_symbols():
    lines = ["A-USDT:无现价", "B-USDT/3d:链(-1)"]
    out = t.count_fails({"A-USDT": 1}, lines, ["A-USDT", "B-USDT", "C-USDT"], built_now=1)
    assert out == {"A-USDT": 2, "B-USDT": 1}


def test_failures_outside_this_round_are_ignored():
    """只给本轮真正尝试过的品种记账。"""
    out = t.count_fails({}, ["Z-USDT:无现价"], ["A-USDT"], built_now=1)
    assert out == {}


def test_blocked_only_after_reaching_the_limit():
    fails = {"A-USDT": t.MAX_DAY_FAILS - 1, "B-USDT": t.MAX_DAY_FAILS,
             "C-USDT": t.MAX_DAY_FAILS + 3}
    assert t.blocked_symbols(fails) == {"B-USDT", "C-USDT"}
    assert t.blocked_symbols({}) == set()


def test_build_codes_skips_blocked_symbols():
    """被拉黑的品种不得再进 todo——这正是被浪费的那 40% 名额。"""
    calls = {}

    class _Ctx:
        def get_market_snapshot(self, codes):
            calls["codes"] = codes
            raise AssertionError("不该走到这里：本用例只验 todo 的筛选")

    try:
        t.build_codes(_Ctx(), ["A-USDT", "B-USDT"], existing={},
                      skip={"A-USDT", "B-USDT"})
    except AssertionError:
        raise
    except Exception:  # noqa: BLE001
        pass
    # 全部被 skip → todo 为空 → 直接返回，连现价快照都不该发
    assert "codes" not in calls, "被拉黑的品种仍然发起了请求"


def test_build_codes_without_skip_still_builds():
    """退避是可选参数，不传时行为与从前一致（防止误伤既有调用方）。"""
    seen = {}

    class _Ctx:
        def get_market_snapshot(self, codes):
            seen["n"] = len(codes)
            raise RuntimeError("stop")

    try:
        t.build_codes(_Ctx(), ["A-USDT", "B-USDT"], existing={})
    except RuntimeError:
        pass
    assert seen.get("n") == 2


# ---------- 容量 ----------

def test_capacity_covers_current_universe_with_margin():
    cap = t.build_capacity(71)
    assert cap["ok"], f"当前品种数已超出建链容量: {cap}"
    assert cap["margin_pct"] > 30, f"余量过薄，扩容会立刻压垮: {cap}"


def test_capacity_reports_shortfall_when_universe_grows():
    cap = t.build_capacity(10_000)
    assert not cap["ok"] and cap["margin_pct"] < 0


def test_round_tops_up_to_batch_when_group_is_short(monkeypatch):
    """本组不足一批时必须用其余缺失品种补满——否则容量白白浪费。

    实测触发：第0组只剩 4 个待建就只做了 4 个，而同时还有 41 个在等、
    BUILD_BATCH 是 16。分组换来的是确定性，不该同时换来吞吐损失。
    """
    import collector
    from regime import calendar_nyse, moomoo_iv, stock_iv_term

    class Ctx:
        def close(self):
            pass

    import sqlite3
    from regime import storage
    conn = sqlite3.connect(":memory:")
    conn.executescript(storage._SCHEMA)

    syms = [f"S{i}-USDT" for i in range(60)]
    seen = []
    # 只有第 0 组的头两个还缺；其余组大量缺失
    group0 = stock_iv_term.build_group(syms, 0)
    done = {s: {t: {} for t in stock_iv_term.TARGETS} for s in group0[2:]}

    monkeypatch.setattr(calendar_nyse, "is_rth", lambda now: True)
    monkeypatch.setattr(moomoo_iv, "opend_alive", lambda: True)
    monkeypatch.setattr(moomoo_iv, "open_ctx", lambda: Ctx())
    monkeypatch.setattr(collector, "_should", lambda *a: True)
    monkeypatch.setattr(collector.instruments, "get",
                        lambda symbol: {"class": "us_stock_perp"})
    monkeypatch.setattr(stock_iv_term, "load_codes_cache", lambda conn: done)

    def build(ctx, symbols, existing=None, skip=None):
        seen.append(list(symbols))
        return dict(existing or {}), []

    monkeypatch.setattr(stock_iv_term, "build_codes", build)
    collector.sync_stock_iv_term(conn, syms)

    todo = seen[0]
    assert len(todo) == stock_iv_term.BUILD_BATCH, (
        f"本组只剩 2 个就该补满到 {stock_iv_term.BUILD_BATCH}，实得 {len(todo)}")
    # 本组的缺失品种必须排在最前（分组仍是主序）
    assert todo[:2] == group0[:2], "补位不得打乱分组的主序"
