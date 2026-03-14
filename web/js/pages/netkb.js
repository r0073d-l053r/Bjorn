/**
 * NetKB (Network Knowledge Base) page module.
 * Displays discovered hosts with ports, actions, search, sort, filter, and 3 view modes.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'netkb';
const L = (key, fallback, vars = {}) => {
  const v = t(key, vars);
  return v === key ? fallback : v;
};

/* ── state ── */
let tracker = null;
let poller = null;
let originalData = [];
let viewMode = 'grid';
let showNotAlive = false;
let currentSort = 'ip';
let sortOrder = 1;
let currentFilter = null;
let searchTerm = '';
let searchDebounce = null;
let prevCardKeys = [];     /* track card order for incremental DOM */
let prevFingerprints = {}; /* track card content for change detection */
let prevDataFingerprint = ''; /* track raw data to skip unnecessary work */

/* ── prefs ── */
const getPref = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
const setPref = (k, v) => { try { localStorage.setItem(k, v); } catch { /* noop */ } };

/* ── lifecycle ── */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);

  const savedView = getPref('netkb:view', isMobile() ? 'list' : 'grid');
  const savedOffline = getPref('netkb:offline', 'false') === 'true';
  const savedSearch = getPref('netkb:search', '');
  viewMode = isMobile() && savedView === 'grid' ? 'list' : savedView;
  showNotAlive = savedOffline;
  if (savedSearch) searchTerm = savedSearch.toLowerCase();

  container.appendChild(buildShell(savedSearch));
  syncViewUI();
  syncOfflineUI();
  syncClearBtn();

  tracker.trackEventListener(window, 'resize', () => {
    if (isMobile() && viewMode === 'grid') { viewMode = 'list'; syncViewUI(); refreshDisplay(); }
  });

  /* close search popover on outside click */
  tracker.trackEventListener(document, 'click', (e) => {
    const pop = $('#netkb-searchPop');
    const btn = $('#netkb-btnSearch');
    if (pop && btn && !pop.contains(e.target) && !btn.contains(e.target)) pop.classList.remove('show');
  });
  tracker.trackEventListener(document, 'keydown', (e) => {
    if (e.key === 'Escape') { const pop = $('#netkb-searchPop'); if (pop) pop.classList.remove('show'); }
  });

  await refresh();
  poller = new Poller(refresh, 5000);
  poller.start();
}

export function unmount() {
  if (searchDebounce != null) {
    if (tracker) tracker.clearTrackedTimeout(searchDebounce);
    else clearTimeout(searchDebounce);
    searchDebounce = null;
  }
  if (poller) { poller.stop(); poller = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  originalData = [];
  searchTerm = '';
  currentFilter = null;
  prevCardKeys = [];
  prevFingerprints = {};
  prevDataFingerprint = '';
}

/* ── data fetch ── */
async function refresh() {
  try {
    const data = await api.get('/netkb_data', { timeout: 8000 });
    if (!tracker) return; /* unmounted while awaiting */
    const newData = Array.isArray(data) ? data : [];
    /* Skip full refresh if data unchanged */
    const fp = newData.map(d => `${d.mac}|${d.ip}|${d.hostname}|${d.alive}|${(d.ports||[]).join(',')}|${(d.actions||[]).map(a=>`${a?.name}:${a?.status}`).join(',')}`).join(';');
    if (fp === prevDataFingerprint) return;
    prevDataFingerprint = fp;
    originalData = newData;
    refreshDisplay();
  } catch (err) {
    if (err.name === 'AbortError') return;
    console.warn(`[${PAGE}]`, err.message);
  }
}

/* ── shell ── */
function buildShell(savedSearch) {
  return el('div', { class: 'netkb-container' }, [
    el('div', { class: 'netkb-toolbar-wrap' }, [
      el('div', { class: 'netkb-toolbar', id: 'netkb-toolbar' }, [
        el('button', {
          class: 'icon-btn', id: 'netkb-btnSearch', title: t('common.search'),
          onclick: toggleSearchPop
        }, ['\u{1F50D}']),
        el('div', { class: 'search-pop', id: 'netkb-searchPop' }, [
          el('div', { class: 'search-input-wrap' }, [
            el('input', {
              type: 'text', id: 'netkb-searchInput',
              placeholder: t('netkb.searchPlaceholder'),
              title: t('netkb.searchHint'),
              value: savedSearch || '', oninput: onSearchInput
            }),
            el('button', {
              class: 'search-clear', id: 'netkb-searchClear', type: 'button',
              'aria-label': 'Clear', onclick: clearSearch
            }, ['\u2715']),
          ]),
          el('div', { class: 'search-hint' }, [t('netkb.searchHint')]),
        ]),
        el('div', { class: 'segmented', id: 'netkb-viewSeg' }, [
          el('button', { 'data-view': 'grid', onclick: () => setView('grid') }, [L('common.grid', 'Grid')]),
          el('button', { 'data-view': 'list', onclick: () => setView('list') }, [L('common.list', 'List')]),
          el('button', { 'data-view': 'table', onclick: () => setView('table') }, [L('common.table', 'Table')]),
        ]),
        el('label', { class: 'kb-switch', id: 'netkb-offlineSwitch', 'data-on': String(showNotAlive) }, [
          el('input', {
            type: 'checkbox', id: 'netkb-toggleOffline',
            ...(showNotAlive ? { checked: '' } : {}),
            onchange: (e) => setOffline(e.target.checked)
          }),
          el('span', {}, [L('netkb.showOffline', 'Show offline')]),
          el('span', { class: 'track' }, [el('span', { class: 'thumb' })]),
        ]),
      ]),
    ]),
    el('div', { class: 'netkb-content' }, [
      el('div', { id: 'netkb-card-container', class: 'card-container' }),
      el('div', { id: 'netkb-table-container', class: 'table-wrap hidden' }),
    ]),
  ]);
}

/* ── search ── */
function toggleSearchPop() {
  const pop = $('#netkb-searchPop');
  if (!pop) return;
  pop.classList.toggle('show');
  if (pop.classList.contains('show')) {
    const inp = $('#netkb-searchInput');
    if (inp) { inp.focus(); inp.select(); }
  }
}

function onSearchInput(e) {
  if (searchDebounce != null) {
    if (tracker) tracker.clearTrackedTimeout(searchDebounce);
    else clearTimeout(searchDebounce);
  }
  const handler = () => {
    searchTerm = e.target.value.trim().toLowerCase();
    setPref('netkb:search', e.target.value.trim());
    refreshDisplay();
    syncClearBtn();
    searchDebounce = null;
  };
  searchDebounce = tracker ? tracker.trackTimeout(handler, 120) : setTimeout(handler, 120);
}

function clearSearch() {
  const inp = $('#netkb-searchInput');
  if (inp) { inp.value = ''; inp.focus(); }
  searchTerm = '';
  setPref('netkb:search', '');
  refreshDisplay();
  syncClearBtn();
}

function syncClearBtn() {
  const btn = $('#netkb-searchClear');
  if (btn) btn.style.display = searchTerm ? '' : 'none';
}

/* ── view mode ── */
function setView(mode) {
  if (isMobile() && mode === 'grid') mode = 'list';
  viewMode = mode;
  setPref('netkb:view', mode);
  syncViewUI();
  refreshDisplay();
}

function syncViewUI() {
  const cards = $('#netkb-card-container');
  const table = $('#netkb-table-container');
  if (!cards || !table) return;
  if (viewMode === 'table') {
    cards.classList.add('hidden');
    table.classList.remove('hidden');
  } else {
    table.classList.add('hidden');
    cards.classList.remove('hidden');
  }
  $$('#netkb-viewSeg button').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.view === viewMode));
  });
}

/* ── offline toggle ── */
function setOffline(on) {
  showNotAlive = !!on;
  syncOfflineUI();
  setPref('netkb:offline', String(on));
  refreshDisplay();
}

function syncOfflineUI() {
  const sw = $('#netkb-offlineSwitch');
  if (sw) sw.dataset.on = String(showNotAlive);
  const cb = $('#netkb-toggleOffline');
  if (cb) cb.checked = showNotAlive;
}

/* ── sort / filter ── */
function sortBy(key) {
  if (currentSort === key) sortOrder = -sortOrder;
  else { currentSort = key; sortOrder = 1; }
  refreshDisplay();
}

function filterBy(criteria, ev) {
  if (ev) ev.stopPropagation();
  currentFilter = (currentFilter === criteria) ? null : criteria;
  refreshDisplay();
}

/* ── paint orchestrator ── */
function refreshDisplay() {
  let data = [...originalData];
  if (searchTerm) data = data.filter(matchesSearch);
  if (currentFilter) {
    data = data.filter(item => {
      switch (currentFilter) {
        case 'hasActions': return item.actions && item.actions.some(a => a && a.status);
        case 'hasPorts': return item.ports && item.ports.some(Boolean);
        case 'toggleAlive': return !item.alive;
        default: return true;
      }
    });
  }
  if (currentSort) {
    const ipToNum = ip => !ip ? 0 : ip.split('.').reduce((a, p) => (a << 8) + (+p || 0), 0);
    data.sort((a, b) => {
      if (currentSort === 'ports') {
        return sortOrder * ((a.ports?.filter(Boolean).length || 0) - (b.ports?.filter(Boolean).length || 0));
      }
      if (currentSort === 'ip') return sortOrder * (ipToNum(a.ip) - ipToNum(b.ip));
      const av = (a[currentSort] || '').toString();
      const bv = (b[currentSort] || '').toString();
      return sortOrder * av.localeCompare(bv, undefined, { numeric: true });
    });
  }
  if (viewMode === 'table') renderTable(data);
  else renderCards(data);
}

/* ── search ── */
const norm = v => (v ?? '').toString().toLowerCase();
function matchesSearch(item) {
  if (!searchTerm) return true;
  const q = searchTerm;
  if (norm(item.hostname).includes(q)) return true;
  if (norm(item.ip).includes(q)) return true;
  if (norm(item.mac).includes(q)) return true;
  if (norm(item.vendor).includes(q)) return true;
  if (norm(item.essid).includes(q)) return true;
  if (Array.isArray(item.ports) && item.ports.some(p => norm(p).includes(q))) return true;
  if (Array.isArray(item.actions) && item.actions.some(a => norm(a?.name).includes(q))) return true;
  return false;
}

/* ── card key + fingerprint for incremental updates ── */
function cardKey(item) {
  return `${item.mac || ''}_${item.ip || ''}`;
}
function cardFingerprint(item) {
  const ports = (item.ports || []).filter(Boolean).join(',');
  const acts  = (item.actions || []).map(a => `${a?.name}:${a?.status}`).join(',');
  return `${item.hostname}|${item.ip}|${item.mac}|${item.vendor}|${item.essid}|${item.alive}|${ports}|${acts}`;
}

/* ── build a single card DOM ── */
function buildCardEl(item) {
  const alive = item.alive;
  const cardClass = `card ${viewMode === 'list' ? 'list' : ''} ${alive ? 'alive' : 'not-alive'}`;
  const title = (item.hostname && item.hostname !== 'N/A') ? item.hostname : (item.ip || 'N/A');

  const sections = [];
  if (item.ip) sections.push(fieldRow(t('netkb.ip'), 'ip', item.ip));
  if (item.mac) sections.push(fieldRow(t('netkb.mac'), 'mac', item.mac));
  if (item.vendor && item.vendor !== 'N/A') sections.push(fieldRow(t('netkb.vendor'), 'vendor', item.vendor));
  if (item.essid && item.essid !== 'N/A') sections.push(fieldRow(t('netkb.essid'), 'essid', item.essid));
  if (item.ports && item.ports.filter(Boolean).length > 0) {
    sections.push(el('div', { class: 'card-section' }, [
      el('strong', {}, [L('netkb.openPorts', 'Open Ports') + ':']),
      el('div', { class: 'port-bubbles' },
        item.ports.filter(Boolean).map(p => chip('port', String(p)))
      ),
    ]));
  }

  const card = el('div', { class: cardClass, 'data-card-key': cardKey(item) }, [
    el('div', { class: 'card-content' }, [
      el('h3', { class: 'card-title' }, [hlText(title)]),
      ...sections,
    ]),
    el('div', { class: 'status-container' }, renderBadges(item.actions, item.ip)),
  ]);
  return card;
}

/* ── card rendering (incremental) ── */
function renderCards(data) {
  const container = $('#netkb-card-container');
  if (!container) return;

  const visible = data.filter(i => showNotAlive || i.alive);

  /* empty state */
  if (visible.length === 0) {
    if (prevCardKeys.length > 0 || container.children.length === 0 ||
        !container.querySelector('.netkb-empty')) {
      empty(container);
      container.appendChild(el('div', { class: 'netkb-empty' }, [t('common.noData')]));
      prevCardKeys = [];
      prevFingerprints = {};
    }
    return;
  }

  /* compute new keys + fingerprints */
  const newKeys = visible.map(cardKey);
  const newFP   = {};
  visible.forEach(item => { newFP[cardKey(item)] = cardFingerprint(item); });

  /* first render or structural change (different keys/order) → full rebuild */
  const keysMatch = newKeys.length === prevCardKeys.length &&
                    newKeys.every((k, i) => k === prevCardKeys[i]);

  if (!keysMatch) {
    /* full rebuild — order or set of items changed */
    empty(container);
    for (const item of visible) container.appendChild(buildCardEl(item));
  } else {
    /* incremental — only replace cards whose fingerprint changed */
    const children = container.children;
    for (let i = 0; i < visible.length; i++) {
      const key = newKeys[i];
      if (newFP[key] !== prevFingerprints[key]) {
        const newCard = buildCardEl(visible[i]);
        container.replaceChild(newCard, children[i]);
      }
    }
  }

  prevCardKeys = newKeys;
  prevFingerprints = newFP;
}

/* ── table rendering ── */
function renderTable(data) {
  const container = $('#netkb-table-container');
  if (!container) return;
  empty(container);

  const thClick = (key) => () => sortBy(key);
  const fClick = (crit) => (e) => filterBy(crit, e);

  const thead = el('thead', {}, [
    el('tr', {}, [
      el('th', { onclick: thClick('hostname') }, [t('common.hostname') + ' ',
      el('img', { src: '/web/images/filter_icon.png', class: 'filter-icon', onclick: fClick('toggleAlive'), title: t('netkb.toggleOffline'), alt: 'Filter' })]),
      el('th', { onclick: thClick('ip') }, [t('netkb.ip')]),
      el('th', { onclick: thClick('mac') }, [t('netkb.mac')]),
      el('th', { onclick: thClick('essid') }, [t('netkb.essid')]),
      el('th', { onclick: thClick('vendor') }, [t('common.vendor')]),
      el('th', { onclick: thClick('ports') }, [t('common.ports') + ' ',
      el('img', { src: '/web/images/filter_icon.png', class: 'filter-icon', onclick: fClick('hasPorts'), title: t('netkb.hasPorts'), alt: 'Filter' })]),
      el('th', {}, [t('common.actions') + ' ',
      el('img', { src: '/web/images/filter_icon.png', class: 'filter-icon', onclick: fClick('hasActions'), title: t('netkb.hasActions'), alt: 'Filter' })]),
    ]),
  ]);

  const visible = data.filter(i => showNotAlive || i.alive);
  const rows = visible.map(item => {
    const hostText = (item.hostname && item.hostname !== 'N/A') ? item.hostname : (item.ip || 'N/A');
    return el('tr', {}, [
      el('td', {}, [chip('host', hostText)]),
      el('td', {}, item.ip ? [chip('ip', item.ip)] : [t('netkb.na')]),
      el('td', {}, item.mac ? [chip('mac', item.mac)] : [t('netkb.na')]),
      el('td', {}, (item.essid && item.essid !== 'N/A') ? [chip('essid', item.essid)] : [t('netkb.na')]),
      el('td', {}, (item.vendor && item.vendor !== 'N/A') ? [chip('vendor', item.vendor)] : [t('netkb.na')]),
      el('td', {}, [el('div', { class: 'port-bubbles' },
        (item.ports || []).filter(Boolean).map(p => chip('port', String(p))))]),
      el('td', {}, [el('div', { class: 'status-container' }, renderBadges(item.actions, item.ip))]),
    ]);
  });

  container.appendChild(el('div', { class: 'table-inner' }, [
    el('table', {}, [thead, el('tbody', {}, rows)]),
  ]));
}

/* ── action badges ── */
function renderBadges(actions, ip) {
  if (!actions || actions.length === 0) return [];
  const parseRaw = (raw) => {
    const m = /^([a-z_]+)_(\d{8})_(\d{6})$/i.exec(raw || '');
    if (!m) return null;
    const s = m[1].toLowerCase();
    const y = m[2].slice(0, 4), mo = m[2].slice(4, 6), d = m[2].slice(6, 8);
    const hh = m[3].slice(0, 2), mm = m[3].slice(2, 4), ss = m[3].slice(4, 6);
    const ts = Date.parse(`${y}-${mo}-${d}T${hh}:${mm}:${ss}Z`) || 0;
    return { status: s, ts, d, mo, y, hh, mm, ss };
  };

  const map = new Map();
  for (const a of actions) {
    if (!a || !a.name || !a.status) continue;
    const p = parseRaw(a.status);
    if (!p) continue;
    const prev = map.get(a.name);
    if (!prev || p.ts > prev.parsed.ts) map.set(a.name, { ...a, parsed: p });
  }

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const label = s => ({ success: t('netkb.success'), failed: t('netkb.failed'), fail: t('netkb.failed'), running: t('netkb.running'), pending: t('netkb.pending'), expired: t('netkb.expired'), cancelled: t('netkb.cancelled') })[s] || s;

  return Array.from(map.values())
    .sort((a, b) => b.parsed.ts - a.parsed.ts)
    .map(a => {
      const s = a.parsed.status === 'fail' ? 'failed' : a.parsed.status;
      const clickable = ['success', 'failed', 'expired', 'cancelled'].includes(s);
      const date = `${a.parsed.d} ${MONTHS[parseInt(a.parsed.mo) - 1] || ''} ${a.parsed.y}`;
      const time = `${a.parsed.hh}:${a.parsed.mm}:${a.parsed.ss}`;
      return el('div', {
        class: `badge ${s} ${clickable ? 'clickable' : ''}`,
        ...(clickable ? {
          onclick: () => {
            if (!confirm(L('netkb.confirmRemoveAction', `Are you sure you want to remove the action "${a.name}" for IP "${ip}"?`, { action: a.name, ip }))) return;
            removeAction(ip, a.name);
          }
        } : {}),
      }, [
        el('div', { class: 'badge-header' }, [hlText(a.name)]),
        el('div', { class: 'badge-status' }, [label(s)]),
        el('div', { class: 'badge-timestamp' }, [el('div', {}, [date]), el('div', {}, [`${t('netkb.at')} ${time}`])]),
      ]);
    });
}

async function removeAction(ip, action) {
  try {
    const result = await api.post('/delete_netkb_action', { ip, action });
    if (result.status === 'success') {
      toast(result.message || t('netkb.actionRemoved'), 2600, 'success');
      await refresh();
    } else throw new Error(result.message || 'Failed');
  } catch (e) {
    console.error(e);
    toast(`${t('common.error')}: ${e.message}`, 3000, 'error');
  }
}

/* ── helpers ── */
function chip(type, text) {
  return el('span', { class: `chip ${type}` }, [hlText(text)]);
}

function fieldRow(label, chipType, value) {
  return el('div', { class: 'card-section' }, [
    el('strong', {}, [`${label}:`]),
    el('span', {}, [' ']),
    chip(chipType, value),
  ]);
}

function hlText(text) {
  if (!searchTerm || !text) return String(text ?? '');
  const str = String(text);
  const lower = str.toLowerCase();
  const idx = lower.indexOf(searchTerm);
  if (idx === -1) return str;
  const frag = document.createDocumentFragment();
  let pos = 0;
  let i = lower.indexOf(searchTerm, pos);
  while (i !== -1) {
    if (i > pos) frag.appendChild(document.createTextNode(str.slice(pos, i)));
    const mark = document.createElement('mark');
    mark.className = 'hl';
    mark.textContent = str.slice(i, i + searchTerm.length);
    frag.appendChild(mark);
    pos = i + searchTerm.length;
    i = lower.indexOf(searchTerm, pos);
  }
  if (pos < str.length) frag.appendChild(document.createTextNode(str.slice(pos)));
  return frag;
}

function isMobile() { return window.matchMedia('(max-width: 720px)').matches; }
