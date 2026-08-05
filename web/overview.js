/* VVV 主页横截面：机会优先分层，只展示 policy 测量与风险标注。 */
'use strict';

const REFRESH_MS = 60_000;
const STATE = {
  trend_up: { label: '趋势上行', color: '#0a8a66' },
  trend_down: { label: '趋势下行', color: '#b91f31' },
  range: { label: '震荡', color: '#4a90d9' },
  squeeze: { label: '低波动挤压（蓄势）', color: '#a87c05' },
  high_vol_chop: { label: '高波动非趋势', color: '#5f35c9' },
};
const MEANING = {
  pullback_long_opportunity: '趋势支撑回踩候选',
  reduce_long_not_open_short: '结构压力位，仅关注持仓风险',
  bounce_short_opportunity: '下降趋势压力反弹候选',
  deep_long_requires_second_confirmation: '长期均线深跌位，仍需二次确认',
  prior_low_support_not_tradeable: '前低支撑，政策条件尚未齐',
  range_edge_opportunity: '震荡区间边沿候选',
  wait_for_confirmed_breakout: '挤压中，关注突破后的右侧确认',
  high_vol_reduce_frequency: '高波环境，policy 建议降频降仓',
  middle_zone_veto: '中间区域，默认观望',
};

const $ = (id) => document.getElementById(id);
const view = { data: null, nextRefresh: Date.now() + REFRESH_MS, loadSeq: 0 };
const pad = (n) => String(n).padStart(2, '0');

function stateMeta(state) {
  return STATE[state] || { label: state || '状态不可用', color: '#7c8595' };
}

function detailHref(symbol) {
  return `/symbol?symbol=${encodeURIComponent(symbol)}`;
}

function fmtNumber(value, digits = 2) {
  return value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(digits);
}

function nameText(item) {
  return item.display ? `${item.symbol}  ${item.display}` : item.symbol;
}

function zoneLabel(item) {
  const kinds = new Set((item.zone && item.zone.kinds) || []);
  if (item.role_flipped) return '突破后回踩前高';
  if (kinds.has('range_hi')) return '区间上沿';
  if (kinds.has('range_lo')) return '区间下沿';
  for (const ema of ['ema21', 'ema55', 'ema100', 'ema200']) {
    if (kinds.has(ema)) return ema.toUpperCase();
  }
  return item.at === 'at_support' ? '支撑区' : '压力区';
}

function makeLink(symbol, className) {
  const link = document.createElement('a');
  link.className = className;
  link.href = detailHref(symbol);
  link.setAttribute('aria-label', `查看 ${symbol} 详情`);
  return link;
}

function appendText(parent, className, text) {
  const el = document.createElement('span');
  if (className) el.className = className;
  el.textContent = text;
  parent.appendChild(el);
  return el;
}

function renderOpportunity(items) {
  const host = $('opportunity');
  host.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-normal';
    empty.textContent = '当前无符合条件的机会位——这是常态，不是故障';
    host.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const card = makeLink(item.symbol, 'card opp-card');
    const top = document.createElement('div');
    top.className = 'opp-top';
    appendText(top, 'opp-symbol', item.symbol);
    if (item.display) appendText(top, 'opp-display', item.display);
    const regime = appendText(top, 'opp-regime', stateMeta(item.regime_4h).label);
    regime.style.color = stateMeta(item.regime_4h).color;
    card.appendChild(top);

    const touches = item.zone && item.zone.touches != null ? ` · 触碰${item.zone.touches}次` : '';
    appendText(card, 'opp-location',
      `${zoneLabel(item)} · 距 ${fmtNumber(item.dist_atr)}ATR${touches}`);
    appendText(card, 'opp-crsi',
      `cRSI ${item.crsi.zone}（${fmtNumber(item.crsi.crsi, 1)}） · 1d ${stateMeta(item.regime_1d).label}`);

    const play = appendText(card, `opp-play${item.signal_ok ? '' : ' waiting'}`,
      item.play ? `建议关注 · ${item.play}` : '建议关注 · 暂无匹配剧本');
    play.title = '位置与信号共振后才满足 policy P06；仍需统一风控门槛';
    if (item.vol_note) appendText(card, 'opp-risk', `⚠ ${item.vol_note}`);
    host.appendChild(card);
  });
}

function compactLink(item) {
  const row = makeLink(item.symbol, 'compact-row');
  const name = document.createElement('span');
  name.className = 'compact-symbol';
  name.textContent = item.symbol;
  if (item.display) appendText(name, 'compact-display', item.display);
  row.appendChild(name);
  const state = appendText(row, '', stateMeta(item.regime_4h).label);
  state.style.color = stateMeta(item.regime_4h).color;
  appendText(row, 'compact-meaning', MEANING[item.meaning] || item.meaning || '位置语义不可用');
  const signal = item.crsi && item.crsi.zone ? ` · cRSI ${item.crsi.zone}` : '';
  appendText(row, 'compact-tail',
    `${item.dist_atr == null ? '' : `距 ${fmtNumber(item.dist_atr)}ATR`}${signal}` || '—');
  return row;
}

function renderCompact(id, items, emptyText) {
  const host = $(id);
  host.replaceChildren();
  if (!items.length) {
    const row = document.createElement('div');
    row.className = 'compact-row muted';
    row.textContent = emptyText;
    host.appendChild(row);
    return;
  }
  items.forEach((item) => host.appendChild(compactLink(item)));
}

function renderRisk(items) {
  const host = $('risk');
  host.replaceChildren();
  if (!items.length) {
    const row = document.createElement('div');
    row.className = 'compact-row muted';
    row.textContent = '当前无可计算的额外波动率或事件风险标注';
    host.appendChild(row);
    return;
  }
  items.forEach((item) => {
    const row = makeLink(item.symbol, 'compact-row');
    appendText(row, 'compact-symbol', nameText(item));
    appendText(row, 'risk-text', item.notes.join(' · '));
    host.appendChild(row);
  });
}

function renderUnavailable(items) {
  const host = $('unavailable');
  host.replaceChildren();
  if (!items.length) {
    const row = document.createElement('div');
    row.className = 'compact-row muted';
    row.textContent = '全部品种均有可用测量结果';
    host.appendChild(row);
    return;
  }
  items.forEach((item) => {
    const row = makeLink(item.symbol, 'compact-row unavailable-row');
    appendText(row, 'compact-symbol', item.symbol || '未知品种');
    appendText(row, 'compact-meaning', item.reason || 'measurement_unavailable');
    host.appendChild(row);
  });
}

function renderHeartbeat(lanes) {
  const host = $('hbStrip');
  host.replaceChildren();
  (lanes || []).forEach((lane) => {
    const el = document.createElement('span');
    el.className = `lane ${lane.state || ''}`;
    const age = lane.age_min == null ? '' :
      lane.age_min < 90 ? `${Math.round(lane.age_min)}分前` : `${(lane.age_min / 60).toFixed(1)}小时前`;
    const state = { ok: '正常', warn: '迟滞', bad: '断流', idle: '盘外' }[lane.state] || lane.state;
    el.title = `${lane.key}：${state}${age ? ` · 最后落库 ${age}` : ''}（${lane.note || ''}）`;
    const dot = document.createElement('span');
    dot.className = 'dot';
    el.appendChild(dot);
    el.append(document.createTextNode(`${lane.key || ''}${lane.state === 'bad' ? '⚠' : ''}`));
    host.appendChild(el);
  });
}

function render(data) {
  view.data = data;
  const counts = data.counts || {};
  $('oppCount').textContent = counts.opportunity || 0;
  $('nearCount').textContent = counts.near || 0;
  $('riskCount').textContent = counts.risk || 0;
  $('middleCount').textContent = counts.middle || 0;
  $('unavailableCount').textContent = counts.unavailable || 0;
  renderOpportunity(data.opportunity || []);
  renderCompact('near', data.near || [], '当前无接近但尚未满足门槛的关键位');
  renderRisk(data.risk || []);
  renderCompact('middle', data.middle || [], '当前无中间区域品种');
  renderUnavailable(data.unavailable || []);
  renderHeartbeat(data.heartbeat || []);
  const updated = data.updated_at ? new Date(data.updated_at) : null;
  $('updated').textContent = updated && !Number.isNaN(updated.getTime())
    ? `更新 ${pad(updated.getUTCHours())}:${pad(updated.getUTCMinutes())}:${pad(updated.getUTCSeconds())} UTC`
    : '更新时间不可用';
  $('updated').className = 'badge ok';
}

async function load() {
  const seq = ++view.loadSeq;
  try {
    const response = await fetch('/api/overview');
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    if (seq !== view.loadSeq) return;
    render(data);
    $('loadError').hidden = true;
  } catch (error) {
    if (seq !== view.loadSeq) return;
    $('loadError').textContent = `总览加载失败：${error.message || error}`;
    $('loadError').hidden = false;
    $('updated').textContent = '刷新失败';
    $('updated').className = 'badge bad';
  } finally {
    if (seq === view.loadSeq) view.nextRefresh = Date.now() + REFRESH_MS;
  }
}

setInterval(() => {
  const now = new Date();
  $('clock').textContent = `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())} UTC`;
  const left = Math.max(0, Math.ceil((view.nextRefresh - Date.now()) / 1000));
  $('countdown').textContent = `刷新 ${left}s`;
  if (left === 0) load();
}, 1000);

load();
