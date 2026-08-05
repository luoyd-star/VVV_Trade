/* VVV 市场状态面板 · 前端逻辑（浅色 · 工程模式）
   状态五色与折线配对色均通过 dataviz 校验（light surface, all-pairs）。 */
'use strict';

const SM = {
  trend_up:      { color: '#0a8a66' },
  trend_down:    { color: '#b91f31' },
  range:         { color: '#4a90d9' },
  squeeze:       { color: '#a87c05' },
  high_vol_chop: { color: '#5f35c9' },
};
const COL = {
  up: '#0a8a66', down: '#b91f31',
  upDim: 'rgba(10,138,102,.45)', downDim: 'rgba(185,31,49,.4)',
  blue: '#3861fb', amber: '#a87c05', azure: '#4a90d9',
  gold: '#dd8500',   // 3d 持仓前端层专属（IV3 实线 / RV3 虚线同色配对）
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

// 中文标签只消费 dashboard.states_map；前端仅保留经过可视化校验的状态颜色。
function stateMeta(state) {
  const local = SM[state] || {};
  const labels = (S.data && S.data.states_map) || {};
  return { label: esc(labels[state] || state), color: local.color || COL.muted };
}

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
  S.symbols = (j.symbols && j.symbols.length) ? j.symbols : ['BTC-USDT'];
  S.symbol = S.symbol || S.symbols[0];
  renderSymList();
  setActiveTab();
  $('symBtn').onclick = (e) => {
    e.stopPropagation();
    const m = $('symMenu');
    m.hidden = !m.hidden;
    if (!m.hidden) { $('symSearch').value = ''; renderSymList(); $('symSearch').focus(); }
  };
  $('symSearch').oninput = renderSymList;
  $('symSearch').onkeydown = (e) => { if (e.key === 'Escape') $('symMenu').hidden = true; };
  document.addEventListener('click', (e) => {
    if (!$('symSel').contains(e.target)) $('symMenu').hidden = true;
  });
}

function renderSymList() {
  const q = ($('symSearch').value || '').trim().toUpperCase();
  const host = $('symList');
  host.innerHTML = '';
  (S.symbols || []).filter((s) => !q || s.toUpperCase().includes(q)).forEach((sym) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = sym === S.symbol ? 'active' : '';
    b.innerHTML = `<span class="sym">${esc(sym)}</span>`;
    b.onclick = () => {
      S.symbol = sym;
      $('symMenu').hidden = true;
      setActiveTab();
      load();
    };
    host.appendChild(b);
  });
  if (!host.children.length) host.innerHTML = '<div class="sub" style="padding:6px">无匹配品种</div>';
}

// 顶栏按钮：名称 + 1d 状态色圆点（数据到了才有颜色）
function setActiveTab() {
  $('symName').textContent = S.symbol || '--';
  $('hermesSym').textContent = S.symbol || '--';
  const t = S.data && S.data.tfs && (S.data.tfs['1d'] || S.data.tfs['4h'] || S.data.tfs['1h']);
  $('symDot').style.background = t ? stateMeta(t.state).color : COL.muted;
  document.querySelectorAll('#symList button').forEach((b) =>
    b.classList.toggle('active', b.textContent === S.symbol));
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
  loadCoupling(); // 耦合雷达跨品种，独立取数（服务端 5 分钟缓存）
}

/* ---------- 耦合雷达（M3 诊断层，独立于单品种数据流） ---------- */
const CSTATE = {
  coupled:        { label: '耦合',   color: COL.blue },
  decoupling:     { label: '脱耦中', color: '#a87c05' },
  decoupled:      { label: '已脱耦', color: COL.down },
  recoupling:     { label: '回耦中', color: COL.azure },
  REBASE_PENDING: { label: '待重锚', color: COL.muted },
  NOT_APPLICABLE: { label: '不适用', color: COL.muted },
};
let COUP_LAST = null;  // 最近一次耦合数据：折叠期间不画矩阵，展开时用它补画

async function loadCoupling() {
  let j;
  try {
    const r = await fetch('/api/coupling');
    j = await r.json();
  } catch (e) { return; }
  const host = $('coupBody');
  if (!host || !j) return;
  COUP_LAST = j;
  const parts = [];
  const pn = { all247: '24/7（加密+商品）', usrth: '美股RTH', cross: '跨类共同RTH' };
  const pl = Object.entries(j.panels || {}).map(([k, p]) => {
    const g = p.global;
    const ins = (p.status_counts || {}).INSUFFICIENT || 0;
    return `<span class="li"><b>${pn[k] || k}</b> ${g
      ? `λ1/N ${fmtN(g.market_mode, 2)} · 均值ρ ${fmtN(g.mean_corr, 2)}`
      : '—'}${ins ? ` · 样本不足对 ${ins}` : ''}</span>`;
  }).join('');
  parts.push(`<div class="coup-panels">${pl}</div>`);
  if ((j.pairs || []).length) {
    parts.push('<div class="coup-pairs">' + j.pairs.map((p) => {
      const m = CSTATE[p.state] || { label: esc(p.state), color: COL.muted };
      return `<span class="cchip" style="border-color:${m.color}">
        <i style="background:${m.color}"></i>${esc(p.a.split('-')[0])}×${esc(p.b.split('-')[0])}
        <b>${fmtN(p.rho_fast, 2, true)}</b> ${m.label}</span>`;
    }).join('') + '</div>');
  }
  if ((j.blocks || []).length) {
    parts.push('<div class="coup-blocks">' + j.blocks.map((b) =>
      `<span class="li">${esc(b.block_a)}×${esc(b.block_b)}：${b.votes}/3票 `
      + `<b>${b.state === 'coupled' ? '耦合' : '未耦合'}</b>`
      + `（中位ρ ${fmtN(b.median_rho_slow, 2, true)} · coupled占比 ${fmtN(b.coupled_share, 2)}）</span>`
    ).join('') + '</div>');
  }
  host.innerHTML = parts.join('');
  $('coupMeta').textContent =
    `阈值代 ${j.threshold_version} · 更新 ${ago(j.updated_at)}`;
  // 折叠时跳过矩阵：display:none 容器里 ECharts 初始化是 0×0，展开时 toggle 补画
  const box = $('coupBox');
  if (!box || box.open) renderCoupMatrix(j.matrix);
}

// 折叠框展开状态持久化 + 展开时补画矩阵（折叠期间跳过了渲染）
function initCoupBox() {
  const box = $('coupBox');
  if (!box) return;
  if (localStorage.getItem('vvv_coup_open') === '1') box.open = true;
  box.addEventListener('toggle', () => {
    if (box.open) {
      localStorage.setItem('vvv_coup_open', '1');
      if (COUP_LAST) renderCoupMatrix(COUP_LAST.matrix);
      const inst = window.echarts && echarts.getInstanceByDom($('coupMatrix'));
      if (inst) inst.resize();
    } else {
      localStorage.removeItem('vvv_coup_open');
    }
  });
}

// 38×38 复合相关矩阵：每格用应有时钟（加密24/7 / 美股RTH / 跨类共同RTH），
// 慢线 EWMA；样本不足（n<400）淡显；观察池符号打 ° 标；轴按主题块排序。
function renderCoupMatrix(m) {
  const host = document.getElementById('coupMatrix');
  if (!host || !m || !(m.symbols || []).length) return;
  // 高度随品种数自适应：74 品种在固定 720px 里每格只剩 ~8px，标签全部挤没
  const want = Math.max(720, m.symbols.length * 13 + 160);
  if (Math.abs(host.clientHeight - want) > 8) {
    host.style.height = want + 'px';
    const old = echarts.getInstanceByDom(host);
    if (old) old.resize();
  }
  const c = chart('coupMatrix');
  if (!c) return;
  const short = (s) => s.split('-')[0] + (m.pools[s] === 'observation' ? '°' : '');
  const labels = m.symbols.map(short);
  const data = [];
  m.cells.forEach(([i, j, rho, n, valid]) => {
    if (rho == null) return;
    const cell = { value: [j, i, rho], n, valid };
    if (!valid) cell.itemStyle = { opacity: 0.22 };
    data.push(cell);
    if (i !== j) {
      const mir = { value: [i, j, rho], n, valid };
      if (!valid) mir.itemStyle = { opacity: 0.22 };
      data.push(mir);
    }
  });
  c.setOption({
    animation: false,
    tooltip: {
      backgroundColor: COL.tipBg, borderColor: COL.border,
      textStyle: { color: COL.ink, fontSize: 11.5 },
      formatter: (p) => {
        const a = m.symbols[p.value[1]], b = m.symbols[p.value[0]];
        const d = p.data;
        return `<b>${esc(a.split('-')[0])} × ${esc(b.split('-')[0])}</b><br>`
          + `ρ_slow = ${fmtN(p.value[2], 3, true)}<br>`
          + `联合样本 ${d.n}${d.valid ? '' : `（<${m.min_eff}，样本不足·淡显）`}`;
      },
    },
    grid: { left: 70, right: 60, top: 8, bottom: 76 },
    xAxis: { type: 'category', data: labels, position: 'bottom',
             axisLabel: { rotate: 60, fontSize: 9, color: COL.sub },
             axisTick: { show: false }, splitLine: { show: false } },
    yAxis: { type: 'category', data: labels, inverse: true,
             axisLabel: { fontSize: 9, color: COL.sub },
             axisTick: { show: false }, splitLine: { show: false } },
    visualMap: {
      min: -1, max: 1, calculable: false, orient: 'vertical',
      right: 4, top: 'center', itemHeight: 160, textStyle: { color: COL.muted, fontSize: 9 },
      inRange: { color: ['#b91f31', '#f6e9ea', '#ffffff', '#e7f2ee', '#0a8a66'] },
    },
    series: [{
      type: 'heatmap', data,
      label: { show: false },
      emphasis: { itemStyle: { borderColor: COL.ink, borderWidth: 1 } },
      itemStyle: { borderColor: '#ffffff', borderWidth: 0.5 },
    }],
  }, true);
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
  [renderMktBadge, renderStateCards, renderPolicy, renderVolRanks, renderAlerts, renderTfPicker,
   renderPriceChart, renderDvol, renderVolRank, renderDeriv, renderStrips,
   renderFeatTable, renderFlips, renderCollector, renderFresh].forEach((fn) => {
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
    const meta = stateMeta(t.state);
    const f = t.features;
    const cl = (t.crsi || {}).last || {};

    let warn = '';
    if (t.health && (t.health.stale || t.health.warmup)) {
      const tips = [];
      if (t.health.stale) tips.push(`数据陈旧：最后收线 ${t.health.last_close_age_min} 分钟前`);
      if (t.health.warmup) tips.push(`预热中：仅 ${t.health.bars} 根，分位参照期不足`);
      warn = `<span class="warn-ico" title="${tips.join('；')}">⚠️</span>`;
    }

    const diverged = t.raw_state && t.raw_state !== t.state;
    const confTxt = `conf(原始) ${fmtN(t.confidence, 2)}`;

    const dyn = [];
    if (t.preview) {
      const pm = stateMeta(t.preview.state);
      dyn.push(`预览(未收线) <b>${pm.label}</b> ${fmtN(t.preview.confidence, 2)}`);
    }
    if (t.candidate) {
      const cm = stateMeta(t.candidate.state);
      dyn.push(`酝酿 <b>${cm.label}</b> ${t.candidate.count}/${t.candidate.need}`
        + (t.candidate.gated ? '（事件窗·门槛+1）' : (t.candidate.event_win ? '（事件窗）' : ''))
        + (diverged && t.candidate.state === t.raw_state ? ` · ${confTxt}` : ''));
    }
    if (diverged && !(t.candidate && t.candidate.state === t.raw_state)) {
      const rm = stateMeta(t.raw_state);
      dyn.push(`原始判定 <b>${rm.label}</b> ${confTxt}`);
    }
    const mg = (f.margin || {});
    if (mg.margin != null && mg.margin < 0.15) {
      dyn.push(`⚡原始树边界 m=${fmtN(mg.margin, 2)}（${esc(mg.nearest)}）`);
    }
    if (!dyn.length) dyn.push('稳定 · 无待确认切换'
      + (mg.margin != null ? ` · 原始树 margin ${fmtN(mg.margin, 2)}` : ''));

    // 标记（原 chips）压成一行灰字，优先级前 4 个
    const flags = [];
    if (f.volatility.squeeze) flags.push('SQZ 挤压');
    if (f.volatility.high_vol) flags.push('HV 高波');
    if (cl.zone && cl.zone !== '带内') flags.push(`cRSI ${esc(cl.zone)}（${fmtN(cl.pos, 0)}%）`);
    const dvg = (t.crsi || {}).last_divergence;
    if (dvg && dvg.bars_ago <= 10) flags.push(`${dvg.kind === 'bull' ? '看涨' : '看跌'}背离 ${dvg.bars_ago}根前`);
    if (f.volume.breakout) {
      const b = f.volume.breakout;
      flags.push(`突破${b.dir === 'up' ? '↑' : '↓'} 量分位 ${fmtN(b.vol_rank, 2)}`);
    }

    // VWAP 偏离双向条：0 居中，±2 ATR 刻线，墨色（琥珀是挤压状态色，不能复用）
    const vw = t.vwap || null;
    const dev = vw && vw.dev != null ? vw.dev : null;
    const w = dev == null ? 0 : Math.min(Math.abs(dev) / 3, 1) * 50;
    const left = dev == null ? 50 : (dev >= 0 ? 50 : 50 - w);
    const vwapHtml = dev == null
      ? '<span class="muted">VWAP 偏离 —（量流未就绪）</span>'
      : `<span class="bipolar"><u class="zero"></u><u class="n2"></u><u class="p2"></u>
           <i style="left:${left}%;width:${w}%"></i></span>
         <span>VWAP 偏离 ${fmtN(dev, 2, true)} ATR`
        + `${vw.dev_rank != null ? ` · 分位 ${fmtN(vw.dev_rank, 2)}` : ''}`
        + ` · 币安量 ${vw.win_hours}h 窗</span>`;

    const el = document.createElement('div');
    el.className = 'sblock';
    el.innerHTML = `
      <div class="sb-top">
        <span class="tf">${tf}</span>${warn}
        <span class="sb-conf">${confTxt}</span>
      </div>
      <div class="sb-name"><i style="background:${meta.color}"></i><b>${meta.label}</b></div>
      <div class="sb-dyn">
        ${dyn.map((x) => `<span>${x}</span>`).join('')}
        ${flags.length ? `<span class="muted">${flags.slice(0, 4).join(' · ')}</span>` : ''}
      </div>
      <div class="sb-vwap">${vwapHtml}</div>`;
    host.appendChild(el);
  });
  setActiveTab(); // 顶栏圆点跟随 1d 状态色
}

function policyZoneMatches(zone, hit) {
  if (!zone || !hit) return false;
  return ['lo', 'hi'].every((key) => Number.isFinite(Number(zone[key]))
    && Number.isFinite(Number(hit[key])) && Math.abs(Number(zone[key]) - Number(hit[key])) < 1e-9);
}

function renderPolicy() {
  const p = S.data.policy;
  const rowsHost = $('policyZoneRows');
  if (!p) {
    $('policyMeta').textContent = 'policy payload 不可用';
    $('policyConclusion').textContent = '建议关注 · 政策测量不可用';
    $('policySignal').textContent = '共振不可判定';
    $('policySignal').className = 'badge';
    rowsHost.innerHTML = '<tr><td colspan="6">关键位不可用</td></tr>';
    $('policyApproach').textContent = '路径不可计算';
    $('policyVolNotes').textContent = '暂无可计算提示';
    $('policyStop').textContent = '止损宽度不可计算';
    $('policyDegraded').textContent = '';
    return;
  }

  $('policyMeta').textContent = `${p.tf || '4h'} · ${p.regime_4h || '状态不可用'}`
    + (p.regime_1d ? ` · 1d ${p.regime_1d}` : ' · 1d 不可用');
  const location = p.location || {};
  let conclusion = p.play;
  if (!conclusion && location.at === 'middle_zone') conclusion = '中间区域，默认观望';
  $('policyConclusion').textContent = `建议关注 · ${conclusion || '暂无匹配剧本'}`;
  const sig = $('policySignal');
  if (p.signal_ok === true) {
    sig.textContent = '位置 + 信号共振'; sig.className = 'badge ok';
  } else if (p.signal_ok === false) {
    sig.textContent = '位置已到 · 信号未共振'; sig.className = 'badge warn';
  } else {
    sig.textContent = '共振不可判定'; sig.className = 'badge';
  }

  const hit = location.zone;
  const zones = p.zones || [];
  rowsHost.innerHTML = zones.map((zone) => {
    const matched = policyZoneMatches(zone, hit);
    const cls = `${matched ? 'matched ' : ''}${zone.eligible ? '' : 'ineligible'}`.trim();
    const role = zone.role_now === 'support' ? '支撑'
      : zone.role_now === 'resistance' ? '压力' : '不可判定';
    const flipped = zone.role_flipped ? ' · 已翻转' : '';
    const eligible = zone.eligible
      ? '<span class="yes">是</span>'
      : '<span class="no" title="当前 regime 未采纳该来源与当前角色的组合">否 · regime/来源不匹配</span>';
    return `<tr class="${cls}">
      <td>${fmtN(zone.dist_atr, 2)}</td>
      <td class="mono">[${fmtPrice(zone.lo)}, ${fmtPrice(zone.hi)}]</td>
      <td style="text-align:left">${(zone.kinds || []).map(esc).join(' · ') || '—'}</td>
      <td>${zone.touches == null ? '—' : esc(zone.touches)}</td>
      <td>${role}${flipped}</td><td>${eligible}${matched ? ' · 命中' : ''}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6">没有可用关键位区间</td></tr>';

  const approachName = {
    from_above: '从区间上方回踩而来',
    from_below: '从区间下方反弹而来',
  }[location.approach] || '来向不可判定';
  let required = null;
  if (p.regime_4h === 'trend_up' && location.at === 'at_support') required = 'from_above';
  if (p.regime_4h === 'trend_down' && location.at === 'at_resistance') required = 'from_below';
  let pathCheck = '该剧本不设方向路径门槛';
  if (required) {
    pathCheck = location.approach === required
      ? '满足该剧本的来向要求'
      : `未满足该剧本要求（需要${required === 'from_above' ? '从上方回踩' : '从下方反弹'}）`;
  }
  $('policyApproach').textContent = `${approachName}；${pathCheck}`;

  const notes = p.vol_notes || [];
  $('policyVolNotes').innerHTML = notes.length
    ? `<ul class="policy-notes">${notes.map((note) => `<li>${esc(note)}</li>`).join('')}</ul>`
    : '暂无可计算的波动率或事件提示';

  const stop = p.stop_check;
  if (stop) {
    const verdict = {
      too_tight: '严重提示：偏紧', tight: '提示：偏紧', ok: '宽度充足',
    }[stop.verdict] || stop.verdict;
    $('policyStop').textContent = `${stop.side === 'long' ? '多头' : '空头'}结构失效参考 ${fmtPrice(stop.stop_price)}`
      + ` · 距现价 ${fmtN(stop.stop_dist_pct, 2)}% · 持仓期预期波动 ${fmtN(stop.expected_move_pct, 2)}%`
      + ` · 比值 ${fmtN(stop.ratio, 2)} · ${verdict}（${stop.note}）`;
  } else {
    $('policyStop').textContent = '结构位或 IV3 缺失，止损宽度校验不可计算';
  }
  const degraded = p.degraded || [];
  $('policyDegraded').textContent = degraded.length ? `降级标注：${degraded.join(' · ')}` : '';
}

// 六条分位条：挤压侧 ATR<0.30 且 BBW<0.15；高波只由 ATR>0.85 判定。
function renderVolRanks() {
  const d = S.data;
  const items = [];
  TF_ORDER.filter((tf) => d.tfs[tf]).forEach((tf) => {
    const v = d.tfs[tf].features.volatility;
    items.push({ label: `${tf} ATR`, val: v.atr_rank, squeezeAt: 0.30, highVolAt: 0.85,
      rule: 'ATR<0.30 为挤压侧条件；ATR>0.85 判高波' });
    items.push({ label: `${tf} BBW`, val: v.bbw_rank, squeezeAt: 0.15, highVolAt: null,
      rule: 'BBW<0.15 为挤压侧条件；高波不看 BBW' });
  });
  $('volRanks').innerHTML = items.map(({ label, val, squeezeAt, highVolAt, rule }) => {
    const squeezeMark = squeezeAt === 0.30
      ? '<u class="t30" title="挤压ATR 0.30"></u>'
      : '<u class="t15" title="挤压BBW 0.15"></u>';
    const highVolMark = highVolAt == null ? '' : '<u class="t85" title="高波ATR 0.85"></u>';
    if (val == null) {
      return `<div class="rank" title="${rule}"><div class="rank-h"><span>${label}</span><b class="muted">—</b></div>
              <div class="bar">${squeezeMark}${highVolMark}</div></div>`;
    }
    const c = val < squeezeAt ? 'var(--squeeze)'
      : (highVolAt != null && val > highVolAt ? 'var(--chop)' : 'var(--accent)');
    return `<div class="rank" title="${rule}">
      <div class="rank-h"><span>${label}</span><b style="color:${c}">${fmtN(val, 2)}</b></div>
      <div class="bar"><i style="width:${Math.round(val * 100)}%;background:${c}"></i>
        ${squeezeMark}${highVolMark}</div></div>`;
  }).join('');
}

// 每个周期一行：酝酿中的候选 > 未收线预览 > 原始树边界 > 数据健康 > 稳定
function renderAlerts() {
  const d = S.data;
  const rows = TF_ORDER.filter((tf) => d.tfs[tf]).map((tf) => {
    const t = d.tfs[tf];
    const mg = (t.features.margin || {});
    const bits = [];
    let cls = '';
    if (t.candidate) {
      const cm = stateMeta(t.candidate.state);
      bits.push(`酝酿 ${cm.label} ${t.candidate.count}/${t.candidate.need}`
        + (t.candidate.event_win ? '（事件窗）' : ''));
      cls = 'warn';
    }
    if (t.preview) {
      const pm = stateMeta(t.preview.state);
      bits.push(`预览(未收线) ${pm.label} ${fmtN(t.preview.confidence, 2)}`);
      cls = cls || 'warn';
    }
    if (mg.margin != null && mg.margin < 0.15) {
      bits.push(`原始树 margin ${fmtN(mg.margin, 2)}（${esc(mg.nearest)}）`);
      cls = cls || 'warn';
    }
    if (t.health && t.health.stale) {
      bits.push(`数据陈旧 ${t.health.last_close_age_min} 分钟`);
      cls = 'bad';
    } else if (t.health && t.health.warmup) {
      bits.push(`预热中 ${t.health.bars} 根`);
      cls = cls || 'warn';
    }
    if (!bits.length) {
      bits.push('稳定 · 无待确认切换'
        + (mg.margin != null ? ` · 原始树 margin ${fmtN(mg.margin, 2)}` : ''));
    }
    return `<div class="alert ${cls}"><span class="tf">${tf}</span><span>${bits.join(' · ')}</span></div>`;
  });
  $('alerts').innerHTML = rows.join('');
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
  legend.innerHTML = Object.keys(SM).map((k) => {
    const m = stateMeta(k);
    return (
    `<span class="li"><span class="sw" style="background:${m.color};opacity:.5"></span>${m.label}</span>`
    );
  }).join('') + `<span class="li"><span class="sw" style="background:${COL.blue}"></span>EMA50 / cRSI</span>
    <span class="li"><span class="sw" style="background:${COL.azure}"></span>cRSI 自适应带</span>
    <span class="li"><span class="sw" style="background:${COL.muted}"></span>H/L 摆动点 · ●背离</span>
    <span class="li"><span class="sw" style="background:${COL.ink}"></span>VWAP（币安量）/ 偏离</span>`;
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
    { xAxis: sg.s, itemStyle: { color: stateMeta(sg.state).color, opacity: 0.09 } },
    { xAxis: sg.e },
  ]);
  const pivH = t.pivots.filter((p) => p.kind === 'H').map((p) => [p.i, p.price]);
  const pivL = t.pivots.filter((p) => p.kind === 'L').map((p) => [p.i, p.price]);
  const cr = t.crsi || { crsi: [], db: [], ub: [], divs: [] };
  const divBull = cr.divs.filter((d) => d.kind === 'bull').map((d) => [d.i, cr.crsi[d.i]]);
  const divBear = cr.divs.filter((d) => d.kind === 'bear').map((d) => [d.i, cr.crsi[d.i]]);
  // VWAP（币安量源，显示层）：主图画 VWAP 线；cRSI 格副轴画偏离度（ATR 单位）
  const vw = t.vwap || null;
  const vwSeries = vw ? vw.series : [];
  const vwDev = vw ? vw.dev_series : [];

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
        const m = st ? stateMeta(st) : null;
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
          vw && vwSeries[i] != null
            ? `VWAP ${fmtPrice(vwSeries[i])}（偏离 ${fmtN(vwDev[i], 2, true)} ATR，币安量 ${vw.win_hours}h窗）`
            : 'VWAP —（量流未就绪）',
          `cRSI ${fmtN(cv, 1)}（带 ${fmtN(cr.db[i], 1)}~${fmtN(cr.ub[i], 1)}${zone}）`,
          m ? `状态 <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${m.color}"></span> ${m.label}` : '状态 —',
        ].join('<br>');
      },
    },
    grid: [
      { left: 62, right: 18, top: 10, height: '42%' },
      { left: 62, right: 18, top: '54%', height: '8%' },
      { left: 62, right: 18, top: '66%', height: '12%' },
      // VWAP 偏离独立面板（用户反馈：与 cRSI 同格量纲混叠）
      { left: 62, right: 18, top: '81%', height: '11%' },
    ],
    xAxis: [
      { ...axisCommon, gridIndex: 0, axisLabel: { show: false } },
      { ...axisCommon, gridIndex: 1, axisLabel: { show: false } },
      { ...axisCommon, gridIndex: 2, axisLabel: { show: false } },
      { ...axisCommon, gridIndex: 3, axisLabel: { color: COL.muted, fontSize: 10 } },
    ],
    yAxis: [
      { scale: true, gridIndex: 0, splitLine: { lineStyle: { color: COL.grid } },
        axisLabel: { color: COL.muted, fontSize: 10, formatter: (v) => fmtPrice(v) } },
      { gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
      { scale: true, gridIndex: 2, splitLine: { lineStyle: { color: COL.grid } },
        axisLabel: { color: COL.muted, fontSize: 9.5 } },
      { scale: true, gridIndex: 3, splitLine: { show: false },
        axisLabel: { color: COL.ink, fontSize: 9 },
        name: 'VWAP偏离(ATR)', nameTextStyle: { color: COL.ink, fontSize: 9 },
        nameGap: 6 },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2, 3], start: Math.max(0, 100 - 11000 / N), end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2, 3], bottom: 4, height: 14,
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
      { name: 'VWAP', type: 'line', data: vwSeries, symbol: 'none', z: 2,
        lineStyle: { width: 1.5, color: COL.ink, type: 'dashed' },
        xAxisIndex: 0, yAxisIndex: 0 },
      { name: 'VWAP偏离', type: 'line', data: vwDev, symbol: 'none', z: 2,
        lineStyle: { width: 1.5, color: COL.ink },
        areaStyle: { color: 'rgba(23,26,32,.07)', origin: 0 },
        xAxisIndex: 3, yAxisIndex: 3,
        markLine: { silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: '#c3c9d4' },
          label: { color: COL.muted, fontSize: 9, position: 'insideEndTop' },
          data: [{ yAxis: 0 }, { yAxis: 2 }, { yAxis: -2 }] } },
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
      formatter: (params) => {
        const values = params.filter((p) => p.seriesType === 'line').map((p) =>
          `${p.marker}${p.seriesName} ${p.value == null ? '—' : Number(p.value).toFixed(2)}`);
        return [`<b>${esc((params[0] || {}).axisValue || '')}</b>`, ...values,
          '<span style="color:#7c8595">规则：ATR&lt;0.30 且 BBW&lt;0.15 才判挤压；高波仅 ATR&gt;0.85</span>'].join('<br>');
      },
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
          data: [{ yAxis: 0.30, label: { formatter: '挤压ATR' } },
                 { yAxis: 0.85, label: { formatter: '高波ATR' } }] } },
      { name: 'BBW分位', type: 'line', data: t.bbw_rank_series, symbol: 'none',
        lineStyle: { width: 2 },
        markLine: { silent: true, symbol: 'none',
          lineStyle: { type: 'dashed', color: '#c3c9d4' },
          label: { color: COL.muted, fontSize: 9, position: 'insideEndBottom' },
          data: [{ yAxis: 0.15, label: { formatter: '挤压BBW' } }] } },
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
    meta.textContent = '该品种无期权 IV 数据';
    if (c) c.clear();
    renderIv3({});   // 同步清空 3d 卡
    return;
  }
  // 近端 IV（币安期权，1-3 天期限，24/7）：持仓前端层，与 30 天口径的 DVOL 分开标注
  const x = d.xopt;
  const xTxt = !x ? null
    : `近端IV ${fmtN(x.iv, 1)}（~${fmtN(x.tenor_days, 1)}d·${x.method === 'nearest' ? '单点' : '插值'}${x.n_expiries != null ? '·' + x.n_expiries + '到期' : ''}）`;
  meta.textContent = [
    d.iv_last == null ? null
      : `DVOL(日结算·30d) ${fmtN(d.iv_last, 1)}（分位 ${fmtN(d.iv_rank, 3)}）`,
    `RV30 ${fmtN(d.rv_last, 1)}`,
    d.spread == null ? null : `IV−RV ${fmtN(d.spread, 1, true)}pt`,
  ].filter(Boolean).join(' · ');
  // 3d 持仓前端独立成卡（RV3 尖峰与 30d 序列不同量级，同轴互相压平）
  renderIv3({ iv3: d.iv3, rv3: d.rv3, iv_txt: xTxt,
              rv3_last: d.rv3_last, spread3: d.spread3 });
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

// 3d 持仓前端卡：IV3（自攒序列，2026-08-05 起在图右缘生长）vs RV3（72×1h 年化，
// 历史即刻可用）。与 30d 卡分图的原因：RV3 崩盘日尖峰可到 90%+，同轴会把 30d 序列压平。
// 色彩沿用 30d 卡语义：蓝=隐含、琥珀=已实现，读图习惯直接迁移。
function renderIv3(o) {
  const meta = $('iv3Meta');
  const c = chart('iv3Chart');
  const has = (o.rv3 && o.rv3.length) || (o.iv3 && o.iv3.length);
  if (!meta) return;
  if (!has) {
    meta.textContent = '暂无 3d 数据';
    if (c) c.clear();
    return;
  }
  meta.textContent = [
    o.iv_txt,
    o.rv3_last == null ? null : `RV3 ${fmtN(o.rv3_last, 1)}`,
    // 3d 剪刀差：同期限对照，"这笔 1-3 天仓的保险贵不贵"
    o.spread3 == null ? null : `3dIV−RV3 ${fmtN(o.spread3, 1, true)}pt`,
  ].filter(Boolean).join(' · ');
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
      // IV3 序列 2026-08-05 清零自攒：稀疏期（<60 点）画出点标记，否则 30 分钟宽的
      // 线段在 100 天轴上是亚像素、完全不可见；攒够后自动退回纯线
      ...(o.iv3 && o.iv3.length ? [{ name: '3d 隐含', type: 'line', data: o.iv3,
        symbol: 'circle', symbolSize: 4.5, showSymbol: o.iv3.length < 60,
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: 'IV3', color: COL.sub, fontSize: 9.5 } }] : []),
      ...(o.rv3 && o.rv3.length ? [{ name: 'RV3 已实现', type: 'line', data: o.rv3,
        symbol: 'none', lineStyle: { width: 1.5 },
        endLabel: { show: true, formatter: 'RV3', color: COL.sub, fontSize: 9.5 } }] : []),
    ],
  }, true);
}

// 美股永续变体：**个股自身 IV 为主线**（moomoo 口径，含自有分位）+ 指数 IV 作长历史锚
// + 本品种 RV30。升级前 29/31 个品种拿 VXN 当自己的 IV，剪刀差因此是口径错配的假象。
function renderUsvol(uv, meta, c) {
  const iv = uv.iv;
  // 分位已按「未来30天内有无财报」条件化（同状态比同状态）——实测把富集从 1.92×
  // 压到 0.94×。与原始分位差距大时两个都显示，让读者看到修正幅度。
  const rDiff = iv && iv.rank != null && iv.rank_raw != null
    && Math.abs(iv.rank - iv.rank_raw) >= 0.1;
  const rankKindTxt = iv && iv.rank_kind === 'cond'
    ? '条件分位' : '原始分位';
  const rankTxt = iv && iv.rank != null
    ? `${rankKindTxt} ${fmtN(iv.rank, 2)}`
      + (iv.rank_kind === 'cond' ? '·同财报状态·504日' : '·252日')
      + `${rDiff ? `（原始 ${fmtN(iv.rank_raw, 2)}）` : ''}`
      + (iv.rank_kind === 'cond' && iv.earn_in30 ? '·财报窗内' : '')
    : `样本 ${iv ? iv.n : 0}d·分位不足`;
  // 财报邻近度：分位在财报窗内是日程驱动而非市场压力，必须标出来
  const ed = iv && iv.earnings_days;
  const earnTxt = ed == null ? ''
    : ed > 0 ? ` ⚑财报还有${ed}日` : ed < 0 ? ` ⚑财报已过${-ed}日` : ' ⚑今日财报';
  // VRP=IV−HV 同源同口径，把"贵"与"波动大"分开；期限结构双速分别给，
  // 因为快端抓急性冲击、慢端抓制度切换，合成单一读数会丢掉这个区分
  const t = uv.term || {};
  const inv = t.both_inverted ? '（全曲线倒挂）'
    : t.fast && t.fast.inverted ? '（快端倒挂）'
    : t.slow && t.slow.inverted ? '（慢端倒挂）' : '';
  // 盘中实时值：与结算值分列展示。分位只在结算序列上算，实时值给的是**预览分位**，
  // 必须显式标注"实时"——同"预览(未收线)"的既有约定
  const lv = iv && iv.live;
  const liveTxt = !lv ? '' :
    `实时(未结算) ${fmtN(lv.iv, 1)}`
    + (lv.chg == null ? '' : `（${fmtN(lv.chg, 1, true)} / ${fmtN(lv.chg_pct, 1, true)}%）`)
    + (lv.rank_preview == null ? '' : `·预览分位 ${fmtN(lv.rank_preview, 2)}`);
  // 商品（XAU/XAG）：iv 来自代理标的（GLD/SLV），必须标注；xopt 是币安期权近端 IV
  //（24/7 更新但期限仅 1-3 天，与 30 天口径不是同一个量，期限随值展示）
  const ivLabel = uv.proxy ? `结算IV(代理${uv.proxy})` : '结算IV';
  const bits = [
    lv ? liveTxt : null,
    iv ? `${ivLabel}${iv.stale ? `⚠${fmtN(iv.age_days, 0)}天前` : ''} ${fmtN(iv.last, 1)}（${rankTxt}）${earnTxt}` : '个股IV 未回填',
    iv && iv.vrp != null
      ? `VRP ${fmtN(iv.vrp, 1, true)}pt（分位 ${fmtN(iv.vrp_rank, 2)}）` : null,
    `RV30 ${fmtN(uv.rv_last, 1)}`,
    uv.spread == null ? null
      : `${uv.spread_src === 'stock' ? '个股' : '指数'}IV−RV ${fmtN(uv.spread, 1, true)}pt`,
    uv.index == null ? null
      : `${uv.index}${uv.index_settled === false ? '°' : ''} ${fmtN(uv.index_last, 1)}（锚·分位 ${fmtN(uv.index_rank, 2)}）`,
    t.fast && t.slow
      ? `期限${t.fast.settled === false ? '°' : ''} 9D/30D ${fmtN(t.fast.ratio, 2)}·${fmtN(t.fast.rank, 2)} / 30D/3M `
        + `${fmtN(t.slow.ratio, 2)}·${fmtN(t.slow.rank, 2)}${inv}`
      : (uv.ts_ratio == null ? null : `9D/3M ${fmtN(uv.ts_ratio, 2)}`),
    // 个股期限曲线（3d/9d/30d ATM）：持仓前端层——3d 是"这笔 1-3 天仓"的直接定价
    uv.term_stock == null ? null
      : `个股期限 ${fmtN(uv.term_stock.iv3, 1)}/${fmtN(uv.term_stock.iv9, 1)}/${fmtN(uv.term_stock.iv30, 1)}`
        + `（3d/9d/30d${uv.term_stock.inverted ? '·倒挂' : ''}）`,
  ];
  meta.textContent = bits.filter(Boolean).join(' · ');
  // 3d 持仓前端独立成卡：美股 IV3 来自期限曲线自攒，商品来自币安近端 IV
  const ts3 = uv.term_stock;
  renderIv3({
    iv3: uv.iv3_hist, rv3: uv.rv3, rv3_last: uv.rv3_last, spread3: uv.spread3,
    iv_txt: ts3 && ts3.iv3 != null
      ? `IV3 ${fmtN(ts3.iv3, 1)}（期限曲线3d·RTH自攒）`
      : (uv.xopt
        ? `币安近端IV ${fmtN(uv.xopt.iv, 1)}（~${fmtN(uv.xopt.tenor_days, 1)}d·${uv.xopt.n_expiries === 1 ? '单到期' : (uv.xopt.method === 'interp' ? '插值' : '近邻')}·${fmtN(uv.xopt.age_min, 0)}分前）` : null),
  });
  if (!c) return;
  c.setOption({
    animation: false,
    useUTC: true,
    color: [COL.blue, COL.amber, COL.muted],
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
      // 个股 IV 与 RV30 是同一标的的同口径对照，实线；指数是跨标的的锚，虚线弱化
      ...(iv ? [{ name: '个股IV', type: 'line', data: iv.series, symbol: 'none',
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: 'IV', color: COL.sub, fontSize: 9.5 } }] : []),
      { name: 'RV30 已实现', type: 'line', data: uv.rv, symbol: 'none',
        lineStyle: { width: 2 },
        endLabel: { show: true, formatter: 'RV', color: COL.sub, fontSize: 9.5 } },
      // 商品无指数锚（uv.index 为空即不画）
      ...(uv.index ? [{ name: `${uv.index} 锚`, type: 'line', data: uv.series, symbol: 'none',
        lineStyle: { width: 1, type: 'dashed', opacity: 0.55 } }] : []),
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
    + `${dr.iv30_span_days != null
      ? ` · ${dr.iv30_src === 'moomoo' ? '个股IV(moomoo)' : 'iv30(CBOE影子)'} ${dr.iv30_span_days}d/${dr.iv30_n}点`
      : ''}${dr.warmup ? '（OI<21天，分位仅供参考）' : ''}`;
  const chg = (v) => (v == null ? '—' : fmtN((Math.exp(v) - 1) * 100, 2, true) + '%');
  const rk = (v) => (v == null ? '' : `（分位 ${fmtN(v, 2)}）`);
  const fundingPeriod = dr.funding_interval_h == null ? '未知周期' : `${dr.funding_interval_h}h`;
  info.innerHTML = [
    [dr.oi == null ? '—' : Math.round(dr.oi).toLocaleString('en-US'), `OI 张数${rk(dr.oi_rank)}`],
    [chg(dr.oi_change_4h), 'OI Δ4h'],
    [chg(dr.oi_change_24h), 'OI Δ24h'],
    [dr.taker_ratio == null ? '—' : fmtN(dr.taker_ratio, 3), `Taker 买卖比${rk(dr.taker_rank)}`],
    [dr.funding_pct == null ? '—' : `${fmtN(dr.funding_pct * 100, 2)}bp`, `Funding /${fundingPeriod}（下期预测）`],
    [dr.funding_settled_pct == null ? '—' : `${fmtN(dr.funding_settled_pct * 100, 2)}bp`, `上期结算${rk(dr.funding_rank)}`],
    [dr.funding_annual_pct == null ? '—' : `${fmtN(dr.funding_annual_pct, 1)}%`, 'Funding 年化'],
    [dr.premium_pct == null ? '—' : `${fmtN(dr.premium_pct * 100, 1)}bp`, `Premium${rk(dr.premium_rank)}`],
    dr.iv30 != null
      ? [fmtN(dr.iv30, 1), `个股 iv30${dr.iv30_src === 'cboe' ? '(CBOE影子·短史)'
        : dr.iv30_rank_kind === 'cond' ? '(moomoo·条件分位)' : '(moomoo·原始分位)'}${rk(dr.iv30_rank)}`]
      : [`${dr.span_days}d`, '样本跨度'],
  ].map(([v, k]) => `<div class="m"><span>${k}</span><b>${v}</b></div>`).join('');
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
      { name: `Funding %/${fundingPeriod}（已结算）`, type: 'bar', data: dr.funding_series,
        xAxisIndex: 1, yAxisIndex: 1, barWidth: 2 },
    ],
  }, true);
}

function renderStrips() {
  const host = $('strips');
  host.innerHTML = '';
  $('stripLegend').innerHTML = Object.keys(SM).map((k) => {
    const m = stateMeta(k);
    return `<span class="li"><span class="sw" style="background:${m.color}"></span>${m.label}</span>`;
  }).join('');
  TF_ORDER.filter((tf) => S.data.tfs[tf]).forEach((tf) => {
    const t = S.data.tfs[tf];
    const N = t.candles.length;
    const row = document.createElement('div');
    row.className = 'strip-row';
    const segs = t.segments.map((sg) => {
      const m = stateMeta(sg.state);
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
  const m = stateMeta(state);
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
    ['路径几何·影子', ['频率', '主周期', 'τ', 'margin(原始树)', '滞后']],
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
      ? stateMeta(t.raw_state).label : '—';
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
  // 后端已先排除 1h 再给 4h/1d 40 条预算；横向时间线仍画 1h 状态。
  const shown = flips;
  const rows = shown.map((f) =>
    `<tr><td>${fmtTs(f.ts, '4h')}</td><td>${esc(f.tf)}</td>` +
    `<td style="text-align:left">${stchip(f.from)} → ${stchip(f.to)}</td>` +
    `<td>${fmtN(f.confidence, 2)}</td></tr>`);
  // summary 只更新文字，不重建 <details>——保持用户手动展开/收起状态
  const sum = $('flipsSummary');
  if (sum) sum.textContent =
    `翻转明细 ${shown.length} 条（4h/1d · 1h 不列 · 点击展开）`;
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
  const countText = (key) => n[key] != null ? n[key].toLocaleString('en-US') : '—';
  $('colInfo').innerHTML = [
    ['采集间隔', c.interval ? `${c.interval}s` : '—'],
    ['单轮耗时', c.cycle_sec != null ? `${c.cycle_sec}s` : '—'],
    ['K线', countText('ohlcv')],
    ['状态', countText('regime_history')],
    ['衍生品', countText('deriv')],
    ['DVOL', countText('dvol')],
    ['VWAP量流', countText('vol1h')],
    ['美国波指', countText('usvol')],
    ['个股IV日线', countText('stock_vol')],
    ['个股IV盘中', countText('stock_vol_live')],
    ['近端IV', countText('opt_iv_near')],
    ['IV期限曲线', countText('stock_iv_term')],
    ['本轮错误', (c.errors || []).length],
  ].map(([k, v]) => `<span>${k}</span><b>${v}</b>`).join('');
  const allCounts = Object.entries(n).sort(([a], [b]) => a.localeCompare(b));
  $('colCountsSummary').textContent = `全部业务表（${allCounts.length}）`;
  $('colCountsAll').innerHTML = allCounts.map(([table, count]) =>
    `<span>${esc(table)}</span><b>${Number(count).toLocaleString('en-US')}</b>`
  ).join('');
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
  renderHeartbeat();
}

// 分管线采集心跳：整体"数据新鲜"只证明循环活着，某条管线静默断流（OpenD 登出、
// 单接口持续失败）时循环照常转——这排点逐管线看 MAX(ts)。灰=盘外按预期不采。
function renderHeartbeat() {
  const el = $('hbStrip');
  if (!el) return;
  const lanes = (S.data && S.data.heartbeat) || [];
  if (!lanes.length) { el.innerHTML = ''; return; }
  el.innerHTML = lanes.map((l) => {
    const ageTxt = l.age_min == null ? '' :
      l.age_min < 90 ? `${Math.round(l.age_min)}分前` : `${(l.age_min / 60).toFixed(1)}小时前`;
    const stateTxt = { ok: '正常', warn: '迟滞', bad: '断流', idle: '盘外' }[l.state] || l.state;
    const tip = `${l.key}：${stateTxt}${ageTxt ? '·最后落库 ' + ageTxt : ''}（${l.note}）`;
    return `<span class="lane ${esc(l.state)}" title="${esc(tip)}">` +
           `<span class="dot"></span>${esc(l.key)}${l.state === 'bad' ? '⚠' : ''}</span>`;
  }).join('');
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
      body: JSON.stringify({ scope: 'symbol', symbol: S.symbol, message: text }),
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

// ATR/BBW 分位历史图在 <details> 里，收起时容器宽高为 0——展开时必须 resize
(() => {
  const D_KEY = 'vvv_detail_open';
  const det = $('detail');
  if (det) {
    if (localStorage.getItem(D_KEY) === '1') det.open = true;
    det.addEventListener('toggle', () => {
      localStorage.setItem(D_KEY, det.open ? '1' : '0');
      if (det.open) Object.values(S.charts).forEach((c) => c && c.resize());
    });
  }
  initCoupBox();
  const chips = $('hermesChips');
  if (chips) {
    chips.addEventListener('click', (e) => {
      const b = e.target.closest('.chipbtn');
      if (!b) return;
      $('hermesText').value = b.textContent;
      $('hermesSend').click();
    });
  }
})();
