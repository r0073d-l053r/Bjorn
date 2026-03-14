/**
 * Web Enum page module.
 * Displays web enumeration/directory brute-force results with filtering,
 * sorting, pagination, detail modal, and JSON/CSV export.
 * Endpoint: GET /api/webenum/results?page=N&limit=M
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api } from '../core/api.js';
import { el, $, $$, empty } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'web-enum';
const MAX_PAGES_FETCH = 200;
const FETCH_LIMIT = 500;
const PER_PAGE_OPTIONS = [25, 50, 100, 250, 500, 0]; // 0 = All
const ANSI_RE = /[\x00-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]/g;

/* ── state ── */
let tracker = null;
let allData = [];
let filteredData = [];
let currentPage = 1;
let itemsPerPage = 50;
let sortField = 'scan_date';
let sortDirection = 'desc';
let exactStatusFilter = null;
let serverTotal = 0;
let fetchedLimit = false;

/* filter state */
let searchText = '';
let filterHost = '';
let filterStatusFamily = '';
let filterPort = '';
let filterDate = '';
let searchDebounceId = null;

/* ── lifecycle ── */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  container.appendChild(buildShell());
  await fetchAllData();
}

export function unmount() {
  if (searchDebounceId != null) {
    if (tracker) tracker.clearTrackedTimeout(searchDebounceId);
    else clearTimeout(searchDebounceId);
    searchDebounceId = null;
  }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  allData = [];
  filteredData = [];
  currentPage = 1;
  itemsPerPage = 50;
  sortField = 'scan_date';
  sortDirection = 'desc';
  exactStatusFilter = null;
  serverTotal = 0;
  fetchedLimit = false;
  searchText = '';
  filterHost = '';
  filterStatusFamily = '';
  filterPort = '';
  filterDate = '';
}

/* ══════════════════════════════════════════════════════════════
   Shell
   ══════════════════════════════════════════════════════════════ */
function buildShell() {
  return el('div', { class: 'webenum-container' }, [
    /* stats bar */
    el('div', { class: 'stats-bar', id: 'we-stats' }, [
      statItem('we-stat-total', t('webenum.totalResults')),
      statItem('we-stat-hosts', t('webenum.uniqueHosts')),
      statItem('we-stat-success', t('webenum.successCount')),
      statItem('we-stat-errors', t('webenum.errorCount')),
    ]),
    /* controls row */
    el('div', { class: 'webenum-controls' }, [
      /* text search */
      el('div', { class: 'global-search-container' }, [
        el('input', {
          type: 'text', class: 'global-search-input', id: 'we-search',
          placeholder: t('webenum.searchPlaceholder'),
          oninput: onSearchInput,
        }),
        el('button', { class: 'clear-global-button', onclick: clearSearch }, ['\u2716']),
      ]),
      el('div', { class: 'webenum-main-actions' }, [
        el('button', { class: 'vuln-btn', onclick: () => fetchAllData() }, [t('common.refresh')]),
      ]),
      /* dropdown filters */
      el('div', { class: 'webenum-filters' }, [
        buildSelect('we-filter-host', t('webenum.allHosts'), onHostFilter),
        buildSelect('we-filter-status', t('webenum.allStatus'), onStatusFamilyFilter),
        buildSelect('we-filter-port', t('webenum.allPorts'), onPortFilter),
        el('input', {
          type: 'date', class: 'webenum-date-input', id: 'we-filter-date',
          onchange: onDateFilter,
        }),
      ]),
      /* export buttons */
      el('div', { class: 'webenum-export-btns' }, [
        el('button', { class: 'vuln-btn', onclick: () => exportData('json') }, [t('webenum.exportJson')]),
        el('button', { class: 'vuln-btn', onclick: () => exportData('csv') }, [t('webenum.exportCsv')]),
      ]),
    ]),
    /* status legend chips */
    el('div', { class: 'webenum-status-legend', id: 'we-status-legend' }),
    /* table container */
    el('div', { class: 'webenum-table-wrap', id: 'we-table-wrap' }),
    /* pagination */
    el('div', { class: 'webenum-pagination', id: 'we-pagination' }),
    /* detail modal */
    el('div', { class: 'vuln-modal', id: 'we-modal', onclick: onModalBackdrop }, [
      el('div', { class: 'vuln-modal-content' }, [
        el('div', { class: 'vuln-modal-header' }, [
          el('span', { class: 'vuln-modal-title', id: 'we-modal-title' }),
          el('button', { class: 'vuln-modal-close', onclick: closeModal }, ['\u2716']),
        ]),
        el('div', { class: 'vuln-modal-body', id: 'we-modal-body' }),
      ]),
    ]),
  ]);
}

function statItem(id, label) {
  return el('div', { class: 'stat-item' }, [
    el('span', { class: 'stat-value', id }, ['0']),
    el('span', { class: 'stat-label' }, [label]),
  ]);
}

function buildSelect(id, defaultLabel, handler) {
  return el('select', { class: 'webenum-filter-select', id, onchange: handler }, [
    el('option', { value: '' }, [defaultLabel]),
  ]);
}

/* ══════════════════════════════════════════════════════════════
   Data fetching — paginate through all server pages
   ══════════════════════════════════════════════════════════════ */
async function fetchAllData() {
  const loading = $('#we-table-wrap');
  if (loading) {
    empty(loading);
    loading.appendChild(el('div', { class: 'page-loading' }, [t('common.loading')]));
  }

  const ac = tracker ? tracker.trackAbortController() : null;
  const signal = ac ? ac.signal : undefined;

  let accumulated = [];
  let page = 1;
  serverTotal = 0;
  fetchedLimit = false;

  try {
    while (page <= MAX_PAGES_FETCH) {
      const url = `/api/webenum/results?page=${page}&limit=${FETCH_LIMIT}`;
      const data = await api.get(url, { signal, timeout: 15000 });

      const results = Array.isArray(data.results) ? data.results : [];
      if (data.total != null) serverTotal = data.total;

      if (results.length === 0) break;

      accumulated = accumulated.concat(results);

      /* all fetched */
      if (serverTotal > 0 && accumulated.length >= serverTotal) break;
      /* page was not full — last page */
      if (results.length < FETCH_LIMIT) break;

      page++;
    }

    if (page > MAX_PAGES_FETCH) fetchedLimit = true;
  } catch (err) {
    if (err.name === 'AbortError' || (err.name === 'ApiError' && err.message === 'Aborted')) return;
    console.warn(`[${PAGE}] fetch error:`, err.message);
  } finally {
    if (ac && tracker) tracker.removeAbortController(ac);
  }

  if (!tracker) return; /* unmounted while fetching */
  allData = accumulated.map(normalizeRow);
  populateFilterDropdowns();
  applyFilters();
}

/* ── row normalization ── */
function normalizeRow(row) {
  const host = (row.host || row.hostname || '').toString();
  let directory = (row.directory || '').toString().replace(ANSI_RE, '');
  return {
    id: row.id,
    host: host,
    ip: (row.ip || '').toString(),
    mac: (row.mac || '').toString(),
    port: row.port != null ? Number(row.port) : 0,
    directory: directory,
    status: row.status != null ? Number(row.status) : 0,
    size: row.size != null ? Number(row.size) : 0,
    scan_date: row.scan_date || '',
    response_time: row.response_time != null ? Number(row.response_time) : 0,
    content_type: (row.content_type || '').toString(),
  };
}

/* ══════════════════════════════════════════════════════════════
   Filter dropdowns — populate from unique values
   ══════════════════════════════════════════════════════════════ */
function populateFilterDropdowns() {
  populateSelect('we-filter-host', t('webenum.allHosts'),
    [...new Set(allData.map(r => r.host).filter(Boolean))].sort());

  const families = [...new Set(allData.map(r => statusFamily(r.status)).filter(Boolean))].sort();
  populateSelect('we-filter-status', t('webenum.allStatus'), families);

  const ports = [...new Set(allData.map(r => r.port).filter(p => p > 0))].sort((a, b) => a - b);
  populateSelect('we-filter-port', t('webenum.allPorts'), ports.map(String));
}

function populateSelect(id, defaultLabel, options) {
  const sel = $(`#${id}`);
  if (!sel) return;
  const current = sel.value;
  empty(sel);
  sel.appendChild(el('option', { value: '' }, [defaultLabel]));
  options.forEach(opt => {
    sel.appendChild(el('option', { value: opt }, [opt]));
  });
  if (current && options.includes(current)) sel.value = current;
}

/* ══════════════════════════════════════════════════════════════
   Filter & sort pipeline
   ══════════════════════════════════════════════════════════════ */
function applyFilters() {
  const needle = searchText.toLowerCase();

  filteredData = allData.filter(row => {
    /* exact status chip filter */
    if (exactStatusFilter != null && row.status !== exactStatusFilter) return false;

    /* text search */
    if (needle) {
      const hay = `${row.host} ${row.ip} ${row.directory} ${row.status}`.toLowerCase();
      if (!hay.includes(needle)) return false;
    }

    /* host dropdown */
    if (filterHost && row.host !== filterHost) return false;

    /* status family dropdown */
    if (filterStatusFamily && statusFamily(row.status) !== filterStatusFamily) return false;

    /* port dropdown */
    if (filterPort && String(row.port) !== filterPort) return false;

    /* date filter */
    if (filterDate) {
      const rowDate = (row.scan_date || '').substring(0, 10);
      if (rowDate !== filterDate) return false;
    }

    return true;
  });

  applySort();
  currentPage = 1;
  updateStats();
  renderStatusLegend();
  renderTable();
  renderPagination();
}

function applySort() {
  const dir = sortDirection === 'asc' ? 1 : -1;
  const field = sortField;

  filteredData.sort((a, b) => {
    let va = a[field];
    let vb = b[field];

    if (va == null) va = '';
    if (vb == null) vb = '';

    if (typeof va === 'number' && typeof vb === 'number') {
      return (va - vb) * dir;
    }

    /* date string comparison */
    if (field === 'scan_date') {
      const da = new Date(va).getTime() || 0;
      const db = new Date(vb).getTime() || 0;
      return (da - db) * dir;
    }

    return String(va).localeCompare(String(vb)) * dir;
  });
}

/* ══════════════════════════════════════════════════════════════
   Stats bar
   ══════════════════════════════════════════════════════════════ */
function updateStats() {
  const totalLabel = fetchedLimit
    ? `${filteredData.length} (truncated)`
    : String(filteredData.length);
  setStatVal('we-stat-total', totalLabel);
  setStatVal('we-stat-hosts', new Set(filteredData.map(r => r.host || r.ip)).size);
  setStatVal('we-stat-success', filteredData.filter(r => r.status >= 200 && r.status < 300).length);
  setStatVal('we-stat-errors', filteredData.filter(r => r.status >= 400).length);
}

function setStatVal(id, val) {
  const e = $(`#${id}`);
  if (e) e.textContent = String(val);
}

/* ══════════════════════════════════════════════════════════════
   Status legend chips
   ══════════════════════════════════════════════════════════════ */
function renderStatusLegend() {
  const container = $('#we-status-legend');
  if (!container) return;
  empty(container);

  /* gather unique status codes from current allData (unfiltered view) */
  const codes = [...new Set(allData.map(r => r.status))].sort((a, b) => a - b);
  if (codes.length === 0) return;

  codes.forEach(code => {
    const count = allData.filter(r => r.status === code).length;
    const isActive = exactStatusFilter === code;
    const chip = el('span', {
      class: `webenum-status-chip ${statusClass(code)} ${isActive ? 'active' : ''}`,
      onclick: () => {
        if (exactStatusFilter === code) {
          exactStatusFilter = null;
        } else {
          exactStatusFilter = code;
        }
        /* clear active class on all chips, re-apply via full filter cycle */
        $$('.webenum-status-chip', container).forEach(c => c.classList.remove('active'));
        applyFilters();
      },
    }, [`${code} (${count})`]);
    container.appendChild(chip);
  });
}

/* ══════════════════════════════════════════════════════════════
   Table rendering
   ══════════════════════════════════════════════════════════════ */
function renderTable() {
  const wrap = $('#we-table-wrap');
  if (!wrap) return;
  empty(wrap);

  if (filteredData.length === 0) {
    wrap.appendChild(emptyState(t('webenum.noResults')));
    return;
  }

  /* current page slice */
  const pageData = getPageSlice();

  /* column definitions */
  const columns = [
    { key: 'host', label: t('webenum.host') },
    { key: 'ip', label: t('webenum.ip') },
    { key: 'port', label: t('webenum.port') },
    { key: 'directory', label: t('webenum.directory') },
    { key: 'status', label: t('webenum.status') },
    { key: 'size', label: t('webenum.size') },
    { key: 'scan_date', label: t('webenum.scanDate') },
    { key: '_actions', label: t('webenum.actions') },
  ];

  /* thead */
  const headerCells = columns.map(col => {
    if (col.key === '_actions') {
      return el('th', {}, [col.label]);
    }
    const isSorted = sortField === col.key;
    const arrow = isSorted ? (sortDirection === 'asc' ? ' \u25B2' : ' \u25BC') : '';
    return el('th', {
      class: `sortable ${isSorted ? 'sort-' + sortDirection : ''}`,
      style: 'cursor:pointer;user-select:none;',
      onclick: () => onSortColumn(col.key),
    }, [col.label + arrow]);
  });

  const thead = el('thead', {}, [el('tr', {}, headerCells)]);

  /* tbody */
  const rows = pageData.map(row => {
    const url = buildUrl(row);
    return el('tr', {
      class: 'webenum-row',
      style: 'cursor:pointer;',
      onclick: (e) => {
        /* ignore if click was on an anchor */
        if (e.target.tagName === 'A') return;
        showDetailModal(row);
      },
    }, [
      el('td', {}, [row.host || '-']),
      el('td', {}, [row.ip || '-']),
      el('td', {}, [row.port ? String(row.port) : '-']),
      el('td', { class: 'webenum-dir-cell', title: row.directory }, [row.directory || '/']),
      el('td', {}, [statusBadge(row.status)]),
      el('td', {}, [formatSize(row.size)]),
      el('td', {}, [formatDate(row.scan_date)]),
      el('td', {}, [
        url
          ? el('a', {
              href: url, target: '_blank', rel: 'noopener noreferrer',
              class: 'webenum-link', title: url,
              onclick: (e) => e.stopPropagation(),
            }, [t('webenum.open')])
          : el('span', { class: 'muted' }, ['-']),
      ]),
    ]);
  });

  const tbody = el('tbody', {}, rows);
  const table = el('table', { class: 'webenum-table' }, [thead, tbody]);
  wrap.appendChild(el('div', { class: 'table-inner' }, [table]));
}

function getPageSlice() {
  if (itemsPerPage === 0) return filteredData; // All
  const start = (currentPage - 1) * itemsPerPage;
  return filteredData.slice(start, start + itemsPerPage);
}

function getTotalPages() {
  if (itemsPerPage === 0) return 1;
  return Math.max(1, Math.ceil(filteredData.length / itemsPerPage));
}

/* ══════════════════════════════════════════════════════════════
   Pagination
   ══════════════════════════════════════════════════════════════ */
function renderPagination() {
  const pag = $('#we-pagination');
  if (!pag) return;
  empty(pag);

  const total = getTotalPages();

  /* per-page selector */
  const perPageSel = el('select', { class: 'webenum-filter-select webenum-perpage', onchange: onPerPageChange }, []);
  PER_PAGE_OPTIONS.forEach(n => {
    const label = n === 0 ? t('common.all') : String(n);
    const opt = el('option', { value: String(n) }, [label]);
    if (n === itemsPerPage) opt.selected = true;
    perPageSel.appendChild(opt);
  });
  pag.appendChild(el('div', { class: 'webenum-perpage-wrap' }, [
    el('span', { class: 'stat-label' }, [t('webenum.perPage')]),
    perPageSel,
  ]));

  if (total <= 1 && itemsPerPage !== 0) {
    pag.appendChild(el('span', { class: 'vuln-page-info' }, [
      t('webenum.resultCount', { count: filteredData.length }),
    ]));
    return;
  }

  if (itemsPerPage === 0) {
    pag.appendChild(el('span', { class: 'vuln-page-info' }, [
      t('webenum.showingAll', { count: filteredData.length }),
    ]));
    return;
  }

  /* Prev */
  pag.appendChild(pageBtn(t('webenum.prev'), currentPage > 1, () => changePage(currentPage - 1)));

  /* numbered buttons */
  const start = Math.max(1, currentPage - 2);
  const end = Math.min(total, start + 4);
  for (let i = start; i <= end; i++) {
    pag.appendChild(pageBtn(String(i), true, () => changePage(i), i === currentPage));
  }

  /* Next */
  pag.appendChild(pageBtn(t('webenum.next'), currentPage < total, () => changePage(currentPage + 1)));

  /* info */
  pag.appendChild(el('span', { class: 'vuln-page-info' }, [
    t('webenum.pageInfo', { current: currentPage, total, count: filteredData.length }),
  ]));
}

function pageBtn(label, enabled, onclick, active = false) {
  return el('button', {
    class: `vuln-page-btn ${active ? 'active' : ''} ${!enabled ? 'disabled' : ''}`,
    onclick: enabled ? onclick : null,
    disabled: !enabled,
  }, [label]);
}

function changePage(p) {
  const total = getTotalPages();
  currentPage = Math.max(1, Math.min(total, p));
  renderTable();
  renderPagination();
  const wrap = $('#we-table-wrap');
  if (wrap) wrap.scrollTop = 0;
}

function onPerPageChange(e) {
  itemsPerPage = parseInt(e.target.value, 10);
  currentPage = 1;
  renderTable();
  renderPagination();
}

/* ══════════════════════════════════════════════════════════════
   Sort handler
   ══════════════════════════════════════════════════════════════ */
function onSortColumn(key) {
  if (sortField === key) {
    sortDirection = sortDirection === 'asc' ? 'desc' : 'asc';
  } else {
    sortField = key;
    sortDirection = 'asc';
  }
  applySort();
  renderTable();
  renderPagination();
}

/* ══════════════════════════════════════════════════════════════
   Filter handlers
   ══════════════════════════════════════════════════════════════ */
function onSearchInput(e) {
  if (searchDebounceId != null) {
    if (tracker) tracker.clearTrackedTimeout(searchDebounceId);
    else clearTimeout(searchDebounceId);
  }
  const val = e.target.value;
  searchDebounceId = tracker
    ? tracker.trackTimeout(() => {
        searchText = val;
        applyFilters();
        const btn = e.target.nextElementSibling;
        if (btn) btn.classList.toggle('show', val.length > 0);
      }, 300)
    : setTimeout(() => {
        searchText = val;
        applyFilters();
      }, 300);
}

function clearSearch() {
  const inp = $('#we-search');
  if (inp) inp.value = '';
  searchText = '';
  applyFilters();
  const btn = inp ? inp.nextElementSibling : null;
  if (btn) btn.classList.remove('show');
}

function onHostFilter(e) {
  filterHost = e.target.value;
  applyFilters();
}

function onStatusFamilyFilter(e) {
  filterStatusFamily = e.target.value;
  /* clear exact chip filter when dropdown changes */
  exactStatusFilter = null;
  applyFilters();
}

function onPortFilter(e) {
  filterPort = e.target.value;
  applyFilters();
}

function onDateFilter(e) {
  filterDate = e.target.value || '';
  applyFilters();
}

/* ══════════════════════════════════════════════════════════════
   Detail modal
   ══════════════════════════════════════════════════════════════ */
function showDetailModal(row) {
  const modal = $('#we-modal');
  const title = $('#we-modal-title');
  const body = $('#we-modal-body');
  if (!modal || !title || !body) return;

  const url = buildUrl(row);

  title.textContent = `${row.host || row.ip}${row.directory || '/'}`;
  empty(body);

  const fields = [
    ['Host', row.host],
    ['IP', row.ip],
    ['MAC', row.mac],
    ['Port', row.port],
    ['Directory', row.directory],
    ['Status', row.status],
    ['Size', formatSize(row.size)],
    ['Content-Type', row.content_type],
    ['Response Time', row.response_time ? row.response_time + ' ms' : '-'],
    ['Scan Date', formatDate(row.scan_date)],
    ['URL', url || 'N/A'],
  ];

  fields.forEach(([label, value]) => {
    body.appendChild(el('div', { class: 'modal-detail-section' }, [
      el('div', { class: 'modal-section-title' }, [label]),
      el('div', { class: 'modal-section-text' }, [
        label === 'Status'
          ? statusBadge(value)
          : String(value != null ? value : '-'),
      ]),
    ]));
  });

  /* action buttons */
  const actions = el('div', { class: 'webenum-modal-actions' }, []);

  if (url) {
    actions.appendChild(el('button', { class: 'vuln-btn', onclick: () => {
      window.open(url, '_blank', 'noopener,noreferrer');
    }}, [t('webenum.openUrl')]));

    actions.appendChild(el('button', { class: 'vuln-btn', onclick: () => {
      copyText(url);
    }}, [t('webenum.copyUrl')]));
  }

  actions.appendChild(el('button', { class: 'vuln-btn', onclick: () => exportSingleResult(row, 'json') }, [t('webenum.exportJson')]));
  actions.appendChild(el('button', { class: 'vuln-btn', onclick: () => exportSingleResult(row, 'csv') }, [t('webenum.exportCsv')]));

  body.appendChild(actions);
  modal.classList.add('show');
}

function closeModal() {
  const modal = $('#we-modal');
  if (modal) modal.classList.remove('show');
}

function onModalBackdrop(e) {
  if (e.target.classList.contains('vuln-modal')) closeModal();
}

/* ══════════════════════════════════════════════════════════════
   Export — JSON & CSV
   ══════════════════════════════════════════════════════════════ */
function exportData(format) {
  const data = filteredData.length > 0 ? filteredData : allData;
  if (data.length === 0) return;

  const dateStr = new Date().toISOString().split('T')[0];

  if (format === 'json') {
    const json = JSON.stringify(data, null, 2);
    downloadBlob(json, `webenum_results_${dateStr}.json`, 'application/json');
  } else {
    const csv = buildCSV(data);
    downloadBlob(csv, `webenum_results_${dateStr}.csv`, 'text/csv');
  }
}

function exportSingleResult(row, format) {
  const dateStr = new Date().toISOString().split('T')[0];
  if (format === 'json') {
    downloadBlob(JSON.stringify(row, null, 2), `webenum_${row.host}_${dateStr}.json`, 'application/json');
  } else {
    downloadBlob(buildCSV([row]), `webenum_${row.host}_${dateStr}.csv`, 'text/csv');
  }
}

function buildCSV(data) {
  const headers = ['Host', 'IP', 'MAC', 'Port', 'Directory', 'Status', 'Size', 'Content-Type', 'Response Time', 'Scan Date', 'URL'];
  const rows = [headers.join(',')];
  data.forEach(r => {
    const url = buildUrl(r) || '';
    const values = [
      r.host, r.ip, r.mac, r.port, r.directory, r.status,
      r.size, r.content_type, r.response_time, r.scan_date, url,
    ].map(v => {
      let s = String(v != null ? v : '');
      /* protect against CSV formula injection */
      if (/^[=+\-@\t\r]/.test(s)) s = `'${s}`;
      return s.includes(',') || s.includes('"') || s.includes('\n')
        ? `"${s.replace(/"/g, '""')}"` : s;
    });
    rows.push(values.join(','));
  });
  return rows.join('\n');
}

function downloadBlob(content, filename, type) {
  const blob = new Blob([content], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/* ══════════════════════════════════════════════════════════════
   Helpers
   ══════════════════════════════════════════════════════════════ */

/** Status family string: '2xx', '3xx', '4xx', '5xx' */
function statusFamily(code) {
  code = Number(code) || 0;
  if (code >= 200 && code < 300) return '2xx';
  if (code >= 300 && code < 400) return '3xx';
  if (code >= 400 && code < 500) return '4xx';
  if (code >= 500) return '5xx';
  return '';
}

/** CSS class for status code */
function statusClass(code) {
  code = Number(code) || 0;
  if (code >= 200 && code < 300) return 'status-2xx';
  if (code >= 300 && code < 400) return 'status-3xx';
  if (code >= 400 && code < 500) return 'status-4xx';
  if (code >= 500) return 'status-5xx';
  return '';
}

/** Status badge element */
function statusBadge(code) {
  return el('span', { class: `webenum-status-badge ${statusClass(code)}` }, [String(code)]);
}

/** Build full URL from row data */
function buildUrl(row) {
  if (!row.host && !row.ip) return '';
  const hostname = row.host || row.ip;
  const port = Number(row.port) || 80;
  const proto = port === 443 ? 'https' : 'http';
  const portPart = (port === 80 || port === 443) ? '' : `:${port}`;
  const dir = row.directory || '/';
  return `${proto}://${hostname}${portPart}${dir}`;
}

/** Format byte size to human-readable */
function formatSize(bytes) {
  bytes = Number(bytes) || 0;
  if (bytes === 0) return '0 B';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB';
}

/** Format date string */
function formatDate(d) {
  if (!d) return '-';
  try {
    const date = new Date(d);
    if (isNaN(date.getTime())) return String(d);
    return date.toLocaleDateString();
  } catch {
    return String(d);
  }
}

/** Empty state */
function emptyState(msg) {
  return el('div', { style: 'text-align:center;color:var(--ink);opacity:.5;padding:40px' }, [
    el('div', { style: 'font-size:3rem;margin-bottom:16px;opacity:.5' }, ['\uD83D\uDD0D']),
    msg,
  ]);
}

/** Copy text to clipboard */
function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  } else {
    fallbackCopy(text);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); } catch { /* noop */ }
  document.body.removeChild(ta);
}
