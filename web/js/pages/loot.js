import { ResourceTracker } from '../core/resource-tracker.js';
import { api } from '../core/api.js';
import { el, $, $$, empty } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'loot';
const MAC_IP_RE = /^[0-9a-f:]{17}_\d+\.\d+\.\d+\.\d+$/i;

let tracker = null;
let root = null;
let fileData = [];
let allFiles = [];
let currentView = 'tree';
let currentCategory = 'all';
let currentSort = 'name';
let sortDirection = 'asc';
let searchTerm = '';
let searchTimer = null;
let expandedDirs = new Set();
let treeExpansionInitialized = false;

const FILE_ICONS = {
  ssh: '🔐',
  sql: '🗄️',
  smb: '🌐',
  other: '📄',
};

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  bindEvents();
  await loadFiles();
}

export function unmount() {
  if (searchTimer) {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  if (tracker) {
    tracker.cleanupAll();
    tracker = null;
  }
  root = null;
  fileData = [];
  allFiles = [];
  currentView = 'tree';
  currentCategory = 'all';
  currentSort = 'name';
  sortDirection = 'asc';
  searchTerm = '';
  expandedDirs = new Set();
  treeExpansionInitialized = false;
}

function buildShell() {
  return el('div', { class: 'loot-container' }, [
    el('div', { class: 'stats-bar' }, [
      statItem('👥', 'stat-victims', t('common.host')),
      statItem('📄', 'stat-files', t('loot.totalFiles')),
      statItem('📁', 'stat-folders', t('loot.directories')),
    ]),

    el('div', { class: 'controls-bar' }, [
      el('div', { class: 'search-container' }, [
        el('span', { class: 'search-icon' }, ['🔍']),
        el('input', {
          type: 'text',
          class: 'search-input',
          id: 'searchInput',
          placeholder: `${t('common.search')}...`,
        }),
        el('span', { class: 'clear-search', id: 'clearSearch' }, ['✖']),
      ]),
      el('div', { class: 'view-controls' }, [
        el('button', { class: 'view-btn active', id: 'treeViewBtn', title: t('loot.treeView'), type: 'button' }, ['🌳']),
        el('button', { class: 'view-btn', id: 'listViewBtn', title: t('common.list'), type: 'button' }, ['📋']),
        el('div', { class: 'sort-dropdown', id: 'sortDropdown' }, [
          el('button', { class: 'sort-btn', id: 'sortBtn', type: 'button', title: t('common.sortBy') }, ['⬇️']),
          el('div', { class: 'sort-menu' }, [
            sortOption('name', t('common.name'), true),
            sortOption('type', t('common.type')),
            sortOption('date', t('common.date')),
            sortOption('asc', t('common.ascending')),
            sortOption('desc', t('common.descending')),
          ]),
        ]),
      ]),
    ]),

    el('div', { class: 'tabs-container', id: 'tabsContainer' }),

    el('div', { class: 'explorer' }, [
      el('div', { class: 'explorer-content', id: 'explorerContent' }, [
        el('div', { class: 'loading' }, [
          el('div', { class: 'loading-spinner' }),
        ]),
      ]),
    ]),
  ]);
}

function statItem(icon, id, label) {
  return el('div', { class: 'stat-item' }, [
    el('span', { class: 'stat-icon' }, [icon]),
    el('span', { class: 'stat-value', id }, ['0']),
    el('span', { class: 'stat-label' }, [label]),
  ]);
}

function sortOption(value, label, active = false) {
  return el('div', {
    class: `sort-option${active ? ' active' : ''}`,
    'data-sort': value,
    role: 'button',
    tabindex: '0',
  }, [label]);
}

function bindEvents() {
  const searchInput = $('#searchInput', root);
  const clearBtn = $('#clearSearch', root);
  const treeBtn = $('#treeViewBtn', root);
  const listBtn = $('#listViewBtn', root);
  const sortDropdown = $('#sortDropdown', root);
  const sortBtn = $('#sortBtn', root);

  if (searchInput) {
    tracker.trackEventListener(searchInput, 'input', (e) => {
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        searchTerm = String(e.target.value || '').toLowerCase().trim();
        renderContent(true);
      }, 300);
    });
  }

  if (clearBtn) {
    tracker.trackEventListener(clearBtn, 'click', () => {
      if (searchInput) searchInput.value = '';
      searchTerm = '';
      renderContent();
    });
  }

  if (treeBtn) tracker.trackEventListener(treeBtn, 'click', () => setView('tree'));
  if (listBtn) tracker.trackEventListener(listBtn, 'click', () => setView('list'));

  if (sortBtn && sortDropdown) {
    tracker.trackEventListener(sortBtn, 'click', () => {
      sortDropdown.classList.toggle('active');
    });
  }

  $$('.sort-option', root).forEach((option) => {
    tracker.trackEventListener(option, 'click', () => onSortOption(option));
    tracker.trackEventListener(option, 'keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        onSortOption(option);
      }
    });
  });

  tracker.trackEventListener(document, 'click', (e) => {
    const dropdown = $('#sortDropdown', root);
    if (dropdown && !dropdown.contains(e.target)) {
      dropdown.classList.remove('active');
    }
  });
}

function onSortOption(option) {
  $$('.sort-option', root).forEach((opt) => opt.classList.remove('active'));
  option.classList.add('active');

  const value = option.dataset.sort;
  if (value === 'asc' || value === 'desc') {
    sortDirection = value;
  } else {
    currentSort = value;
  }

  $('#sortDropdown', root)?.classList.remove('active');
  renderContent();
}

function setView(view) {
  currentView = view;
  $$('.view-btn', root).forEach((btn) => btn.classList.remove('active'));
  $(`#${view}ViewBtn`, root)?.classList.add('active');
  renderContent();
}

async function loadFiles() {
  try {
    const data = await api.get('/loot_directories', { timeout: 15000 });
    if (!data || data.status !== 'success' || !Array.isArray(data.data)) {
      throw new Error(t('common.error'));
    }

    fileData = data.data;
    expandedDirs = new Set();
    treeExpansionInitialized = false;
    processFiles();
    updateStats();
    renderContent();
  } catch (err) {
    const explorer = $('#explorerContent', root);
    if (!explorer) return;
    empty(explorer);
    explorer.appendChild(noResults('⚠️', `${t('common.error')}: ${t('common.noData')}`));
  }
}

function processFiles() {
  allFiles = [];
  const stats = {};

  function extractFiles(items, path = '') {
    for (const item of items || []) {
      if (item.type === 'directory' && Array.isArray(item.children)) {
        extractFiles(item.children, `${path}${item.name}/`);
      } else if (item.type === 'file') {
        const category = getFileCategory(item.name, path);
        const fullPath = `${path}${item.name}`;
        allFiles.push({
          ...item,
          category,
          fullPath,
          path: item.path || fullPath,
        });
        stats[category] = (stats[category] || 0) + 1;
      }
    }
  }

  extractFiles(fileData);
  renderTabs(Object.keys(stats));

  const allBadge = $('#badge-all', root);
  if (allBadge) allBadge.textContent = String(allFiles.length);

  for (const cat of Object.keys(stats)) {
    const badge = $(`#badge-${cat}`, root);
    if (badge) badge.textContent = String(stats[cat]);
  }
}

function getFileCategory(filename, path) {
  const lowerName = String(filename || '').toLowerCase();
  const lowerPath = String(path || '').toLowerCase();

  if (lowerPath.includes('ssh') || lowerName.includes('ssh') || lowerName.includes('key')) return 'ssh';
  if (lowerPath.includes('sql') || lowerName.includes('sql') || lowerName.includes('database')) return 'sql';
  if (lowerPath.includes('smb') || lowerName.includes('smb') || lowerName.includes('share')) return 'smb';
  return 'other';
}

function getDirCategory(path) {
  const lowerPath = String(path || '').toLowerCase();
  if (lowerPath.includes('ssh')) return 'ssh';
  if (lowerPath.includes('sql')) return 'sql';
  if (lowerPath.includes('smb')) return 'smb';
  return 'other';
}

function updateStats() {
  const victims = new Set();
  let totalFiles = 0;
  let totalFolders = 0;

  function scan(items) {
    for (const item of items || []) {
      if (item.type === 'directory') {
        totalFolders += 1;
        if (MAC_IP_RE.test(String(item.name || ''))) victims.add(item.name);
        if (Array.isArray(item.children)) scan(item.children);
      } else if (item.type === 'file') {
        totalFiles += 1;
      }
    }
  }

  scan(fileData);

  setText('stat-victims', victims.size);
  setText('stat-files', totalFiles);
  setText('stat-folders', totalFolders);
}

function setText(id, value) {
  const node = $(`#${id}`, root);
  if (node) node.textContent = String(value ?? '');
}

function fileMatchesSearch(file) {
  if (!searchTerm) return true;
  const n = String(file?.name || '').toLowerCase();
  const p = String(file?.fullPath || '').toLowerCase();
  return n.includes(searchTerm) || p.includes(searchTerm);
}

function computeSearchFilteredFiles() {
  return allFiles.filter(fileMatchesSearch);
}

function updateBadgesFromFiltered() {
  const filtered = computeSearchFilteredFiles();
  setText('badge-all', filtered.length);

  const byCat = filtered.reduce((acc, f) => {
    acc[f.category] = (acc[f.category] || 0) + 1;
    return acc;
  }, {});

  $$('.tab', root).forEach((tab) => {
    const cat = tab.dataset.category;
    if (cat === 'all') return;
    setText(`badge-${cat}`, byCat[cat] || 0);
  });
}

function renderTabs(categories) {
  const tabs = $('#tabsContainer', root);
  if (!tabs) return;
  empty(tabs);

  tabs.appendChild(tabNode('all', t('common.all'), true));
  for (const cat of categories) {
    tabs.appendChild(tabNode(cat, cat.toUpperCase(), false));
  }

  $$('.tab', tabs).forEach((tab) => {
    tracker.trackEventListener(tab, 'click', () => {
      $$('.tab', tabs).forEach((tEl) => tEl.classList.remove('active'));
      tab.classList.add('active');
      currentCategory = tab.dataset.category;
      renderContent();
    });
  });
}

function tabNode(category, label, active) {
  return el('div', {
    class: `tab${active ? ' active' : ''}`,
    'data-category': category,
  }, [
    label,
    el('span', { class: 'tab-badge', id: `badge-${category}` }, ['0']),
  ]);
}

function renderContent(autoExpand = false) {
  const container = $('#explorerContent', root);
  if (!container) return;

  if (currentView === 'tree') {
    renderTreeView(container, autoExpand);
  } else {
    renderListView(container);
  }
}

function renderTreeView(container, autoExpand = false) {
  updateBadgesFromFiltered();
  const filteredData = filterDataForTree();

  empty(container);

  if (!filteredData.length) {
    container.appendChild(noResults('🔍', t('common.noData')));
    return;
  }

  if (!treeExpansionInitialized && !searchTerm) {
    expandRootDirectories(filteredData);
    treeExpansionInitialized = true;
  }

  const tree = el('div', { class: 'tree-view active' });
  tree.appendChild(renderTreeItems(filteredData, 0, '', autoExpand || !!searchTerm));
  container.appendChild(tree);
}

function filterDataForTree() {
  function filterItems(items, path = '') {
    return (items || [])
      .map((item) => {
        if (item.type === 'directory') {
          const dirPath = `${path}${item.name}/`;
          const filteredChildren = Array.isArray(item.children)
            ? filterItems(item.children, dirPath)
            : [];
          const nameMatch = String(item.name || '').toLowerCase().includes(searchTerm);
          const dirMatchesCategory = currentCategory === 'all' || getDirCategory(dirPath) === currentCategory;

          if (filteredChildren.length > 0) {
            return { ...item, children: filteredChildren };
          }
          if (searchTerm) return nameMatch ? { ...item, children: [] } : null;
          if (currentCategory === 'all') return { ...item, children: [] };
          if (dirMatchesCategory) return { ...item, children: [] };
          return null;
        }

        if (item.type === 'file') {
          const category = getFileCategory(item.name, path);
          const temp = {
            ...item,
            category,
            fullPath: `${path}${item.name}`,
            path: item.path || `${path}${item.name}`,
          };
          const matchesSearch = fileMatchesSearch(temp);
          const matchesCategory = currentCategory === 'all' || category === currentCategory;
          return matchesSearch && matchesCategory ? temp : null;
        }

        return null;
      })
      .filter(Boolean);
  }

  return filterItems(fileData, '');
}

function renderTreeItems(items, level, path = '', forceExpand = false) {
  const frag = document.createDocumentFragment();
  const sortedItems = sortTreeItems(items, path);

  sortedItems.forEach((item, index) => {
    if (item.type === 'directory') {
      const dirPath = `${path}${item.name}/`;
      const hasChildren = Array.isArray(item.children) && item.children.length > 0;
      const expanded = forceExpand || expandedDirs.has(dirPath);
      const treeItem = el('div', { class: `loot-tree-node${expanded ? ' expanded' : ''}` });
      treeItem.style.animationDelay = `${index * 0.05}s`;
      treeItem.style.setProperty('--loot-level', String(level));

      const stats = directoryStats(item);
      const header = el('button', { class: 'loot-tree-row', type: 'button' }, [
        el('span', { class: 'loot-tree-chevron' }, [hasChildren ? '▶' : '•']),
        el('span', { class: 'loot-tree-icon folder-icon' }, ['📁']),
        el('span', { class: 'loot-tree-name' }, [item.name]),
        el('span', { class: 'loot-tree-meta' }, [t('loot.filesCount', { count: stats.files })]),
      ]);

      tracker.trackEventListener(header, 'click', (e) => {
        e.stopPropagation();
        if (!hasChildren) return;
        const next = !treeItem.classList.contains('expanded');
        treeItem.classList.toggle('expanded', next);
        if (next) expandedDirs.add(dirPath);
        else expandedDirs.delete(dirPath);
      });

      treeItem.appendChild(header);

      if (hasChildren) {
        const children = el('div', { class: 'loot-tree-children' });
        children.appendChild(renderTreeItems(item.children, level + 1, dirPath, forceExpand));
        treeItem.appendChild(children);
      }

      frag.appendChild(treeItem);
      return;
    }

    if (item.type === 'file') {
      const category = getFileCategory(item.name, path);
      frag.appendChild(renderFileItem({
        ...item,
        category,
        fullPath: `${path}${item.name}`,
        path: item.path || `${path}${item.name}`,
      }, category, index, false, { treeLevel: level + 1, treeMode: true }));
    }
  });

  return frag;
}

function renderListView(container) {
  updateBadgesFromFiltered();

  let filtered = allFiles.filter((f) => fileMatchesSearch(f) && (currentCategory === 'all' || f.category === currentCategory));

  filtered.sort((a, b) => {
    let res = 0;
    switch (currentSort) {
      case 'type':
        res = a.category.localeCompare(b.category) || a.name.localeCompare(b.name);
        break;
      case 'date':
        res = fileTimestamp(a) - fileTimestamp(b);
        break;
      case 'name':
      default:
        res = String(a.name || '').localeCompare(String(b.name || ''));
        break;
    }
    return sortDirection === 'desc' ? -res : res;
  });

  empty(container);

  if (!filtered.length) {
    container.appendChild(noResults('🔍', t('common.noData')));
    return;
  }

  const list = el('div', { class: 'list-view active' });
  filtered.forEach((file, index) => {
    list.appendChild(renderFileItem(file, file.category, index, true));
  });
  container.appendChild(list);
}

function fileTimestamp(file) {
  const candidates = [
    file?.modified,
    file?.modified_at,
    file?.date,
    file?.mtime,
    file?.created_at,
  ];
  for (const v of candidates) {
    if (v == null || v === '') continue;
    if (typeof v === 'number' && Number.isFinite(v)) return v;
    const ts = Date.parse(String(v));
    if (Number.isFinite(ts)) return ts;
  }
  return 0;
}

function renderFileItem(file, category, index = 0, showPath = false, opts = {}) {
  const path = file.path || file.fullPath || file.name;
  const item = el('div', { class: `file-item${opts.treeMode ? ' is-tree-file' : ''}`, 'data-path': path });
  item.style.animationDelay = `${index * 0.02}s`;
  if (typeof opts.treeLevel === 'number') {
    item.style.setProperty('--loot-level', String(opts.treeLevel));
  }

  tracker.trackEventListener(item, 'click', () => {
    downloadFile(path);
  });

  const icon = el('div', { class: `file-icon ${category}` }, [FILE_ICONS[category] || FILE_ICONS.other]);
  const name = el('div', { class: 'file-name' }, [String(file.name || '')]);

  if (showPath) {
    name.appendChild(el('span', { style: 'color:var(--_muted);font-size:0.75rem' }, [` — ${file.fullPath || path}`]));
  }

  const type = el('span', { class: `file-type ${category}` }, [String(category || 'other')]);
  item.append(icon, name, type);

  return item;
}

function compareBySort(a, b, path = '') {
  let res = 0;
  switch (currentSort) {
    case 'type': {
      const ca = a.type === 'directory' ? getDirCategory(`${path}${a.name}/`) : getFileCategory(a.name, path);
      const cb = b.type === 'directory' ? getDirCategory(`${path}${b.name}/`) : getFileCategory(b.name, path);
      res = ca.localeCompare(cb) || String(a.name || '').localeCompare(String(b.name || ''));
      break;
    }
    case 'date':
      res = fileTimestamp(a) - fileTimestamp(b);
      break;
    case 'name':
    default:
      res = String(a.name || '').localeCompare(String(b.name || ''));
      break;
  }
  return sortDirection === 'desc' ? -res : res;
}

function sortTreeItems(items, path = '') {
  return [...(items || [])].sort((a, b) => {
    const ad = a.type === 'directory';
    const bd = b.type === 'directory';
    if (ad !== bd) return ad ? -1 : 1;
    return compareBySort(a, b, path);
  });
}

function expandRootDirectories(items) {
  (items || []).forEach((item) => {
    if (item.type === 'directory') {
      expandedDirs.add(`${item.name}/`);
    }
  });
}

function directoryStats(item) {
  let files = 0;
  const walk = (nodes) => {
    for (const n of nodes || []) {
      if (n.type === 'directory') walk(n.children || []);
      if (n.type === 'file') files += 1;
    }
  };
  walk(item.children || []);
  return { files };
}

function downloadFile(path) {
  window.location.href = `/loot_download?path=${encodeURIComponent(path)}`;
}

function noResults(icon, message) {
  return el('div', { class: 'no-results' }, [
    el('div', { class: 'no-results-icon' }, [icon]),
    String(message || t('common.noData')),
  ]);
}
