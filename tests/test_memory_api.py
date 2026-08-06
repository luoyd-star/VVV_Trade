"""经验路径页面、API 与 Hermes 点名入口的 HTTP 边界回归。"""
from __future__ import annotations

import builtins
import io
import json
import os
import urllib.parse

import pytest

import dashboard


ENTRY = {
    "slug": "safe-entry",
    "title": "安全样本",
    "pattern": "挤压 → 释放",
    "aliases": ["样本那条"],
    "event_from": "2026-08-03",
    "event_to": "2026-08-06",
    "symbols": ["XAU-USDT"],
    "trigger_regimes": ["squeeze"],
    "trigger_classes": ["commodity"],
    "evidence_status": "观察性单事件",
    "retrospective_path_clarity": "HIGH",
    "prospective_trade_edge_evidence": "NONE",
    "derivation_timing": "post_hoc",
    "status": "active",
    "superseded_by": "",
    "archive_reason": "",
    "created": "2026-08-06",
    "updated": "2026-08-06",
    "body": "## 核心经验\n\n> 历史正文",
    "core_quote": "历史正文",
    "path": "/不应泄漏/knowledge/experience_paths/safe-entry.md",
    "warnings": [],
}


class _HandlerHarness(dashboard.Handler):
    """绕过 socket 初始化，同时完整执行生产 Handler 的分派逻辑。"""

    def __init__(self, path: str, *, body: dict | None = None):
        self.path = path
        self.command = "POST" if body is not None else "GET"
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else b""
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = {}

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, key, value):
        self.response_headers[key] = value

    def end_headers(self):
        pass

    def send_error(self, code, message=None, explain=None):
        return self._json({"error": f"HTTP {code}"}, code=code)


def _get(path: str) -> _HandlerHarness:
    handler = _HandlerHarness(path)
    handler.do_GET()
    return handler


def _post(path: str, body: dict) -> _HandlerHarness:
    handler = _HandlerHarness(path, body=body)
    handler.do_POST()
    return handler


def _json_body(handler: _HandlerHarness) -> dict:
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _loaded(*, errors=None, loaded_at=1_723_000_000_000) -> dict:
    return {
        "entries": [dict(ENTRY)],
        "errors": list(errors or []),
        "loaded_at": loaded_at,
    }


def _assert_no_path_key(value) -> None:
    if isinstance(value, dict):
        assert "path" not in value
        for child in value.values():
            _assert_no_path_key(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_path_key(child)


def test_memory_list_uses_public_projection_without_path_or_body(monkeypatch):
    calls = []
    real_public_dict = dashboard.memory.public_dict
    monkeypatch.setattr(dashboard.memory, "load_all", lambda: _loaded())

    def tracked_public_dict(entry):
        calls.append(entry["slug"])
        return real_public_dict(entry)

    monkeypatch.setattr(dashboard.memory, "public_dict", tracked_public_dict)
    handler = _get("/api/memory")
    payload = _json_body(handler)

    assert handler.status == 200
    assert [item["slug"] for item in payload["entries"]] == ["safe-entry"]
    assert "body" not in payload["entries"][0]
    for entry in payload["entries"]:
        _assert_no_path_key(entry)
    assert calls == ["safe-entry"]


def test_memory_list_exposes_loader_errors_and_snapshot_time(monkeypatch):
    absolute = "/不应泄漏/knowledge/experience_paths/broken.md"
    errors = [{
        "path": absolute,
        "error": f"无法扫描 {os.path.dirname(absolute)}；无法打开 {absolute}",
    }]
    loaded_at = 1_723_123_456_789
    monkeypatch.setattr(
        dashboard.memory, "load_all", lambda: _loaded(errors=errors, loaded_at=loaded_at),
    )

    handler = _get("/api/memory")
    payload = _json_body(handler)

    assert handler.status == 200
    assert payload["errors"] == [{
        "path": "broken.md",
        "error": "无法扫描 experience_paths；无法打开 broken.md",
    }]
    encoded = handler.wfile.getvalue().decode("utf-8")
    assert absolute not in encoded
    assert "/不应泄漏/knowledge/experience_paths" not in encoded
    assert payload["loaded_at"] == loaded_at


def test_memory_detail_returns_body_from_public_projection(monkeypatch):
    monkeypatch.setattr(dashboard.memory, "load_all", lambda: _loaded())

    handler = _get("/api/memory/entry?slug=safe-entry")
    payload = _json_body(handler)

    assert handler.status == 200
    assert payload["slug"] == "safe-entry"
    assert payload["body"].startswith("## 核心经验")
    _assert_no_path_key(payload)


@pytest.mark.parametrize("slug", ["../", "/tmp/secret", "missing-entry", "nested/name"])
def test_memory_detail_rejects_every_non_whitelisted_slug_without_opening(
    monkeypatch, slug,
):
    monkeypatch.setattr(dashboard.memory, "load_all", lambda: _loaded())

    def forbidden_open(*args, **kwargs):
        raise AssertionError("非法 slug 不得触发任何文件读取")

    monkeypatch.setattr(builtins, "open", forbidden_open)
    query = urllib.parse.urlencode({"slug": slug})
    handler = _get(f"/api/memory/entry?{query}")

    assert handler.status == 404
    assert handler.response_headers["Content-Type"] == "application/json; charset=utf-8"
    assert _json_body(handler) == {"error": "经验路径不存在"}


def _prepare_chat(monkeypatch, captured: list[dict]) -> None:
    class Conn:
        def close(self):
            pass

    monkeypatch.setattr(dashboard.memory, "load_all", lambda: _loaded())
    monkeypatch.setattr(dashboard.storage, "connect_rw_nomigrate", Conn)
    monkeypatch.setattr(dashboard, "_ensure_chat_scope", lambda conn: None)
    monkeypatch.setattr(dashboard, "_get_chat_messages", lambda conn, limit: [])
    monkeypatch.setattr(dashboard, "_add_chat_message", lambda *args: None)
    monkeypatch.setattr(dashboard, "overview_payload", lambda: {"scope": "overview"})

    def fake_agent(payload, messages, **kwargs):
        captured.append(kwargs)
        return {"reply": "收到", "error": "测试短路，不写聊天记录"}

    monkeypatch.setattr(dashboard, "agent_chat", fake_agent)


@pytest.mark.parametrize("memory_slug", ["../", "/tmp/secret", "missing-entry", "nested/name"])
def test_invalid_memory_slug_is_ignored_instead_of_forwarded(monkeypatch, memory_slug):
    captured = []
    _prepare_chat(monkeypatch, captured)

    handler = _post("/api/agent/chat", {
        "scope": "overview",
        "message": "检查当前市场",
        "memory_slug": memory_slug,
    })

    assert handler.status == 200
    assert captured == [{"scope": "overview"}]


def test_valid_memory_slug_is_forwarded(monkeypatch):
    captured = []
    _prepare_chat(monkeypatch, captured)

    handler = _post("/api/agent/chat", {
        "scope": "overview",
        "message": "回顾这条经验",
        "memory_slug": "safe-entry",
    })

    assert handler.status == 200
    assert captured == [{"scope": "overview", "memory_slug": "safe-entry"}]


def test_memory_route_serves_memory_html(monkeypatch, tmp_path):
    page = tmp_path / "memory.html"
    page.write_text("<main>经验路径</main>", encoding="utf-8")
    monkeypatch.setattr(dashboard, "WEB_DIR", str(tmp_path))

    handler = _get("/memory")

    assert handler.status == 200
    assert handler.response_headers["Content-Type"] == "text/html; charset=utf-8"
    assert handler.wfile.getvalue() == page.read_bytes()


def test_memory_route_returns_404_when_i4_page_is_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(dashboard, "WEB_DIR", str(tmp_path))

    handler = _get("/memory")

    assert handler.status == 404
