/* VVV 市场状态面板 · 前端逻辑（浅色 · 工程模式）
   状态五色与折线配对色均通过 dataviz 校验（light surface, all-pairs）。 */
'use strict';

const SM = {
  trend_up:      { label: '趋势上行',   color: '#0a8a66' },
  trend_down:    { label: '趋势下行',   color: '#b91f31' },
  range:         { label: '震荡',       color: '#4a90d9' },
  squeeze:       { label: '低波动挤压', color: '#a87c05' },
  high_vol_chop: { label: '高波动非趋势', color: '#5f35c9' },
};
const COL = {
  up: '#0a8a66', down: '#b91f31',
  upDim: 'rgba(10,138,102,.45)', downDim: 'rgba(185,31,49,.4)',
  blue: '#3861fb', amber: '#a87c05', azure: '#4a90d9',
  ink: '#171a20', muted: '#7c8595', sub: '#46505f',
  grid: '#edeff3', border: '#dfe3e9', tipBg: '#ffffff',
};
const REFRESH_MS = 60_000;
const TF_ORDER = ['1d', '4h', '1h'];

const S = {
  symbol: null, tf: '1d', data: null, charts: {},
  nextRefresh: Date.now() + REFRESH_MS,
  loadSeq: 0,
  hermes: { busy: false, lastId: 0, syncSeq: 0 },
};
const $ = (id) => document.getElementById(id);
// 服务端字符串进 innerHTML 前必须过这里：正常后端只产枚举值，但库被污染或
// payload 异常时，未知 state 原样回退进 HTML 就是存储型 XSS 的入口
const esc = (v) => String(v ?? '').replace(/[&<>"']/g,
  (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
const hasEcharts = typeof echarts !== 'undefined';

/* ---------- 格式化 ---------- */
const pad = (n) => String(n).padStart(2, '0');
function fmtTs(ms, tf) {
  const d = new Date(ms);
  const md = `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  if (tf === '1d') return md;
  return `${md} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
function fmtPrice(v) {
  if (v == null || !isFinite(v)) return '—';
  if (v >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 0 });
  if (v >= 1) return v.toFixed(2);
  if (v >= 0.01) return v.toFixed(4);
  return v.toPrecision(4);
}
function fmtN(v, d = 2, signed = false) {
  if (v == null || !isFinite(v)) return '—';
  const s = v.toFixed(d);
  return signed && v > 0 ? `+${s}` : s;
}
function ago(ms) {
  if (!ms) return '—';
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 90) return `${Math.round(s)}s 前`;
  if (s < 5400) return `${Math.round(s / 60)}m 前`;
  return `${(s / 3600).toFixed(1)}h 前`;
}

/* ---------- 数据加载 ---------- */
async function loadSymbols() {
  const r = await fetch('/api/symbols');
  const j = await r.json();
  const syms = j.symbols && j.symbols.length ? j.symbols : ['BTC-USDT'];
  const nav = $('tabs');
  nav.innerHTML = '';
  syms.forEach((sym) => {
    const b = document.createElement('button');
    b.textContent = sym;
    b.onclick = () => { S.symbol = sym; setActiveTab(); load(); };
    b.dataset.sym = sym;
    nav.appendChild(b);
  });
  S.symbol = S.symbol || syms[0];
  setActiveTab();
}
function setActiveTab() {
  document.querySelectorAll('#tabs button').forEach((b) =>
    b.classList.toggle('active', b.dataset.sym === S.symbol));
  $('hermesSym').textContent = S.symbol || '--';
}
async function load() {
  // 过期响应丢弃：切标的/手动加载/定时刷新并发时，晚到的旧响应不得覆盖新数据
  // （曾可复现"标题是 ETH、内容是 BTC"的错标行情）
  const sym = S.symbol;
  const seq = ++S.loadSeq;
  let j;
  try {
    const r = await fetch(`/api/dashboard?symbol=${encodeURIComponent(sym)}`);
    j = await r.json();
  } catch (e) {
    if (seq === S.loadSeq) banner(`接口请求失败：${e}`);
    return;
  }
  if (seq !== S.loadSeq || sym !== S.symbol) return; // 已有更新的请求在途/已完成
  S.data = j;
  S.nextRefresh = Date.now() + REFRESH_MS;
  renderAll();
  hermesSync(); // 顺带同步共享对话（终端里的新提问会出现在这里）
}

/* ---------- 渲染 ---------- */
function banner(msg) {
  const el = $('banner');
  if (!msg) { el.hidden = true; return; }
  el.textContent = msg;
  el.hidden = false;
}

function renderAll() {
  const d = S.data;
  if (!d || d.error) { banner(`服务端错误：${d && d.error}`); return; }
  const tfs = Object.keys(d.tfs || {});
  if (!tfs.length) {
    banner('数据库为空或历史不足——请先运行采集器：.venv/bin/python collector.py');
    return;
  }
  const issues = [];
  if (!hasEcharts) issues.push('图表库（ECharts CDN）加载失败，仅显示表格与时间线');
  ((d.health && d.health.issues) || []).forEach((x) => issues.push(x));
  banner(issues.length ? `⚠ ${issues.join(' · ')}` : '');
  if (!d.tfs[S.tf]) S.tf = tfs[0];

  // 每个区块独立 try：单个卡片的数据异常只坏它自己，不把整页渲染拖死
  [renderMktBadge, renderStateCards, renderTfPicker, renderPriceChart,
   renderDvol, renderVolRank, renderDeriv, renderStrips, renderFeatTable,
   renderFlips, renderCollector, renderFresh].forEach((fn) => {
    try { fn(); } catch (e) { console.error(`渲染区块 ${fn.name} 失败:`, e); }
  });
}

function renderMktBadge() {
  const inst = S.data.instrument || {};
  const mb = $('mktBadge');
  if (inst.class !== 'us_stock_perp') { mb.hidden = true; return; }
  mb.hidden = false;
  if (inst.market_open) {
    mb.textContent = '标的盘中 (NYSE)';
    mb.className = 'badge ok';
    mb.title = '正股交易时段 9:30-16:00 ET';
  } else {
    mb.textContent = '标的休市 · 场外定价';
    mb.className = 'badge warn';
    mb.title = '正股休市中：合约照常交易但波动塌陷，低波/挤压读数可能是休市假象';
  }
}

function renderStateCards() {
  const d = S.data;
  const host = $('stateCards');
  host.innerHTML = '';
  TF_ORDER.filter((tf) => d.tfs[tf]).forEach((tf) => {
    const t = d.tfs[tf];
    const meta = SM[t.state] || { label: esc(t.state), color: COL.muted };
    const f = t.features;
    const cl = (t.crsi || {}).last || {};

    // 数据健康 → tf 旁角标（悬浮看详情），不再占用标记行
    let warn = '';
    if (t.health && (t.health.stale || t.health.warmup)) {
      const tips = [];
      if (t.health.stale) tips.push(`数据陈旧：最后收线 ${t.health.last_close_age_min} 分钟前`);
      if (t.health.warmup) tips.push(`预热中：仅 ${t.health.bars} 根，分位参照期不足`);
      warn = `<span class="warn-ico" title="${tips.join('；')}">⚠️</span>`;
    }

    // conf 属于最新**原始判定**，且各状态公式不同、不可跨状态挪用（squeeze 的
    // conf=1-bbw_rank 不能给"趋势上行"背书）。与确认态分歧时必须挂在原始判定旁。
    const diverged = t.raw_state && t.raw_state !== t.state;
    const confTxt = `conf ${fmtN(t.confidence, 2)}`;

    // 动态行：预览 / 酝酿 / 原始判定 统一收纳成一行
    const dyn = [];
    if (t.preview) {
      const pm = SM[t.preview.state] || { label: esc(t.preview.state) };
      dyn.push(`预览(未收线) <b>${pm.label}</b> ${fmtN(t.preview.confidence, 2)}`);
    }
    if (t.candidate) {
      const cm = SM[t.candidate.state] || { label: esc(t.candidate.state) };
      // candidate.state 恒等于最新 raw（confirm_states 保证），conf 可直接挂这
      dyn.push(`酝酿 <b>${cm.label}</b> ${t.candidate.count}/${t.candidate.need}`
        + (diverged && t.candidate.state === t.raw_state ? ` · ${confTxt}` : ''));
    }
    if (diverged && !(t.candidate && t.candidate.state === t.raw_state)) {
      const rm = SM[t.raw_state] || { label: esc(t.raw_state) };
      dyn.push(`原始判定 <b>${rm.label}</b> ${confTxt}`);
    }
    const mg = (f.margin || {});
    if (mg.margin != null && mg.margin < 0.15) {
      dyn.push(`⚡边界过渡 m=${fmtN(mg.margin, 2)}（${mg.nearest}）`);
    }
    const transHtml = dyn.length ? dyn.join(' · ') : '稳定 · 无待确认切换';

    // 标记 chips：按优先级最多 4 个
    const flags = [];
    if (f.volatility.squeeze) flags.push('SQZ 挤压');
    if (f.volatility.high_vol) flags.push('HV 高波');
    if (cl.zone && cl.zone !== '带内') flags.push(`cRSI ${cl.zone}（${fmtN(cl.pos, 0)}%）`);
    const dv = (t.crsi || {}).last_divergence;
    if (dv && dv.bars_ago <= 10) flags.push(`${dv.kind === 'bull' ? '看涨' : '看跌'}背离 ${dv.bars_ago}根前`);
    if (f.volume.breakout) {
      const b = f.volume.breakout;
      flags.push(`突破${b.dir === 'up' ? '↑' : '↓'} 量分位 ${fmtN(b.vol_rank, 2)}`);
    }

    // 置信度条恒表示原始判定：分歧时用原始态的颜色（与下方动态行配对），
    // 不再借确认态的颜色/位置为其背书；无分歧时二者本就是同一状态
    const rawMeta = diverged ? (SM[t.raw_state] || { color: COL.muted }) : meta;
    const el = document.createElement('div');
    el.className = 'scard';
    el.style.borderLeftColor = meta.color;
    el.innerHTML = `
      <div class="top">
        <span class="tf">${tf}</span>${warn}
        <span class="dot" style="background:${meta.color}"></span>
        <span class="stname">${meta.label}</span>
        ${diverged ? '' : `<span class="conf">${confTxt}</span>`}
      </div>
      <div class="meter" title="原始判定置信度"><i style="width:${Math.round(t.confidence * 100)}%;background:${rawMeta.color}"></i></div>
      <div class="trans">${transHtml}</div>
      <div class="metrics">
        <div class="m"><b>${fmtN(f.structure.direction, 2, true)}</b><span>dir</span></div>
        <div class="m"><b>${fmtN(f.er_rank, 2)}</b><span>ER%</span></div>
        <div class="m"><b>${fmtN(f.volatility.atr_rank, 2)}</b><span>ATR%</span></div>
        <div class="m"><b>${fmtN(f.volatility.bbw_rank, 2)}</b><span>BBW%</span></div>
        <div class="m"><b>${fmtN(f.volume.updown_tilt_20, 2, true)}</b><span>tilt</span></div>
        <div class="m"><b>${fmtN(cl.crsi, 1)}</b><span>cRSI</span></div>
      </div>
      <div class="flagline">${flags.slice(0, 4).map((x) => `<span class="chip">${x}</span>`).join('') || '<span class="chip">无标记</span>'}</div>`;
    host.appendChild(el);
  });
}

function renderTfPicker() {
  const host = $('tfPicker');
  host.innerHTML = '';
  TF_ORDER.filter((tf) => S.data.tfs[tf]).forEach((tf) => {
    const b = document.createElement('button');
    b.textContent = tf;
    b.classList.toggle('active', tf === S.tf);
    b.onclick = () => { S.tf = tf; renderPriceChart(); renderVolRank(); renderTfPicker(); };
    host.appendChild(b);
  });
}

function chart(id) {
  if (!hasEcharts) return null;
  if (!S.charts[id]) S.charts[id] = echarts.init($(id));
  return S.charts[id];
}

function renderPriceChart() {
  const t = S.data.tfs[S.tf];
  const disp = (S.data.instrument || {}).display;
  $('priceMeta').textContent =
    `${S.symbol}${disp ? `（${disp}）` : ''} · ${S.tf} · ${t.candles.length} 根 · 源 ${t.source || '—'} · UTC`;
  const legend = $('priceLegend');
  legend.innerHTML = Object.entries(SM).map(([k, m]) =>
    `<span class="li"><span class="sw" style="background:${m.color};opacity:.5"></span>${m.label}</span>`
  ).join('') + `<span class="li"><span class="sw" style="background:${COL.blue}"></span>EMA50 / cRSI</span>
    <span class="li"><span class="sw" style="background:${COL.azure}"></span>cRSI 自适应带</span>
    <span class="li"><span class="sw" style="background:${COL.muted}"></span>H/L 摆动点 · ●背离</span>`;
  const c = chart('priceChart');
  if (!c) return;

  const rows = t.candles;
  const N = rows.length;
  const labels = rows.map((r) => fmtTs(r[0], S.tf));
  const kdata = rows.map((r) => [r[1], r[4], r[3], r[2]]);
  const vols = rows.map((r) => ({
    value: r[5],
    itemStyle: { color: r[4] >= r[1] ? COL.upDim : COL.downDim },
  }));
  const stateByIdx = new Array(N).fill(null);
  t.segments.forEach((sg) => { for (let i = sg.s; i <= sg.e; i++) stateByIdx[i] = sg.state; });
  const markData = t.segments.map((sg) => [
    { xAxis: sg.s, itemStyle: { color: (SM[sg.state] || {}).color || COL.muted, opacity: 0.09 } },
    { xAxis: sg.e },
  ]);
  const pivH = t.pivots.filter((p) => p.kind === 'H').map((p) => [p.i, p.price]);
  const pivL = t.pivots.filter((p) => p.kind === 'L').map((p) => [p.i, p.price]);
  const cr = t.crsi || { crsi: [], db: [], ub: [], divs: [] };
  const divBull = cr.divs.filter((d) => d.kind === 'bull').map((d) => [d.i, cr.crsi[d.i]]);
  const divBear = cr.divs.filter((d) => d.kind === 'bear').map((d) => [d.i, cr.crsi[d.i]]);

  const axisCommon = {
    type: 'category', data: labels, boundaryGap: true,
    axisLine: { lineStyle: { color: COL.border } }, axisTick: { show: false },
  };
  c.setOption({
    animation: false,
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: COL.sub } },
      backgroundColor: COL.tipBg, borderColor: COL.border,
      textStyle: { color: COL.ink, fontSize: 11.5 },
      formatter: (params) => {
        const i = params[0].dataIndex;
        const r = rows[i];
        const st = stateByIdx[i];
        const m = st ? SM[st] : null;
        const chg = (r[4] - r[1]) / r[1] * 100;
        const cv = cr.crsi[i];
        let zone = '';
        if (cv != null && cr.db[i] != null && cr.ub[i] != null) {
          zone = cv >= cr.ub[i] ? ' 超买区' : (cv <= cr.db[i] ? ' 超卖区' : ' 带内');
        }
        return [
          `<b>${fmtTs(r[0], S.tf)} UTC</b>`,
          `开 ${fmtPrice(r[1])} 高 ${fmtPrice(r[2])}`,
          `低 ${fmtPrice(r[3])} 收 ${fmtPrice(r[4])}（${fmtN(chg, 2, true)}%）`,
          `量 ${r[5] >= 1000 ? Math.round(r[5]).toLocaleString('en-US') : fmtN(r[5], 1)} · EMA50 ${fmtPrice(t.ema50[i])}`,
          `cRSI ${fmtN(cv, 1)}（带 ${fmtN(cr.db[i], 1)}~${fmtN(cr.ub[i], 1)}${zone}）`,
          m ? `状态 <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${m.color}"></span> ${m.label}` : '状态 —',
        ].join('<br>');
      },
    },
    grid: [
      { left: 62, right: 18, top: 10, height: '47%' },
      { left: 62, right: 18, top: '60%', height: '11%' },
      { left: 62, right: 18, top: '75%', height: '16%' },
    ],
    xAxis: [
      { ...axisCommon, gridIndex: 0, axisLabel: { show: false } },
      { ...axisCommon, gridIndex: 1, axisLabel: { show: false } },
      { ...axisCommon, gridIndex: 2, axisLabel: { color: COL.muted, fontSize: 10 } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: COL.grid } },
        axisLabel: { color: COL.muted, fontSize: 10, formatter: (v) => fmtPrice(v) } },
      { gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
      { scale: true, gridIndex: 2, splitLine: { lineStyle: { color: COL.grid } },
        axisLabel: { color: COL.muted, fontSize: 9.5 } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], start: Math.max(0, 100 - 11000 / N), end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 4, height: 14,
        borderColor: COL.border, backgroundColor: 'rgba(23,26,32,.03)',
        fillerColor: 'rgba(56,97,251,.10)', handleStyle: { color: '#b9c0cc' },
        textStyle: { color: COL.muted, fontSize: 9 } },
    ],
    series: [
      { name: 'K线', type: 'candlestick', data: kdata, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: COL.up, color0: COL.down, borderColor: COL.up, borderColor0: COL.down },
        markArea: { silent: true, data: markData } },
      { name: 'EMA50', type: 'line', data: t.ema50, symbol: 'none', z: 3,
        lineStyle: { width: 2, color: COL.blue }, xAxisIndex: 0, yAxisIndex: 0 },
      { name: '摆动高', type: 'scatter', data: pivH, symbolSize: 6, z: 4,
        itemStyle: { color: 'transparent', borderColor: COL.down, borderWidth: 1.5 },
        label: { show: true, formatter: 'H', position: 'top', color: COL.muted, fontSize: 9 },
        xAxisIndex: 0, yAxisIndex: 0 },
      { name: '摆动低', type: 'scatter', data: pivL, symbolSize: 6, z: 4,
        itemStyle: { color: 'transparent', borderColor: COL.up, borderWidth: 1.5 },
        label: { show: true, formatter: 'L', position: 'bottom', color: COL.muted, fontSize: 9 },
        xAxisIndex: 0, yAxisIndex: 0 },
      { name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 1, barWidth: '62%' },
      { name: 'cRSI', type: 'line', data: cr.crsi, symbol: 'none', z: 3,
        lineStyle: { width: 2, color: COL.blue }, xAxisIndex: 2, yAxisIndex: 2,
        markLine: { silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: '#c3c9d4' },
          label: { color: COL.muted, fontSize: 9, position: 'insideEndTop' },
          data: [{ yAxis: 30 }, { yAxis: 70 }] } },
      { name: '下带', type: 'line', data: cr.db, symbol: 'none',
        lineStyle: { width: 1.5, color: COL.azure }, xAxisIndex: 2, yAxisIndex: 2 },
      { name: '上带', type: 'line', data: cr.ub, symbol: 'none',
        lineStyle: { width: 1.5, color: COL.azure }, xAxisIndex: 2, yAxisIndex: 2 },
      { name: '看涨背离', type: 'scatter', data: divBull, symbolSize: 8, z: 5,
        itemStyle: { color: COL.up }, xAxisIndex: 2, yAxisIndex: 2 },
      { name: '看跌背离', type: 'scatter', data: divBear, symbolSize: 8, z: 5,
        itemStyle: { color: COL.down }, xAxisIndex: 2, yAxisIndex: 2 },
    ],
  }, true);
}

function renderVolRank() {
  const t = S.data.tfs[S.tf];
  const c = chart('volRankChart');
  if (!c) return;
  const labels = t.candles.map((r) => fmtTs(r[0], S.tf));
  c.setOption({
    animation: false,
    color: [COL.blue, COL.amber],
    tooltip: {
      trigger: 'axis', backgroundColor: COL.tipBg, borderColor: COL.border,
      textStyle: { color: COL.ink, fontSize: 11.5 },
      valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(2)),
    },
    legend: { top: 0, right: 10, textStyle: { color: COL.sub, fontSize: 10.5 },
      itemWidth: 12, itemHeight: 3, icon: 'rect' },
    grid: { left: 34, right: 16, top: 22, bottom: 20 },
    xAxis: { type: 'category', data: labels, axisLine: { lineStyle: { color: COL.border } },
      axisTick: { show: false }, axisLabel: { color: COL.muted, fontSize: 9.5 } },
    yAxis: { min: 0, max: 1, splitLine: { lineStyle: { color: COL.grid } },
      axisLabel: { color: COL.muted, fontSize: 9.5 } },
    series: [
      { name: 'ATR分位', type: 'line', data: t.atr_rank_series, symbol: 'none',
        lineStyle: { width: 2 },
        markLine: { silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: '#c3c9d4' },
          label: { color: COL.muted, fontSize: 9, position: 'insideEndTop' },
          data: [{ yAxis: 0.15, label: { formatter: '挤压 0.15' } },
                 { yAxis: 0.85, label: { formatter: '高波 0.85' } }] } },
      { name: 'BBW分位', type: 'line', data: t.bbw_rank_series, symbol: 'none',
        lineStyle: { width: 2 } },
    ],
  }, true);
}

function renderDvol() {
  const d = S.data.dvol;
  const uv = S.data.usvol;
  const meta = $('dvolMeta');
  const c = chart('dvolChart');
  if (uv) { renderUsvol(uv, meta, c); return; }
  if (!d) {
    meta.textContent = '该品种无 DVOL（仅 BTC/ETH）';
    if (c) c.clear();
    return;
  }
  meta.textContent =
    `IV ${fmtN(d.iv_last, 1)}（分位 ${fmtN(d.iv_rank, 2)}）· RV ${fmtN(d.rv_last, 1)} · IV−RV ${fmtN(d.spread, 1, true)}pt`;
  if (!c) return;
  c.setOption({
    animation: false,
    useUTC: true,
    color: [COL.blue, COL.amber],
    tooltip: {
      trigger: 'axis', backgroundColor: COL.tipBg, borderColor: COL.border,
      textStyle: { color: COL.ink, fontSize: 11.5 },
      valueFormatter: (v) => (v == null ? '—' : `${Number(v).toFixed(1)}%`),
    },
    legend: { top: 0, right: 40, textStyle: { color: COL.sub, fontSize: 10.5 },
      itemWidth: 12, itemHeight: 3, icon: 'rect' },
    grid: { left: 38, right: 44, top: 22, bottom: 20 },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: COL.border } },
      axisLabel: { color: COL.muted, fontSize: 9.5 } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: COL.grid } },
      axisLabel: { color: COL.muted, fontSize: 9.5, formatter: '{value}%' } },
    series: [
      { name: 'DVOL 隐含', type: 'line', data: d.iv, symbol: 'none',
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: 'IV', color: COL.sub, fontSize: 9.5 } },
      { name: 'RV30 已实现', type: 'line', data: d.rv, symbol: 'none',
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: 'RV', color: COL.sub, fontSize: 9.5 } },
    ],
  }, true);
}

// 美股永续变体：CBOE 指数 IV（VXN/VIX，板块级）+ 本品种 RV30。
// 个股 iv30 无免费历史，只在 meta 行展示最新值与自采天数（攒够才有分位意义）。
function renderUsvol(uv, meta, c) {
  const bits = [
    `${uv.index} ${fmtN(uv.index_last, 1)}（一年分位 ${fmtN(uv.index_rank, 2)}）`,
    `RV30 ${fmtN(uv.rv_last, 1)}`,
    uv.spread == null ? null : `指数IV−RV ${fmtN(uv.spread, 1, true)}pt`,
    uv.iv30_last == null ? '个股iv30 采集中' : `个股iv30 ${fmtN(uv.iv30_last, 1)}（自采 ${uv.iv30_days}d）`,
    uv.ts_ratio == null ? null : `9D/3M ${fmtN(uv.ts_ratio, 2)}${uv.ts_ratio > 1 ? '（倒挂）' : ''}`,
  ];
  meta.textContent = bits.filter(Boolean).join(' · ');
  if (!c) return;
  c.setOption({
    animation: false,
    useUTC: true,
    color: [COL.blue, COL.amber],
    tooltip: {
      trigger: 'axis', backgroundColor: COL.tipBg, borderColor: COL.border,
      textStyle: { color: COL.ink, fontSize: 11.5 },
      valueFormatter: (v) => (v == null ? '—' : `${Number(v).toFixed(1)}%`),
    },
    legend: { top: 0, right: 40, textStyle: { color: COL.sub, fontSize: 10.5 },
      itemWidth: 12, itemHeight: 3, icon: 'rect' },
    grid: { left: 38, right: 44, top: 22, bottom: 20 },
    xAxis: { type: 'time', axisLine: { lineStyle: { color: COL.border } },
      axisLabel: { color: COL.muted, fontSize: 9.5 } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: COL.grid } },
      axisLabel: { color: COL.muted, fontSize: 9.5, formatter: '{value}%' } },
    series: [
      { name: `${uv.index} 指数IV`, type: 'line', data: uv.series, symbol: 'none',
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: uv.index, color: COL.sub, fontSize: 9.5 } },
      { name: 'RV30 已实现', type: 'line', data: uv.rv, symbol: 'none',
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: 'RV', color: COL.sub, fontSize: 9.5 } },
    ],
  }, true);
}

function renderDeriv() {
  const dr = S.data.deriv;
  const meta = $('derivMeta');
  const info = $('derivInfo');
  const c = chart('derivChart');
  if (!dr) {
    meta.textContent = '暂无数据（等采集器下一轮）';
    info.innerHTML = '';
    if (c) c.clear();
    return;
  }
  const sp = dr.spans || {};
  meta.textContent = `历史 OI ${sp.oi ?? dr.span_days}d · Funding ${sp.funding ?? '—'}d`
    + `${sp.iv30 != null ? ` · iv30 ${sp.iv30}d` : ''}${dr.warmup ? '（OI<21天，分位仅供参考）' : ''}`;
  const chg = (v) => (v == null ? '—' : fmtN((Math.exp(v) - 1) * 100, 2, true) + '%');
  const rk = (v) => (v == null ? '' : `（分位 ${fmtN(v, 2)}）`);
  info.innerHTML = [
    [dr.oi == null ? '—' : Math.round(dr.oi).toLocaleString('en-US'), `OI 张数${rk(dr.oi_rank)}`],
    [chg(dr.oi_change_4h), 'OI Δ4h'],
    [chg(dr.oi_change_24h), 'OI Δ24h'],
    [dr.taker_ratio == null ? '—' : fmtN(dr.taker_ratio, 3), `Taker 买卖比${rk(dr.taker_rank)}`],
    [dr.funding_pct == null ? '—' : `${fmtN(dr.funding_pct * 100, 2)}bp`, `Funding /${dr.funding_interval_h || 8}h（下期预测）`],
    [dr.funding_settled_pct == null ? '—' : `${fmtN(dr.funding_settled_pct * 100, 2)}bp`, `上期结算${rk(dr.funding_rank)}`],
    [dr.funding_annual_pct == null ? '—' : `${fmtN(dr.funding_annual_pct, 1)}%`, 'Funding 年化'],
    [dr.premium_pct == null ? '—' : `${fmtN(dr.premium_pct * 100, 1)}bp`, `Premium${rk(dr.premium_rank)}`],
    dr.iv30 != null
      ? [fmtN(dr.iv30, 1), `个股 iv30${rk(dr.iv30_rank)}`]
      : [`${dr.span_days}d`, '样本跨度'],
  ].map(([v, k]) => `<div class="m"><b>${v}</b><span>${k}</span></div>`).join('');
  if (!c) return;
  c.setOption({
    animation: false,
    useUTC: true,
    color: [COL.blue, COL.amber],
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      trigger: 'axis', backgroundColor: COL.tipBg, borderColor: COL.border,
      textStyle: { color: COL.ink, fontSize: 11 },
    },
    grid: [
      { left: 58, right: 14, top: 8, height: '42%' },
      { left: 58, right: 14, top: '62%', height: '30%' },
    ],
    xAxis: [
      { type: 'time', gridIndex: 0, axisLabel: { show: false }, axisLine: { lineStyle: { color: COL.border } } },
      { type: 'time', gridIndex: 1, axisLabel: { color: COL.muted, fontSize: 9 }, axisLine: { lineStyle: { color: COL.border } } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: COL.grid } },
        axisLabel: { color: COL.muted, fontSize: 9, formatter: (v) => (v >= 1000 ? `${Math.round(v / 1000)}k` : v) } },
      { scale: true, gridIndex: 1, splitLine: { show: false },
        axisLabel: { color: COL.muted, fontSize: 9, formatter: '{value}%' } },
    ],
    series: [
      { name: 'OI', type: 'line', data: dr.oi_series, symbol: 'none',
        lineStyle: { width: 2 }, xAxisIndex: 0, yAxisIndex: 0,
        endLabel: { show: true, formatter: 'OI', color: COL.sub, fontSize: 9 } },
      { name: `Funding %/${dr.funding_interval_h || 8}h（已结算）`, type: 'bar', data: dr.funding_series,
        xAxisIndex: 1, yAxisIndex: 1, barWidth: 2 },
    ],
  }, true);
}

function renderStrips() {
  const host = $('strips');
  host.innerHTML = '';
  $('stripLegend').innerHTML = Object.entries(SM).map(([k, m]) =>
    `<span class="li"><span class="sw" style="background:${m.color}"></span>${m.label}</span>`).join('');
  TF_ORDER.filter((tf) => S.data.tfs[tf]).forEach((tf) => {
    const t = S.data.tfs[tf];
    const N = t.candles.length;
    const row = document.createElement('div');
    row.className = 'strip-row';
    const segs = t.segments.map((sg) => {
      const m = SM[sg.state] || { label: esc(sg.state), color: COL.muted };
      const w = ((sg.e - sg.s + 1) / N * 100).toFixed(3);
      const tip = `${tf} · ${m.label} · ${fmtTs(t.candles[sg.s][0], tf)} → ${fmtTs(t.candles[sg.e][0], tf)} UTC`;
      return `<span class="seg" style="width:${w}%;background:${m.color};opacity:.85" title="${tip}"></span>`;
    }).join('');
    row.innerHTML = `<span class="lbl">${tf}</span><span class="track">${segs}</span>`;
    host.appendChild(row);
    const time = document.createElement('div');
    time.className = 'strip-time';
    time.innerHTML = `<span>${fmtTs(t.candles[0][0], tf)}</span><span>${fmtTs(t.candles[N - 1][0], tf)} UTC</span>`;
    host.appendChild(time);
  });
}

function stchip(state) {
  const m = SM[state] || { label: esc(state), color: COL.muted };
  return `<span class="stchip"><span class="dot" style="background:${m.color}"></span>${m.label}</span>`;
}

function renderFeatTable() {
  const d = S.data;
  // 按支柱分组的两行表头；组首列画左分隔线（gs）
  const groups = [
    // conf 属于"原始"列的判定（分歧时"状态"列是迟滞确认态，没有自己的置信度）
    ['标识', ['TF', '状态', '原始', 'conf(原始)']],
    ['结构', ['dir', 'ER%', '摆动高', '摆动低']],
    ['波动率', ['ATR%', 'ATR%ds', 'BBW%', 'RV年化%', '加速度', '下行方差']],
    ['量能', ['tilt', 'volZ', '突破']],
    ['cRSI', ['值', '带位%', '区域', '背离']],
    ['路径几何·影子', ['频率', '主周期', 'τ', 'margin', '滞后']],
  ];
  const grpRow = groups.map(([g, cols], i) =>
    `<th class="grp${i ? ' gs' : ''}" colspan="${cols.length}">${g}</th>`).join('');
  const colRow = groups.map(([, cols], i) =>
    cols.map((c, j) => `<th class="${i && j === 0 ? 'gs' : ''}">${c}</th>`).join('')).join('');

  const rows = TF_ORDER.filter((tf) => d.tfs[tf]).map((tf) => {
    const t = d.tfs[tf];
    const f = t.features;
    const b = f.volume.breakout;
    const cl = (t.crsi || {}).last || {};
    const dv = (t.crsi || {}).last_divergence;
    const rawTxt = t.raw_state && t.raw_state !== t.state
      ? ((SM[t.raw_state] || {}).label || t.raw_state) : '—';
    // [值, 模式('s'=正负着色/'l'=左对齐), 组首列?]
    const cells = [
      [tf], [stchip(t.state), 'l'], [rawTxt], [fmtN(t.confidence, 2)],
      [fmtN(f.structure.direction, 2, true), 's', 1], [fmtN(f.er_rank, 2)],
      [fmtPrice(f.structure.swing_high)], [fmtPrice(f.structure.swing_low)],
      [fmtN(f.volatility.atr_rank, 2), '', 1],
      [fmtN(f.volatility.atr_rank_ds, 2)], [fmtN(f.volatility.bbw_rank, 2)],
      [fmtN(f.volatility.rv30_annual_pct, 1)], [fmtN(f.volatility.vol_accel, 2)],
      [fmtN(f.volatility.downside_share, 2)],
      [fmtN(f.volume.updown_tilt_20, 2, true), 's', 1], [fmtN(f.volume.vol_z20, 1, true), 's'],
      [b ? `${b.dir === 'up' ? '↑' : '↓'} v${fmtN(b.vol_rank, 2)}` : '—'],
      [fmtN(cl.crsi, 1), '', 1], [fmtN(cl.pos, 0)], [cl.zone || '—'],
      [dv ? `${dv.kind === 'bull' ? '看涨' : '看跌'} ${dv.bars_ago}根前` : '—'],
      [fmtN((f.pathgeom || {}).chop_freq, 1), '', 1],
      [(f.pathgeom || {}).dom_period != null ? `${fmtN(f.pathgeom.dom_period, 0)}根` : '—'],
      [fmtN((f.pathgeom || {}).kendall_tau, 2, true), 's'],
      [(f.margin || {}).margin != null ? `${fmtN(f.margin.margin, 2)}(${f.margin.nearest})` : '—'],
      [(f.lag_bars || {}).pathgeom != null ? `~${f.lag_bars.pathgeom}根` : '—'],
    ];
    return `<tr>${cells.map(([v, mode, gs]) => {
      let cls = gs ? 'gs' : '';
      if (mode === 's' && typeof v === 'string') {
        if (v.startsWith('+')) cls += ' pos';
        if (v.startsWith('-')) cls += ' neg';
      }
      const align = mode === 'l' ? ' style="text-align:left"' : '';
      return `<td class="${cls}"${align}>${v}</td>`;
    }).join('')}</tr>`;
  });
  $('featTable').innerHTML =
    `<thead><tr>${grpRow}</tr><tr>${colRow}</tr></thead><tbody>${rows.join('')}</tbody>`;
}

function renderFlips() {
  const flips = S.data.flips || [];
  const rows = flips.map((f) =>
    `<tr><td>${fmtTs(f.ts, '4h')}</td><td>${esc(f.tf)}</td>` +
    `<td style="text-align:left">${stchip(f.from)} → ${stchip(f.to)}</td>` +
    `<td>${fmtN(f.confidence, 2)}</td></tr>`);
  $('flipTable').innerHTML =
    '<thead><tr><th>时间 UTC</th><th>TF</th><th>变化</th><th>conf</th></tr></thead>' +
    `<tbody>${rows.join('') || '<tr><td colspan="4">暂无翻转记录</td></tr>'}</tbody>`;
}

function renderCollector() {
  const c = S.data.collector || {};
  const age = c.last_run ? Date.now() - c.last_run : null;
  const staleMs = (c.interval || 300) * 2000;
  const stat = $('colStat');
  if (age == null) { stat.textContent = '未运行'; stat.className = 'badge bad'; }
  else if (age < staleMs) { stat.textContent = `运行中 · ${ago(c.last_run)}`; stat.className = 'badge ok'; }
  else { stat.textContent = `已停止? · ${ago(c.last_run)}`; stat.className = 'badge bad'; }
  const n = c.counts || {};
  $('colInfo').innerHTML = [
    ['采集间隔', c.interval ? `${c.interval}s` : '—'],
    ['单轮耗时', c.cycle_sec != null ? `${c.cycle_sec}s` : '—'],
    ['K线行数', n.ohlcv != null ? n.ohlcv.toLocaleString('en-US') : '—'],
    ['DVOL行数', n.dvol != null ? n.dvol.toLocaleString('en-US') : '—'],
    ['状态行数', n.regime_history != null ? n.regime_history.toLocaleString('en-US') : '—'],
    ['本轮错误', (c.errors || []).length],
  ].map(([k, v]) => `<span>${k}</span><b>${v}</b>`).join('');
  $('colLog').textContent = (c.log_tail || []).join('\n') || '（暂无日志）';
  const lg = $('colLog');
  lg.scrollTop = lg.scrollHeight;
}

function renderFresh() {
  const c = (S.data && S.data.collector) || {};
  const el = $('fresh');
  const age = c.last_run ? Date.now() - c.last_run : null;
  const staleMs = (c.interval || 300) * 2000;
  if (age == null) { el.textContent = '无采集数据'; el.className = 'badge bad'; }
  else if (age < staleMs) { el.textContent = `数据新鲜 · ${ago(c.last_run)}`; el.className = 'badge ok'; }
  else { el.textContent = `数据过期 · ${ago(c.last_run)}`; el.className = 'badge warn'; }
}

/* ---------- Hermes（历史在服务端 SQLite，面板与终端共享同一份对话） ---------- */
const OPEN_KEY = 'vvvhermes_open';
const HERMES_INTRO = '你好，我是 VVVhermes。我能读到左边面板的实时状态、特征值、cRSI、IV/RV 与翻转历史——直接问即可。历史与终端 VVVhermes 共享。';

function hermesAdd(cls, text) {
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.textContent = text;
  $('hermesMsgs').appendChild(el);
  $('hermesMsgs').scrollTop = $('hermesMsgs').scrollHeight;
  return el;
}

function hermesRenderAll(messages) {
  const box = $('hermesMsgs');
  box.innerHTML = '';
  hermesAdd('bot', HERMES_INTRO);
  messages.forEach((m) => hermesAdd(m.role === 'user' ? 'user' : 'bot', m.content));
}

async function hermesSync(force) {
  if (S.hermes.busy) return; // 回答进行中不重建，避免吃掉乐观气泡
  const seq = ++S.hermes.syncSeq;
  try {
    const r = await fetch('/api/agent/history?limit=60');
    if (!r.ok) return;                       // 500 不是"空历史"，保持现状
    const j = await r.json();
    if (!Array.isArray(j.messages)) return;
    // await 之后世界可能变了：有更新的同步在途，或用户已开始提问——都不准回滚
    if (seq !== S.hermes.syncSeq || S.hermes.busy) return;
    const msgs = j.messages;
    const lastId = msgs.length ? msgs[msgs.length - 1].id : 0;
    if (force || lastId !== S.hermes.lastId) {
      S.hermes.lastId = lastId;
      hermesRenderAll(msgs);
    }
  } catch (e) { /* 服务不可达时保持现状 */ }
}

function hermesRestore() {
  try { localStorage.removeItem('vvvhermes_chat'); } catch (e) { /* 旧本地历史已弃用 */ }
  if (localStorage.getItem(OPEN_KEY) === '0') $('hermes').classList.add('hidden');
  hermesSync(true);
}

async function hermesClear() {
  if (S.hermes.busy) { hermesAdd('err', '回答生成中，暂不能清空（否则在途回答会重新入库）。'); return; }
  let okc = false;
  try {
    const r = await fetch('/api/agent/clear', { method: 'POST' });
    okc = r.ok && ((await r.json()).ok === true);
  } catch (e) { /* fallthrough */ }
  if (!okc) { hermesAdd('err', '清空失败：服务端未确认，历史保持不变。'); return; }
  S.hermes.lastId = 0;
  hermesRenderAll([]);
  hermesAdd('bot', '已开始新会话（面板与终端共享的历史已清空）。');
}
async function hermesInfo() {
  try {
    const r = await fetch('/api/agent/info');
    const j = await r.json();
    const sys = j.custom_system ? ' · 提示词:hermes_system.md' : '';
    // config_error 优先：agent.json 解析失败时 provider 会静默退回 mock，
    // 只显示"未配置模型"会让人以为是没配，而不是配坏了
    if (j.config_error) {
      $('hermesMeta').textContent = `⚠ agent.json 解析失败：${j.config_error}（已退回 mock）`;
      return;
    }
    $('hermesMeta').textContent = (j.provider === 'mock'
      ? 'mock · 未配置模型（见 agent.example.json）'
      : `${j.provider} · ${j.model}`) + sys;
  } catch (e) {
    $('hermesMeta').textContent = '状态未知';
  }
}
async function hermesSend() {
  const ta = $('hermesText');
  const text = ta.value.trim();
  if (!text || S.hermes.busy) return;
  ta.value = '';
  const optimistic = hermesAdd('user', text); // 乐观展示；服务端成功后以库中记录为准
  S.hermes.busy = true;
  $('hermesSend').disabled = true;
  const busyEl = hermesAdd('bot busy', 'VVVhermes 思考中…（codex 后端通常需要 1-3 分钟）');
  let ok = false;
  try {
    const r = await fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: S.symbol, message: text }),
    });
    const j = await r.json();
    busyEl.remove();
    if (j.error) {
      hermesAdd('err', j.error);
      optimistic.classList.add('unsent');
      optimistic.title = '发送失败：此条未入共享历史';
    } else {
      hermesAdd('bot', j.reply);
      ok = true;
    }
  } catch (e) {
    busyEl.remove();
    hermesAdd('err', `请求失败：${e}`);
    optimistic.classList.add('unsent');
    optimistic.title = '发送失败：此条未入共享历史';
  } finally {
    S.hermes.busy = false;
    $('hermesSend').disabled = false;
    ta.focus();
    if (ok) hermesSync(true); // 与服务端对齐（终端若同时在聊也会一并带回）
  }
}
function hermesToggle(show) {
  const el = $('hermes');
  const want = show != null ? show : el.classList.contains('hidden');
  el.classList.toggle('hidden', !want);
  try { localStorage.setItem(OPEN_KEY, want ? '1' : '0'); } catch (e) { /* 忽略 */ }
  Object.values(S.charts).forEach((c) => c && c.resize());
}
$('hermesSend').onclick = hermesSend;
$('hermesClose').onclick = () => hermesToggle(false);
$('hermesToggle').onclick = () => hermesToggle();
$('hermesClear').onclick = hermesClear;
$('hermesText').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); hermesSend(); }
});

/* ---------- 时钟 / 自动刷新 ---------- */
setInterval(() => {
  const d = new Date();
  $('clock').textContent =
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} UTC`;
  const left = Math.max(0, Math.round((S.nextRefresh - Date.now()) / 1000));
  $('countdown').textContent = `刷新 ${left}s`;
  if (left === 0 && S.symbol) { S.nextRefresh = Date.now() + REFRESH_MS; load(); }
}, 1000);

window.addEventListener('resize', () => {
  Object.values(S.charts).forEach((c) => c && c.resize());
});

/* ---------- 启动 ---------- */
(async () => {
  try {
    hermesRestore();
    await loadSymbols();
    await Promise.all([load(), hermesInfo()]);
  } catch (e) {
    banner(`初始化失败：${e}`);
  }
})();
