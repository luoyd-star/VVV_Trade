#!/usr/bin/env python3
"""fetch_dvol 分页的确定性回归测试。

用法: .venv/bin/pytest tests/test_dvol_paging.py

守的是一个真实存在的静默截断：Deribit get_volatility_index_data 单次最多回
1000 行，超限时**不报错、不分页提示**，直接只给最近 1000 天。2026-08-06 实测
求 1500 天只拿到 1000 天（2023-11-11 起），看起来像"DVOL 历史就这么长"——
实际 BTC/ETH 自 2021-03-24 就有数，分三页可取满 1961 天。

用假的分页端点跑，全程不碰网络。
"""
from __future__ import annotations

import os
import sys

import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from regime import data as rdata  # noqa: E402

DAY = 86_400_000
# 假历史起点：距 END 1500 天，跨越 1000 行上限，逼出第二页
HISTORY_DAYS = 1500


class _FakeEndpoint:
    """模拟 Deribit：按 [start,end] 返回日线，但单次截断到 max_rows（保留最新的）。"""

    def __init__(self, end_ms: int, max_rows: int = rdata.DVOL_MAX_ROWS):
        self.end_ms = end_ms
        self.first_ms = end_ms - HISTORY_DAYS * DAY
        self.max_rows = max_rows
        self.calls = []

    def __call__(self, currency: str, start_ms: int, end_ms: int) -> list:
        self.calls.append((start_ms, end_ms))
        lo = max(start_ms, self.first_ms)
        rows = []
        t = self.first_ms
        while t <= min(end_ms, self.end_ms):
            if t >= lo:
                # [ts, open, high, low, close] —— close 用天序号，便于断言取到了谁
                n = (t - self.first_ms) // DAY
                rows.append([t, 0.0, 0.0, 0.0, float(n)])
            t += DAY
        return rows[-self.max_rows:] if len(rows) > self.max_rows else rows


@pytest.fixture()
def fake(monkeypatch):
    end = 1_785_000_000_000          # 固定时间戳：测试不依赖当前时刻
    ep = _FakeEndpoint(end)
    monkeypatch.setattr(rdata.time, "time", lambda: end / 1000)
    monkeypatch.setattr(rdata, "_fetch_dvol_page", ep)
    return ep


def test_single_page_when_within_cap(fake):
    """≤1000 天只发一次请求——常规同步不该白翻页。"""
    df = rdata.fetch_dvol("BTC", days=730)
    assert len(fake.calls) == 1
    # 请求 N 天覆盖 N+1 个日格（首尾闭区间），末行（未收线当日）按既有口径丢弃
    assert len(df) == 730


def test_paging_beats_the_1000_row_cap(fake):
    """>1000 天必须翻页取满；不翻页就会静默停在 1000 行。"""
    df = rdata.fetch_dvol("BTC", days=HISTORY_DAYS)
    assert len(fake.calls) >= 2, "超过单次上限却只发了一次请求＝回到静默截断"
    assert len(df) == HISTORY_DAYS, "未取满：分页游标没走到历史起点"
    # 第 0 天必须在结果里——它正是不分页时会丢掉的那一端
    assert float(df["dvol"].iloc[0]) == 0.0
    assert df["ts"].is_monotonic_increasing
    assert not df["ts"].duplicated().any(), "页间重叠必须去重"


def test_stops_at_history_start_without_spinning(fake):
    """请求深度超过真实历史时，翻到空页即停，不空转到 days 耗尽。"""
    df = rdata.fetch_dvol("BTC", days=HISTORY_DAYS * 3)
    assert len(df) == HISTORY_DAYS
    assert len(fake.calls) <= 4, f"空页后仍在翻：{len(fake.calls)} 次请求"


def test_raises_when_endpoint_returns_nothing(monkeypatch):
    """全空必须抛错——静默返回空表会让上游把"没数据"当成"历史为零"。"""
    monkeypatch.setattr(rdata, "_fetch_dvol_page", lambda *a, **k: [])
    with pytest.raises(RuntimeError):
        rdata.fetch_dvol("BTC", days=365)


def test_drops_forming_bar(fake):
    """末行是形成中的当日值，必须与 fetch_ohlcv 同口径丢弃。"""
    df = rdata.fetch_dvol("BTC", days=10)
    last_full = pd.Timestamp(fake.end_ms - DAY, unit="ms", tz="UTC")
    assert df["ts"].iloc[-1] == last_full
