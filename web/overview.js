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
const view = {
  data: null, nextRefresh: Date.now() + REFRESH_MS, loadSeq: 0,
  hermes: { busy: false, lastId: 0, syncSeq: 0 },
};
const pad = (n) => String(n).padStart(2, '0');

function utcDateTime(date) {
  return `${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} `
    + `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())} UTC`;
}

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

function resonanceLabel(item) {
  const resonance = item.resonance || {};
  if (resonance.main_ok === true) {
    const score = Number(resonance.score);
    const scoreText = Number.isFinite(score) ? `${score >= 0 ? '+' : ''}${score}` : '—';
    return `4h 主票共振 · ${resonance.grade || '未分级'}（辅助 ${scoreText}）`;
  }
  if (resonance.main_ok === false) return '4h 主票未共振';
  return '4h 主票不可判定';
}

function auxCrsiLabel(item, tf) {
  const value = ((item.crsi_by_tf || {})[tf] || {}).zone;
  const vote = ((item.resonance || {}).aux || {})[tf];
  return `${tf} ${value || '缺失'}（${vote == null ? '不计票' : vote >= 0 ? `+${vote}` : vote}）`;
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

function renderCandidates(hostId, items, emptyText) {
  const host = $(hostId);
  host.replaceChildren();
  if (!items.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-normal';
    empty.textContent = emptyText;
    host.appendChild(empty);
    return;
  }
  items.forEach((item) => {
    const card = makeLink(item.symbol, 'card opp-card');
    const top = document.createElement('div');
    top.className = 'opp-top';
    appendText(top, 'opp-symbol', item.symbol);
    if (item.display) appendText(top, 'opp-display', item.display);
    if (item.warmup === true) appendText(top, 'warmup-badge', '预热');
    if ((item.resonance || {}).grade) {
      appendText(top, 'warmup-badge', `共振${item.resonance.grade}`);
    }
    const regime = appendText(top, 'opp-regime', stateMeta(item.regime_4h).label);
    regime.style.color = stateMeta(item.regime_4h).color;
    card.appendChild(top);

    const touches = item.zone && item.zone.touches != null ? ` · 触碰${item.zone.touches}次` : '';
    appendText(card, 'opp-location',
      `${zoneLabel(item)} · 距 ${fmtNumber(item.dist_atr)}ATR${touches}`);
    appendText(card, 'opp-crsi',
      `${resonanceLabel(item)} · 4h cRSI ${item.crsi.zone || '不可用'}`
      + `（${fmtNumber(item.crsi.crsi, 1)}） · ${auxCrsiLabel(item, '1d')}`
      + ` · ${auxCrsiLabel(item, '1h')} · 1d regime ${stateMeta(item.regime_1d).label}`);

    const play = appendText(card, `opp-play${item.signal_ok ? '' : ' waiting'}`,
      item.play ? `建议关注 · ${item.play}` : '建议关注 · 暂无匹配剧本');
    play.title = '位置与 4h 主票共振后才满足 policy P06；1d/1h 只作强弱分级';
    if (item.stop_check) {
      appendText(card, 'opp-stop',
        `止损宽度 ${item.stop_check.verdict || '不可判定'} · 比值 ${fmtNumber(item.stop_check.ratio)}`
        + `${item.stop_check.note ? ` · ${item.stop_check.note}` : ''}`);
    }
    if (item.vol_note) appendText(card, 'opp-risk', `⚠ ${item.vol_note}`);
    if ((item.resonance || {}).grade === '弱') {
      const conflicts = (item.resonance.conflicts || []).join('；');
      appendText(card, 'opp-risk', `⚠ 共振弱：${conflicts || '辅助票净分为负'}`);
    }
    if (item.regime_conflict && item.regime_conflict.note) {
      appendText(card, 'opp-risk', `⚠ ${item.regime_conflict.note}`);
    }
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
  const signal = item.crsi && item.crsi.zone ? ` · 4h cRSI ${item.crsi.zone}` : '';
  const conflict = item.regime_conflict && item.regime_conflict.note
    ? ` · ⚠ ${item.regime_conflict.note}` : '';
  appendText(row, 'compact-tail',
    `${item.dist_atr == null ? '' : `距 ${fmtNumber(item.dist_atr)}ATR`}${signal}${conflict}` || '—');
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
  $('armedCount').textContent = counts.armed ?? '—';
  $('waitSignalCount').textContent = counts.wait_signal ?? '—';
  $('nearCount').textContent = counts.near ?? '—';
  $('riskCount').textContent = counts.risk ?? '—';
  $('middleCount').textContent = counts.middle ?? '—';
  $('unavailableCount').textContent = counts.unavailable ?? '—';
  renderCandidates(
    'armed', data.armed || [],
    '当前没有“位置 + 4h 主票”同时成立的候选——这是常态，不是故障',
  );
  renderCandidates(
    'waitSignal', data.wait_signal || [],
    '当前没有等待信号的位置候选',
  );
  renderCompact('near', data.near || [], '当前无接近但尚未满足门槛的关键位');
  renderRisk(data.risk || []);
  renderCompact('middle', data.middle || [], '当前无中间区域品种');
  renderUnavailable(data.unavailable || []);
  renderHeartbeat(data.heartbeat || []);
  const updated = data.updated_at ? new Date(data.updated_at) : null;
  $('updated').textContent = updated && !Number.isNaN(updated.getTime())
    ? `计算 ${utcDateTime(updated)}`
    : '计算时间不可用';
  $('updated').className = 'badge ok';
  const asof = data.asof ? new Date(data.asof) : null;
  $('asof').textContent = asof && !Number.isNaN(asof.getTime())
    ? `数据截至 ${utcDateTime(asof)}`
    : '数据截至不可用';
  $('asof').className = 'badge';
}

async function load() {
  const seq = ++view.loadSeq;
  try {
    const response = await fetch('/api/overview');
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    if (seq !== view.loadSeq) return;
    render(data);
    hermesSync();
    $('loadError').hidden = true;
  } catch (error) {
    if (seq !== view.loadSeq) return;
    $('loadError').textContent = `总览加载失败：${error.message || error}`;
    $('loadError').hidden = false;
    $('updated').textContent = '刷新失败';
    $('updated').className = 'badge bad';
    $('asof').textContent = '数据截至不可用';
  } finally {
    if (seq === view.loadSeq) view.nextRefresh = Date.now() + REFRESH_MS;
  }
}

/* ---------- Hermes：总览 scope，共享服务端对话流 ---------- */
const OPEN_KEY = 'vvvhermes_open';
const HERMES_INTRO = '你好，我是 VVVhermes。我能读到总览当前的机会、接近、风险与观望横截面。历史与详情页、终端共享。';

function hermesAdd(cls, text) {
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.textContent = text;
  $('hermesMsgs').appendChild(el);
  $('hermesMsgs').scrollTop = $('hermesMsgs').scrollHeight;
  return el;
}

// 聊天写入交互第 2/3 份副本：本轮保持三页隔离，避免把独立重构风险混入写入闭环。
function hermesRenderDraft(draft, assistantEl) {
  if (draft === undefined) return;
  const panel = document.createElement('div');
  if (!draft || draft.ok !== true) {
    panel.className = 'sub';
    panel.textContent = `草稿格式不合规：${draft && draft.error != null ? draft.error : '未知错误'}`;
    assistantEl.appendChild(panel);
    return;
  }

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'hbtn primary';
  button.textContent = `存入经验库 · ${draft.title}`;
  const slug = document.createElement('span');
  slug.className = 'sub';
  slug.textContent = `slug：${draft.slug}`;
  const status = document.createElement('span');
  status.className = 'sub';
  panel.append(button, document.createTextNode(' '), slug, document.createTextNode(' '), status);
  assistantEl.appendChild(panel);

  button.onclick = async () => {
    button.disabled = true;
    try {
      const response = await fetch('/api/memory/save', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ raw: draft.raw }),
      });
      const data = await response.json();
      if (!response.ok || data.ok !== true) {
        status.textContent = data.error != null ? String(data.error) : `HTTP ${response.status}`;
        button.disabled = false;
        return;
      }
      button.textContent = '已存入';
      status.textContent = '';
      const link = document.createElement('a');
      link.className = 'sub';
      link.href = '/memory';
      link.textContent = '查看经验库';
      panel.append(document.createTextNode(' '), link);
    } catch (error) {
      status.textContent = `请求失败：${error.message || error}`;
      button.disabled = false;
    }
  };
}

function hermesRenderAll(messages) {
  $('hermesMsgs').replaceChildren();
  hermesAdd('bot', HERMES_INTRO);
  messages.forEach((message) =>
    hermesAdd(message.role === 'user' ? 'user' : 'bot', message.content));
}

async function hermesSync(force) {
  if (view.hermes.busy) return;
  const seq = ++view.hermes.syncSeq;
  try {
    const response = await fetch('/api/agent/history?limit=60');
    if (!response.ok) return;
    const data = await response.json();
    if (!Array.isArray(data.messages) || seq !== view.hermes.syncSeq || view.hermes.busy) return;
    const lastId = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
    if (force || lastId !== view.hermes.lastId) {
      view.hermes.lastId = lastId;
      hermesRenderAll(data.messages);
    }
  } catch (error) { /* 服务不可达时保留当前消息 */ }
}

function hermesRestore() {
  if (localStorage.getItem(OPEN_KEY) === '0') $('hermes').classList.add('hidden');
  hermesSync(true);
}

async function hermesInfo() {
  try {
    const response = await fetch('/api/agent/info');
    const data = await response.json();
    const custom = data.custom_system ? ' · 提示词:hermes_system.md' : '';
    $('hermesMeta').textContent = data.config_error
      ? `⚠ agent.json 解析失败：${data.config_error}（已退回 mock）`
      : (data.provider === 'mock' ? 'mock · 未配置模型' : `${data.provider} · ${data.model}`) + custom;
  } catch (error) {
    $('hermesMeta').textContent = '状态未知';
  }
}

async function hermesClear() {
  if (view.hermes.busy) {
    hermesAdd('err', '回答生成中，暂不能清空（否则在途回答会重新入库）。');
    return;
  }
  try {
    const response = await fetch('/api/agent/clear', { method: 'POST' });
    const data = await response.json();
    if (!response.ok || data.ok !== true) throw new Error(data.error || `HTTP ${response.status}`);
    view.hermes.lastId = 0;
    hermesRenderAll([]);
    hermesAdd('bot', '已开始新会话（总览、详情页与终端的共享历史已清空）。');
  } catch (error) {
    hermesAdd('err', `清空失败：${error.message || error}`);
  }
}

async function hermesSend() {
  const input = $('hermesText');
  const message = input.value.trim();
  if (!message || view.hermes.busy) return;
  input.value = '';
  const optimistic = hermesAdd('user', message);
  view.hermes.busy = true;
  $('hermesSend').disabled = true;
  const busy = hermesAdd('bot busy', 'VVVhermes 思考中…（codex 后端通常需要 1-3 分钟）');
  let ok = false;
  let hasDraft = false;
  try {
    const response = await fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scope: 'overview', message }),
    });
    const data = await response.json();
    busy.remove();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    const assistant = hermesAdd('bot', data.reply);
    hermesRenderDraft(data.draft, assistant);
    hasDraft = data.draft !== undefined;
    ok = true;
  } catch (error) {
    busy.remove();
    hermesAdd('err', `请求失败：${error.message || error}`);
    optimistic.classList.add('unsent');
    optimistic.title = '发送失败：此条未入共享历史';
  } finally {
    view.hermes.busy = false;
    $('hermesSend').disabled = false;
    input.focus();
    // history 不携带瞬时 draft；立即重建会把用户尚未确认的按钮或错误提示抹掉。
    if (ok && !hasDraft) hermesSync(true);
  }
}

function hermesToggle(show) {
  const panel = $('hermes');
  const open = show != null ? show : panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !open);
  try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch (error) { /* 忽略 */ }
}

$('hermesSend').onclick = hermesSend;
$('hermesClear').onclick = hermesClear;
$('hermesClose').onclick = () => hermesToggle(false);
$('hermesToggle').onclick = () => hermesToggle();
$('hermesText').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    hermesSend();
  }
});
$('hermesChips').addEventListener('click', (event) => {
  const button = event.target.closest('.chipbtn');
  if (!button) return;
  $('hermesText').value = button.textContent;
  hermesSend();
});

setInterval(() => {
  const now = new Date();
  $('clock').textContent = `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())} UTC`;
  const left = Math.max(0, Math.ceil((view.nextRefresh - Date.now()) / 1000));
  $('countdown').textContent = `刷新 ${left}s`;
  if (left === 0) load();
}, 1000);

hermesRestore();
Promise.all([load(), hermesInfo()]);
