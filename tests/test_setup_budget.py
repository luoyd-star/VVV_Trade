#!/usr/bin/env python3
"""setup 装配的单轮预算与水位语义。

守一个真实发生过的事故（2026-08-07）：首轮水位为空时目标是**全部历史**，
实测约 30 根/秒、85 品种 6.8 万根 ≈ 38 分钟，把整个采集轮堵死——
期间 IV 期限链一轮都没跑上，RTH 的不可重来数据白白丢掉。

预算本身还有个更隐蔽的坑：裁剪之后水位若仍按**全量** targets 的 max 推进，
被切掉的根会被永久跳过、再也不会补算——那比不做预算更糟（静默丢数据）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import collector  # noqa: E402


def _install(monkeypatch, all_ts, watermark=None, existing=frozenset()):
    """把 sync_setup_history 的外部依赖全部换成可控假件，只留预算逻辑。"""
    import pandas as pd

    calls = {"targets": None, "scanned_through": None}
    frame = pd.DataFrame({"ts": pd.to_datetime(all_ts, unit="ms", utc=True)})

    monkeypatch.setattr(collector.storage, "get_ohlcv",
                        lambda conn, sym, tf, limit: frame if tf == "4h" else frame)
    monkeypatch.setattr(collector.storage, "ts_to_ms", lambda s: list(all_ts))
    monkeypatch.setattr(collector.storage, "setup_scan_watermark",
                        lambda *a, **k: watermark)
    monkeypatch.setattr(collector.storage, "setup_ts_set", lambda *a, **k: set(existing))
    monkeypatch.setattr(collector.storage, "get_states", lambda *a, **k: [])
    monkeypatch.setattr(collector.storage, "get_vol1h", lambda *a, **k: None)
    monkeypatch.setattr(collector.instruments, "get", lambda s: {"class": "crypto"})

    def walk_forward(symbol, *, target_ts, **kw):
        calls["targets"] = list(target_ts)
        return list(target_ts), []

    monkeypatch.setattr(collector.setup_tracking, "walk_forward", walk_forward)

    def replace_scan(conn, symbol, processed, rows, *, scanned_through, **kw):
        calls["scanned_through"] = scanned_through
        return len(rows)

    monkeypatch.setattr(collector.storage, "replace_setup_scan", replace_scan)
    return calls


BARS = [1_000_000_000_000 + i * 14_400_000 for i in range(500)]


def test_budget_caps_bars_and_is_consumed(monkeypatch):
    """预算限制本轮根数，并从预算里扣掉——否则单轮会跑满全部历史。"""
    calls = _install(monkeypatch, BARS)
    budget = {"bars": 120}
    scanned, _ = collector.sync_setup_history(None, "X-USDT", budget=budget)
    assert scanned == 120, f"应只扫 120 根，实得 {scanned}"
    assert budget["bars"] == 0, "预算没有被扣减，下一个品种会超支"


def test_budget_exhausted_does_no_work(monkeypatch):
    calls = _install(monkeypatch, BARS)
    budget = {"bars": 0}
    scanned, written = collector.sync_setup_history(None, "X-USDT", budget=budget)
    assert (scanned, written) == (0, 0)
    assert calls["targets"] is None, "预算为零时不该调用 walk_forward"


def test_watermark_advances_only_to_processed_bar(monkeypatch):
    """**最关键的一条**：水位只能推进到本轮真正扫到的那根。

    若按全量 targets 的 max 推进，被预算切掉的 380 根会被永久跳过——
    静默丢数据，比不做预算更糟。
    """
    calls = _install(monkeypatch, BARS)
    collector.sync_setup_history(None, "X-USDT", budget={"bars": 120})
    assert calls["scanned_through"] == BARS[119], (
        f"水位应停在第 120 根 {BARS[119]}，实得 {calls['scanned_through']}"
        "——跳过的根将永远不会被补算")
    assert calls["scanned_through"] != BARS[-1], "水位跳到了未处理的末根"


def test_targets_are_a_time_ordered_prefix(monkeypatch):
    """必须取时间升序的**前缀**：walk-forward 的水位语义是"扫到这里为止"，
    取中间一段会让水位跨过未算的根。"""
    calls = _install(monkeypatch, BARS)
    collector.sync_setup_history(None, "X-USDT", budget={"bars": 50})
    assert calls["targets"] == BARS[:50]


def test_next_round_resumes_after_watermark(monkeypatch):
    """第二轮从水位之后继续，不重算也不跳过。"""
    calls = _install(monkeypatch, BARS, watermark=BARS[119])
    collector.sync_setup_history(None, "X-USDT", budget={"bars": 120})
    assert calls["targets"][0] == BARS[120], "没有从水位的下一根继续"
    assert calls["scanned_through"] == BARS[239]


def test_no_budget_keeps_legacy_behaviour(monkeypatch):
    """不传预算时行为不变——防止误伤离线回填脚本等既有调用方。"""
    calls = _install(monkeypatch, BARS)
    scanned, _ = collector.sync_setup_history(None, "X-USDT")
    assert scanned == len(BARS)
    assert calls["scanned_through"] == BARS[-1]
