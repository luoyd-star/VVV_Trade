/* 经验路径页面。
   Hermes 前端逻辑目前是第三份副本；为避免扩大本轮回归面，抽取共享模块留在 backlog。 */
'use strict';

const $ = (id) => document.getElementById(id);
const STATUS_LABELS = {
  active: '当前有效',
  archived: '已归档',
  superseded: '已被继任',
};
const memoryState = {
  entries: [], status: 'active', query: '', selectedSlug: null,
  hermes: { busy: false, lastId: 0, syncSeq: 0 },
};

function textLeaf(value) {
  const leaf = document.createElement('span');
  leaf.textContent = String(value ?? '');
  return leaf;
}

function appendInline(parent, source) {
  const text = String(source ?? '');
  let cursor = 0;
  while (cursor < text.length) {
    const codeAt = text.indexOf('`', cursor);
    const strongAt = text.indexOf('**', cursor);
    let nextAt = -1;
    let kind = '';
    if (codeAt >= 0 && (strongAt < 0 || codeAt < strongAt)) {
      nextAt = codeAt;
      kind = 'code';
    } else if (strongAt >= 0) {
      nextAt = strongAt;
      kind = 'strong';
    }
    if (nextAt < 0) {
      parent.appendChild(textLeaf(text.slice(cursor)));
      break;
    }
    if (nextAt > cursor) parent.appendChild(textLeaf(text.slice(cursor, nextAt)));
    const markerSize = kind === 'strong' ? 2 : 1;
    const marker = kind === 'strong' ? '**' : '`';
    const closeAt = text.indexOf(marker, nextAt + markerSize);
    if (closeAt < 0) {
      parent.appendChild(textLeaf(text.slice(nextAt)));
      break;
    }
    const el = document.createElement(kind === 'strong' ? 'strong' : 'code');
    const content = text.slice(nextAt + markerSize, closeAt);
    if (kind === 'strong') appendInline(el, content);
    else el.textContent = content;
    parent.appendChild(el);
    cursor = closeAt + markerSize;
  }
}

function leadingSpaces(line) {
  const match = String(line).match(/^ */);
  return match ? match[0].length : 0;
}

function listMatch(line) {
  const match = String(line).match(/^( *)([-+*]|\d+[.)]) +(.*)$/);
  if (!match) return null;
  return {
    indent: match[1].length,
    marker: match[2],
    ordered: /^\d/.test(match[2]),
    start: /^\d/.test(match[2]) ? Number.parseInt(match[2], 10) : null,
    content: match[3],
    contentColumn: match[1].length + match[2].length + 1,
  };
}

function isBlockStart(line) {
  return /^#{1,2} +/.test(line) || /^> ?/.test(line) || /^ *```/.test(line)
    || Boolean(listMatch(line));
}

function renderMarkdownList(lines, start, parent) {
  const first = listMatch(lines[start]);
  const list = document.createElement(first.ordered ? 'ol' : 'ul');
  if (first.ordered && first.start !== 1) list.start = first.start;
  const listIndent = first.indent;
  let index = start;

  while (index < lines.length) {
    const item = listMatch(lines[index]);
    if (!item || item.indent !== listIndent || item.ordered !== first.ordered) break;
    const itemLines = [item.content];
    index += 1;
    while (index < lines.length) {
      const line = lines[index];
      if (line.trim() === '') {
        itemLines.push('');
        index += 1;
        continue;
      }
      const nextItem = listMatch(line);
      const indent = leadingSpaces(line);
      if (nextItem && nextItem.indent === listIndent) break;
      if (indent <= listIndent) break;
      itemLines.push(line.slice(Math.min(item.contentColumn, indent)));
      index += 1;
    }
    while (itemLines.length && itemLines[itemLines.length - 1] === '') itemLines.pop();
    const li = document.createElement('li');
    renderBlocks(itemLines, li);
    list.appendChild(li);
  }
  parent.appendChild(list);
  return index;
}

function renderBlocks(lines, parent) {
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.trim() === '') {
      index += 1;
      continue;
    }

    const fence = line.match(/^ *```(.*)$/);
    if (fence) {
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^ *``` *$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      const pre = document.createElement('pre');
      const code = document.createElement('code');
      code.textContent = codeLines.join('\n');
      const language = fence[1].trim();
      if (language) code.setAttribute('data-language', language);
      pre.appendChild(code);
      parent.appendChild(pre);
      continue;
    }

    const heading = line.match(/^(#{1,2}) +(.*)$/);
    if (heading) {
      const el = document.createElement(heading[1].length === 1 ? 'h1' : 'h2');
      appendInline(el, heading[2]);
      parent.appendChild(el);
      index += 1;
      continue;
    }

    if (/^> ?/.test(line)) {
      const quote = document.createElement('blockquote');
      let firstLine = true;
      while (index < lines.length && /^> ?/.test(lines[index])) {
        if (!firstLine) quote.appendChild(document.createElement('br'));
        appendInline(quote, lines[index].replace(/^> ?/, ''));
        firstLine = false;
        index += 1;
      }
      parent.appendChild(quote);
      continue;
    }

    if (listMatch(line)) {
      index = renderMarkdownList(lines, index, parent);
      continue;
    }

    const paragraphLines = [line.trim()];
    index += 1;
    while (index < lines.length && lines[index].trim() !== '' && !isBlockStart(lines[index])) {
      paragraphLines.push(lines[index].trim());
      index += 1;
    }
    const paragraph = document.createElement('p');
    appendInline(paragraph, paragraphLines.join(' '));
    parent.appendChild(paragraph);
  }
}

function renderMarkdown(markdown, host) {
  const target = host || document.createElement('div');
  target.replaceChildren();
  const normalized = String(markdown ?? '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  renderBlocks(normalized.split('\n'), target);
  return target;
}

function addMetaRow(list, label, value) {
  const term = document.createElement('dt');
  term.textContent = label;
  const detail = document.createElement('dd');
  detail.textContent = value || '不限';
  list.append(term, detail);
}

function makeStatusBadge(status) {
  const badge = document.createElement('span');
  badge.className = `badge status-badge ${status === 'active' ? 'ok' : (status === 'archived' ? '' : 'warn')}`;
  badge.textContent = STATUS_LABELS[status] || status;
  return badge;
}

function makeAxes(entry) {
  const pair = document.createElement('div');
  pair.className = 'axis-pair';

  const clarity = document.createElement('div');
  clarity.className = 'axis';
  const clarityLabel = document.createElement('span');
  clarityLabel.textContent = '事后路径清晰度';
  const clarityValue = document.createElement('b');
  clarityValue.textContent = entry.retrospective_path_clarity || 'UNKNOWN';
  clarity.append(clarityLabel, clarityValue);

  const edge = document.createElement('div');
  const edgeValueText = entry.prospective_trade_edge_evidence || 'NONE';
  edge.className = `axis${edgeValueText === 'NONE' ? ' edge-none' : ''}`;
  const edgeLabel = document.createElement('span');
  edgeLabel.textContent = '前瞻交易边证据';
  const edgeValue = document.createElement('b');
  edgeValue.textContent = edgeValueText;
  edge.append(edgeLabel, edgeValue);
  if (edgeValueText === 'NONE') {
    const warning = document.createElement('span');
    warning.textContent = '没有前瞻交易边证据';
    edge.appendChild(warning);
  }
  pair.append(clarity, edge);
  return pair;
}

function successorLink(slug) {
  const link = document.createElement('a');
  link.className = 'successor';
  link.textContent = `查看继任条目：${slug}`;
  link.href = `/memory?slug=${encodeURIComponent(slug)}`;
  return link;
}

function renderCard(entry) {
  const card = document.createElement('article');
  card.className = `card memory-card ${entry.status || ''}`;

  const top = document.createElement('div');
  top.className = 'memory-card-top';
  const title = document.createElement('button');
  title.type = 'button';
  title.className = 'memory-title';
  title.textContent = entry.title || entry.slug;
  title.addEventListener('click', () => openDetail(entry.slug));
  top.append(title, makeStatusBadge(entry.status));

  const pattern = document.createElement('div');
  pattern.className = 'memory-pattern';
  pattern.textContent = entry.pattern || '—';

  const note = document.createElement('div');
  note.className = 'memory-rule-note';
  note.textContent = '观察记录，不是系统规则或交易信号';

  const meta = document.createElement('dl');
  meta.className = 'memory-meta';
  addMetaRow(meta, '事件日期', `${entry.event_from || '—'} 至 ${entry.event_to || '—'}`);
  addMetaRow(meta, '品种', (entry.symbols || []).join('、'));
  addMetaRow(meta, '触发 regime', (entry.trigger_regimes || []).join(' / '));
  addMetaRow(meta, '触发类别', (entry.trigger_classes || []).join(' / '));
  addMetaRow(meta, '证据状态', entry.evidence_status);

  card.append(top, pattern, note, meta, makeAxes(entry));
  if (entry.status === 'superseded' && entry.superseded_by) {
    card.appendChild(successorLink(entry.superseded_by));
  }
  return card;
}

function normalizedSearch(entry) {
  return [entry.title, entry.pattern, ...(entry.symbols || []), ...(entry.trigger_regimes || [])]
    .filter(Boolean).join(' ').toLocaleLowerCase('zh-CN');
}

function renderList() {
  const host = $('memoryList');
  host.replaceChildren();
  const query = memoryState.query.trim().toLocaleLowerCase('zh-CN');
  const visible = memoryState.entries.filter((entry) =>
    entry.status === memoryState.status && (!query || normalizedSearch(entry).includes(query)));
  visible.forEach((entry) => host.appendChild(renderCard(entry)));
  if (!visible.length) {
    const empty = document.createElement('div');
    empty.className = 'memory-empty';
    empty.textContent = query ? '当前状态下没有匹配的经验路径' : '当前状态下还没有经验路径';
    host.appendChild(empty);
  }
  $('memoryCount').textContent = `${visible.length} 条`;
}

function renderErrors(errors) {
  const host = $('memoryError');
  if (!Array.isArray(errors) || !errors.length) {
    host.hidden = true;
    host.textContent = '';
    return;
  }
  host.textContent = `记忆加载有 ${errors.length} 个错误：${errors.map((item) => item.error || item).join('；')}`;
  host.hidden = false;
}

function listEntries(payload) {
  if (Array.isArray(payload)) return payload;
  if (Array.isArray(payload.entries)) return payload.entries;
  return [];
}

async function loadMemories() {
  try {
    const response = await fetch('/api/memory');
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    memoryState.entries = listEntries(data);
    renderErrors(data.errors);
    renderList();
    const loadedAt = Number(data.loaded_at);
    $('memoryUpdated').textContent = Number.isFinite(loadedAt)
      ? `索引 ${new Date(loadedAt).toLocaleString('zh-CN')}`
      : `${memoryState.entries.length} 条记录`;
    $('memoryUpdated').className = 'badge ok';
    const deepLinked = new URLSearchParams(window.location.search).get('slug');
    if (deepLinked) await openDetail(deepLinked, false);
  } catch (error) {
    $('memoryError').textContent = `经验路径加载失败：${error.message || error}`;
    $('memoryError').hidden = false;
    $('memoryUpdated').textContent = '加载失败';
    $('memoryUpdated').className = 'badge bad';
  }
}

function detailMeta(entry) {
  const wrapper = document.createElement('div');
  const meta = document.createElement('dl');
  meta.className = 'memory-meta';
  addMetaRow(meta, '事件日期', `${entry.event_from || '—'} 至 ${entry.event_to || '—'}`);
  addMetaRow(meta, '品种', (entry.symbols || []).join('、'));
  addMetaRow(meta, '触发条件', [
    ...(entry.trigger_regimes || []), ...(entry.trigger_classes || []),
  ].join(' / '));
  addMetaRow(meta, '证据状态', entry.evidence_status);
  addMetaRow(meta, '条目状态', STATUS_LABELS[entry.status] || entry.status);
  if (entry.status === 'archived') addMetaRow(meta, '归档原因', entry.archive_reason);
  wrapper.append(meta, makeAxes(entry));
  if (entry.status === 'superseded' && entry.superseded_by) {
    wrapper.appendChild(successorLink(entry.superseded_by));
  }
  return wrapper;
}

async function openDetail(slug, pushHistory = true) {
  if (!/^[a-z0-9-]+$/.test(String(slug))) {
    renderErrors([{ error: '详情 slug 格式非法' }]);
    return;
  }
  try {
    const response = await fetch(`/api/memory/entry?slug=${encodeURIComponent(slug)}`);
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    const entry = data.entry || data;
    $('memoryDetailTitle').textContent = entry.title || entry.slug;
    $('memoryDetailPattern').textContent = entry.pattern || '—';
    $('memoryDetailMeta').replaceChildren(detailMeta(entry));
    renderMarkdown(entry.body || '', $('memoryBody'));
    $('memoryBrowser').hidden = true;
    $('memoryDetail').hidden = false;
    memoryState.selectedSlug = entry.slug;
    $('hermesNote').textContent = `当前回顾：${entry.title || entry.slug}；提问会提交 memory_slug=${entry.slug}`;
    if (pushHistory) window.history.pushState({ slug: entry.slug }, '', `/memory?slug=${encodeURIComponent(entry.slug)}`);
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (error) {
    renderErrors([{ error: `详情加载失败：${error.message || error}` }]);
  }
}

function closeDetail(pushHistory = true) {
  $('memoryDetail').hidden = true;
  $('memoryBrowser').hidden = false;
  memoryState.selectedSlug = null;
  $('hermesNote').textContent = '与总览、详情页和终端共享同一条对话；打开条目后会附带该条目的 slug';
  if (pushHistory) window.history.pushState({}, '', '/memory');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function bindMemoryControls() {
  $('memorySearch').addEventListener('input', (event) => {
    memoryState.query = event.target.value;
    renderList();
  });
  $('statusTabs').addEventListener('click', (event) => {
    const button = event.target.closest('button[data-status]');
    if (!button) return;
    memoryState.status = button.dataset.status;
    $('statusTabs').querySelectorAll('button').forEach((item) => {
      const active = item === button;
      item.classList.toggle('active', active);
      item.setAttribute('aria-pressed', active ? 'true' : 'false');
    });
    renderList();
  });
  $('memoryBack').addEventListener('click', () => closeDetail());
  window.addEventListener('popstate', () => {
    const slug = new URLSearchParams(window.location.search).get('slug');
    if (slug) openDetail(slug, false);
    else closeDetail(false);
  });
}

/* ---------- Hermes：共享服务端对话流 ---------- */
const OPEN_KEY = 'vvvhermes_open';
const HERMES_INTRO = '你好，我是 VVVhermes。打开某条经验后提问，我会把它作为不可信历史档案进行回顾。历史与总览、详情页和终端共享。';

function hermesAdd(cls, text) {
  const el = document.createElement('div');
  el.className = `msg ${cls}`;
  el.textContent = text;
  $('hermesMsgs').appendChild(el);
  $('hermesMsgs').scrollTop = $('hermesMsgs').scrollHeight;
  return el;
}

function hermesRenderAll(messages) {
  $('hermesMsgs').replaceChildren();
  hermesAdd('bot', HERMES_INTRO);
  messages.forEach((message) =>
    hermesAdd(message.role === 'user' ? 'user' : 'bot', message.content));
}

async function hermesSync(force) {
  if (memoryState.hermes.busy) return;
  const seq = ++memoryState.hermes.syncSeq;
  try {
    const response = await fetch('/api/agent/history?limit=60');
    if (!response.ok) return;
    const data = await response.json();
    if (!Array.isArray(data.messages) || seq !== memoryState.hermes.syncSeq
        || memoryState.hermes.busy) return;
    const lastId = data.messages.length ? data.messages[data.messages.length - 1].id : 0;
    if (force || lastId !== memoryState.hermes.lastId) {
      memoryState.hermes.lastId = lastId;
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
  if (memoryState.hermes.busy) {
    hermesAdd('err', '回答生成中，暂不能清空（否则在途回答会重新入库）。');
    return;
  }
  try {
    const response = await fetch('/api/agent/clear', { method: 'POST' });
    const data = await response.json();
    if (!response.ok || data.ok !== true) throw new Error(data.error || `HTTP ${response.status}`);
    memoryState.hermes.lastId = 0;
    hermesRenderAll([]);
    hermesAdd('bot', '已开始新会话（经验路径、总览、详情页与终端的共享历史已清空）。');
  } catch (error) {
    hermesAdd('err', `清空失败：${error.message || error}`);
  }
}

async function hermesSend() {
  const input = $('hermesText');
  const message = input.value.trim();
  if (!message || memoryState.hermes.busy) return;
  input.value = '';
  const optimistic = hermesAdd('user', message);
  memoryState.hermes.busy = true;
  $('hermesSend').disabled = true;
  const busy = hermesAdd('bot busy', 'VVVhermes 思考中…（codex 后端通常需要 1-3 分钟）');
  let ok = false;
  try {
    const body = { scope: 'overview', message };
    if (memoryState.selectedSlug) body.memory_slug = memoryState.selectedSlug;
    const response = await fetch('/api/agent/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await response.json();
    busy.remove();
    if (!response.ok || data.error) throw new Error(data.error || `HTTP ${response.status}`);
    hermesAdd('bot', data.reply);
    ok = true;
  } catch (error) {
    busy.remove();
    hermesAdd('err', `请求失败：${error.message || error}`);
    optimistic.classList.add('unsent');
    optimistic.title = '发送失败：此条未入共享历史';
  } finally {
    memoryState.hermes.busy = false;
    $('hermesSend').disabled = false;
    input.focus();
    if (ok) hermesSync(true);
  }
}

function hermesToggle(show) {
  const panel = $('hermes');
  const open = show != null ? show : panel.classList.contains('hidden');
  panel.classList.toggle('hidden', !open);
  try { localStorage.setItem(OPEN_KEY, open ? '1' : '0'); } catch (error) { /* 忽略 */ }
}

function bindHermes() {
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
}

window.VVVMemory = Object.freeze({ renderMarkdown });

if ($('memoryList')) {
  bindMemoryControls();
  bindHermes();
  hermesRestore();
  Promise.all([loadMemories(), hermesInfo()]);
}
