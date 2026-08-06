"""三个聊天框的经验草稿确认写入行为测试。"""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = os.environ.get("VVV_WEB_SAVE_JS_UNDER_TEST")
if OVERRIDE:
    SCRIPT_CASES = [(os.environ.get("VVV_WEB_SAVE_KIND", "memory"), Path(OVERRIDE))]
else:
    SCRIPT_CASES = [
        ("app", ROOT / "web" / "app.js"),
        ("overview", ROOT / "web" / "overview.js"),
        ("memory", ROOT / "web" / "memory.js"),
    ]


# 不引入 jsdom，且拒绝 innerHTML；测试执行生产函数和 hermesSend 的真实调用链。
NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

class MiniNode {
  constructor(tagName = null, nodeType = 1) {
    this.nodeType = nodeType;
    this.tagName = tagName ? String(tagName).toUpperCase() : null;
    this.children = [];
    this.parentNode = null;
    this.className = '';
    this.disabled = false;
    this.value = '';
    this.href = '';
    this.type = '';
    this.style = {};
    this._text = '';
    this._listeners = {};
    this.classList = {
      add: (...names) => this._setClasses(names, true),
      remove: (...names) => this._setClasses(names, false),
      toggle: (name, force) => {
        const present = this._classes().has(name);
        const wanted = force === undefined ? !present : Boolean(force);
        this._setClasses([name], wanted);
        return wanted;
      },
      contains: (name) => this._classes().has(name),
    };
    // 普通对象会接受未知属性；代理确保不可信草稿一旦回退到 HTML 拼接就立即失败。
    return new Proxy(this, {
      set(target, property, value, receiver) {
        if (property === 'innerHTML') throw new Error('测试 DOM 禁止 innerHTML');
        return Reflect.set(target, property, value, receiver);
      },
    });
  }

  _classes() {
    return new Set(String(this.className || '').split(/\s+/).filter(Boolean));
  }

  _setClasses(names, add) {
    const classes = this._classes();
    names.forEach((name) => add ? classes.add(name) : classes.delete(name));
    this.className = [...classes].join(' ');
  }

  set textContent(value) {
    this._text = String(value ?? '');
    this.children = [];
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join('');
  }

  appendChild(child) {
    if (!(child instanceof MiniNode)) throw new Error('appendChild 只接受节点');
    if (child.parentNode) child.remove();
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) =>
      this.appendChild(child instanceof MiniNode ? child : new MiniText(child)));
  }

  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = '';
    this.append(...children);
  }

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  remove() {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    if (index >= 0) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }

  focus() {}
}

class MiniText extends MiniNode {
  constructor(value) {
    super(null, 3);
    this._text = String(value ?? '');
  }
}

const elements = new Map();
const document = {
  createElement: (tagName) => new MiniNode(tagName),
  createTextNode: (value) => new MiniText(value),
  getElementById: (id) => elements.get(id) || null,
};

function register(id, tagName = 'div') {
  const node = document.createElement(tagName);
  elements.set(id, node);
  return node;
}

function descendants(node, tagName) {
  const wanted = tagName.toUpperCase();
  const found = [];
  function visit(current) {
    if (current.nodeType === 1 && current.tagName === wanted) found.push(current);
    current.children.forEach(visit);
  }
  visit(node);
  return found;
}

function dump(node) {
  if (node.nodeType === 3) return { type: 'text', text: node.textContent };
  return {
    type: 'element',
    tag: node.tagName.toLowerCase(),
    text: node._text,
    className: node.className,
    disabled: node.disabled,
    href: node.href,
    children: node.children.map(dump),
  };
}

const storage = new Map();
const sandbox = {
  console,
  document,
  URL,
  URLSearchParams,
  encodeURIComponent,
  localStorage: {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  },
  fetch: async () => { throw new Error('测试未配置 fetch'); },
};
sandbox.window = sandbox;
sandbox.location = { search: '', href: 'http://localhost/' };
sandbox.history = { replaceState() {}, pushState() {} };
sandbox.addEventListener = () => {};
sandbox.setInterval = () => 0;

const kind = process.env.VVV_WEB_SAVE_KIND;
const sourcePath = process.env.VVV_WEB_SAVE_JS_UNDER_TEST;
const source = fs.readFileSync(sourcePath, 'utf8');
const markers = {
  app: "$('hermesSend').onclick = hermesSend;",
  overview: "$('hermesSend').onclick = hermesSend;",
  memory: 'function bindHermes() {',
};
const markerIndex = source.indexOf(markers[kind]);
if (markerIndex < 0) throw new Error(`找不到 ${kind} 测试截断点`);
const stateName = { app: 'S', overview: 'view', memory: 'memoryState' }[kind];
const testSource = source.slice(0, markerIndex) + `
;window.__webSaveTest = { hermesRenderDraft, hermesSend, state: ${stateName} };
`;
vm.createContext(sandbox);
vm.runInContext(testSource, sandbox, { filename: sourcePath });
const api = sandbox.__webSaveTest;

async function main() {
  const action = process.argv[1];
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));

  if (action === 'render') {
    const assistant = document.createElement('div');
    assistant.textContent = '测试回答';
    api.hermesRenderDraft(input.hasDraft ? input.draft : undefined, assistant);
    process.stdout.write(JSON.stringify(dump(assistant)));
    return;
  }

  if (action === 'send-save') {
    const inputNode = register('hermesText', 'textarea');
    register('hermesSend', 'button');
    const messages = register('hermesMsgs');
    inputNode.value = '把这个存下来';
    if (kind === 'app') api.state.symbol = 'BTC-USDT';
    if (kind === 'memory') api.state.selectedSlug = 'source-entry';
    const requests = [];
    sandbox.fetch = async (url, options = {}) => {
      requests.push({ url, options });
      if (url === '/api/agent/chat') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ reply: '测试回答', draft: input.draft }),
        };
      }
      if (url === '/api/memory/save') {
        return {
          ok: input.save.httpOk !== false,
          status: input.save.status || 200,
          json: async () => input.save.body,
        };
      }
      throw new Error(`未预期请求：${url}`);
    };
    await api.hermesSend();
    const saveButton = descendants(messages, 'button')[0];
    if (!saveButton || typeof saveButton.onclick !== 'function') {
      throw new Error('聊天回复没有可点击的保存按钮');
    }
    await saveButton.onclick();
    process.stdout.write(JSON.stringify({
      tree: dump(messages),
      requests: requests.map((request) => ({
        url: request.url,
        method: request.options.method,
        headers: request.options.headers,
        body: request.options.body ? JSON.parse(request.options.body) : null,
      })),
    }));
    return;
  }

  throw new Error(`未知测试动作：${action}`);
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
"""


def _run_node(kind: str, path: Path, action: str, payload: dict) -> dict:
    env = os.environ.copy()
    env["VVV_WEB_SAVE_KIND"] = kind
    env["VVV_WEB_SAVE_JS_UNDER_TEST"] = str(path)
    result = subprocess.run(
        ["node", "-e", NODE_DRIVER, action],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _walk(tree: dict):
    yield tree
    for child in tree.get("children", []):
        yield from _walk(child)


def _elements(tree: dict, tag: str) -> list[dict]:
    return [node for node in _walk(tree) if node.get("tag") == tag]


def _text(tree: dict) -> str:
    return "".join(node.get("text", "") for node in _walk(tree))


@pytest.mark.parametrize(("kind", "path"), SCRIPT_CASES)
def test_valid_draft_renders_save_button_and_slug(kind: str, path: Path):
    tree = _run_node(kind, path, "render", {
        "hasDraft": True,
        "draft": {"ok": True, "slug": "breakout-retest", "title": "突破回踩", "raw": "全文"},
    })

    buttons = _elements(tree, "button")
    assert len(buttons) == 1
    assert buttons[0]["text"] == "存入经验库 · 突破回踩"
    assert "slug：breakout-retest" in _text(tree)


@pytest.mark.parametrize(("kind", "path"), SCRIPT_CASES)
def test_invalid_draft_shows_error_without_button(kind: str, path: Path):
    tree = _run_node(kind, path, "render", {
        "hasDraft": True,
        "draft": {"ok": False, "error": "缺少必填字段 title"},
    })

    assert _elements(tree, "button") == []
    assert "草稿格式不合规：缺少必填字段 title" in _text(tree)


@pytest.mark.parametrize(("kind", "path"), SCRIPT_CASES)
def test_absent_draft_adds_no_visual_noise(kind: str, path: Path):
    tree = _run_node(kind, path, "render", {"hasDraft": False})

    assert tree["children"] == []
    assert _text(tree) == "测试回答"


@pytest.mark.parametrize(("kind", "path"), SCRIPT_CASES)
def test_malicious_title_stays_literal_text(kind: str, path: Path):
    payload = '<img src=x onerror="globalThis.pwned=true">'
    tree = _run_node(kind, path, "render", {
        "hasDraft": True,
        "draft": {"ok": True, "slug": "safe-slug", "title": payload, "raw": "全文"},
    })

    assert payload in _text(tree)
    assert _elements(tree, "img") == []
    assert {node.get("tag") for node in _walk(tree) if node.get("tag")} <= {"div", "button", "span"}


@pytest.mark.parametrize(("kind", "path"), SCRIPT_CASES)
def test_click_posts_raw_then_disables_button_and_links_memory(kind: str, path: Path):
    raw = "---\nslug: saved-entry\ntitle: 已保存\n---\n\n## 核心经验"
    result = _run_node(kind, path, "send-save", {
        "draft": {"ok": True, "slug": "saved-entry", "title": "已保存", "raw": raw},
        "save": {"body": {"ok": True, "slug": "saved-entry", "path": "knowledge/experience_paths/saved-entry.md"}},
    })
    save_request = [request for request in result["requests"] if request["url"] == "/api/memory/save"]

    assert len(save_request) == 1
    assert save_request[0] == {
        "url": "/api/memory/save",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": {"raw": raw},
    }
    button = _elements(result["tree"], "button")[0]
    assert button["text"] == "已存入"
    assert button["disabled"] is True
    assert [link["href"] for link in _elements(result["tree"], "a")] == ["/memory"]


@pytest.mark.parametrize(("kind", "path"), SCRIPT_CASES)
def test_save_error_is_verbatim_and_button_remains_retryable(kind: str, path: Path):
    error = "这条 slug 已存在：duplicate-entry"
    result = _run_node(kind, path, "send-save", {
        "draft": {"ok": True, "slug": "duplicate-entry", "title": "重名条目", "raw": "全文"},
        "save": {"httpOk": False, "status": 409, "body": {"ok": False, "error": error}},
    })

    assert error in _text(result["tree"])
    button = _elements(result["tree"], "button")[0]
    assert button["text"] == "存入经验库 · 重名条目"
    assert button["disabled"] is False
    assert _elements(result["tree"], "a") == []
