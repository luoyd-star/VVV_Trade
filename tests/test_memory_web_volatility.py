"""30d 波动率图与指标表的前端 DOM/ECharts 行为测试。"""

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
APP_JS = Path(os.environ.get("VVV_VOL_APP_JS_UNDER_TEST", ROOT / "web" / "app.js"))


# 沿用 memory 前端测试的零依赖垫片：拒绝 innerHTML，让 payload 文本的安全边界可执行。
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
    this.open = false;
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

  replaceChildren(...children) {
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
    this._text = '';
    children.forEach((child) => this.appendChild(child));
  }
}

const elements = new Map();
const document = {
  createElement: (tagName) => new MiniNode(tagName),
  getElementById: (id) => elements.get(id) || null,
};

function register(id, tagName = 'div') {
  const node = document.createElement(tagName);
  elements.set(id, node);
  return node;
}

function dump(node) {
  return {
    tag: node.tagName.toLowerCase(),
    text: node._text,
    className: node.className,
    open: node.open,
    children: node.children.map(dump),
  };
}

const sandbox = {
  console,
  document,
  URL,
  URLSearchParams,
  encodeURIComponent,
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
};
sandbox.window = sandbox;
sandbox.location = { href: 'http://localhost/', search: '' };
sandbox.history = { replaceState() {} };
sandbox.addEventListener = () => {};

const sourcePath = process.env.VVV_VOL_APP_JS_UNDER_TEST;
const source = fs.readFileSync(sourcePath, 'utf8');
const marker = "$('hermesSend').onclick = hermesSend;";
const markerIndex = source.indexOf(marker);
if (markerIndex < 0) throw new Error('找不到 app.js 测试截断点');
const testSource = source.slice(0, markerIndex) + `
;window.__volTest = { renderVolMetrics, renderUsvol };
`;
vm.createContext(sandbox);
vm.runInContext(testSource, sandbox, { filename: sourcePath });
const api = sandbox.__volTest;

function baseUv(input) {
  return {
    view_points: 252,
    iv: { series: [[1, 50], [2, 52]], last: 52, rank: 0.5, rank_raw: 0.5,
          rank_kind: 'raw', n: 252, live: null, vrp: null },
    rv: [[1, 40], [2, 41]],
    rv_last: 41,
    series: [[1, 20], [2, 21]],
    index: 'VXN',
    index_last: 21,
    index_rank: 0.4,
    index_settled: true,
    term: null,
    term_stock: null,
    metrics: [],
    ma: { ma20: [], ma60: [], ma200: [] },
    bands: null,
    ...input,
  };
}

function renderUsvol(input) {
  const metrics = register('dvolMetrics');
  register('iv3Meta');
  const meta = register('dvolMeta', 'span');
  let option = null;
  const chart = { setOption: (value) => { option = value; } };
  api.renderUsvol(baseUv(input), meta, chart);
  return { metrics: dump(metrics), meta: meta.textContent, option };
}

const action = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if (action === 'metrics') {
  const host = register('dvolMetrics');
  api.renderVolMetrics(input.metrics, host);
  process.stdout.write(JSON.stringify(dump(host)));
} else if (action === 'usvol') {
  process.stdout.write(JSON.stringify(renderUsvol(input.uv || {})));
} else {
  throw new Error(`未知测试动作：${action}`);
}
"""


def _run_node(action: str, payload: dict) -> dict:
    env = os.environ.copy()
    env["VVV_VOL_APP_JS_UNDER_TEST"] = str(APP_JS)
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


def _bands():
    return {
        "mean": 49.9,
        "sd": 10.5,
        "win": 252,
        "basis": "raw",
        "n": 252,
        "levels": [
            {"k": 1, "lo": 39.4, "hi": 60.4, "coverage": 0.700, "hi_pct": 0.860},
            {"k": 2, "lo": 28.9, "hi": 70.9, "coverage": 0.946, "hi_pct": 0.946},
            {"k": 3, "lo": 18.4, "hi": 81.4, "coverage": 0.990, "hi_pct": 0.982},
        ],
    }


def test_metrics_render_as_four_column_table_with_one_row_per_metric():
    metrics = [
        {"label": "实时IV", "value": 62.7, "rank": 0.92, "rank_kind": "preview",
         "rank_note": "预览分位", "settled": False, "chg": 1.7, "chg_pct": 2.7},
        {"label": "结算IV", "value": 61.0, "rank": 0.90, "rank_kind": "cond",
         "rank_note": "同财报状态·504日", "raw_rank": 0.63, "settled": True},
    ]
    tree = _run_node("metrics", {"metrics": metrics})

    assert len(_elements(tree, "table")) == 1
    assert len(_elements(tree, "th")) == 4
    assert len(_elements(tree, "tr")) == len(metrics) + 1
    assert all(len(row["children"]) == 4 for row in _elements(tree, "tbody")[0]["children"])
    text = _text(tree)
    assert "条件分位 0.90" in text
    assert "同财报状态·504日 · 原始分位 0.63" in text
    assert "未结算 · +1.7 / +2.7%" in text


def test_metrics_malicious_text_stays_literal_and_creates_no_payload_elements():
    malicious = '<img src=x onerror="globalThis.pwned=true">'
    tree = _run_node("metrics", {"metrics": [{
        "label": malicious, "value": 61.0, "rank": 0.9,
        "rank_kind": "cond", "rank_note": malicious, "note": malicious,
        "settled": True,
    }]})

    assert malicious in _text(tree)
    assert _elements(tree, "img") == []
    assert _elements(tree, "script") == []


def test_null_bands_create_no_mark_area_or_mark_line():
    result = _run_node("usvol", {"uv": {"bands": None}})
    primary = result["option"]["series"][0]

    assert "markArea" not in primary
    assert "markLine" not in primary


def test_empty_ma_does_not_enter_series_or_legend():
    result = _run_node("usvol", {"uv": {
        "ma": {
            "ma20": [[1, 49], [2, 50]],
            "ma60": [],
            "ma200": [[1, 45], [2, 46]],
        },
    }})
    names = [series["name"] for series in result["option"]["series"]]

    assert "MA20" in names and "MA200" in names
    assert "MA60" not in names
    assert result["option"]["legend"]["data"] == names


def test_sigma_labels_show_empirical_percentile_coverage_and_raw_window():
    result = _run_node("usvol", {"uv": {"bands": _bands()}})
    mark_line = result["option"]["series"][0]["markLine"]["data"]
    labels = [line["label"]["formatter"] for line in mark_line]

    assert any("+2σ 70.9" in label and "实测 P94.6" in label
               and "±覆盖94.6%" in label for label in labels)
    assert any("σ基准 原始(raw)·窗252·n=252" in label for label in labels)
    assert len(result["option"]["series"][0]["markArea"]["data"]) == 3
