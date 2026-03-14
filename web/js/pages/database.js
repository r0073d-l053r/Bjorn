/**
 * Database page module — Full SQLite browser.
 * Sidebar tree with tables/views, main content area with table data,
 * inline editing, search/sort/limit, CRUD, CSV/JSON export, danger zone ops.
 * All endpoints under /api/db/*.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { initSharedSidebarLayout } from '../core/sidebar-layout.js';

const PAGE = 'database';

/* ── state ── */
let tracker = null;
let poller = null;
let catalog = [];        // [{ name, type:'table'|'view', columns:[] }]
let activeTable = null;  // name of the selected table/view
let tableData = null;    // { columns:[], rows:[], total:0 }
let dirty = new Map();   // pk → { col: newVal, ... }
let selected = new Set();
let sortCol = null;
let sortDir = 'asc';
let searchText = '';
let rowLimit = 100;
let sidebarFilter = '';
let liveRefresh = false;
let disposeSidebarLayout = null;
let searchDebounce = null;
let loadSequence = 0;

/* ── lifecycle ── */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  const shell = buildShell();
  container.appendChild(shell);
  disposeSidebarLayout = initSharedSidebarLayout(shell, {
    sidebarSelector: '.db-sidebar',
    mainSelector: '.db-main',
    storageKey: 'sidebar:database',
    mobileBreakpoint: 900,
    toggleLabel: t('common.menu'),
    mobileDefaultOpen: true,
  });
  await loadCatalog();
}

export function unmount() {
  clearTimeout(searchDebounce);
  searchDebounce = null;
  if (disposeSidebarLayout) { disposeSidebarLayout(); disposeSidebarLayout = null; }
  if (poller) { poller.stop(); poller = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  catalog = []; activeTable = null; tableData = null;
  dirty = new Map(); selected = new Set();
  sortCol = null; sortDir = 'asc'; searchText = '';
  rowLimit = 100; sidebarFilter = ''; liveRefresh = false;
  loadSequence = 0;
}

/* ── shell ── */
function buildShell() {
  const hideLabel = (() => {
    const v = t('common.hide');
    return v && v !== 'common.hide' ? v : t('common.hide');
  })();
  return el('div', { class: 'db-container page-with-sidebar' }, [
    /* sidebar */
    el('aside', { class: 'db-sidebar page-sidebar', id: 'db-sidebar' }, [
      el('div', { class: 'sidehead' }, [
        el('div', { class: 'sidetitle' }, [t('nav.database')]),
        el('div', { class: 'spacer' }),
        el('button', { class: 'btn', id: 'hideSidebar', 'data-hide-sidebar': '1', type: 'button' }, [hideLabel]),
      ]),
      el('div', { class: 'sidecontent' }, [
        el('div', { class: 'tree-head' }, [
          el('div', { class: 'pill' }, [t('db.tables')]),
          el('div', { class: 'spacer' }),
          el('button', { class: 'btn', type: 'button', onclick: loadCatalog }, [t('common.refresh')]),
        ]),
        el('input', {
          type: 'text', class: 'db-sidebar-filter', placeholder: t('db.filterTables'),
          oninput: onSidebarFilter
        }),
        el('div', { class: 'db-tree', id: 'db-tree' }),
      ]),
    ]),
    /* main */
    el('div', { class: 'db-main page-main', id: 'db-main' }, [
      el('div', { class: 'db-toolbar', id: 'db-toolbar', style: 'display:none' }, [
        /* search + sort + limit */
        el('input', {
          type: 'text', class: 'db-search-input', placeholder: t('db.searchRows'),
          oninput: onSearch
        }),
        el('select', { class: 'db-limit-select', onchange: onLimitChange }, [
          ...[50, 100, 250, 500, 1000].map(n =>
            el('option', { value: String(n), ...(n === 100 ? { selected: '' } : {}) }, [String(n)])),
        ]),
        el('label', { class: 'db-live-label' }, [
          el('input', { type: 'checkbox', id: 'db-live', onchange: onLiveToggle }),
          ` ${t('db.autoRefresh')}`,
        ]),
      ]),
      el('div', { class: 'db-actions', id: 'db-actions', style: 'display:none' }, [
        el('button', { class: 'btn', id: 'db-btn-save', onclick: onSave }, [t('db.saveChanges')]),
        el('button', { class: 'btn', id: 'db-btn-discard', onclick: onDiscard }, [t('db.discardChanges')]),
        el('button', { class: 'btn', onclick: () => loadTable(activeTable) }, [t('common.refresh')]),
        el('button', { class: 'btn', onclick: onAddRow }, [t('db.addRowBtn')]),
        el('button', { class: 'btn btn-danger', onclick: onDeleteSelected }, [t('db.deleteSelected')]),
        el('button', { class: 'btn', onclick: () => exportTable('csv') }, [t('db.csv')]),
        el('button', { class: 'btn', onclick: () => exportTable('json') }, [t('db.json')]),
      ]),
      /* table content */
      el('div', { class: 'db-table-wrap', id: 'db-table-wrap' }, [
        el('div', { class: 'db-empty-state' }, [
          el('div', { style: 'font-size:3rem;margin-bottom:12px;opacity:.5' }, ['\u{1F5C4}\uFE0F']),
          t('db.selectTableFromSidebar'),
        ]),
      ]),
      /* danger zone */
      el('div', { class: 'db-danger', id: 'db-danger', style: 'display:none' }, [
        el('span', { style: 'font-weight:700;color:var(--critical)' }, [t('db.dangerZone')]),
        el('button', { class: 'btn btn-danger btn-sm', onclick: onVacuum }, [t('db.vacuum')]),
        el('button', { class: 'btn btn-danger btn-sm', onclick: onTruncate }, [t('db.truncate')]),
        el('button', { class: 'btn btn-danger btn-sm', onclick: onDrop }, [t('db.drop')]),
      ]),
      /* status */
      el('div', { class: 'db-status', id: 'db-status' }),
    ]),
  ]);
}

/* ── catalog ── */
async function loadCatalog() {
  try {
    const data = await api.get('/api/db/catalog', { timeout: 8000 });
    if (Array.isArray(data)) {
      catalog = data.map((item) => ({
        name: typeof item === 'string' ? item : (item?.name || item?.table || item?.id || ''),
        type: item?.type || 'table',
      })).filter((item) => item.name);
    } else {
      const tables = Array.isArray(data?.tables) ? data.tables : [];
      const views = Array.isArray(data?.views) ? data.views : [];
      catalog = [
        ...tables.map((item) => ({
          name: typeof item === 'string' ? item : (item?.name || item?.table || item?.id || ''),
          type: item?.type || 'table',
        })),
        ...views.map((item) => ({
          name: typeof item === 'string' ? item : (item?.name || item?.view || item?.id || ''),
          type: item?.type || 'view',
        })),
      ].filter((item) => item.name);
    }
    renderTree();
  } catch (err) {
    console.warn(`[${PAGE}]`, err.message);
    setStatus(t('db.failedLoadCatalog'));
  }
}

function renderTree() {
  const tree = $('#db-tree');
  if (!tree) return;
  empty(tree);

  const needle = sidebarFilter.toLowerCase();
  const tables = catalog.filter((t) => (t.type || 'table') === 'table');
  const views = catalog.filter((t) => t.type === 'view');

  const renderGroup = (label, items) => {
    const filtered = needle ? items.filter(i => i.name.toLowerCase().includes(needle)) : items;
    if (filtered.length === 0) return;
    tree.appendChild(el('div', { class: 'db-tree-group' }, [
      el('div', { class: 'db-tree-label' }, [`${label} (${filtered.length})`]),
      ...filtered.map(item =>
        el('button', {
          type: 'button',
          class: `db-tree-item ${item.name === activeTable ? 'active' : ''}`,
          'data-name': item.name,
          onclick: () => selectTable(item.name),
        }, [
          el('span', { class: 'db-tree-icon' }, [item.type === 'view' ? '\u{1F50D}' : '\u{1F4CB}']),
          el('span', { class: 'db-tree-item-name' }, [item.name]),
        ])
      ),
    ]));
  };

  renderGroup(t('db.tables'), tables);
  renderGroup(t('db.views'), views);

  if (catalog.length === 0) {
    tree.appendChild(el('div', { style: 'text-align:center;padding:20px;opacity:.5' }, [t('db.noTables')]));
  }
}

function onSidebarFilter(e) {
  sidebarFilter = e.target.value;
  renderTree();
}

/* ── select table ── */
async function selectTable(name) {
  activeTable = name;
  sortCol = null; sortDir = 'asc';
  searchText = ''; dirty.clear(); selected.clear();
  const searchInput = $('.db-search-input');
  if (searchInput) searchInput.value = '';
  renderTree();
  showToolbar(true);
  closeSidebarOnMobile();
  await loadTable(name);
}

function showToolbar(show) {
  const toolbar = $('#db-toolbar');
  const actions = $('#db-actions');
  const danger = $('#db-danger');
  if (toolbar) toolbar.style.display = show ? '' : 'none';
  if (actions) actions.style.display = show ? '' : 'none';
  if (danger) danger.style.display = show ? '' : 'none';
}

/* ── load table data ── */
async function loadTable(name) {
  if (!name) return;
  const seq = ++loadSequence;
  setStatus(t('common.loading'));
  try {
    const params = new URLSearchParams();
    params.set('limit', String(rowLimit));
    if (sortCol) { params.set('sort', sortCol); params.set('dir', sortDir); }
    if (searchText) params.set('search', searchText);

    const data = await api.get(`/api/db/table/${encodeURIComponent(name)}?${params}`, { timeout: 10000 });
    if (seq !== loadSequence) return;
    tableData = data;
    renderTable();
    setStatus(t('db.rowsInfo', { shown: data.rows?.length || 0, total: data.total ?? '?' }));
  } catch (err) {
    if (seq !== loadSequence) return;
    console.warn(`[${PAGE}]`, err.message);
    setStatus(t('db.failedLoadTable'));
    const wrap = $('#db-table-wrap');
    if (wrap) { empty(wrap); wrap.appendChild(el('div', { class: 'db-empty-state' }, [t('db.errorLoadingData')])); }
  }
}

/* ── render table ── */
function renderTable() {
  const wrap = $('#db-table-wrap');
  if (!wrap || !tableData) return;
  empty(wrap);

  const cols = tableData.columns || [];
  const rows = tableData.rows || [];

  if (cols.length === 0) {
    wrap.appendChild(el('div', { class: 'db-empty-state' }, [t('db.emptyTable')]));
    return;
  }

  const thead = el('thead', {}, [
    el('tr', {}, [
      el('th', { class: 'db-th-sel' }, [
        el('input', { type: 'checkbox', onchange: onSelectAll }),
      ]),
      ...cols.map((col) =>
        el('th', {
          class: sortCol === col ? 'sorted' : '',
          onclick: () => toggleSort(col),
        }, [col, sortCol === col ? (sortDir === 'asc' ? ' \u2191' : ' \u2193') : '']),
      ),
    ]),
  ]);

  const tbody = el('tbody');
  rows.forEach((row, idx) => {
    const pk = rowPK(row, idx);
    const isSelected = selected.has(pk);
    const isDirty = dirty.has(pk);
    const tr = el('tr', {
      class: `db-tr ${isSelected ? 'selected' : ''} ${isDirty ? 'dirty' : ''}`,
      'data-pk': pk,
    }, [
      el('td', { class: 'db-td db-td-sel' }, [
        el('input', {
          type: 'checkbox',
          ...(isSelected ? { checked: '' } : {}),
          onchange: (e) => toggleRowSelection(pk, e.target.checked),
        }),
      ]),
      ...cols.map((col) => {
        const currentVal = dirty.get(pk)?.[col] ?? (row[col] ?? '').toString();
        const originalVal = (row[col] ?? '').toString();
        return el('td', { class: 'db-td', 'data-col': col }, [
          el('span', {
            class: 'db-cell',
            contentEditable: 'true',
            spellcheck: 'false',
            'data-pk': pk,
            'data-col': col,
            'data-orig': originalVal,
            onblur: onCellBlur,
          }, [currentVal]),
        ]);
      }),
    ]);
    tbody.appendChild(tr);
  });

  wrap.appendChild(el('table', { class: 'db data-table' }, [thead, tbody]));
  updateDirtyUI();
}

function rowPK(row, idx) {
  /* Try 'id' or 'rowid' as PK; fallback to index */
  if (row.id !== undefined) return String(row.id);
  if (row.rowid !== undefined) return String(row.rowid);
  return `_idx_${idx}`;
}

/* ── sorting ── */
function toggleSort(col) {
  if (sortCol === col) {
    sortDir = sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    sortCol = col;
    sortDir = 'asc';
  }
  loadTable(activeTable);
}

/* ── search ── */
function onSearch(e) {
  const nextValue = e.target.value;
  clearTimeout(searchDebounce);
  searchDebounce = setTimeout(() => {
    searchText = nextValue;
    loadTable(activeTable);
  }, 220);
}

/* ── limit ── */
function onLimitChange(e) {
  rowLimit = parseInt(e.target.value, 10) || 100;
  loadTable(activeTable);
}

/* ── live refresh ── */
function onLiveToggle(e) {
  liveRefresh = e.target.checked;
  if (liveRefresh) {
    poller = new Poller(() => loadTable(activeTable), 5000);
    poller.start();
  } else {
    if (poller) { poller.stop(); poller = null; }
  }
}

/* ── selection ── */
function onSelectAll(e) {
  const rows = tableData?.rows || [];
  if (e.target.checked) {
    rows.forEach((r, i) => selected.add(rowPK(r, i)));
  } else {
    selected.clear();
  }
  renderTable();
}

function toggleRowSelection(pk, checked) {
  if (checked) selected.add(pk); else selected.delete(pk);
  const tr = document.querySelector(`.db-container tr.db-tr[data-pk="${pk}"]`);
  if (tr) tr.classList.toggle('selected', checked);
}

/* ── inline editing ── */
function onCellBlur(e) {
  const span = e.target;
  const pk = span.dataset.pk;
  const col = span.dataset.col;
  const orig = span.dataset.orig;
  const newVal = span.textContent;

  if (newVal === orig) {
    /* revert — remove from dirty if no other changes */
    const changes = dirty.get(pk);
    if (changes) {
      delete changes[col];
      if (Object.keys(changes).length === 0) dirty.delete(pk);
    }
  } else {
    if (!dirty.has(pk)) dirty.set(pk, {});
    dirty.get(pk)[col] = newVal;
  }
  updateDirtyUI();
}

function updateDirtyUI() {
  const saveBtn = $('#db-btn-save');
  const discardBtn = $('#db-btn-discard');
  const hasDirty = dirty.size > 0;
  if (saveBtn) saveBtn.classList.toggle('btn-primary', hasDirty);
  if (discardBtn) discardBtn.style.opacity = hasDirty ? '1' : '0.4';
}

/* ── save ── */
async function onSave() {
  if (dirty.size === 0) return;
  setStatus(t('common.saving'));
  try {
    const updates = [];
    dirty.forEach((changes, pk) => {
      updates.push({ pk, changes });
    });
    await api.post('/api/db/update', { table: activeTable, updates });
    dirty.clear();
    toast(t('db.changesSaved'), 2000, 'success');
    await loadTable(activeTable);
  } catch (err) {
    toast(`${t('db.saveFailed')}: ${err.message}`, 3000, 'error');
    setStatus(t('db.saveFailed'));
  }
}

function onDiscard() {
  dirty.clear();
  renderTable();
  toast(t('db.changesDiscarded'), 1500);
}

/* ── add row ── */
async function onAddRow() {
  setStatus(t('db.insertingRow'));
  try {
    await api.post('/api/db/insert', { table: activeTable });
    toast(t('db.rowInserted'), 2000, 'success');
    await loadTable(activeTable);
  } catch (err) {
    toast(`${t('db.insertFailed')}: ${err.message}`, 3000, 'error');
  }
}

/* ── delete selected ── */
async function onDeleteSelected() {
  if (selected.size === 0) { toast(t('db.noRowsSelected'), 1500); return; }
  setStatus(t('db.deletingRowsCount', { count: selected.size }));
  try {
    await api.post('/api/db/delete', { table: activeTable, pks: [...selected] });
    selected.clear();
    toast(t('db.rowsDeleted'), 2000, 'success');
    await loadTable(activeTable);
  } catch (err) {
    toast(`${t('common.deleteFailed')}: ${err.message}`, 3000, 'error');
  }
}

/* ── export ── */
function exportTable(format) {
  if (!activeTable) return;
  window.location.href = `/api/db/export/${encodeURIComponent(activeTable)}?format=${format}`;
}

/* ── danger zone ── */
async function onVacuum() {
  setStatus(t('db.runningVacuum'));
  try {
    await api.post('/api/db/vacuum', {});
    toast(t('db.vacuumComplete'), 2000, 'success');
    setStatus(t('db.vacuumDone'));
  } catch (err) {
    toast(`${t('db.vacuumFailed')}: ${err.message}`, 3000, 'error');
  }
}

async function onTruncate() {
  if (!activeTable) return;
  if (!confirm(t('db.confirmTruncate', { table: activeTable }))) return;
  setStatus(t('db.truncating'));
  try {
    await api.post(`/api/db/truncate/${encodeURIComponent(activeTable)}`, {});
    toast(t('db.tableTruncated'), 2000, 'success');
    await loadTable(activeTable);
  } catch (err) {
    toast(`${t('db.truncateFailed')}: ${err.message}`, 3000, 'error');
  }
}

async function onDrop() {
  if (!activeTable) return;
  if (!confirm(t('db.confirmDrop', { table: activeTable }))) return;
  setStatus(t('db.dropping'));
  try {
    await api.post(`/api/db/drop/${encodeURIComponent(activeTable)}`, {});
    toast(t('db.droppedTable', { table: activeTable }), 2000, 'success');
    activeTable = null;
    showToolbar(false);
    const wrap = $('#db-table-wrap');
    if (wrap) { empty(wrap); wrap.appendChild(el('div', { class: 'db-empty-state' }, [t('db.tableDropped')])); }
    await loadCatalog();
  } catch (err) {
    toast(`${t('db.dropFailed')}: ${err.message}`, 3000, 'error');
  }
}

/* ── status bar ── */
function setStatus(msg) {
  const el2 = $('#db-status');
  if (el2) el2.textContent = msg || '';
}

function closeSidebarOnMobile() {
  if (window.matchMedia('(max-width: 900px)').matches) {
    const hideBtn = $('#hideSidebar');
    if (hideBtn) hideBtn.click();
  }
}
