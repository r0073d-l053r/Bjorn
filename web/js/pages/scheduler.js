/**
 * Scheduler page module.
 * Kanban-style board with 6 lanes, live refresh, countdown timers, search, history modal.
 * Endpoints: GET /action_queue, POST /queue_cmd, GET /attempt_history
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'scheduler';
const PAGE_SIZE = 100;
const LANES = ['running', 'pending', 'upcoming', 'success', 'failed', 'cancelled'];
const LANE_LABELS = {
  running: () => t('sched.running'),
  pending: () => t('sched.pending'),
  upcoming: () => t('sched.upcoming'),
  success: () => t('sched.success'),
  failed: () => t('sched.failed'),
  cancelled: () => t('sched.cancelled')
};

/* ── state ── */
let tracker = null;
let poller = null;
let clockTimer = null;
let LIVE = true;
let FOCUS = false;
let COMPACT = false;
let COLLAPSED = false;
let INCLUDE_SUPERSEDED = false;
let lastBuckets = null;
let showCount = null;
let lastFilterKey = '';
let iconCache = new Map();
/** Map<lane, Map<cardKey, DOM element>> for incremental updates */
let laneCardMaps = new Map();

/* ── lifecycle ── */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  container.appendChild(buildShell());
  tracker.trackEventListener(window, 'keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
  await tick();
  setLive(true);
}

export function unmount() {
  clearTimeout(searchDeb);
  searchDeb = null;
  if (poller) { poller.stop(); poller = null; }
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  lastBuckets = null;
  showCount = null;
  lastFilterKey = '';
  iconCache.clear();
  laneCardMaps.clear();
  LIVE = true; FOCUS = false; COMPACT = false;
  COLLAPSED = false; INCLUDE_SUPERSEDED = false;
}

/* ── shell ── */
function buildShell() {
  return el('div', { class: 'scheduler-container' }, [
    el('div', { id: 'sched-errorBar', class: 'notice', style: 'display:none' }),
    el('div', { class: 'controls' }, [
      el('input', {
        type: 'text', id: 'sched-search', placeholder: t('sched.filterPlaceholder'),
        oninput: onSearch
      }),
      pill('sched-liveBtn', t('common.on'), true, () => setLive(!LIVE)),
      pill('sched-refBtn', t('common.refresh'), false, () => tick()),
      pill('sched-focBtn', t('sched.focusActive'), false, () => { FOCUS = !FOCUS; $('#sched-focBtn')?.classList.toggle('active', FOCUS); lastFilterKey = ''; tick(); }),
      pill('sched-cmpBtn', t('sched.compact'), false, () => { COMPACT = !COMPACT; $('#sched-cmpBtn')?.classList.toggle('active', COMPACT); lastFilterKey = ''; tick(); }),
      pill('sched-colBtn', t('sched.collapse'), false, toggleCollapse),
      pill('sched-supBtn', INCLUDE_SUPERSEDED ? t('sched.hideSuperseded') : t('sched.showSuperseded'), false, toggleSuperseded),
      el('span', { id: 'sched-stats', class: 'stats' }),
    ]),
    el('div', { id: 'sched-boardWrap', class: 'boardWrap' }, [
      el('div', { id: 'sched-board', class: 'board' }),
    ]),
    /* history modal */
    el('div', {
      id: 'sched-histModal', class: 'modalOverlay', style: 'display:none', 'aria-hidden': 'true',
      onclick: (e) => { if (e.target.id === 'sched-histModal') closeModal(); }
    }, [
      el('div', { class: 'modal' }, [
        el('div', { class: 'modalHeader' }, [
          el('div', { class: 'title' }, [t('sched.history')]),
          el('div', { id: 'sched-histTitle', class: 'muted' }),
          el('div', { class: 'spacer' }),
          el('button', { class: 'xBtn', onclick: closeModal }, [t('common.close')]),
        ]),
        el('div', { id: 'sched-histBody', class: 'modalBody' }),
        el('div', { class: 'modalFooter' }, [
          el('small', {}, [t('sched.historyColorCoded')]),
        ]),
      ]),
    ]),
  ]);
}

function pill(id, text, active, onclick) {
  return el('span', { id, class: `pill ${active ? 'active' : ''}`, onclick }, [text]);
}

/* ── data fetch ── */
async function fetchQueue() {
  const data = await api.get('/action_queue', { timeout: 8000 });
  const rawRows = Array.isArray(data) ? data : (data?.rows || []);
  return rawRows.map(normalizeRow);
}

function normalizeRow(r) {
  const status = (r.status || '').toLowerCase() === 'expired' ? 'failed' : (r.status || '').toLowerCase();
  const scheduled_ms = isoToMs(r.scheduled_for);
  const created_ms = isoToMs(r.created_at) || Date.now();
  const started_ms = isoToMs(r.started_at);
  const completed_ms = isoToMs(r.completed_at);

  let _computed_status = status;
  if (status === 'scheduled') _computed_status = 'upcoming';
  else if (status === 'pending' && scheduled_ms > Date.now()) _computed_status = 'upcoming';

  const tags = dedupeArr(toArray(r.tags));
  const metadata = typeof r.metadata === 'string' ? parseJSON(r.metadata, {}) : (r.metadata || {});

  return {
    ...r, status, scheduled_ms, created_ms, started_ms, completed_ms,
    _computed_status, tags, metadata,
    mac: r.mac || r.mac_address || '',
    priority_effective: r.priority_effective ?? r.priority ?? 0,
  };
}

/* ── tick / render ── */
async function tick() {
  try {
    const rows = await fetchQueue();
    render(rows);
  } catch (e) {
    showError(t('sched.fetchError') + ': ' + e.message);
  }
}

function render(rows) {
  const q = ($('#sched-search')?.value || '').toLowerCase();

  /* filter */
  let filtered = rows;
  if (q) {
    filtered = filtered.filter(r => {
      const bag = `${r.action_name} ${r.mac} ${r.ip} ${r.hostname} ${r.service} ${r.port} ${(r.tags || []).join(' ')}`.toLowerCase();
      return bag.includes(q);
    });
  }
  if (FOCUS) filtered = filtered.filter(r => ['upcoming', 'pending', 'running'].includes(r._computed_status));

  /* superseded filter */
  if (!INCLUDE_SUPERSEDED) {
    const activeKeys = new Set();
    filtered.forEach(r => {
      if (['upcoming', 'pending', 'running'].includes(r._computed_status)) {
        activeKeys.add(`${r.action_name}|${r.mac}|${r.port || 0}`);
      }
    });
    filtered = filtered.filter(r => {
      if (r._computed_status !== 'failed') return true;
      const key = `${r.action_name}|${r.mac}|${r.port || 0}`;
      return !activeKeys.has(key);
    });
  }

  /* dedupe failed: keep highest retry per key */
  const failMap = new Map();
  filtered.filter(r => r._computed_status === 'failed').forEach(r => {
    const key = `${r.action_name}|${r.mac}|${r.port || 0}`;
    const prev = failMap.get(key);
    if (!prev || (r.retry_count || 0) > (prev.retry_count || 0) || r.created_ms > prev.created_ms) failMap.set(key, r);
  });
  const failIds = new Set(Array.from(failMap.values()).map(r => r.id));
  filtered = filtered.filter(r => r._computed_status !== 'failed' || failIds.has(r.id));

  /* bucket */
  const buckets = {};
  LANES.forEach(l => buckets[l] = []);
  filtered.forEach(r => {
    const lane = buckets[r._computed_status];
    if (lane) lane.push(r);
  });

  /* sort per lane */
  const byNewest = (a, b) => Math.max(b.completed_ms, b.started_ms, b.created_ms) - Math.max(a.completed_ms, a.started_ms, a.created_ms);
  const byPrio = (a, b) => (b.priority_effective - a.priority_effective) || byNewest(a, b);
  buckets.running.sort(byPrio);
  buckets.pending.sort((a, b) => byPrio(a, b) || (a.scheduled_ms || a.created_ms) - (b.scheduled_ms || b.created_ms));
  buckets.upcoming.sort((a, b) => (a.scheduled_ms || Infinity) - (b.scheduled_ms || Infinity));
  buckets.success.sort((a, b) => (b.completed_ms || b.started_ms || b.created_ms) - (a.completed_ms || a.started_ms || a.created_ms));
  buckets.failed.sort((a, b) => (b.completed_ms || b.started_ms || b.created_ms) - (a.completed_ms || a.started_ms || a.created_ms));
  buckets.cancelled.sort(byPrio);

  if (COMPACT) {
    LANES.forEach(l => {
      buckets[l] = keepLatest(buckets[l], r => `${r.action_name}|${r.mac}|${r.port || 0}`, r => Math.max(r.completed_ms, r.started_ms, r.created_ms));
    });
  }

  /* stats */
  const total = filtered.length;
  const statsEl = $('#sched-stats');
  if (statsEl) statsEl.textContent = `${total} ${t('sched.entries')} | R:${buckets.running.length} P:${buckets.pending.length} U:${buckets.upcoming.length} S:${buckets.success.length} F:${buckets.failed.length}`;

  /* pagination */
  const fk = filterKey(q);
  if (fk !== lastFilterKey) { showCount = {}; LANES.forEach(l => showCount[l] = PAGE_SIZE); lastFilterKey = fk; }
  if (!showCount) { showCount = {}; LANES.forEach(l => showCount[l] = PAGE_SIZE); }
  lastBuckets = buckets;

  renderBoard(buckets);
}

/* ── cardKey: stable identifier for a card row ── */
function cardKey(r) {
  return `${r.id || ''}|${r.action_name}|${r.mac}|${r.port || 0}|${r._computed_status}`;
}

/* ── card fingerprint for detecting data changes ── */
function cardFingerprint(r) {
  return `${r.status}|${r.retry_count || 0}|${r.priority_effective}|${r.started_at || ''}|${r.completed_at || ''}|${r.error_message || ''}|${r.result_summary || ''}|${(r.tags || []).join(',')}`;
}

/**
 * Incremental board rendering — updates DOM in-place instead of destroying/recreating.
 * This prevents flickering of countdown timers and progress bars.
 */
function renderBoard(buckets) {
  const board = $('#sched-board');
  if (!board) return;

  /* First render: build full structure */
  if (!board.children.length) {
    laneCardMaps.clear();
    LANES.forEach(lane => {
      const items = buckets[lane] || [];
      const visible = items.slice(0, showCount?.[lane] || PAGE_SIZE);
      const cardMap = new Map();
      laneCardMaps.set(lane, cardMap);

      const laneBody = el('div', { class: 'laneBody' });
      if (visible.length === 0) {
        laneBody.appendChild(el('div', { class: 'empty' }, [t('sched.noEntries')]));
      } else {
        visible.forEach(r => {
          const card = cardEl(r);
          card.dataset.cardKey = cardKey(r);
          card.dataset.fp = cardFingerprint(r);
          cardMap.set(cardKey(r), card);
          laneBody.appendChild(card);
        });
        if (items.length > visible.length) {
          laneBody.appendChild(moreBtn(lane));
        }
      }

      const laneEl = el('div', { class: `lane status-${lane}`, 'data-lane': lane }, [
        el('div', { class: 'laneHeader' }, [
          el('span', { class: 'dot' }),
          el('strong', {}, [LANE_LABELS[lane]()]),
          el('span', { class: 'count' }, [String(items.length)]),
        ]),
        laneBody,
      ]);
      board.appendChild(laneEl);
    });

    if (COLLAPSED) $$('.card', board).forEach(c => c.classList.add('collapsed'));
    startClock();
    return;
  }

  /* Incremental update: patch each lane in-place */
  LANES.forEach(lane => {
    const items = buckets[lane] || [];
    const visible = items.slice(0, showCount?.[lane] || PAGE_SIZE);
    const laneEl = board.querySelector(`[data-lane="${lane}"]`);
    if (!laneEl) return;

    /* Update header count */
    const countEl = laneEl.querySelector('.laneHeader .count');
    if (countEl) countEl.textContent = String(items.length);

    const laneBody = laneEl.querySelector('.laneBody');
    if (!laneBody) return;

    const oldMap = laneCardMaps.get(lane) || new Map();
    const newMap = new Map();
    const desiredKeys = visible.map(r => cardKey(r));
    const desiredSet = new Set(desiredKeys);

    /* Remove cards no longer present */
    for (const [key, cardDom] of oldMap) {
      if (!desiredSet.has(key)) {
        cardDom.remove();
      }
    }

    /* Remove "more" button and empty message (will re-add if needed) */
    laneBody.querySelectorAll('.moreBtn, .empty').forEach(n => n.remove());

    /* Add/update cards in order */
    let prevNode = null;
    for (let i = 0; i < visible.length; i++) {
      const r = visible[i];
      const key = cardKey(r);
      const fp = cardFingerprint(r);
      let cardDom = oldMap.get(key);

      if (cardDom) {
        /* Card exists - check if data changed */
        if (cardDom.dataset.fp !== fp) {
          /* Data changed - replace with fresh card */
          const newCard = cardEl(r);
          newCard.dataset.cardKey = key;
          newCard.dataset.fp = fp;
          if (COLLAPSED) newCard.classList.add('collapsed');
          cardDom.replaceWith(newCard);
          cardDom = newCard;
        }
        newMap.set(key, cardDom);
      } else {
        /* New card */
        cardDom = cardEl(r);
        cardDom.dataset.cardKey = key;
        cardDom.dataset.fp = fp;
        if (COLLAPSED) cardDom.classList.add('collapsed');
        newMap.set(key, cardDom);
      }

      /* Ensure correct order in DOM */
      const expectedAfter = prevNode;
      const actualPrev = cardDom.previousElementSibling;
      if (actualPrev !== expectedAfter || !cardDom.parentNode) {
        if (expectedAfter) {
          expectedAfter.after(cardDom);
        } else {
          laneBody.prepend(cardDom);
        }
      }
      prevNode = cardDom;
    }

    /* Empty state */
    if (visible.length === 0) {
      laneBody.appendChild(el('div', { class: 'empty' }, [t('sched.noEntries')]));
    }

    /* "More" button */
    if (items.length > visible.length) {
      laneBody.appendChild(moreBtn(lane));
    }

    laneCardMaps.set(lane, newMap);
  });

  startClock();
}

function moreBtn(lane) {
  return el('button', {
    class: 'moreBtn', onclick: () => {
      showCount[lane] = (showCount[lane] || PAGE_SIZE) + PAGE_SIZE;
      if (lastBuckets) renderBoard(lastBuckets);
    }
  }, [t('sched.displayMore')]);
}

function startClock() {
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = setInterval(updateCountdowns, 1000);
}

/* ── origin badge resolver ── */
function _resolveOrigin(r) {
  const md = r.metadata || {};
  const trigger = (r.trigger_source || md.trigger_source || '').toLowerCase();
  const method = (md.decision_method || '').toLowerCase();
  const origin = (md.decision_origin || '').toLowerCase();

  // LLM orchestrator (autonomous or advisor)
  if (trigger === 'llm_autonomous' || origin === 'llm' || method === 'llm_autonomous')
    return { label: 'LLM', cls: 'llm' };
  if (trigger === 'llm_advisor' || method === 'llm_advisor')
    return { label: 'LLM Advisor', cls: 'llm' };
  // AI model (ML-based decision)
  if (method === 'ai_confirmed' || method === 'ai_boosted' || origin === 'ai_confirmed')
    return { label: 'AI', cls: 'ai' };
  // MCP (external tool call)
  if (trigger === 'mcp' || trigger === 'mcp_tool')
    return { label: 'MCP', cls: 'mcp' };
  // Manual (UI or API)
  if (trigger === 'ui' || trigger === 'manual' || trigger === 'api')
    return { label: 'Manual', cls: 'manual' };
  // Scheduler heuristic (default)
  if (trigger === 'scheduler' || trigger === 'trigger_event' || method === 'heuristic')
    return { label: 'Heuristic', cls: 'heuristic' };
  // Fallback: show trigger if known
  if (trigger) return { label: trigger, cls: 'heuristic' };
  return null;
}

/* ── card ── */
function cardEl(r) {
  const cs = r._computed_status;
  const children = [];

  /* info button */
  children.push(el('button', {
    class: 'infoBtn', title: t('sched.history'),
    onclick: () => openHistory(r.action_name, r.mac, r.port || 0)
  }, ['i']));

  /* header */
  children.push(el('div', { class: 'cardHeader' }, [
    el('div', { class: 'actionIconWrap' }, [
      el('img', {
        class: 'actionIcon', src: resolveIconSync(r.action_name),
        width: '80', height: '80', onerror: (e) => { e.target.src = '/actions/actions_icons/default.png'; }
      }),
    ]),
    el('div', { class: 'actionName' }, [
      el('span', { class: 'chip', style: `--h:${hashHue(r.action_name)}` }, [r.action_name]),
    ]),
    el('span', { class: `badge status-${cs}` }, [cs]),
  ]));

  /* origin badge — shows who queued this action */
  const origin = _resolveOrigin(r);
  if (origin) {
    children.push(el('div', { class: 'originBadge origin-' + origin.cls }, [origin.label]));
  }

  /* chips */
  const chips = [];
  if (r.hostname) chips.push(chipEl(r.hostname, 195));
  if (r.ip) chips.push(chipEl(r.ip, 195));
  if (r.port) chips.push(chipEl(`${t('sched.port')} ${r.port}`, 210, t('sched.port')));
  if (r.mac) chips.push(chipEl(r.mac, 195));
  if (chips.length) children.push(el('div', { class: 'chips' }, chips));

  /* service kv */
  if (r.service) children.push(el('div', { class: 'kv' }, [el('span', {}, [`${t('sched.service')}: ${r.service}`])]));

  /* tags */
  if (r.tags?.length) {
    children.push(el('div', { class: 'tags' },
      r.tags.map(tag => el('span', { class: 'tag' }, [tag]))));
  }

  /* timer */
  if ((cs === 'upcoming' || (cs === 'pending' && r.scheduled_ms > Date.now())) && r.scheduled_ms) {
    children.push(el('div', { class: 'timer', 'data-type': 'start', 'data-ts': String(r.scheduled_ms) }, [
      t('sched.eligibleIn') + ' ', el('span', { class: 'cd' }, ['-']),
    ]));
    children.push(el('div', { class: 'progress' }, [
      el('div', { class: 'bar', 'data-start': String(r.created_ms), 'data-end': String(r.scheduled_ms), style: 'width:0%' }),
    ]));
  } else if (cs === 'running' && r.started_ms) {
    children.push(el('div', { class: 'timer', 'data-type': 'elapsed', 'data-ts': String(r.started_ms) }, [
      t('sched.elapsed') + ' ', el('span', { class: 'cd' }, ['-']),
    ]));
  }

  /* meta */
  const meta = [el('span', {}, [`${t('sched.created')}: ${fmt(r.created_at)}`])];
  if (r.started_at) meta.push(el('span', {}, [`${t('sched.started')}: ${fmt(r.started_at)}`]));
  if (r.completed_at) meta.push(el('span', {}, [`${t('sched.done')}: ${fmt(r.completed_at)}`]));
  if (r.retry_count > 0) meta.push(el('span', { class: 'chip', style: '--h:30' }, [
    `${t('sched.retries')} ${r.retry_count}${r.max_retries != null ? '/' + r.max_retries : ''}`]));
  if (r.priority_effective) meta.push(el('span', {}, [`${t('sched.priority')}: ${r.priority_effective}`]));
  children.push(el('div', { class: 'meta' }, meta));

  /* buttons */
  const btns = [];
  if (['upcoming', 'scheduled', 'pending', 'running'].includes(r.status)) {
    btns.push(el('button', { class: 'btn warn', onclick: () => queueCmd(r.id, 'cancel') }, [t('common.cancel')]));
  }
  if (!['running', 'pending', 'scheduled'].includes(r.status)) {
    btns.push(el('button', { class: 'btn danger', onclick: () => queueCmd(r.id, 'delete') }, [t('common.delete')]));
  }
  if (btns.length) children.push(el('div', { class: 'btns' }, btns));

  /* error / result */
  if (r.error_message) children.push(el('div', { class: 'notice error' }, [r.error_message]));
  if (r.result_summary) children.push(el('div', { class: 'notice success' }, [r.result_summary]));

  return el('div', { class: `card status-${cs}` }, children);
}

function chipEl(text, hue, prefix) {
  const parts = [];
  if (prefix) parts.push(el('span', { class: 'k' }, [prefix]), '\u00A0');
  parts.push(text);
  return el('span', { class: 'chip', style: `--h:${hue}` }, parts);
}

/* ── countdown / progress ── */
function updateCountdowns() {
  const now = Date.now();
  $$('.timer').forEach(timer => {
    const type = timer.dataset.type;
    const ts = parseInt(timer.dataset.ts);
    const cd = timer.querySelector('.cd');
    if (!cd || !ts) return;
    if (type === 'start') {
      const diff = ts - now;
      cd.textContent = diff <= 0 ? t('sched.due') : ms2str(diff);
    } else if (type === 'elapsed') {
      cd.textContent = ms2str(now - ts);
    }
  });
  $$('.progress .bar').forEach(bar => {
    const start = parseInt(bar.dataset.start);
    const end = parseInt(bar.dataset.end);
    if (!start || !end || end <= start) return;
    const pct = Math.min(100, Math.max(0, ((now - start) / (end - start)) * 100));
    bar.style.width = pct + '%';
  });
}

/* ── queue command ── */
async function queueCmd(id, cmd) {
  try {
    await api.post('/queue_cmd', { id, cmd });
    tick();
  } catch (e) {
    showError(t('sched.cmdFailed') + ': ' + e.message);
  }
}

/* ── history modal ── */
async function openHistory(action, mac, port) {
  const modal = $('#sched-histModal');
  const title = $('#sched-histTitle');
  const body = $('#sched-histBody');
  if (!modal || !body) return;

  if (title) title.textContent = `\u2014 ${action} \u00B7 ${mac}${port && port !== 0 ? ` \u00B7 port ${port}` : ''}`;
  empty(body);
  body.appendChild(el('div', { class: 'empty' }, [t('common.loading')]));
  modal.style.display = 'flex';
  modal.setAttribute('aria-hidden', 'false');

  try {
    const url = `/attempt_history?action=${encodeURIComponent(action)}&mac=${encodeURIComponent(mac)}&port=${encodeURIComponent(port)}&limit=100`;
    const data = await api.get(url, { timeout: 8000 });
    const rows = Array.isArray(data) ? data : (data?.rows || data || []);

    empty(body);
    if (!rows.length) {
      body.appendChild(el('div', { class: 'empty' }, [t('sched.noHistory')]));
      return;
    }

    const norm = rows.map(x => ({
      status: (x.status || '').toLowerCase(),
      retry_count: Number(x.retry_count || 0),
      max_retries: x.max_retries,
      ts: x.ts || x.completed_at || x.started_at || x.scheduled_for || x.created_at || '',
    })).sort((a, b) => (b.ts > a.ts ? 1 : -1));

    norm.forEach(hr => {
      const st = hr.status || 'unknown';
      const retry = (hr.retry_count || hr.max_retries != null)
        ? el('span', { style: 'color:var(--ink)' }, [`${t('sched.retry')} ${hr.retry_count}${hr.max_retries != null ? '/' + hr.max_retries : ''}`])
        : null;

      body.appendChild(el('div', { class: `histRow hist-${st}` }, [
        el('span', { class: 'ts' }, [fmt(hr.ts)]),
        retry,
        el('span', { style: 'margin-left:auto' }),
        el('span', { class: 'st' }, [st]),
      ].filter(Boolean)));
    });
  } catch (e) {
    empty(body);
    body.appendChild(el('div', { class: 'empty' }, [`${t('common.error')}: ${e.message}`]));
  }
}

function closeModal() {
  const modal = $('#sched-histModal');
  if (!modal) return;
  modal.style.display = 'none';
  modal.setAttribute('aria-hidden', 'true');
}

/* ── controls ── */
function setLive(on) {
  LIVE = on;
  const btn = $('#sched-liveBtn');
  if (btn) btn.classList.toggle('active', LIVE);
  if (poller) { poller.stop(); poller = null; }
  if (LIVE) {
    poller = new Poller(tick, 2500, { immediate: false });
    poller.start();
  }
}

function toggleCollapse() {
  COLLAPSED = !COLLAPSED;
  const btn = $('#sched-colBtn');
  if (btn) btn.textContent = COLLAPSED ? t('sched.expand') : t('sched.collapse');
  $$('#sched-board .card').forEach(c => c.classList.toggle('collapsed', COLLAPSED));
}

function toggleSuperseded() {
  INCLUDE_SUPERSEDED = !INCLUDE_SUPERSEDED;
  const btn = $('#sched-supBtn');
  if (btn) {
    btn.classList.toggle('active', INCLUDE_SUPERSEDED);
    btn.textContent = INCLUDE_SUPERSEDED ? t('sched.hideSuperseded') : t('sched.showSuperseded');
  }
  lastFilterKey = '';
  tick();
}

let searchDeb = null;
function onSearch() {
  clearTimeout(searchDeb);
  searchDeb = setTimeout(() => { lastFilterKey = ''; tick(); }, 180);
}

function showError(msg) {
  const bar = $('#sched-errorBar');
  if (!bar) return;
  bar.textContent = msg;
  bar.style.display = 'block';
  setTimeout(() => { bar.style.display = 'none'; }, 5000);
}

/* ── icon resolution ── */
function resolveIconSync(name) {
  if (iconCache.has(name)) return iconCache.get(name);
  resolveIconAsync(name);
  return '/actions/actions_icons/default.png';
}

async function resolveIconAsync(name) {
  if (iconCache.has(name)) return;
  const candidates = [
    `/actions/actions_icons/${name}.png`,
    `/resources/images/status/${name}/${name}.bmp`,
  ];
  for (const url of candidates) {
    try {
      const r = await fetch(url, { method: 'HEAD', cache: 'force-cache' });
      if (r.ok) { iconCache.set(name, url); updateIconsInDOM(name, url); return; }
    } catch { /* next */ }
  }
  iconCache.set(name, '/actions/actions_icons/default.png');
}

function updateIconsInDOM(name, url) {
  $$(`img.actionIcon`).forEach(img => {
    if (img.closest('.cardHeader')?.querySelector('.actionName')?.textContent?.trim() === name) {
      if (img.src !== url) img.src = url;
    }
  });
}

/* ── helpers ── */
function isoToMs(ts) { if (!ts) return 0; return new Date(ts + (ts.includes('Z') || ts.includes('+') ? '' : 'Z')).getTime() || 0; }
function fmt(ts) { if (!ts) return '-'; try { return new Date(ts + (ts.includes('Z') || ts.includes('+') ? '' : 'Z')).toLocaleString(); } catch { return ts; } }
function ms2str(ms) {
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`;
  return `${sec}s`;
}
function toArray(v) {
  if (!v) return [];
  if (Array.isArray(v)) return v.map(String).filter(Boolean);
  try { const p = JSON.parse(v); if (Array.isArray(p)) return p.map(String).filter(Boolean); } catch { /* noop */ }
  return String(v).split(',').map(s => s.trim()).filter(Boolean);
}
function dedupeArr(a) { return [...new Set(a)]; }
function parseJSON(s, fb) { try { return JSON.parse(s); } catch { return fb; } }
function hashHue(str) { let h = 0; for (let i = 0; i < str.length; i++) h = ((h << 5) - h + str.charCodeAt(i)) | 0; return ((h % 360) + 360) % 360; }
function filterKey(q) { return `${q}|${FOCUS}|${COMPACT}|${INCLUDE_SUPERSEDED}`; }
function keepLatest(rows, keyFn, dateFn) {
  const map = new Map();
  rows.forEach(r => {
    const k = keyFn(r);
    const prev = map.get(k);
    if (!prev || dateFn(r) > dateFn(prev)) map.set(k, r);
  });
  return Array.from(map.values());
}
