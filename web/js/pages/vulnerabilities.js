/**
 * Vulnerabilities page module — Bjorn Project
 *
 * Changes vs previous version:
 *  - Card click → opens detail modal directly (no manual expand needed)
 *  - Direct chips on every card: 🐱 GitHub PoC · 🛡 Rapid7 · NVD ↗ · MITRE ↗
 *  - Global "💣 Search All Exploits" button: batch enrichment, stored in DB
 *  - Exploit chips rendered from DB data, updated after enrichment
 *  - Progress indicator during global exploit search
 *  - Poller suspended while modal is open
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller }     from '../core/api.js';
import { el, $, empty }    from '../core/dom.js';
import { t }               from '../core/i18n.js';
import { initSharedSidebarLayout } from '../core/sidebar-layout.js';

const PAGE           = 'vulnerabilities';
const ITEMS_PER_PAGE = 20;
const SEVERITY_ORDER = { critical: 4, high: 3, medium: 2, low: 1 };

/* ── state ── */
let tracker              = null;
let poller               = null;
let disposeSidebarLayout = null;
let vulnerabilities      = [];
let filteredVulns        = [];
let currentView          = 'cve';
let showActiveOnly       = false;
let severityFilters      = new Set();
let searchTerm           = '';
let currentPage          = 1;
let totalPages           = 1;
let expandedHosts        = new Set();
let historyMode          = false;
let sortField            = 'cvss_score';
let sortDir              = 'desc';
let dateFrom             = '';
let dateTo               = '';
let lastFetchTime        = null;
let modalInFlight        = null;
let searchDebounce       = null;
let historyPage          = 1;
let historySearch        = '';
let allHistory           = [];
let exploitSearchRunning = false;

/* ── prefs ── */
const getPref = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };

/* ════════════════════════════════════════
   LIFECYCLE
═══════════════════════════════════════ */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  const shell = buildShell();
  container.appendChild(shell);
  disposeSidebarLayout = initSharedSidebarLayout(shell, {
    sidebarSelector: '.vuln-sidebar',
    mainSelector:    '.vuln-main',
    storageKey:      'sidebar:vulnerabilities',
    toggleLabel:     t('common.menu'),
  });
  await fetchVulnerabilities();
  loadFeedStatus();
  const interval = parseInt(getPref('vuln:refresh', '30000'), 10) || 30000;
  if (interval > 0) {
    poller = new Poller(fetchVulnerabilities, interval);
    poller.start();
  }
}

export function unmount() {
  if (searchDebounce != null) {
    if (tracker) tracker.clearTrackedTimeout(searchDebounce);
    else clearTimeout(searchDebounce);
    searchDebounce = null;
  }
  if (poller)               { poller.stop(); poller = null; }
  if (disposeSidebarLayout) { try { disposeSidebarLayout(); } catch {} disposeSidebarLayout = null; }
  if (tracker)              { tracker.cleanupAll(); tracker = null; }
  vulnerabilities = []; filteredVulns = [];
  currentView = 'cve'; showActiveOnly = false;
  severityFilters.clear(); searchTerm = '';
  currentPage = 1; expandedHosts.clear();
  historyMode = false; modalInFlight = null; allHistory = [];
}

/* ════════════════════════════════════════
   SHELL
═══════════════════════════════════════ */
function buildShell() {
  const sidebar = el('aside', { class: 'vuln-sidebar page-sidebar panel' }, [
    el('div', { class: 'sidehead' }, [
      el('div', { class: 'sidetitle' }, [t('nav.vulnerabilities')]),
      el('div', { class: 'spacer' }),
      el('button', { class: 'btn', id: 'hideSidebar', 'data-hide-sidebar': '1', type: 'button' }, [t('common.hide')]),
    ]),
    el('div', { class: 'sidecontent' }, [
      /* stats */
      el('div', { class: 'stats-header' }, [
        statItem('\u{1F6E1}', 'total-cves',       t('vulns.totalCVEs')),
        statItem('\u{1F534}', 'active-vulns',      t('vulns.active')),
        statItem('\u2705',    'remediated-vulns',  t('vulns.remediated')),
        statItem('\u{1F525}', 'critical-count',    t('vulns.critical')),
        statItem('\u{1F5A5}', 'affected-hosts',    t('vulns.hosts')),
        statItem('\u{1F4A3}', 'exploit-count',     t('vulns.withExploit')),
        statItem('\u26A0',    'kev-count',         t('vulns.kev')),
      ]),
      /* freshness */
      el('div', { id: 'vuln-freshness', style: 'font-size:.75rem;opacity:.5;padding:8px 0 0 4px' }),
      /* ── feed sync ── */
      el('div', { style: 'margin-top:14px;padding:0 4px' }, [
        el('button', {
          id:      'btn-feed-sync',
          class:   'vuln-btn exploit-btn',
          style:   'width:100%;font-weight:600',
          onclick: runFeedSync,
        }, ['\u{1F504} ' + t('vulns.updateFeeds')]),
        el('div', { id: 'feed-sync-status', style: 'font-size:.72rem;opacity:.55;margin-top:4px;min-height:16px' }),
      ]),
      /* sort */
      el('div', { style: 'margin-top:14px;padding:0 4px' }, [
        el('div', { style: 'font-size:.75rem;opacity:.55;margin-bottom:4px' }, [t('vulns.sortBy')]),
        el('select', { id: 'vuln-sort-field', class: 'vuln-select', onchange: onSortChange }, [
          el('option', { value: 'cvss_score' }, [t('vulns.cvssScore')]),
          el('option', { value: 'severity'   }, [t('vulns.severity')]),
          el('option', { value: 'last_seen'  }, [t('vulns.lastSeen')]),
          el('option', { value: 'first_seen' }, [t('vulns.firstSeen')]),
        ]),
        el('select', { id: 'vuln-sort-dir', class: 'vuln-select', onchange: onSortChange, style: 'margin-top:4px' }, [
          el('option', { value: 'desc' }, [t('common.descending')]),
          el('option', { value: 'asc'  }, [t('common.ascending')]),
        ]),
      ]),
      /* date filter */
      el('div', { style: 'margin-top:14px;padding:0 4px' }, [
        el('div', { style: 'font-size:.75rem;opacity:.55;margin-bottom:4px' }, [t('vulns.dateFilter')]),
        el('input', { type: 'date', id: 'vuln-date-from', class: 'vuln-date-input', onchange: onDateChange }),
        el('input', { type: 'date', id: 'vuln-date-to',   class: 'vuln-date-input', onchange: onDateChange, style: 'margin-top:4px' }),
        el('button', { class: 'vuln-btn', style: 'margin-top:6px;width:100%', onclick: clearDateFilter }, [t('vulns.clearDates')]),
      ]),
    ]),
  ]);

  const main = el('div', { class: 'vuln-main page-main' }, [
    el('div', { class: 'vuln-controls' }, [
      el('div', { class: 'global-search-container' }, [
        el('input', { type: 'text', class: 'global-search-input', id: 'vuln-search', placeholder: t('common.search'), oninput: onSearch }),
        el('button', { class: 'clear-global-button', onclick: clearSearch }, ['\u2716']),
      ]),
      el('div', { class: 'vuln-buttons' }, [
        el('button', { class: 'vuln-btn active', id: 'vuln-view-cve',      onclick: () => switchView('cve') },      [t('vulns.cveView')]),
        el('button', { class: 'vuln-btn',        id: 'vuln-view-host',     onclick: () => switchView('host') },     [t('vulns.hostView')]),
        el('button', { class: 'vuln-btn',        id: 'vuln-view-exploits', onclick: () => switchView('exploits') }, ['\u{1F4A3} ' + t('vulns.exploits')]),
        el('button', { class: 'vuln-btn',        id: 'vuln-active-toggle', onclick: toggleActiveFilter }, [t('status.online')]),
        el('button', { class: 'vuln-btn',        id: 'vuln-history-btn',   onclick: toggleHistory },      [t('sched.history')]),
        el('button', { class: 'vuln-btn',        onclick: exportCSV  }, [t('common.export') + ' CSV']),
        el('button', { class: 'vuln-btn',        onclick: exportJSON }, [t('common.export') + ' JSON']),
      ]),
    ]),
    el('div', { class: 'vuln-severity-bar' }, [
      severityBtn('critical'), severityBtn('high'), severityBtn('medium'), severityBtn('low'),
    ]),
    el('div', { class: 'services-grid', id: 'vuln-grid' }),
    el('div', { class: 'vuln-pagination', id: 'vuln-pagination' }),
    /* ── MODAL ── */
    el('div', { class: 'vuln-modal', id: 'vuln-modal', onclick: onModalBackdrop }, [
      el('div', { class: 'vuln-modal-content' }, [
        el('div', { class: 'vuln-modal-header' }, [
          el('span', { class: 'vuln-modal-title', id: 'vuln-modal-title' }),
          /* ref chips in modal header */
          el('div',  { class: 'vuln-modal-header-chips', id: 'vuln-modal-header-chips' }),
          el('button', { class: 'vuln-modal-close', onclick: closeModal }, ['\u2716']),
        ]),
        el('div', { class: 'vuln-modal-body', id: 'vuln-modal-body' }),
      ]),
    ]),
  ]);

  return el('div', { class: 'vuln-container page-with-sidebar' }, [sidebar, main]);
}

function statItem(icon, id, label) {
  return el('div', { class: 'stat-card stat-item' }, [
    el('span', { class: 'stat-icon' },               [icon]),
    el('span', { class: 'stat-number stat-value', id }, ['0']),
    el('span', { class: 'stat-label' },              [label]),
  ]);
}
function severityBtn(sev) {
  return el('button', {
    class: `vuln-severity-btn severity-${sev}`,
    'data-severity': sev,
    onclick: (e) => toggleSeverity(sev, e.currentTarget),
  }, [sev.charAt(0).toUpperCase() + sev.slice(1)]);
}

/* ════════════════════════════════════════
   DATA FETCH
═══════════════════════════════════════ */
async function fetchVulnerabilities() {
  if (historyMode) return;
  try {
    const data = await api.get('/list_vulnerabilities', { timeout: 10000 });
    if (!tracker) return; /* unmounted while awaiting */
    vulnerabilities = Array.isArray(data) ? data : (data?.vulnerabilities || []);
    lastFetchTime   = new Date();
    const f = $('#vuln-freshness');
    if (f) f.textContent = t('vulns.lastRefresh', { time: lastFetchTime.toLocaleTimeString() });
    updateStats();
    filterAndRender();
  } catch (err) {
    console.warn(`[${PAGE}]`, err.message);
  }
}

/* ════════════════════════════════════════
   FEED SYNC
   POST /api/feeds/sync — downloads CISA KEV + Exploit-DB + EPSS into local DB
   GET  /api/feeds/status — last sync timestamps
═══════════════════════════════════════ */
async function runFeedSync() {
  const btn    = $('#btn-feed-sync');
  const status = $('#feed-sync-status');
  if (btn && btn.disabled) return;
  if (btn)    { btn.disabled = true; btn.textContent = '\u23F3 ' + t('vulns.downloading'); }
  if (status) status.textContent = t('vulns.syncingFeeds');

  try {
    const res = await api.post('/api/feeds/sync', {}, { timeout: 120000 });
    const feeds = res?.feeds || {};
    const parts = [];
    for (const [name, info] of Object.entries(feeds)) {
      if (info.status === 'ok')    parts.push(`${name}: ${info.count} records`);
      else                          parts.push(`${name}: \u274C ${info.message || 'error'}`);
    }
    if (status) status.textContent = '\u2705 ' + (parts.join(' \u00B7 ') || 'Done');
    await fetchVulnerabilities();
  } catch (err) {
    if (status) status.textContent = `\u274C ${err.message}`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '\u{1F504} ' + t('vulns.updateFeeds'); }
  }
}

async function loadFeedStatus() {
  try {
    const res = await api.get('/api/feeds/status');
    const status = $('#feed-sync-status');
    if (!status || !res?.feeds) return;
    const entries = Object.entries(res.feeds);
    if (!entries.length) { status.textContent = t('vulns.noSyncYet'); return; }
    // show the most recent sync time
    const latest = entries.reduce((a, [, v]) => Math.max(a, v.last_synced || 0), 0);
    if (latest) {
      const d = new Date(latest * 1000);
      status.textContent = t('vulns.lastSync', { date: `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`, count: res.total_exploits || 0 });
    }
  } catch { /* ignore */ }
}


/* ════════════════════════════════════════
   STATS
═══════════════════════════════════════ */
function updateStats() {
  const sv = (id, v) => { const e = $(`#${id}`); if (e) e.textContent = v; };
  sv('total-cves',       vulnerabilities.length);
  sv('active-vulns',     vulnerabilities.filter(v => v.is_active === 1).length);
  sv('remediated-vulns', vulnerabilities.filter(v => v.is_active === 0).length);
  sv('critical-count',   vulnerabilities.filter(v => v.is_active === 1 && v.severity === 'critical').length);
  sv('exploit-count',    vulnerabilities.filter(v => v.has_exploit).length);
  sv('kev-count',        vulnerabilities.filter(v => v.is_kev).length);
  const macs = new Set(vulnerabilities.map(v => v.mac_address).filter(Boolean));
  sv('affected-hosts', macs.size);
}

/* ════════════════════════════════════════
   FILTER + SORT
═══════════════════════════════════════ */
function filterAndRender() {
  const needle = searchTerm.toLowerCase();
  const from   = dateFrom ? new Date(dateFrom).getTime() : null;
  const to     = dateTo   ? new Date(dateTo + 'T23:59:59').getTime() : null;

  filteredVulns = vulnerabilities.filter(v => {
    if (showActiveOnly && v.is_active === 0) return false;
    if (severityFilters.size > 0 && !severityFilters.has(v.severity)) return false;
    if (needle) {
      if (!`${v.vuln_id} ${v.ip} ${v.hostname} ${v.port} ${v.description}`.toLowerCase().includes(needle)) return false;
    }
    if (from || to) {
      const ls = v.last_seen ? new Date(v.last_seen).getTime() : null;
      if (from && (!ls || ls < from)) return false;
      if (to   && (!ls || ls > to))   return false;
    }
    return true;
  });

  filteredVulns.sort((a, b) => {
    let va, vb;
    switch (sortField) {
      case 'severity':   va = SEVERITY_ORDER[a.severity] || 0; vb = SEVERITY_ORDER[b.severity] || 0; break;
      case 'last_seen':  va = a.last_seen  ? new Date(a.last_seen).getTime()  : 0; vb = b.last_seen  ? new Date(b.last_seen).getTime()  : 0; break;
      case 'first_seen': va = a.first_seen ? new Date(a.first_seen).getTime() : 0; vb = b.first_seen ? new Date(b.first_seen).getTime() : 0; break;
      default:           va = parseFloat(a.cvss_score) || 0; vb = parseFloat(b.cvss_score) || 0;
    }
    return sortDir === 'asc' ? va - vb : vb - va;
  });

  totalPages = Math.max(1, Math.ceil(filteredVulns.length / ITEMS_PER_PAGE));
  if (currentPage > totalPages) currentPage = 1;

  if      (currentView === 'host')     renderHostView();
  else if (currentView === 'exploits') renderExploitsView();
  else                                 renderCVEView();
  renderPagination();
}

/* ════════════════════════════════════════
   CHIP BUILDERS  (shared across all views)
═══════════════════════════════════════ */

/** Four external reference chips — always visible on every card & in modal */
function buildRefChips(cveId) {
  const enc = encodeURIComponent(cveId);
  return el('div', { class: 'vuln-ref-chips', onclick: e => e.stopPropagation() }, [
    refChip('\u{1F431} GitHub',  `https://github.com/search?q=${enc}&type=repositories`,   'chip-github'),
    refChip('\u{1F6E1} Rapid7',  `https://www.rapid7.com/db/?q=${enc}`,                    'chip-rapid7'),
    refChip('NVD \u2197',        `https://nvd.nist.gov/vuln/detail/${enc}`,                'chip-nvd'),
    refChip('MITRE \u2197',      `https://cve.mitre.org/cgi-bin/cvename.cgi?name=${enc}`,  'chip-mitre'),
  ]);
}

/** Exploit chips built from DB data — shown only when exploit data exists */
function buildExploitChips(v) {
  const exploits = Array.isArray(v.exploits) ? v.exploits : [];
  if (!v.has_exploit && exploits.length === 0) return null;

  const chips = exploits.slice(0, 5).map(entry => {
    const isStr = typeof entry === 'string';
    const label = isStr
      ? (entry.startsWith('http') ? 'ExploitDB' : entry.substring(0, 28))
      : (entry.title || 'Exploit').substring(0, 28);
    const href  = isStr
      ? (entry.startsWith('http') ? entry : `https://www.exploit-db.com/exploits/${entry}`)
      : (entry.url || `https://www.exploit-db.com/search?cve=${encodeURIComponent(v.vuln_id)}`);
    return refChip('\u26A1 ' + label, href, 'chip-exploit');
  });

  /* fallback generic chip if flag set but no detail yet */
  if (chips.length === 0)
    chips.push(refChip('\u{1F4A3} ExploitDB', `https://www.exploit-db.com/search?cve=${encodeURIComponent(v.vuln_id)}`, 'chip-exploit'));

  return el('div', { class: 'vuln-exploit-chips', onclick: e => e.stopPropagation() }, chips);
}

function refChip(label, href, cls) {
  return el('a', { href, target: '_blank', rel: 'noopener noreferrer', class: `vuln-chip ${cls}` }, [label]);
}

/* ════════════════════════════════════════
   CVE VIEW  — full-card click → modal
═══════════════════════════════════════ */
function renderCVEView() {
  const grid = $('#vuln-grid');
  if (!grid) return;
  empty(grid);
  const page = filteredVulns.slice((currentPage - 1) * ITEMS_PER_PAGE, currentPage * ITEMS_PER_PAGE);
  if (!page.length) { grid.appendChild(emptyState(t('vulns.noVulns'))); return; }

  page.forEach((v, i) => {
    const exploitChips = buildExploitChips(v);
    const card = el('div', {
      class:  `vuln-card ${v.is_active === 0 ? 'inactive' : ''}`,
      style:  `animation-delay:${i * 0.03}s;cursor:pointer`,
      onclick: (e) => {
        if (e.target.closest('a, .vuln-ref-chips, .vuln-exploit-chips')) return;
        showCVEDetails(v.vuln_id);
      },
    }, [
      /* header */
      el('div', { class: 'vuln-card-header' }, [
        el('div', { class: 'vuln-card-title' }, [
          el('span', { class: 'vuln-id' }, [v.vuln_id || 'N/A']),
          el('span', { class: `severity-badge severity-${v.severity}` }, [v.severity || '?']),
          el('span', { class: 'cvss-pill' }, [`CVSS ${parseFloat(v.cvss_score || 0).toFixed(1)}`]),
          ...(v.is_active === 0 ? [el('span', { class: 'vuln-tag remediated' }, ['REMEDIATED'])] : []),
          ...(v.is_kev           ? [el('span', { class: 'vuln-tag kev',  title: 'CISA Known Exploited' }, ['KEV'])] : []),
          ...(v.epss > 0.1       ? [el('span', { class: 'vuln-tag epss' }, [`EPSS ${(v.epss * 100).toFixed(1)}%`])] : []),
        ]),
        el('span', { style: 'font-size:.72rem;opacity:.35;white-space:nowrap' }, ['\u{1F4CB} ' + t('vulns.clickDetails')]),
      ]),
      /* meta */
      el('div', { class: 'vuln-meta' }, [
        metaItem(t('common.ip'),   v.ip),
        metaItem(t('common.host'), v.hostname),
        metaItem(t('common.port'), v.port),
      ]),
      /* description */
      el('div', { style: 'font-size:.83rem;opacity:.7;margin:6px 0 8px;line-height:1.4' }, [
        (v.description || '').substring(0, 160) + ((v.description || '').length > 160 ? '\u2026' : ''),
      ]),
      /* ★ reference chips — always visible */
      buildRefChips(v.vuln_id),
      /* ★ exploit chips — from DB, only if available */
      ...(exploitChips ? [exploitChips] : []),
    ]);
    grid.appendChild(card);
  });
}

/* ════════════════════════════════════════
   HOST VIEW
═══════════════════════════════════════ */
function renderHostView() {
  const grid = $('#vuln-grid');
  if (!grid) return;
  empty(grid);

  const groups = new Map();
  filteredVulns.forEach(v => {
    const key = `${v.mac_address}_${v.hostname || 'unknown'}`;
    if (!groups.has(key)) groups.set(key, { mac: v.mac_address, hostname: v.hostname, ip: v.ip, vulns: [] });
    groups.get(key).vulns.push(v);
  });

  const hostArr = [...groups.values()];
  totalPages    = Math.max(1, Math.ceil(hostArr.length / ITEMS_PER_PAGE));
  if (currentPage > totalPages) currentPage = 1;
  const page = hostArr.slice((currentPage - 1) * ITEMS_PER_PAGE, currentPage * ITEMS_PER_PAGE);
  if (!page.length) { grid.appendChild(emptyState(t('vulns.noHostsFound'))); return; }

  page.forEach((host, i) => {
    const hostId     = `host-${i + (currentPage - 1) * ITEMS_PER_PAGE}`;
    const isExpanded = expandedHosts.has(hostId);
    const sevCounts  = countSeverities(host.vulns);
    const remediated = host.vulns.filter(v => v.is_active === 0).length;

    const card = el('div', {
      class:     `vuln-card host-card ${isExpanded ? 'expanded' : ''}`,
      'data-id': hostId,
      style:     `animation-delay:${i * 0.03}s`,
    }, [
      el('div', { class: 'vuln-card-header', onclick: () => toggleHostCard(hostId) }, [
        el('div', { class: 'vuln-card-title' }, [
          el('span', { class: 'vuln-id' }, [host.hostname || host.ip || host.mac || 'Unknown']),
          el('span', { class: 'stat-label' }, [t('vulns.vulnsCount', { count: host.vulns.length })]),
          ...(remediated > 0                          ? [el('span', { class: 'vuln-tag remediated' }, [`${remediated} ${t('vulns.fixed')}`])] : []),
          ...(host.vulns.some(v => v.has_exploit) ? [el('span', { class: 'vuln-tag exploit' }, ['\u{1F4A3}'])] : []),
        ]),
        el('div', { class: 'host-severity-pills' }, [
          ...(sevCounts.critical > 0 ? [sevPill('critical', sevCounts.critical)] : []),
          ...(sevCounts.high     > 0 ? [sevPill('high',     sevCounts.high)]     : []),
          ...(sevCounts.medium   > 0 ? [sevPill('medium',   sevCounts.medium)]   : []),
          ...(sevCounts.low      > 0 ? [sevPill('low',      sevCounts.low)]      : []),
        ]),
        el('span', { class: 'collapse-indicator' }, ['\u25BC']),
      ]),
      el('div', { class: 'vuln-content' }, [
        el('div', { class: 'vuln-meta' }, [
          metaItem(t('common.ip'),       host.ip),
          metaItem(t('common.mac'),      host.mac),
          metaItem(t('vulns.active'),   host.vulns.filter(v => v.is_active === 1).length),
          metaItem(t('vulns.maxCvss'), Math.max(...host.vulns.map(v => parseFloat(v.cvss_score) || 0)).toFixed(1)),
        ]),
        ...sortVulnsByPriority(host.vulns).map(v => {
          const exploitChips = buildExploitChips(v);
          return el('div', {
            class:   `host-vuln-item ${v.is_active === 0 ? 'inactive' : ''}`,
            style:   'cursor:pointer',
            onclick: (e) => {
              if (e.target.closest('a, .vuln-ref-chips, .vuln-exploit-chips')) return;
              showCVEDetails(v.vuln_id);
            },
          }, [
            el('div', { class: 'host-vuln-info' }, [
              el('span', { class: 'vuln-id' }, [v.vuln_id]),
              el('span', { class: `severity-badge severity-${v.severity}` }, [v.severity]),
              el('span', { class: 'cvss-pill' }, [`CVSS ${parseFloat(v.cvss_score || 0).toFixed(1)}`]),
              ...(v.is_active === 0 ? [el('span', { class: 'vuln-tag remediated' }, ['REMEDIATED'])] : []),
            ]),
            el('div', { class: 'vuln-meta', style: 'margin:4px 0' }, [
              metaItem(t('common.port'), v.port),
              metaItem(t('common.last'), formatDate(v.last_seen)),
            ]),
            el('div', { style: 'font-size:.82rem;opacity:.65;margin-bottom:6px' }, [
              (v.description || '').substring(0, 110) + ((v.description || '').length > 110 ? '\u2026' : ''),
            ]),
            buildRefChips(v.vuln_id),
            ...(exploitChips ? [exploitChips] : []),
          ]);
        }),
      ]),
    ]);
    grid.appendChild(card);
  });
}

/* ════════════════════════════════════════
   EXPLOITS VIEW
═══════════════════════════════════════ */
function renderExploitsView() {
  const grid = $('#vuln-grid');
  if (!grid) return;
  empty(grid);

  const withExploit = filteredVulns.filter(v => v.has_exploit || (v.exploits && v.exploits.length > 0));
  totalPages = Math.max(1, Math.ceil(withExploit.length / ITEMS_PER_PAGE));
  if (currentPage > totalPages) currentPage = 1;
  const page = withExploit.slice((currentPage - 1) * ITEMS_PER_PAGE, currentPage * ITEMS_PER_PAGE);

  if (!page.length) {
    const wrapper = el('div', { style: 'text-align:center;padding:40px' }, [
      emptyState('\u{1F4A3} ' + t('vulns.noExploitData')),
      el('div', { style: 'margin-top:16px' }, [
        el('button', { class: 'vuln-btn exploit-btn', onclick: runGlobalExploitSearch },
          ['\u{1F4A3} ' + t('vulns.searchExploits')]),
      ]),
    ]);
    grid.appendChild(wrapper);
    return;
  }

  page.forEach((v, i) => {
    const exploitChips = buildExploitChips(v);
    const card = el('div', {
      class:   `vuln-card exploit-card ${v.is_active === 0 ? 'inactive' : ''}`,
      style:   `animation-delay:${i * 0.03}s;cursor:pointer`,
      onclick: (e) => {
        if (e.target.closest('a, .vuln-ref-chips, .vuln-exploit-chips')) return;
        showCVEDetails(v.vuln_id);
      },
    }, [
      el('div', { class: 'vuln-card-header' }, [
        el('div', { class: 'vuln-card-title' }, [
          el('span', { class: 'vuln-tag exploit' }, ['\u{1F4A3}']),
          el('span', { class: 'vuln-id' }, [v.vuln_id || 'N/A']),
          el('span', { class: `severity-badge severity-${v.severity}` }, [v.severity || '?']),
          el('span', { class: 'cvss-pill' }, [`CVSS ${parseFloat(v.cvss_score || 0).toFixed(1)}`]),
          ...(v.is_kev     ? [el('span', { class: 'vuln-tag kev' }, ['KEV'])] : []),
          ...(v.epss > 0.1 ? [el('span', { class: 'vuln-tag epss' }, [`EPSS ${(v.epss * 100).toFixed(1)}%`])] : []),
        ]),
        el('span', { style: 'font-size:.72rem;opacity:.35' }, ['\u{1F4CB} ' + t('vulns.clickDetails')]),
      ]),
      el('div', { class: 'vuln-meta' }, [metaItem('IP', v.ip), metaItem('Host', v.hostname), metaItem('Port', v.port)]),
      el('div', { style: 'font-size:.83rem;opacity:.7;margin:6px 0 8px' }, [
        (v.description || '').substring(0, 180) + ((v.description || '').length > 180 ? '\u2026' : ''),
      ]),
      buildRefChips(v.vuln_id),
      ...(exploitChips ? [exploitChips] : []),
    ]);
    grid.appendChild(card);
  });
}

/* ════════════════════════════════════════
   HISTORY VIEW
═══════════════════════════════════════ */
async function toggleHistory() {
  const btn = $('#vuln-history-btn');
  if (historyMode) {
    historyMode = false;
    if (btn) btn.classList.remove('active');
    await fetchVulnerabilities();
    return;
  }
  historyMode = true;
  if (btn) btn.classList.add('active');
  try {
    const data = await api.get('/vulnerabilities/history?limit=500', { timeout: 10000 });
    allHistory = data?.history || [];
    historyPage = 1; historySearch = '';
    renderHistory();
  } catch (err) {
    console.warn(`[${PAGE}]`, err.message);
  }
}

function renderHistory() {
  const grid = $('#vuln-grid'); const pagDiv = $('#vuln-pagination');
  if (!grid) return;
  empty(grid); if (pagDiv) empty(pagDiv);

  const needle   = historySearch.toLowerCase();
  const filtered = allHistory.filter(e => !needle || `${e.vuln_id} ${e.ip} ${e.hostname}`.toLowerCase().includes(needle));
  const hTotal   = Math.max(1, Math.ceil(filtered.length / ITEMS_PER_PAGE));
  if (historyPage > hTotal) historyPage = 1;

  grid.appendChild(el('div', { style: 'margin-bottom:12px' }, [
    el('input', {
      type: 'text', class: 'global-search-input', value: historySearch,
      placeholder: t('vulns.filterHistory'),
      oninput: (e) => { historySearch = e.target.value; historyPage = 1; renderHistory(); },
      style: 'width:100%;max-width:360px',
    }),
  ]));

  if (!filtered.length) { grid.appendChild(emptyState(t('vulns.noHistory'))); return; }

  filtered.slice((historyPage - 1) * ITEMS_PER_PAGE, historyPage * ITEMS_PER_PAGE).forEach((entry, i) => {
    grid.appendChild(el('div', { class: 'vuln-card', style: `animation-delay:${i * 0.02}s` }, [
      el('div', { class: 'vuln-card-header' }, [
        el('span', { class: 'vuln-id' }, [entry.vuln_id || 'N/A']),
        el('span', { class: 'vuln-tag' }, [entry.event || '']),
      ]),
      el('div', { class: 'vuln-meta' }, [
        metaItem(t('common.date'), entry.seen_at ? new Date(entry.seen_at).toLocaleString() : t('vulns.na')),
        metaItem(t('common.ip'), entry.ip), metaItem(t('common.host'), entry.hostname),
        metaItem(t('common.port'), entry.port), metaItem(t('common.mac'), entry.mac_address),
      ]),
    ]));
  });

  if (pagDiv && hTotal > 1) {
    pagDiv.appendChild(pageBtn(t('webenum.prev'), historyPage > 1, () => { historyPage--; renderHistory(); }));
    for (let i = Math.max(1, historyPage - 2); i <= Math.min(hTotal, historyPage + 2); i++) {
      pagDiv.appendChild(pageBtn(String(i), true, () => { historyPage = i; renderHistory(); }, i === historyPage));
    }
    pagDiv.appendChild(pageBtn(t('webenum.next'), historyPage < hTotal, () => { historyPage++; renderHistory(); }));
    pagDiv.appendChild(el('span', { class: 'vuln-page-info' }, [t('vulns.pageInfo', { page: historyPage, total: hTotal, count: filtered.length })]));
  }
}

/* ════════════════════════════════════════
   CVE DETAIL MODAL
═══════════════════════════════════════ */
async function showCVEDetails(cveId) {
  if (!cveId || modalInFlight === cveId) return;
  modalInFlight = cveId;
  if (poller) poller.stop();

  const titleEl = $('#vuln-modal-title');
  const body    = $('#vuln-modal-body');
  const modal   = $('#vuln-modal');
  const chipsEl = $('#vuln-modal-header-chips');
  if (!modal) { modalInFlight = null; return; }

  if (titleEl) titleEl.textContent = cveId;

  /* reference chips in modal header */
  if (chipsEl) {
    empty(chipsEl);
    const enc = encodeURIComponent(cveId);
    [
      ['\u{1F431} GitHub', `https://github.com/search?q=${enc}&type=repositories`,   'chip-github'],
      ['\u{1F6E1} Rapid7', `https://www.rapid7.com/db/?q=${enc}`,                    'chip-rapid7'],
      ['NVD \u2197',       `https://nvd.nist.gov/vuln/detail/${enc}`,                'chip-nvd'],
      ['MITRE \u2197',     `https://cve.mitre.org/cgi-bin/cvename.cgi?name=${enc}`,  'chip-mitre'],
    ].forEach(([label, href, cls]) => chipsEl.appendChild(refChip(label, href, cls)));
  }

  if (body) { empty(body); body.appendChild(el('div', { class: 'page-loading' }, [t('common.loading')])); }
  modal.classList.add('show');

  try {
    const data = await api.get(`/api/cve/${encodeURIComponent(cveId)}`, { timeout: 10000 });
    if (!body) return;
    empty(body);

    if (data.description) body.appendChild(modalSection(t('vulns.description'), data.description));
    if (data.cvss) {
      const s = data.cvss;
      body.appendChild(modalSection('CVSS',
        `${t('vulns.score')}: ${s.baseScore || t('vulns.na')} | ${t('vulns.severity')}: ${s.baseSeverity || t('vulns.na')}` +
        (s.vectorString ? ` | Vector: ${s.vectorString}` : '')
      ));
    }
    if (data.is_kev) body.appendChild(modalSection('\u26A0 ' + t('vulns.cisaKev'), t('vulns.cisaKevMsg')));
    if (data.epss)   body.appendChild(modalSection(t('vulns.epss'),
      `${t('vulns.probability')}: ${(data.epss.probability * 100).toFixed(2)}% | ${t('vulns.percentile')}: ${(data.epss.percentile * 100).toFixed(2)}%`
    ));

    /* Affected */
    if (data.affected && data.affected.length > 0) {
      const rows = normalizeAffected(data.affected);
      body.appendChild(el('div', { class: 'modal-detail-section' }, [
        el('div', { class: 'modal-section-title' }, [t('vulns.affectedProducts')]),
        el('div', { class: 'vuln-affected-table' }, [
          el('div', { class: 'vuln-affected-row header' }, [el('span', {}, [t('common.vendor')]), el('span', {}, [t('vulns.product')]), el('span', {}, [t('vulns.versions')])]),
          ...rows.map(r => el('div', { class: 'vuln-affected-row' }, [el('span', {}, [r.vendor]), el('span', {}, [r.product]), el('span', {}, [r.versions])])),
        ]),
      ]));
    }

    /* Exploits section */
    const exploits = data.exploits || [];
    const exploitSection = el('div', { class: 'modal-detail-section' }, [
      el('div', { class: 'modal-section-title' }, ['\u{1F4A3} ' + t('vulns.exploitsRefs')]),
      /* dynamic entries from DB */
      ...exploits.map(entry => {
        const isStr = typeof entry === 'string';
        const label = isStr ? entry : (entry.title || entry.url || 'Exploit');
        const href  = isStr
          ? (entry.startsWith('http') ? entry : `https://www.exploit-db.com/exploits/${entry}`)
          : (entry.url || '#');
        return el('div', { class: 'modal-exploit-item' }, [
          refChip('\u26A1 ' + String(label).substring(0, 120), href, 'chip-exploit chip-exploit-detail'),
        ]);
      }),
      /* always-present search chips row */
      el('div', { class: 'exploit-links-block', style: 'margin-top:10px;display:flex;flex-wrap:wrap;gap:6px' }, [
        refChip('\u{1F50D} ExploitDB',  `https://www.exploit-db.com/search?cve=${encodeURIComponent(cveId)}`, 'chip-exploit chip-exploitdb'),
        refChip('\u{1F431} GitHub PoC', `https://github.com/search?q=${encodeURIComponent(cveId)}&type=repositories`, 'chip-github'),
        refChip('\u{1F6E1} Rapid7',     `https://www.rapid7.com/db/?q=${encodeURIComponent(cveId)}`, 'chip-rapid7'),
        refChip('NVD \u2197',           `https://nvd.nist.gov/vuln/detail/${encodeURIComponent(cveId)}`, 'chip-nvd'),
        refChip('MITRE \u2197',         `https://cve.mitre.org/cgi-bin/cvename.cgi?name=${encodeURIComponent(cveId)}`, 'chip-mitre'),
      ]),
      exploits.length === 0
        ? el('div', { style: 'opacity:.45;font-size:.8rem;margin-top:6px' }, [t('vulns.noExploitRecords')])
        : null,
    ].filter(Boolean));
    body.appendChild(exploitSection);

    /* References */
    if (data.references && data.references.length > 0) {
      body.appendChild(el('div', { class: 'modal-detail-section' }, [
        el('div', { class: 'modal-section-title' }, [t('vulns.references')]),
        ...data.references.map(url => el('div', {}, [
          el('a', { href: url, target: '_blank', rel: 'noopener', class: 'vuln-ref-link' }, [url]),
        ])),
      ]));
    }

    if (data.lastModified) body.appendChild(modalSection(t('vulns.lastModified'), formatDate(data.lastModified)));
    if (!data.description && !data.cvss && !data.affected) {
      body.appendChild(el('div', { style: 'opacity:.6;padding:20px;text-align:center' }, [t('vulns.noEnrichment')]));
    }
  } catch (err) {
    if (body) { empty(body); body.appendChild(el('div', { style: 'color:var(--danger);padding:20px' }, [`Failed: ${err.message}`])); }
  } finally {
    modalInFlight = null;
  }
}

function normalizeAffected(affected) {
  return affected.map(item => {
    const vendor = item.vendor || item.vendor_name || item.vendorName || 'N/A';
    let product  = item.product || item.product_name || item.productName || 'N/A';
    if (Array.isArray(product)) product = product.join(', ');
    else if (typeof product === 'object' && product !== null)
      product = product.product || product.product_name || product.productName || 'N/A';
    let versions = 'unspecified';
    if (Array.isArray(item.versions)) {
      versions = item.versions.map(ver => {
        if (typeof ver === 'string') return ver;
        const parts = [ver.version || ver.versionName || ver.version_value || ''];
        if (ver.lessThan)        parts.push(`< ${ver.lessThan}`);
        if (ver.lessThanOrEqual) parts.push(`<= ${ver.lessThanOrEqual}`);
        if (ver.status)          parts.push(`(${ver.status})`);
        return parts.join(' ');
      }).join('; ');
    } else if (typeof item.versions === 'string') {
      versions = item.versions;
    }
    return { vendor, product: String(product), versions };
  });
}

/* ════════════════════════════════════════
   GLOBAL EXPLOIT SEARCH (alias for feed sync from exploits view)
═══════════════════════════════════════ */
async function runGlobalExploitSearch() {
  await runFeedSync();
}

/* ════════════════════════════════════════
   SEARCH / FILTER / SORT HANDLERS
═══════════════════════════════════════ */
function onSearch(e) {
  if (searchDebounce != null) {
    if (tracker) tracker.clearTrackedTimeout(searchDebounce);
    else clearTimeout(searchDebounce);
  }
  const handler = () => {
    searchTerm = e.target.value; currentPage = 1; filterAndRender();
    const b = e.target.nextElementSibling; if (b) b.classList.toggle('show', searchTerm.length > 0);
    searchDebounce = null;
  };
  searchDebounce = tracker ? tracker.trackTimeout(handler, 300) : setTimeout(handler, 300);
}
function clearSearch() {
  const inp = $('#vuln-search'); if (inp) inp.value = '';
  searchTerm = ''; currentPage = 1; filterAndRender();
  const b = $('#vuln-search')?.nextElementSibling; if (b) b.classList.remove('show');
}
function switchView(view) {
  currentView = view; currentPage = 1;
  ['cve','host','exploits'].forEach(v => { const b = $(`#vuln-view-${v}`); if (b) b.classList.toggle('active', v === view); });
  filterAndRender();
}
function toggleActiveFilter() {
  showActiveOnly = !showActiveOnly;
  const b = $('#vuln-active-toggle'); if (b) b.classList.toggle('active', showActiveOnly);
  currentPage = 1; filterAndRender();
}
function toggleSeverity(sev, btn) {
  if (severityFilters.has(sev)) { severityFilters.delete(sev); btn.classList.remove('active'); }
  else                           { severityFilters.add(sev);    btn.classList.add('active'); }
  currentPage = 1; filterAndRender();
}
function onSortChange() {
  const f = $('#vuln-sort-field'); const d = $('#vuln-sort-dir');
  if (f) sortField = f.value; if (d) sortDir = d.value;
  currentPage = 1; filterAndRender();
}
function onDateChange() {
  dateFrom = ($('#vuln-date-from') || {}).value || '';
  dateTo   = ($('#vuln-date-to')   || {}).value || '';
  currentPage = 1; filterAndRender();
}
function clearDateFilter() {
  dateFrom = ''; dateTo = '';
  const f = $('#vuln-date-from'); const t_ = $('#vuln-date-to');
  if (f) f.value = ''; if (t_) t_.value = '';
  currentPage = 1; filterAndRender();
}
function toggleHostCard(id) {
  if (expandedHosts.has(id)) expandedHosts.delete(id); else expandedHosts.add(id);
  const card = document.querySelector(`.vuln-card[data-id="${id}"]`);
  if (card) card.classList.toggle('expanded');
}

/* ════════════════════════════════════════
   PAGINATION
═══════════════════════════════════════ */
function renderPagination() {
  const pag = $('#vuln-pagination'); if (!pag) return;
  empty(pag);
  if (historyMode || totalPages <= 1) return;
  pag.appendChild(pageBtn(t('webenum.prev'), currentPage > 1, () => changePage(currentPage - 1)));
  for (let i = Math.max(1, currentPage - 2); i <= Math.min(totalPages, currentPage + 2); i++)
    pag.appendChild(pageBtn(String(i), true, () => changePage(i), i === currentPage));
  pag.appendChild(pageBtn(t('webenum.next'), currentPage < totalPages, () => changePage(currentPage + 1)));
  pag.appendChild(el('span', { class: 'vuln-page-info' }, [t('vulns.resultsInfo', { page: currentPage, total: totalPages, count: filteredVulns.length })]));
}
function pageBtn(label, enabled, onclick, active = false) {
  return el('button', {
    class: `vuln-page-btn ${active ? 'active' : ''} ${!enabled ? 'disabled' : ''}`,
    onclick: enabled ? onclick : null, disabled: !enabled,
  }, [label]);
}
function changePage(p) {
  currentPage = Math.max(1, Math.min(totalPages, p)); filterAndRender();
  const g = $('#vuln-grid'); if (g) g.scrollTop = 0;
}

/* ════════════════════════════════════════
   EXPORT
═══════════════════════════════════════ */
function csvCell(val) {
  const s = String(val ?? '');
  const safe = /^[=+\-@\t\r]/.test(s) ? `'${s}` : s;
  return safe.includes(',') || safe.includes('"') || safe.includes('\n') ? `"${safe.replace(/"/g, '""')}"` : safe;
}
function exportCSV() {
  const data = filteredVulns.length ? filteredVulns : vulnerabilities;
  if (!data.length) return;
  const rows = [['CVE ID','IP','Hostname','Port','Severity','CVSS','Status','First Seen','Last Seen','KEV','Has Exploit','EPSS'].join(',')];
  data.forEach(v => rows.push([
    v.vuln_id, v.ip, v.hostname, v.port, v.severity,
    v.cvss_score != null ? parseFloat(v.cvss_score).toFixed(1) : '',
    v.is_active === 1 ? 'Active' : 'Remediated',
    v.first_seen, v.last_seen,
    v.is_kev      ? 'Yes' : 'No',
    v.has_exploit ? 'Yes' : 'No',
    v.epss != null ? (v.epss * 100).toFixed(2) + '%' : '',
  ].map(csvCell).join(',')));
  downloadBlob(rows.join('\n'), `vulnerabilities_${isoDate()}.csv`, 'text/csv');
}
function exportJSON() {
  const data = filteredVulns.length ? filteredVulns : vulnerabilities;
  if (!data.length) return;
  downloadBlob(JSON.stringify(data, null, 2), `vulnerabilities_${isoDate()}.json`, 'application/json');
}
function downloadBlob(content, filename, type) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const a = document.createElement('a'); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
}

/* ════════════════════════════════════════
   MODAL CLOSE
═══════════════════════════════════════ */
function closeModal() {
  const modal = $('#vuln-modal'); if (modal) modal.classList.remove('show');
  modalInFlight = null;
  if (poller) poller.start(); // resume polling
}
function onModalBackdrop(e) { if (e.target.classList.contains('vuln-modal')) closeModal(); }

/* ════════════════════════════════════════
   HELPERS
═══════════════════════════════════════ */
function metaItem(label, value) {
  return el('div', { class: 'meta-item' }, [
    el('span', { class: 'meta-label' }, [label + ':']),
    el('span', { class: 'meta-value' }, [String(value ?? t('vulns.na'))]),
  ]);
}
function modalSection(title, text) {
  return el('div', { class: 'modal-detail-section' }, [
    el('div', { class: 'modal-section-title' }, [title]),
    el('div', { class: 'modal-section-text' }, [String(text)]),
  ]);
}
function emptyState(msg) {
  return el('div', { style: 'text-align:center;color:var(--ink);opacity:.5;padding:40px' }, [
    el('div', { style: 'font-size:3rem;margin-bottom:16px;opacity:.5' }, ['\u{1F50D}']),
    msg,
  ]);
}
function sevPill(sev, count) {
  return el('span', { class: `severity-badge severity-${sev}` }, [`${count} ${sev}`]);
}
function formatDate(d) {
  if (!d) return t('vulns.unknown');
  try { return new Date(d).toLocaleString('en-US', { year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' }); }
  catch { return String(d); }
}
function isoDate()  { return new Date().toISOString().split('T')[0]; }
function countSeverities(vulns) {
  const c = { critical: 0, high: 0, medium: 0, low: 0 };
  vulns.forEach(v => { if (v.is_active === 1 && c[v.severity] !== undefined) c[v.severity]++; });
  return c;
}
function sortVulnsByPriority(vulns) {
  return [...vulns].sort((a, b) => {
    if (a.is_active !== b.is_active) return b.is_active - a.is_active;
    return (SEVERITY_ORDER[b.severity] || 0) - (SEVERITY_ORDER[a.severity] || 0);
  });
}