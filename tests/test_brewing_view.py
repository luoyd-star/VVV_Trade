"""总览“酝酿中”生命周期列表与 Hermes 边界回归。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from regime import agent


ROOT = Path(__file__).resolve().parents[1]
OVERVIEW_JS = ROOT / "web" / "overview.js"


# 沿用 memory_web 的零依赖 DOM：任何 innerHTML 回退都会在生产函数执行时直接失败。
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
    this.hidden = false;
    this.href = '';
    this.attributes = {};
    this.style = {};
    this._text = '';
    return new Proxy(this, {
      set(target, property, value, receiver) {
        if (property === 'innerHTML') throw new Error('测试 DOM 禁止 innerHTML');
        return Reflect.set(target, property, value, receiver);
      },
    });
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
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  append(...children) {
    children.forEach((child) => this.appendChild(
      child instanceof MiniNode ? child : new MiniText(child)));
  }

  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = '';
    this.append(...children);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
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
    type: 'element', tag: node.tagName.toLowerCase(), text: node._text,
    className: node.className, hidden: node.hidden, href: node.href,
    attributes: node.attributes, children: node.children.map(dump),
  };
}

const sandbox = {
  console, document, encodeURIComponent,
  localStorage: { getItem: () => null, setItem() {} },
};
sandbox.window = sandbox;
sandbox.setInterval = () => 0;

const sourcePath = process.env.VVV_BREWING_JS_UNDER_TEST;
const source = fs.readFileSync(sourcePath, 'utf8');
const marker = "$('hermesSend').onclick = hermesSend;";
const markerIndex = source.indexOf(marker);
if (markerIndex < 0) throw new Error('找不到 overview.js 测试截断点');
const testSource = source.slice(0, markerIndex) + `
;window.__brewingTest = { renderBrewing, view };
`;
vm.createContext(sandbox);
vm.runInContext(testSource, sandbox, { filename: sourcePath });
const api = sandbox.__brewingTest;

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const section = register('brewingSection', 'section');
const count = register('brewingCount', 'span');
const toggle = register('brewingExpiredToggle', 'button');
const host = register('brewing');
api.view.brewingShowExpired = Boolean(input.showExpired);
api.renderBrewing(input.items);
process.stdout.write(JSON.stringify({
  sectionHidden: section.hidden,
  count: count.textContent,
  toggle: dump(toggle),
  host: dump(host),
}));
"""


def _run_node(items: list[dict], *, show_expired: bool = False) -> dict:
    env = os.environ.copy()
    env["VVV_BREWING_JS_UNDER_TEST"] = str(OVERVIEW_JS)
    result = subprocess.run(
        ["node", "-e", NODE_DRIVER],
        input=json.dumps({"items": items, "showExpired": show_expired}, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=ROOT,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _item(symbol: str, trend: str, gap: float, **overrides) -> dict:
    item = {
        "symbol": symbol,
        "at": "at_support",
        "regime_4h": "trend_up",
        "bars": 18,
        "crsi_last": 27.5,
        "gap_now": gap,
        "gap_trend": trend,
        "status": "brewing",
        "play": "S4 趋势回踩做多",
    }
    item.update(overrides)
    return item


def _walk(tree: dict):
    yield tree
    for child in tree.get("children", []):
        yield from _walk(child)


def _elements(tree: dict, tag: str) -> list[dict]:
    return [node for node in _walk(tree) if node.get("tag") == tag]


def _text(tree: dict) -> str:
    return "".join(node.get("text", "") for node in _walk(tree))


def test_nonempty_brewing_renders_duration_gap_and_trend_arrow():
    result = _run_node([_item("ORCL-USDT", "收敛中", 12.4)])

    assert result["sectionHidden"] is False
    assert result["count"] == "1"
    text = _text(result["host"])
    assert "ORCL-USDT" in text
    assert "已持续 18 根 4h（约 3.0 天）" in text
    assert "4h cRSI 27.5" in text and "距门槛 12.4 点" in text
    assert "收敛↓" in text


def test_empty_brewing_hides_the_whole_section_without_shell():
    result = _run_node([])

    assert result["sectionHidden"] is True
    assert result["host"]["children"] == []
    assert result["count"] == "0"


def test_expired_is_hidden_by_default_and_can_be_explicitly_shown():
    items = [
        _item("LIVE-USDT", "横盘", 8.0),
        _item("OLD-USDT", "收敛中", 2.0, status="expired"),
    ]
    default = _run_node(items)
    expanded = _run_node(items, show_expired=True)

    assert "LIVE-USDT" in _text(default["host"])
    assert "OLD-USDT" not in _text(default["host"])
    assert default["toggle"]["hidden"] is False
    assert default["toggle"]["text"] == "查看已失效"
    assert "OLD-USDT" in _text(expanded["host"])
    assert expanded["toggle"]["text"] == "隐藏已失效"


def test_converging_rows_sort_before_diverging_rows_even_with_larger_gap():
    result = _run_node([
        _item("DIVERGING-USDT", "发散中", 1.0),
        _item("CONVERGING-USDT", "收敛中", 40.0),
    ])
    rows = result["host"]["children"]

    assert "CONVERGING-USDT" in _text(rows[0])
    assert "DIVERGING-USDT" in _text(rows[1])


def test_same_trend_sorts_smaller_gap_first():
    result = _run_node([
        _item("FAR-USDT", "收敛中", 22.0),
        _item("CLOSE-USDT", "收敛中", 8.0),
    ])
    rows = result["host"]["children"]

    assert "CLOSE-USDT" in _text(rows[0])
    assert "FAR-USDT" in _text(rows[1])


def test_malicious_brewing_fields_stay_literal_and_create_no_payload_elements():
    malicious = '<img src=x onerror="globalThis.pwned=true">'
    result = _run_node([_item(
        malicious, malicious, 3.0,
        at=malicious, regime_4h=malicious, play=malicious,
    )])

    assert malicious in _text(result["host"])
    assert _elements(result["host"], "img") == []
    assert _elements(result["host"], "script") == []


def test_brewing_section_is_between_wait_signal_and_near_and_initially_hidden():
    html = (ROOT / "web" / "overview.html").read_text(encoding="utf-8")

    wait_at = html.index('id="waitSignalTitle"')
    brewing_at = html.index('id="brewingSection"')
    near_at = html.index('id="nearTitle"')
    assert wait_at < brewing_at < near_at
    assert 'id="brewingSection"\n           aria-labelledby="brewingTitle" hidden' in html


def test_agent_overview_context_includes_brewing_lifecycle_and_gap():
    text = agent.render_overview_context({
        "tf": "4h",
        "counts": {
            "armed": 0, "wait_signal": 0, "near": 0, "risk": 0,
            "middle": 1, "unavailable": 0,
        },
        "brewing": [{
            **_item("ORCL-USDT", "收敛中", 21.8),
            "started_at": 1_000,
            "last_at": 2_000,
            "crsi_first": 62.0,
            "crsi_last": 56.0,
            "crsi_slope": -1.2,
            "ever_main_ok": False,
            "zone_lo": 95.0,
            "zone_hi": 100.0,
        }],
    })

    assert "酝酿中（跨4h跟踪，尚未满足门槛，不是交易信号）" in text
    assert "ORCL-USDT" in text and "持续=18根4h(约3.0天)" in text
    assert "4h cRSI=62.0→56.0" in text
    assert "gap_now距门槛21.8点，gap_trend=收敛中" in text
    assert "尚缺=4h cRSI门槛" in text


def test_brewing_legend_and_guard_forbid_treating_it_as_entry():
    assert "gap_trend 的收敛中↓/发散中↑/横盘→" in agent.PANEL_LEGEND
    assert "它只让 WAIT 的过程可见" in agent.PANEL_LEGEND
    assert "酝酿中（brewing）条目尚未满足 policy 门槛，不是交易信号" in agent.POLICY_GUARD
    assert "gap_now 距门槛多少点" in agent.POLICY_GUARD
    assert "禁止把 brewing、收敛中或持续时长表述成可以入场" in agent.POLICY_GUARD
