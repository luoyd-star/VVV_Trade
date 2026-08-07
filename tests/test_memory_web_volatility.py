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
sandbox.echarts = {
  init: (host) => {
    const instance = {
      setOption: (value) => { host._chartOption = value; },
      clear: () => { host._chartOption = null; },
      resize() {},
    };
    host._chartInstance = instance;
    return instance;
  },
  getInstanceByDom: (host) => host && host._chartInstance,
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
;window.__volTest = { renderVolMetrics, renderUsvol, renderIv3 };
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
    ema: { ema20: [], ema60: [], ema200: [], window_span_desc: {} },
    bands: null,
    ...input,
  };
}

function renderUsvol(input) {
  const metrics = register('dvolMetrics');
  register('iv3Metrics');
  register('iv3Meta');
  register('iv3Chart');
  const meta = register('dvolMeta', 'span');
  let option = null;
  const chart = { setOption: (value) => { option = value; } };
  api.renderUsvol(baseUv(input), meta, chart);
  return { metrics: dump(metrics), meta: meta.textContent, option };
}

function renderIv3(input) {
  const metrics = register('iv3Metrics');
  const meta = register('iv3Meta', 'span');
  const host = register('iv3Chart');
  api.renderIv3({
    iv3: [[1, 48], [2, 51]], rv3: [[1, 42], [2, 43]],
    rv3_last: 43, spread3: 8, ...input,
  });
  return { metrics: dump(metrics), meta: meta.textContent, option: host._chartOption };
}

const action = process.argv[1];
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
if (action === 'metrics') {
  const host = register('dvolMetrics');
  api.renderVolMetrics(input.metrics, host);
  process.stdout.write(JSON.stringify(dump(host)));
} else if (action === 'usvol') {
  process.stdout.write(JSON.stringify(renderUsvol(input.uv || {})));
} else if (action === 'iv3') {
  process.stdout.write(JSON.stringify(renderIv3(input.iv3 || {})));
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
        "center": [[1, 49.0], [2, 50.0]],
        "u1": [[1, 59.0], [2, 60.0]],
        "l1": [[1, 39.0], [2, 40.0]],
        "u2": [[1, 69.0], [2, 70.0]],
        "l2": [[1, 29.0], [2, 30.0]],
        "win": 200,
        "coverage1": 0.685,
        "coverage2": 0.946,
        "now": {"value": 50.0, "z": 0.10, "pos": "in_1"},
    }


def _ema(span="200 点 ≈ 4.2 天"):
    return {
        "ema20": [[1, 51.0], [2, 52.0]],
        "ema60": [],
        "ema200": [[1, 49.0], [2, 50.0]],
        "window_span_desc": {"ema200": span},
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


def _area_series(result: dict) -> list[dict]:
    return [series for series in result["option"]["series"] if "areaStyle" in series]


def test_nonempty_bands_create_nested_stacked_areas_in_both_30d_and_3d_cards():
    usvol = _run_node("usvol", {"uv": {"ema": _ema(), "bands": _bands()}})
    iv3 = _run_node("iv3", {"iv3": {
        "iv3_ema": _ema(), "iv3_bands": _bands(),
        "window_span_desc": "200 点 ≈ 4.2 天",
    }})

    for result in (usvol, iv3):
        fills = _area_series(result)
        assert len(fills) == 2
        assert {series["stack"] for series in fills} == {
            "ema200-band-1", "ema200-band-2",
        }
        assert all("markLine" not in series and "markArea" not in series
                   for series in result["option"]["series"])


def test_null_bands_create_no_fill_area_or_mark_line():
    results = [
        _run_node("usvol", {"uv": {"bands": None}}),
        _run_node("iv3", {"iv3": {"iv3_ema": _ema(), "iv3_bands": None}}),
    ]

    for result in results:
        assert _area_series(result) == []
        assert all("markLine" not in series and "markArea" not in series
                   for series in result["option"]["series"])


def test_empty_ema_does_not_enter_legend_and_short_emas_default_off():
    result = _run_node("usvol", {"uv": {
        "ema": _ema(),
    }})
    legend = result["option"]["legend"]

    assert "EMA20" in legend["data"] and "EMA200" in legend["data"]
    assert "EMA60" not in legend["data"]
    assert legend["selected"]["EMA20"] is False
    assert legend["selected"]["EMA200"] is True


def test_canvas_keeps_only_minimal_end_labels_and_no_sigma_numbers():
    result = _run_node("usvol", {"uv": {"ema": _ema(), "bands": _bands()}})
    labels = [
        series["endLabel"]["formatter"]
        for series in result["option"]["series"] if "endLabel" in series
    ]

    assert labels
    assert max(map(len, labels)) <= 3
    assert all("markLine" not in series for series in result["option"]["series"])
    assert "σ基准" not in json.dumps(result["option"], ensure_ascii=False)


def test_band_position_empirical_coverage_and_window_move_into_metrics():
    result = _run_node("usvol", {"uv": {"ema": _ema(), "bands": _bands()}})
    text = _text(result["metrics"])

    assert "EMA200 带位置" in text and "±1σ 内" in text
    assert "±1σ 实测覆盖" in text and "68.5%" in text
    assert "±2σ 实测覆盖" in text and "94.6%" in text
    assert "EMA200 带窗口" in text and "200点" in text
    assert "经验覆盖率，不套高斯概率" in text


def test_window_span_description_is_visible_on_3d_card_and_is_literal_text():
    malicious = '<img src=x onerror="globalThis.pwned=true">'
    result = _run_node("iv3", {"iv3": {
        "iv3_ema": _ema(), "iv3_bands": _bands(), "window_span_desc": malicious,
    }})

    assert malicious in result["meta"]
    assert malicious in _text(result["metrics"])
    assert _elements(result["metrics"], "img") == []
    assert _elements(result["metrics"], "script") == []


def test_3d_and_30d_volatility_cards_share_the_same_half_width_row():
    index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    risk = index.split('<!-- 第 2 屏', 1)[1].split('<!-- 第 3 屏', 1)[0]
    vol_start = risk.index('<div class="row volatility-row">')
    next_row = risk.index('<div class="row">', vol_start)
    volatility_row = risk[vol_start:next_row]

    assert 'class="card volatility-card span6"' in volatility_row
    assert volatility_row.count('class="card volatility-card span6"') == 2
    assert 'IV vs RV3' in volatility_row and 'IV vs RV30' in volatility_row
    assert '持仓与杠杆' in risk[next_row:]
    assert 'span8' not in index and 'span9' not in index
