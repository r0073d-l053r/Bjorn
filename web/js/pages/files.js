/**
 * Files Explorer page module.
 * Parity target: web_old/files_explorer.html behavior in SPA form.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { el, $, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'files';

let tracker = null;
let root = null;

let currentPath = [];
let allFiles = [];
let isGridView = true;
let isMultiSelectMode = false;
let searchValue = '';
let selectedTargetPath = null;
let absoluteBasePath = '/home/bjorn';
const selectedItems = new Map(); // relPath -> { name, is_directory, relPath, absPath, size }

let contextMenuEl = null;
let moveModalEl = null;

function L(key, fallback, vars = {}) {
  const v = t(key, vars);
  return v === key ? fallback : v;
}

function q(sel, base = root) {
  return base ? base.querySelector(sel) : null;
}

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  wireStaticEvents();
  updateViewModeButton();
  await loadAllFiles();
}

export function unmount() {
  removeContextMenu();
  closeMoveDialog();
  if (tracker) {
    tracker.cleanupAll();
    tracker = null;
  }
  root = null;
  currentPath = [];
  allFiles = [];
  isGridView = true;
  isMultiSelectMode = false;
  searchValue = '';
  selectedTargetPath = null;
  absoluteBasePath = '/home/bjorn';
  selectedItems.clear();
}

function buildShell() {
  return el('div', { class: 'files-container' }, [
    el('div', { class: 'loot-container' }, [
      el('div', { class: 'file-explorer' }, [
        el('div', { class: 'toolbar-buttons' }, [
          el('button', {
            class: 'action-button',
            id: 'viewModeBtn',
            title: L('common.view', 'View'),
          }, ['\u25A6']),
          el('button', {
            class: 'action-button',
            id: 'multiSelectBtn',
          }, [`\u229E ${L('common.selectAll', 'Select')}`]),
          el('button', {
            class: 'action-button',
            id: 'newFolderBtn',
          }, [`\u{1F4C1}+ ${L('common.new', 'New')} ${L('common.directory', 'folder')}`]),
          el('button', {
            class: 'action-button',
            id: 'renameBtn',
            style: 'display:none',
          }, [`\u270E ${L('common.rename', 'Rename')}`]),
          el('button', {
            class: 'action-button',
            id: 'moveBtn',
            style: 'display:none',
          }, [`\u2194 ${L('common.move', 'Move')}`]),
          el('button', {
            class: 'action-button delete',
            id: 'deleteBtn',
            style: 'display:none',
          }, [`\u{1F5D1} ${L('common.delete', 'Delete')}`]),
          el('button', {
            class: 'action-button',
            id: 'refreshBtn',
          }, [`\u21BB ${L('common.refresh', 'Refresh')}`]),
        ]),

        el('div', { class: 'search-container' }, [
          el('input', {
            type: 'text',
            class: 'search-input',
            id: 'search-input',
            placeholder: L('files.searchPlaceholder', 'Search files...'),
          }),
          el('button', { class: 'clear-button', id: 'clear-button' }, ['\u2716']),
        ]),

        el('div', { class: 'path-navigator' }, [
          el('div', { class: 'nav-buttons' }, [
            el('button', {
              class: 'back-button',
              id: 'backBtn',
              title: L('common.back', 'Back'),
            }, ['\u2190 ', L('common.back', 'Back')]),
          ]),
          el('div', { class: 'current-path', id: 'currentPath' }),
        ]),

        el('div', { class: 'files-grid', id: 'file-list' }),
      ]),

      el('div', { class: 'upload-container' }, [
        el('input', {
          id: 'file-upload',
          type: 'file',
          multiple: '',
          style: 'display:none',
        }),
        el('div', { id: 'drop-zone', class: 'drop-zone' }, [
          L('files.dropzoneHint', 'Drag files or folders here or click to upload'),
        ]),
      ]),

      el('div', { class: 'db-status', id: 'files-status' }),
    ]),
  ]);
}

function wireStaticEvents() {
  const viewModeBtn = q('#viewModeBtn');
  const multiSelectBtn = q('#multiSelectBtn');
  const newFolderBtn = q('#newFolderBtn');
  const renameBtn = q('#renameBtn');
  const moveBtn = q('#moveBtn');
  const deleteBtn = q('#deleteBtn');
  const refreshBtn = q('#refreshBtn');
  const searchInput = q('#search-input');
  const clearBtn = q('#clear-button');
  const backBtn = q('#backBtn');
  const fileInput = q('#file-upload');
  const dropZone = q('#drop-zone');
  const list = q('#file-list');

  if (viewModeBtn) tracker.trackEventListener(viewModeBtn, 'click', toggleView);
  if (multiSelectBtn) tracker.trackEventListener(multiSelectBtn, 'click', toggleMultiSelect);
  if (newFolderBtn) tracker.trackEventListener(newFolderBtn, 'click', createNewFolder);
  if (renameBtn) tracker.trackEventListener(renameBtn, 'click', renameSelected);
  if (moveBtn) tracker.trackEventListener(moveBtn, 'click', moveSelected);
  if (deleteBtn) tracker.trackEventListener(deleteBtn, 'click', deleteSelectedItems);
  if (refreshBtn) tracker.trackEventListener(refreshBtn, 'click', loadAllFiles);
  if (searchInput) tracker.trackEventListener(searchInput, 'input', onSearchInput);
  if (clearBtn) tracker.trackEventListener(clearBtn, 'click', clearSearch);
  if (backBtn) tracker.trackEventListener(backBtn, 'click', navigateUp);
  if (fileInput) tracker.trackEventListener(fileInput, 'change', handleFileUploadInput);
  if (dropZone) {
    tracker.trackEventListener(dropZone, 'click', () => fileInput?.click());
    tracker.trackEventListener(dropZone, 'dragover', onDropZoneDragOver);
    tracker.trackEventListener(dropZone, 'dragleave', onDropZoneDragLeave);
    tracker.trackEventListener(dropZone, 'drop', onDropZoneDrop);
  }
  if (list) tracker.trackEventListener(list, 'contextmenu', showEmptySpaceContextMenu);

  tracker.trackEventListener(document, 'click', () => removeContextMenu());
  tracker.trackEventListener(window, 'keydown', onKeyDown);
  tracker.trackEventListener(window, 'i18n:changed', () => {
    updateStaticI18n();
    renderCurrentFolder();
  });
}

function onKeyDown(e) {
  if (e.key === 'Escape') {
    removeContextMenu();
    closeMoveDialog();
  }
}

function updateStaticI18n() {
  const multiSelectBtn = q('#multiSelectBtn');
  const newFolderBtn = q('#newFolderBtn');
  const renameBtn = q('#renameBtn');
  const moveBtn = q('#moveBtn');
  const deleteBtn = q('#deleteBtn');
  const refreshBtn = q('#refreshBtn');
  const searchInput = q('#search-input');
  const backBtn = q('#backBtn');
  const dropZone = q('#drop-zone');

  if (multiSelectBtn) multiSelectBtn.textContent = `\u229E ${isMultiSelectMode ? L('common.cancel', 'Cancel') : L('common.select', 'Select')}`;
  if (newFolderBtn) newFolderBtn.textContent = `\u{1F4C1}+ ${L('common.new', 'New')} ${L('common.directory', 'folder')}`;
  if (renameBtn) renameBtn.textContent = `\u270E ${L('common.rename', 'Rename')}`;
  if (moveBtn) moveBtn.textContent = `\u2194 ${L('common.move', 'Move')}`;
  if (deleteBtn) deleteBtn.textContent = `\u{1F5D1} ${L('common.delete', 'Delete')}`;
  if (refreshBtn) refreshBtn.textContent = `\u21BB ${L('common.refresh', 'Refresh')}`;
  if (searchInput) searchInput.placeholder = L('files.searchPlaceholder', 'Search files...');
  if (backBtn) backBtn.textContent = `\u2190 ${L('common.back', 'Back')}`;
  if (dropZone) dropZone.textContent = L('files.dropzoneHint', 'Drag files or folders here or click to upload');
  updateViewModeButton();
}

async function loadAllFiles() {
  setStatus(L('common.loading', 'Loading...'));
  try {
    const ac = tracker ? tracker.trackAbortController() : new AbortController();
    const response = await fetch('/list_files', { signal: ac.signal });
    if (tracker) tracker.removeAbortController(ac);
    if (!tracker) return; /* unmounted while awaiting */
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    allFiles = Array.isArray(data) ? data : [];
    absoluteBasePath = inferAbsoluteBasePath(allFiles) || '/home/bjorn';
    renderCurrentFolder();
  } catch (err) {
    if (err.name === 'AbortError') return;
    console.error(`[${PAGE}] loadAllFiles:`, err);
    allFiles = [];
    renderCurrentFolder();
    setStatus(L('files.failedLoadDir', 'Failed to load directory'));
  }
}

function inferAbsoluteBasePath(tree) {
  let best = null;

  function walk(items, segs) {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      const nextSegs = [...segs, item.name].filter(Boolean);
      if (!item.is_directory && item.path && typeof item.path === 'string') {
        const abs = item.path.replace(/\\/g, '/');
        const rel = nextSegs.join('/');
        if (rel && abs.endsWith('/' + rel)) {
          best = abs.slice(0, abs.length - rel.length - 1);
        } else {
          best = abs.slice(0, abs.lastIndexOf('/'));
        }
        return;
      }
      if (item.is_directory && item.children) {
        walk(item.children, nextSegs);
        if (best) return;
      }
    }
  }

  walk(tree, []);
  return best;
}

function renderCurrentFolder() {
  const currentContent = findFolderContents(allFiles, currentPath);
  const visibleItems = searchValue
    ? filterAllFiles(allFiles, searchValue)
    : decorateFolderItems(currentContent, currentPath);
  displayFiles(visibleItems);
  updateCurrentPathDisplay();
  updateButtonStates();
  setStatus(L('files.itemsCount', '{{count}} item(s)', { count: visibleItems.length }));
}

function findFolderContents(data, path) {
  if (!Array.isArray(data)) return [];
  if (!path.length) return data;

  let current = data;
  for (const folder of path) {
    const found = current.find((item) => item?.is_directory && item.name === folder);
    if (!found || !Array.isArray(found.children)) return [];
    current = found.children;
  }
  return current;
}

function decorateFolderItems(items, basePath) {
  return (Array.isArray(items) ? items : []).map((item) => {
    const relPath = [...basePath, item.name].filter(Boolean).join('/');
    const absPath = item.path || buildAbsolutePath(relPath);
    return {
      ...item,
      _relPath: relPath,
      _absPath: absPath,
      _folderPath: item.is_directory ? relPath : basePath.join('/'),
      _segments: item.is_directory ? [...basePath, item.name] : [...basePath],
    };
  });
}

function filterAllFiles(items, rawNeedle, segs = []) {
  const needle = String(rawNeedle || '').toLowerCase().trim();
  if (!needle) return [];
  let out = [];

  for (const item of (Array.isArray(items) ? items : [])) {
    if (!item || typeof item !== 'object') continue;
    const relPath = [...segs, item.name].filter(Boolean).join('/');
    const absPath = item.path || buildAbsolutePath(relPath);

    if ((item.name || '').toLowerCase().includes(needle)) {
      out.push({
        ...item,
        _relPath: relPath,
        _absPath: absPath,
        _folderPath: item.is_directory ? relPath : segs.join('/'),
        _segments: item.is_directory ? [...segs, item.name] : [...segs],
      });
    }

    if (item.is_directory && Array.isArray(item.children)) {
      out = out.concat(filterAllFiles(item.children, needle, [...segs, item.name]));
    }
  }

  return out;
}

function displayFiles(items) {
  const container = q('#file-list');
  if (!container) return;

  empty(container);
  container.className = isGridView ? 'files-grid' : 'files-list';

  const sorted = [...items].sort((a, b) => {
    if (a.is_directory && !b.is_directory) return -1;
    if (!a.is_directory && b.is_directory) return 1;
    return String(a.name || '').localeCompare(String(b.name || ''), undefined, { numeric: true, sensitivity: 'base' });
  });

  if (!sorted.length) {
    container.appendChild(el('div', { class: 'item-meta', style: 'padding:16px' }, [L('files.noFiles', 'No files found')]));
    return;
  }

  for (const item of sorted) {
    const relPath = item._relPath || '';
    const absPath = item._absPath || buildAbsolutePath(relPath);
    const nodeClass = `${isGridView ? 'grid-item' : 'list-item'} ${item.is_directory ? 'folder' : 'file'}`;
    const node = el('div', { class: nodeClass });
    node.dataset.path = relPath;
    if (selectedItems.has(relPath)) node.classList.add('item-selected');

    const icon = el('img', {
      src: `/web/images/${item.is_directory ? 'mainfolder' : 'file'}.png`,
      alt: item.is_directory ? L('common.directory', 'directory') : L('common.file', 'file'),
    });
    tracker.trackEventListener(icon, 'error', () => {
      icon.src = '/web/images/attack.png';
    });

    const body = el('div', {}, [
      el('div', { class: 'item-name' }, [item.name || L('common.unknown', 'unknown')]),
      el('div', { class: 'item-meta' }, [
        item.is_directory
          ? L('common.directory', 'directory')
          : formatBytes(Number(item.size) || 0),
      ]),
    ]);
    node.append(icon, body);

    tracker.trackEventListener(node, 'click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (isMultiSelectMode) {
        toggleItemSelection(node, {
          name: item.name,
          is_directory: !!item.is_directory,
          relPath,
          absPath,
          size: item.size,
        });
        return;
      }
      if (item.is_directory) {
        currentPath = Array.isArray(item._segments) ? [...item._segments] : relPath.split('/').filter(Boolean);
        renderCurrentFolder();
      } else {
        window.location.href = `/download_file?path=${encodeURIComponent(relPath)}`;
      }
    });

    tracker.trackEventListener(node, 'contextmenu', (e) => {
      e.preventDefault();
      e.stopPropagation();
      showContextMenu(e, {
        name: item.name,
        is_directory: !!item.is_directory,
        relPath,
        absPath,
        size: item.size,
      });
    });

    container.appendChild(node);
  }
}

function updateCurrentPathDisplay() {
  const wrap = q('#currentPath');
  if (!wrap) return;
  empty(wrap);

  const rootSeg = el('span', { class: 'path-segment' }, ['/']);
  tracker.trackEventListener(rootSeg, 'click', () => {
    currentPath = [];
    renderCurrentFolder();
  });
  wrap.appendChild(rootSeg);

  currentPath.forEach((folder, idx) => {
    const seg = el('span', { class: 'path-segment' }, [folder]);
    tracker.trackEventListener(seg, 'click', () => {
      currentPath = currentPath.slice(0, idx + 1);
      renderCurrentFolder();
    });
    wrap.appendChild(seg);
  });
}

function navigateUp() {
  if (!currentPath.length) return;
  currentPath.pop();
  renderCurrentFolder();
}

function toggleView() {
  isGridView = !isGridView;
  updateViewModeButton();
  renderCurrentFolder();
}

function onSearchInput(e) {
  searchValue = String(e.target?.value || '').toLowerCase().trim();
  const clearBtn = q('#clear-button');
  if (clearBtn) clearBtn.classList.toggle('show', !!searchValue);
  renderCurrentFolder();
}

function clearSearch() {
  const input = q('#search-input');
  if (input) input.value = '';
  searchValue = '';
  const clearBtn = q('#clear-button');
  if (clearBtn) clearBtn.classList.remove('show');
  renderCurrentFolder();
}

function toggleMultiSelect() {
  isMultiSelectMode = !isMultiSelectMode;
  const explorer = q('.file-explorer');
  const btn = q('#multiSelectBtn');
  if (explorer) explorer.classList.toggle('multi-select-mode', isMultiSelectMode);
  if (btn) btn.classList.toggle('active', isMultiSelectMode);
  if (!isMultiSelectMode) clearSelection();
  updateButtonStates();
  updateStaticI18n();
}

function toggleItemSelection(node, item) {
  if (!isMultiSelectMode) return;
  const key = item.relPath;
  if (selectedItems.has(key)) {
    selectedItems.delete(key);
    node.classList.remove('item-selected');
  } else {
    selectedItems.set(key, item);
    node.classList.add('item-selected');
  }
  updateButtonStates();
}

function clearSelection() {
  selectedItems.clear();
  q('#file-list')?.querySelectorAll('.grid-item, .list-item').forEach((n) => n.classList.remove('item-selected'));
  updateButtonStates();
}

function updateButtonStates() {
  const n = selectedItems.size;
  const renameBtn = q('#renameBtn');
  const moveBtn = q('#moveBtn');
  const deleteBtn = q('#deleteBtn');
  const newFolderBtn = q('#newFolderBtn');

  if (renameBtn) {
    renameBtn.style.display = isMultiSelectMode && n === 1 ? 'inline-flex' : 'none';
    renameBtn.disabled = !(isMultiSelectMode && n === 1);
  }
  if (moveBtn) {
    moveBtn.style.display = isMultiSelectMode && n > 0 ? 'inline-flex' : 'none';
    moveBtn.disabled = !(isMultiSelectMode && n > 0);
  }
  if (deleteBtn) {
    deleteBtn.style.display = isMultiSelectMode ? 'inline-flex' : 'none';
    deleteBtn.disabled = n === 0;
    deleteBtn.textContent = `\u{1F5D1} ${L('common.delete', 'Delete')}${n > 0 ? ` (${n})` : ''}`;
  }
  if (newFolderBtn) {
    newFolderBtn.style.display = isMultiSelectMode ? 'none' : 'inline-flex';
  }
}

function showEmptySpaceContextMenu(event) {
  if (event.target !== q('#file-list')) return;
  event.preventDefault();
  removeContextMenu();

  const menu = createContextMenu(event.clientX, event.clientY);
  const newFolder = el('div', {}, [`${L('common.new', 'New')} ${L('common.directory', 'Folder')}`]);
  tracker.trackEventListener(newFolder, 'click', async () => {
    removeContextMenu();
    await createNewFolder();
  });
  menu.appendChild(newFolder);
  openContextMenu(menu);
}

function showContextMenu(event, item) {
  removeContextMenu();
  const menu = createContextMenu(event.clientX, event.clientY);

  const rename = el('div', {}, [L('common.rename', 'Rename')]);
  const duplicate = el('div', {}, [L('common.duplicate', 'Duplicate')]);
  const move = el('div', {}, [t('files.moveTo')]);
  const del = el('div', {}, [L('common.delete', 'Delete')]);

  tracker.trackEventListener(rename, 'click', async () => {
    removeContextMenu();
    await renameItem(item);
  });
  tracker.trackEventListener(duplicate, 'click', async () => {
    removeContextMenu();
    await duplicateItem(item);
  });
  tracker.trackEventListener(move, 'click', async () => {
    removeContextMenu();
    await showMoveToDialog([item]);
  });
  tracker.trackEventListener(del, 'click', async () => {
    removeContextMenu();
    await deleteItems([item], true);
  });

  menu.append(rename, duplicate, move, del);
  openContextMenu(menu);
}

function createContextMenu(x, y) {
  const menu = el('div', { class: 'context-menu' });
  menu.style.position = 'fixed';
  menu.style.left = `${x}px`;
  menu.style.top = `${y}px`;
  return menu;
}

function openContextMenu(menu) {
  const host = root || document.body;
  host.appendChild(menu);
  contextMenuEl = menu;
}

function removeContextMenu() {
  if (contextMenuEl && contextMenuEl.parentElement) {
    contextMenuEl.parentElement.removeChild(contextMenuEl);
  }
  contextMenuEl = null;
}

async function renameSelected() {
  if (selectedItems.size !== 1) return;
  const item = Array.from(selectedItems.values())[0];
  await renameItem(item);
}

async function moveSelected() {
  if (!selectedItems.size) return;
  await showMoveToDialog(Array.from(selectedItems.values()));
}

async function createNewFolder() {
  const folderName = prompt(`${L('common.new', 'New')} ${L('common.directory', 'folder')}:`, 'New Folder');
  if (!folderName) return;
  const rel = buildRelativePath(folderName);
  try {
    const resp = await fetch('/create_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ folder_path: rel }),
    });
    const data = await resp.json();
    if (data.status !== 'success') throw new Error(data.message || 'Failed');
    await loadAllFiles();
    toast(L('common.success', 'Success'), 1600, 'success');
  } catch (err) {
    toast(`${L('common.error', 'Error')}: ${err.message}`, 2800, 'error');
  }
}

async function renameItem(item) {
  const newName = prompt(L('files.newNamePrompt', 'New name:'), item.name);
  if (!newName || newName === item.name) return;

  const parent = item.relPath.split('/').slice(0, -1).join('/');
  const newPath = parent ? `${parent}/${newName}` : newName;

  try {
    const resp = await fetch('/rename_file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_path: item.relPath, new_path: newPath }),
    });
    const data = await resp.json();
    if (data.status !== 'success') throw new Error(data.message || 'Failed');
    await loadAllFiles();
    clearSelection();
    toast(L('files.renamed', 'Renamed'), 1600, 'success');
  } catch (err) {
    toast(`${L('files.renameFailed', 'Rename failed')}: ${err.message}`, 3200, 'error');
  }
}

async function duplicateItem(item) {
  const dot = item.name.lastIndexOf('.');
  const base = dot > 0 ? item.name.slice(0, dot) : item.name;
  const ext = dot > 0 ? item.name.slice(dot) : '';
  const newName = `${base} (copy)${ext}`;
  const parent = item.relPath.split('/').slice(0, -1).join('/');
  const targetPath = parent ? `${parent}/${newName}` : newName;

  try {
    const resp = await fetch('/duplicate_file', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_path: item.relPath, target_path: targetPath }),
    });
    const data = await resp.json();
    if (data.status !== 'success') throw new Error(data.message || 'Failed');
    await loadAllFiles();
    toast(L('files.duplicated', 'Duplicated'), 1600, 'success');
  } catch (err) {
    toast(`${L('files.duplicateFailed', 'Duplicate failed')}: ${err.message}`, 3200, 'error');
  }
}

async function deleteSelectedItems() {
  if (!selectedItems.size) return;
  await deleteItems(Array.from(selectedItems.values()), true);
}

async function deleteItems(items, askConfirm) {
  if (!Array.isArray(items) || !items.length) return;
  if (askConfirm) {
    if (items.length === 1) {
      const one = items[0];
      const label = one.is_directory ? L('common.directory', 'directory') : L('common.file', 'file');
      if (!confirm(L('files.confirmDelete', `Delete ${label} "${one.name}"?`, { label, name: one.name }))) return;
    } else {
      if (!confirm(L('files.confirmDeleteMany', 'Delete {{count}} item(s)?', { count: items.length }))) return;
    }
  }

  const errors = [];
  for (const item of items) {
    const absPath = item.absPath || buildAbsolutePath(item.relPath);
    let ok = false;
    try {
      const r1 = await fetch('/delete_file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file_path: absPath }),
      });
      const d1 = await r1.json();
      ok = d1.status === 'success';
    } catch {
      ok = false;
    }
    if (!ok) {
      try {
        const r2 = await fetch('/delete_file', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file_path: item.relPath }),
        });
        const d2 = await r2.json();
        ok = d2.status === 'success';
      } catch {
        ok = false;
      }
    }
    if (!ok) errors.push(item.name);
  }

  await loadAllFiles();
  clearSelection();
  if (isMultiSelectMode) toggleMultiSelect();
  if (errors.length) toast(`${L('common.error', 'Error')}: ${errors.join(', ')}`, 3800, 'error');
  else toast(L('common.deleted', 'Deleted'), 1600, 'success');
}

async function showMoveToDialog(items) {
  closeMoveDialog();
  selectedTargetPath = null;

  moveModalEl = el('div', { class: 'modal' }, [
    el('div', { class: 'modal-content' }, [
      el('h2', {}, [L('files.moveToTitle', 'Move {{count}} item(s) to...', { count: items.length })]),
      el('div', { id: 'folder-tree' }),
      el('div', { class: 'modal-buttons' }, [
        el('button', { id: 'cancelMoveBtn' }, [L('common.cancel', 'Cancel')]),
        el('button', { class: 'primary', id: 'confirmMoveBtn' }, [L('common.move', 'Move')]),
      ]),
    ]),
  ]);

  (root || document.body).appendChild(moveModalEl);

  const cancelBtn = $('#cancelMoveBtn', moveModalEl);
  const confirmBtn = $('#confirmMoveBtn', moveModalEl);

  if (cancelBtn) tracker.trackEventListener(cancelBtn, 'click', closeMoveDialog);
  if (confirmBtn) tracker.trackEventListener(confirmBtn, 'click', () => processMove(items));
  tracker.trackEventListener(moveModalEl, 'click', (e) => {
    if (e.target === moveModalEl) closeMoveDialog();
  });

  await loadFolderTree();
}

function closeMoveDialog() {
  selectedTargetPath = null;
  if (moveModalEl && moveModalEl.parentElement) {
    moveModalEl.parentElement.removeChild(moveModalEl);
  }
  moveModalEl = null;
}

async function loadFolderTree() {
  if (!moveModalEl) return;
  const treeWrap = $('#folder-tree', moveModalEl);
  if (!treeWrap) return;
  empty(treeWrap);

  try {
    const resp = await fetch('/list_directories');
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const dirs = await resp.json();

    const rootItem = el('div', { class: 'folder-item', 'data-path': '' }, ['/', ' ', L('files.root', 'Root')]);
    treeWrap.appendChild(rootItem);
    bindFolderItem(rootItem);
    renderDirectoryTree(treeWrap, dirs, 1);
  } catch (err) {
    treeWrap.appendChild(el('div', { class: 'item-meta' }, [`${L('common.error', 'Error')}: ${err.message}`]));
  }
}

function renderDirectoryTree(container, dirs, level) {
  for (const dir of (Array.isArray(dirs) ? dirs : [])) {
    if (!dir.is_directory) continue;
    const row = el('div', {
      class: 'folder-item',
      'data-path': dir.path || '',
      style: `padding-left:${level * 16}px`,
    }, ['\u{1F4C1} ', dir.name || 'folder']);
    container.appendChild(row);
    bindFolderItem(row);
    if (Array.isArray(dir.children) && dir.children.length) {
      renderDirectoryTree(container, dir.children, level + 1);
    }
  }
}

function bindFolderItem(node) {
  tracker.trackEventListener(node, 'click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    q('#folder-tree')?.querySelectorAll('.folder-item.selected').forEach((n) => n.classList.remove('selected'));
    node.classList.add('selected');
    selectedTargetPath = node.getAttribute('data-path') || '';
  });
}

async function processMove(items) {
  if (selectedTargetPath == null) {
    toast(L('files.selectDestinationFolder', 'Select a destination folder'), 2200, 'warning');
    return;
  }
  const errors = [];

  for (const item of items) {
    const targetPath = selectedTargetPath ? `${selectedTargetPath}/${item.name}` : item.name;
    try {
      const resp = await fetch('/move_file', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source_path: item.relPath, target_path: targetPath }),
      });
      const data = await resp.json();
      if (data.status !== 'success') errors.push(item.name);
    } catch {
      errors.push(item.name);
    }
  }

  closeMoveDialog();
  await loadAllFiles();
  clearSelection();
  if (errors.length) toast(`${L('common.error', 'Error')}: ${errors.join(', ')}`, 3600, 'error');
  else toast(L('files.moved', 'Moved'), 1600, 'success');
}

function updateViewModeButton() {
  const viewModeBtn = q('#viewModeBtn');
  if (!viewModeBtn) return;
  if (isGridView) {
    viewModeBtn.textContent = '\u2630';
    viewModeBtn.title = L('files.switchToList', 'Switch to list view');
  } else {
    viewModeBtn.textContent = '\u25A6';
    viewModeBtn.title = L('files.switchToGrid', 'Switch to grid view');
  }
}

async function handleFileUploadInput(event) {
  const files = event.target?.files;
  if (!files || !files.length) return;
  await handleFiles(files);
  event.target.value = '';
}

async function handleFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const formData = new FormData();
  files.forEach((file) => {
    const relativeName = file.webkitRelativePath || file.name;
    formData.append('files[]', file, relativeName);
  });
  formData.append('currentPath', JSON.stringify(currentPath));

  setStatus(L('files.uploadingCount', 'Uploading {{count}} file(s)...', { count: files.length }));
  try {
    const resp = await fetch('/upload_files', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.status !== 'success') throw new Error(data.message || 'Upload failed');
    await loadAllFiles();
    toast(L('files.uploadComplete', 'Upload complete'), 1800, 'success');
  } catch (err) {
    toast(`${L('files.uploadFailed', 'Upload failed')}: ${err.message}`, 3000, 'error');
    setStatus(L('files.uploadFailed', 'Upload failed'));
  }
}

function onDropZoneDragOver(e) {
  e.preventDefault();
  q('#drop-zone')?.classList.add('dragover');
}

function onDropZoneDragLeave() {
  q('#drop-zone')?.classList.remove('dragover');
}

async function onDropZoneDrop(e) {
  e.preventDefault();
  q('#drop-zone')?.classList.remove('dragover');
  const dt = e.dataTransfer;
  if (!dt) return;

  if (dt.items && dt.items.length && dt.items[0]?.webkitGetAsEntry) {
    const files = await collectDroppedFiles(dt.items);
    if (files.length) await handleFiles(files);
    return;
  }
  if (dt.files && dt.files.length) await handleFiles(dt.files);
}

async function collectDroppedFiles(items) {
  const files = [];
  const entries = Array.from(items).map((i) => i.webkitGetAsEntry?.()).filter(Boolean);

  async function walk(entry, path = '') {
    if (entry.isFile) {
      const file = await new Promise((resolve) => entry.file(resolve));
      Object.defineProperty(file, 'webkitRelativePath', { value: path + entry.name, configurable: true });
      files.push(file);
      return;
    }
    if (!entry.isDirectory) return;

    const reader = entry.createReader();
    const children = await new Promise((resolve) => {
      const acc = [];
      function read() {
        reader.readEntries((batch) => {
          if (batch.length) {
            acc.push(...batch);
            read();
          } else {
            resolve(acc);
          }
        });
      }
      read();
    });
    const next = path + entry.name + '/';
    for (const child of children) {
      // eslint-disable-next-line no-await-in-loop
      await walk(child, next);
    }
  }

  for (const entry of entries) {
    // eslint-disable-next-line no-await-in-loop
    await walk(entry);
  }
  return files;
}

function buildRelativePath(fileName) {
  return [...currentPath, fileName].filter(Boolean).join('/');
}

function buildAbsolutePath(relPath) {
  const cleanRel = String(relPath || '').replace(/^\/+/, '').replace(/\\/g, '/');
  if (!cleanRel) return absoluteBasePath;
  return `${absoluteBasePath.replace(/\/+$/, '')}/${cleanRel}`;
}

function formatBytes(bytes, decimals = 1) {
  const n = Number(bytes) || 0;
  if (n <= 0) return '0 B';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(n) / Math.log(k));
  return `${parseFloat((n / Math.pow(k, i)).toFixed(dm))} ${sizes[i]}`;
}

function setStatus(msg) {
  const status = q('#files-status');
  if (status) status.textContent = String(msg || '');
}
