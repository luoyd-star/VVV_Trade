"""经验路径页面的 JavaScript 行为测试。"""

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MEMORY_JS = Path(os.environ.get("VVV_MEMORY_JS_UNDER_TEST", ROOT / "web" / "memory.js"))
ENTRY_PATH = ROOT / "knowledge" / "experience_paths" / "metals-squeeze-failed-release-reversal.md"


# 这里不用 jsdom：生产环境没有 npm 依赖，而一个拒绝 innerHTML 的小垫片能让安全回归直接失败。
NODE_DRIVER = r"""
const fs = require('fs');
const vm = require('vm');

class MiniNode {
  constructor(tagName = null, nodeType = 1) {
    this.nodeType = nodeType;
    this.tagName = tagName ? String(tagName).toUpperCase() : null;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.className = '';
    this.hidden = false;
    this.value = '';
    this.disabled = false;
    this.dataset = {};
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
    // 普通对象会默默接受未知属性；代理让任何 innerHTML 回退都在测试里立即爆炸。
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
    children.forEach((child) => {
      this.appendChild(child instanceof MiniNode ? child : new MiniText(child));
    });
  }

  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = '';
    this.append(...children);
  }

  setAttribute(name, value) {
    const text = String(value);
    this.attributes[name] = text;
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
      this.dataset[key] = text;
    }
  }

  addEventListener(type, handler) {
    this._listeners[type] = handler;
  }

  remove() {
    if (!this.parentNode) return;
    const siblings = this.parentNode.children;
    const index = siblings.indexOf(this);
    if (index >= 0) siblings.splice(index, 1);
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

function dump(node) {
  if (node.nodeType === 3) return { type: 'text', text: node.textContent };
  return {
    type: 'element',
    tag: node.tagName.toLowerCase(),
    text: node._text,
    className: node.className,
    attributes: node.attributes,
    children: node.children.map(dump),
  };
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

const storage = new Map();
const sandbox = {
  console,
  document,
  localStorage: {
    getItem: (key) => storage.has(key) ? storage.get(key) : null,
    setItem: (key, value) => storage.set(key, String(value)),
  },
  URLSearchParams,
  encodeURIComponent,
  fetch: async () => { throw new Error('测试未配置 fetch'); },
};
sandbox.window = sandbox;
sandbox.location = { search: '' };
sandbox.history = { pushState() {} };
sandbox.addEventListener = () => {};
sandbox.scrollTo = () => {};

const sourcePath = process.env.VVV_MEMORY_JS_UNDER_TEST;
const source = fs.readFileSync(sourcePath, 'utf8') + `
;window.__memoryTest = {
  renderMarkdown, renderBlocks, renderMarkdownList, renderList, hermesSend, memoryState
};`;
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: sourcePath });
const api = sandbox.__memoryTest;

async function main() {
  const action = process.argv[1];
  const input = JSON.parse(fs.readFileSync(0, 'utf8'));

  if (action === 'render') {
    const host = document.createElement('div');
    api.renderMarkdown(input.markdown, host);
    process.stdout.write(JSON.stringify(dump(host)));
    return;
  }

  if (action === 'states') {
    const list = register('memoryList');
    const count = register('memoryCount', 'span');
    api.memoryState.entries = input.entries;
    const result = {};
    for (const status of ['active', 'archived', 'superseded']) {
      api.memoryState.status = status;
      api.renderList();
      result[status] = {
        titles: descendants(list, 'button').map((button) => button.textContent),
        count: count.textContent,
      };
    }
    process.stdout.write(JSON.stringify(result));
    return;
  }

  if (action === 'send') {
    const inputNode = register('hermesText', 'textarea');
    register('hermesSend', 'button');
    register('hermesMsgs');
    inputNode.value = input.message;
    api.memoryState.selectedSlug = input.slug;
    const requests = [];
    sandbox.fetch = async (url, options = {}) => {
      requests.push({ url, options });
      if (url === '/api/agent/chat') {
        return { ok: true, json: async () => ({ reply: '测试回答' }) };
      }
      if (url.startsWith('/api/agent/history')) {
        return { ok: true, json: async () => ({ messages: [] }) };
      }
      throw new Error(`未预期请求：${url}`);
    };
    await api.hermesSend();
    const chat = requests.find((request) => request.url === '/api/agent/chat');
    process.stdout.write(JSON.stringify({
      url: chat.url,
      method: chat.options.method,
      headers: chat.options.headers,
      body: JSON.parse(chat.options.body),
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


def _run_node(action: str, payload: dict) -> dict:
    env = os.environ.copy()
    env["VVV_MEMORY_JS_UNDER_TEST"] = str(MEMORY_JS)
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


def _entry_body() -> str:
    lines = ENTRY_PATH.read_text(encoding="utf-8").splitlines()
    closing = lines[1:].index("---") + 1
    return "\n".join(lines[closing + 1:])


def _walk(tree: dict):
    yield tree
    for child in tree.get("children", []):
        yield from _walk(child)


def _elements(tree: dict, tag: str) -> list[dict]:
    return [node for node in _walk(tree) if node.get("tag") == tag]


def _text(tree: dict) -> str:
    return "".join(
        node.get("text", "")
        for node in _walk(tree)
        if node.get("type") == "text" or node.get("type") == "element"
    )


def test_real_entry_keeps_continuation_paragraphs_inside_numbered_items():
    tree = _run_node("render", {"markdown": _entry_body()})
    outer = _elements(tree, "ol")[0]
    first_item = [child for child in outer["children"] if child.get("tag") == "li"][0]

    assert "XAU、XAG的4H ATR/BBW降至低分位" in _text(first_item)
    assert "裁决：WAIT，双向准备。" in _text(first_item)
    assert len([child for child in first_item["children"] if child.get("tag") == "p"]) >= 3
    assert not any(
        child.get("tag") == "p" and "XAU、XAG的4H ATR/BBW" in _text(child)
        for child in tree["children"]
    )


def test_real_entry_preserves_nested_list_structure():
    tree = _run_node("render", {"markdown": _entry_body()})
    outer = _elements(tree, "ol")[0]
    second_item = [child for child in outer["children"] if child.get("tag") == "li"][1]
    nested = _elements(second_item, "ul")

    assert len(nested) == 1
    assert len([child for child in nested[0]["children"] if child.get("tag") == "li"]) == 2
    assert "XAU低点 4027.74" in _text(nested[0])
    assert "XAG低点 56.68" in _text(nested[0])


def test_xss_payloads_remain_text_and_create_only_allowed_elements():
    payloads = [
        '<img src=x onerror=alert(1)>',
        '<script>alert(2)</script>',
        '<svg onload=alert(3)>',
        '<iframe srcdoc="危险"></iframe>',
        '[链接](javascript:alert(4))',
        '&lt;img src=x onerror=alert(5)&gt;',
        '&#x3c;script&#x3e;alert(6)&#x3c;/script&#x3e;',
    ]
    tree = _run_node("render", {"markdown": "\n\n".join(payloads)})
    allowed = {"div", "span", "strong", "code", "pre", "h1", "h2", "blockquote", "br", "ol", "ul", "li", "p"}
    tags = {node["tag"] for node in _walk(tree) if node.get("type") == "element"}

    assert tags <= allowed
    rendered_text = _text(tree)
    for payload in payloads:
        assert payload in rendered_text


def test_consecutive_quote_lines_form_one_blockquote():
    tree = _run_node("render", {"markdown": "> 第一行\n> 第二行\n> **第三行**"})
    quotes = _elements(tree, "blockquote")

    assert len(quotes) == 1
    assert _text(quotes[0]) == "第一行第二行第三行"
    assert len(_elements(quotes[0], "br")) == 2


def test_status_filter_renders_each_state_independently():
    entries = [
        {"slug": "active-one", "title": "有效条目", "status": "active"},
        {"slug": "archived-one", "title": "归档条目", "status": "archived"},
        {
            "slug": "superseded-one", "title": "继任条目", "status": "superseded",
            "superseded_by": "active-one",
        },
    ]
    result = _run_node("states", {"entries": entries})

    assert result["active"] == {"titles": ["有效条目"], "count": "1 条"}
    assert result["archived"] == {"titles": ["归档条目"], "count": "1 条"}
    assert result["superseded"] == {"titles": ["继任条目"], "count": "1 条"}


def test_chat_request_carries_open_entry_slug():
    result = _run_node(
        "send",
        {"message": "回顾这条经验", "slug": "metals-squeeze-failed-release-reversal"},
    )

    assert result["url"] == "/api/agent/chat"
    assert result["method"] == "POST"
    assert result["headers"] == {"Content-Type": "application/json"}
    assert result["body"] == {
        "scope": "overview",
        "message": "回顾这条经验",
        "memory_slug": "metals-squeeze-failed-release-reversal",
    }
