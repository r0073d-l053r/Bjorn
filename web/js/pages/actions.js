/**
 * Actions page (SPA) — old actions_launcher parity.
 * Sidebar (actions/arguments) + multi-console panes.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api } from '../core/api.js';
import { el, $, $$, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { initSharedSidebarLayout } from '../core/sidebar-layout.js';

const PAGE = 'actions';

let tracker = null;
let root = null;
let sidebarLayoutCleanup = null;

let actions = [];
let activeActionId = null;
let panes = [null, null, null, null];
let split = 1;
let assignTargetPaneIndex = null;
let focusedPaneIndex = 0;
let searchQuery = '';
let currentTab = 'actions';

const logsByAction = new Map(); // actionId -> string[]
const pollingTimers = new Map(); // actionId -> timeoutId
const autoClearPane = [false, false, false, false];

function isMobile() {
  return window.matchMedia('(max-width: 860px)').matches;
}

const STATE_KEY = 'bjorn.actions.state';

function saveState() {
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify({
      split,
      panes,
      activeActionId,
      focusedPaneIndex,
      autoClear: [...autoClearPane],
    }));
  } catch { /* noop */ }
}

function restoreState() {
  try {
    const raw = sessionStorage.getItem(STATE_KEY);
    if (!raw) return false;
    const s = JSON.parse(raw);
    if (typeof s.split === 'number' && s.split >= 1 && s.split <= 4) split = s.split;
    if (Array.isArray(s.panes)) {
      panes = s.panes.slice(0, 4).map(v => v || null);
      while (panes.length < 4) panes.push(null);
    }
    if (s.activeActionId) activeActionId = s.activeActionId;
    if (typeof s.focusedPaneIndex === 'number') focusedPaneIndex = s.focusedPaneIndex;
    if (Array.isArray(s.autoClear)) {
      for (let i = 0; i < 4; i++) autoClearPane[i] = !!s.autoClear[i];
    }
    return true;
  } catch { return false; }
}

async function recoverLogs() {
  const seen = new Set();
  for (const actionId of panes) {
    if (!actionId || seen.has(actionId)) continue;
    seen.add(actionId);
    const action = actions.find(a => a.id === actionId);
    if (!action) continue;
    try {
      const scriptPath = action.path || action.module || action.id;
      const res = await api.get('/get_script_output/' + encodeURIComponent(scriptPath), { timeout: 8000, retries: 0 });
      if (res?.status === 'success' && res.data) {
        const output = Array.isArray(res.data.output) ? res.data.output : [];
        if (output.length) logsByAction.set(actionId, output);
        if (res.data.is_running) {
          action.status = 'running';
          startOutputPolling(actionId);
        }
      }
    } catch { /* ignore */ }
  }
  renderActionsList();
  renderConsoles();
}

function q(sel, base = root) { return base?.querySelector(sel) || null; }

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  sidebarLayoutCleanup = initSharedSidebarLayout(root, {
    sidebarSelector: '.al-sidebar',
    mainSelector: '#actionsLauncher',
    storageKey: 'sidebar:actions',
    toggleLabel: t('common.menu'),
  });

  bindStaticEvents();
  enforceMobileOnePane();

  await loadActions();
  const restored = restoreState();
  if (restored) {
    // Validate pane assignments
    for (let i = 0; i < panes.length; i++) {
      if (panes[i] && !actions.some(a => a.id === panes[i])) panes[i] = null;
    }
    // Update split segment buttons
    $$('#splitSeg button', root).forEach(btn =>
      btn.classList.toggle('active', Number(btn.dataset.split) === split)
    );
  }
  enforceMobileOnePane();
  renderActionsList();
  renderConsoles();
  if (restored) recoverLogs();
}

export function unmount() {
  saveState();
  if (typeof sidebarLayoutCleanup === 'function') {
    sidebarLayoutCleanup();
    sidebarLayoutCleanup = null;
  }

  clearTimeout(onResizeDebounced._t);
  onResizeDebounced._t = null;

  for (const tmr of pollingTimers.values()) clearTimeout(tmr);
  pollingTimers.clear();

  if (tracker) {
    tracker.cleanupAll();
    tracker = null;
  }

  root = null;
  actions = [];
  activeActionId = null;
  panes = [null, null, null, null];
  split = 1;
  assignTargetPaneIndex = null;
  focusedPaneIndex = 0;
  searchQuery = '';
  currentTab = 'actions';
  logsByAction.clear();
}

function buildShell() {
  const sideTabs = el('div', { class: 'tabs-container' }, [
    el('button', { class: 'tab-btn active', id: 'tabBtnActions', type: 'button' }, [t('actions.tabs.actions')]),
    el('button', { class: 'tab-btn', id: 'tabBtnArgs', type: 'button' }, [t('actions.tabs.arguments')]),
    el('button', { class: 'tab-btn', id: 'tabBtnPkgs', type: 'button' }, ['Packages']),
  ]);

  const sideHeader = el('div', { class: 'sideheader' }, [
    el('div', { class: 'al-side-meta' }, [
      el('div', { class: 'sidetitle' }, [t('actions.title')]),
      el('button', { class: 'al-btn', id: 'hideSidebar', 'data-hide-sidebar': '1', type: 'button' }, [t('common.hide')]),
    ]),
    sideTabs,
    el('div', { class: 'al-search' }, [
      el('input', {
        id: 'searchInput',
        class: 'al-input',
        type: 'text',
        placeholder: t('actions.searchPlaceholder'),
      }),
    ]),
  ]);

  const actionsSidebar = el('div', { id: 'tab-actions', class: 'sidebar-page al-split-layout' }, [
    el('div', { id: 'actionsList', class: 'al-list al-builtins-scroll' }),
    el('div', { class: 'al-custom-section' }, [
      el('div', { class: 'al-section-divider' }, [
        el('span', { class: 'al-section-title' }, ['Custom Scripts']),
        el('button', { class: 'al-btn al-upload-btn', type: 'button' }, ['\u2B06 Upload']),
      ]),
      el('div', { id: 'customActionsList', class: 'al-list al-custom-scroll' }),
    ]),
  ]);

  const argsSidebar = el('div', { id: 'tab-arguments', class: 'sidebar-page', style: 'display:none' }, [
    el('div', { class: 'section' }, [
      el('div', { class: 'h' }, [t('actions.args.title')]),
      el('div', { class: 'sub' }, [t('actions.args.subtitle')]),
    ]),
    el('div', { id: 'argBuilder', class: 'builder' }),
    el('div', { class: 'section' }, [
      el('input', {
        id: 'freeArgs',
        class: 'ctl',
        type: 'text',
        placeholder: t('actions.args.free'),
      }),
    ]),
    el('div', { id: 'presetChips', class: 'chips' }),
  ]);

  const pkgsSidebar = el('div', { id: 'tab-packages', class: 'sidebar-page', style: 'display:none' }, [
    el('div', { class: 'pkg-install-form' }, [
      el('input', { type: 'text', class: 'pkg-install-input', placeholder: 'Package name (e.g. requests)', id: 'pkgNameInput' }),
      el('button', { class: 'pkg-install-btn', type: 'button' }, ['Install']),
    ]),
    el('div', { class: 'pkg-console', id: 'pkgConsole' }),
    el('ul', { class: 'pkg-list', id: 'pkgList' }),
  ]);

  const sideContent = el('div', { class: 'sidecontent' }, [actionsSidebar, argsSidebar, pkgsSidebar]);

  const sidebarPanel = el('aside', { class: 'panel al-sidebar' }, [sideHeader, sideContent]);

  const splitSeg = el('div', { class: 'seg', id: 'splitSeg' }, [
    el('button', { type: 'button', 'data-split': '1', class: 'active' }, ['1']),
    el('button', { type: 'button', 'data-split': '2' }, ['2']),
    el('button', { type: 'button', 'data-split': '3' }, ['3']),
    el('button', { type: 'button', 'data-split': '4' }, ['4']),
  ]);

  const toolbar = el('div', { class: 'toolbar2' }, [
    el('div', { class: 'spacer' }),
    splitSeg,
  ]);

  const multiConsole = el('div', { class: 'multiConsole split-1', id: 'multiConsole' });

  const centerPanel = el('section', { class: 'center panel' }, [toolbar, multiConsole]);

  return el('div', { class: 'actions-container page-with-sidebar' }, [
    sidebarPanel,
    el('main', { id: 'actionsLauncher' }, [centerPanel]),
  ]);
}

function bindStaticEvents() {
  // Hidden file input for custom script uploads
  const fileInput = el('input', { type: 'file', accept: '.py', id: 'customScriptFileInput', style: 'display:none' });
  root.appendChild(fileInput);
  tracker.trackEventListener(fileInput, 'change', () => {
    const file = fileInput.files?.[0];
    if (file) {
      uploadCustomScript(file);
      fileInput.value = '';
    }
  });

  // Wire upload button (now static in buildShell)
  const uploadBtnStatic = q('.al-upload-btn');
  if (uploadBtnStatic) {
    tracker.trackEventListener(uploadBtnStatic, 'click', () => {
      const fi = q('#customScriptFileInput');
      if (fi) fi.click();
    });
  }

  const tabActions = q('#tabBtnActions');
  const tabArgs = q('#tabBtnArgs');
  const tabPkgs = q('#tabBtnPkgs');

  if (tabActions) tracker.trackEventListener(tabActions, 'click', () => switchTab('actions'));
  if (tabArgs) tracker.trackEventListener(tabArgs, 'click', () => switchTab('arguments'));
  if (tabPkgs) tracker.trackEventListener(tabPkgs, 'click', () => switchTab('packages'));

  const pkgInstallBtn = q('.pkg-install-btn');
  if (pkgInstallBtn) tracker.trackEventListener(pkgInstallBtn, 'click', () => installPackage());

  const searchInput = q('#searchInput');
  if (searchInput) {
    tracker.trackEventListener(searchInput, 'input', () => {
      searchQuery = String(searchInput.value || '').trim().toLowerCase();
      renderActionsList();
    });
  }

  $$('#splitSeg button', root).forEach((btn) => {
    tracker.trackEventListener(btn, 'click', () => {
      if (isMobile()) {
        enforceMobileOnePane();
        return;
      }
      split = Number(btn.dataset.split || '1');
      $$('#splitSeg button', root).forEach((b) => b.classList.toggle('active', b === btn));
      renderConsoles();
      saveState();
    });
  });

  tracker.trackEventListener(window, 'resize', onResizeDebounced);
}

function onResizeDebounced() {
  clearTimeout(onResizeDebounced._t);
  onResizeDebounced._t = setTimeout(() => {
    enforceMobileOnePane();
    renderConsoles();
  }, 120);
}

function switchTab(tab) {
  currentTab = tab;
  const tabActions = q('#tabBtnActions');
  const tabArgs = q('#tabBtnArgs');
  const tabPkgs = q('#tabBtnPkgs');
  const actionsPane = q('#tab-actions');
  const argsPane = q('#tab-arguments');
  const pkgsPane = q('#tab-packages');

  if (tabActions) tabActions.classList.toggle('active', tab === 'actions');
  if (tabArgs) tabArgs.classList.toggle('active', tab === 'arguments');
  if (tabPkgs) tabPkgs.classList.toggle('active', tab === 'packages');
  if (actionsPane) actionsPane.style.display = tab === 'actions' ? '' : 'none';
  if (argsPane) argsPane.style.display = tab === 'arguments' ? '' : 'none';
  if (pkgsPane) pkgsPane.style.display = tab === 'packages' ? '' : 'none';

  if (tab === 'packages') loadPackages();
}

function enforceMobileOnePane() {
  if (!isMobile()) {
    $$('#splitSeg button', root).forEach((btn) => {
      btn.disabled = false;
      btn.style.opacity = '';
      btn.style.pointerEvents = '';
    });
    return;
  }

  split = 1;
  if (!panes[0] && activeActionId) panes[0] = activeActionId;
  for (let i = 1; i < panes.length; i++) panes[i] = null;

  $$('#splitSeg button', root).forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.split === '1');
    btn.disabled = true;
    btn.style.opacity = '0.6';
    btn.style.pointerEvents = 'none';
  });
}

async function loadActions() {
  try {
    const response = await api.get('/list_scripts', { timeout: 12000, retries: 1 });
    const list = Array.isArray(response?.data) ? response.data : [];

    const prev = new Map(actions.map((a) => [a.id, a.status]));

    actions = list.map((raw) => normalizeAction(raw));
    actions.forEach((a) => {
      a.status = prev.get(a.id) || (a.is_running ? 'running' : 'ready');
      if (!logsByAction.has(a.id)) logsByAction.set(a.id, []);
    });

    if (activeActionId && !actions.some((a) => a.id === activeActionId)) {
      activeActionId = null;
      empty(q('#argBuilder'));
      empty(q('#presetChips'));
    }
  } catch (err) {
    toast(`${t('common.error')}: ${err.message}`, 2600, 'error');
    actions = [];
  }
}

function normalizeAction(raw) {
  const id = raw.b_module || (raw.name ? raw.name.replace(/\.py$/, '') : 'unknown');

  let args = raw.b_args ?? {};
  if (typeof args === 'string') {
    try { args = JSON.parse(args); } catch { args = {}; }
  }

  let examples = raw.b_examples;
  if (typeof examples === 'string') {
    try { examples = JSON.parse(examples); } catch { examples = []; }
  }
  if (!Array.isArray(examples)) examples = [];

  return {
    id,
    name: raw.name || raw.b_class || raw.b_module || 'Unnamed',
    module: raw.b_module || raw.module || id,
    bClass: raw.b_class || id,
    category: (raw.b_action || raw.category || 'normal').toLowerCase(),
    description: raw.description || t('common.description'),
    args,
    icon: raw.b_icon || `/actions_icons/${encodeURIComponent(raw.b_class || id)}.png`,
    version: raw.b_version || '',
    author: raw.b_author || '',
    docsUrl: raw.b_docs_url || '',
    examples,
    path: raw.path || raw.module_path || raw.b_module || id,
    is_running: !!raw.is_running,
    status: raw.is_running ? 'running' : 'ready',
    isCustom: !!raw.is_custom,
    scriptFormat: raw.script_format || 'bjorn',
  };
}

function renderActionsList() {
  const builtinContainer = q('#actionsList');
  const customContainer = q('#customActionsList');
  if (!builtinContainer) return;
  empty(builtinContainer);
  if (customContainer) empty(customContainer);

  const filtered = actions.filter((a) => {
    if (!searchQuery) return true;
    const hay = `${a.name} ${a.description} ${a.module} ${a.id} ${a.author} ${a.category}`.toLowerCase();
    return searchQuery.split(/\s+/).every((term) => hay.includes(term));
  });

  const builtIn = filtered.filter((a) => a.category !== 'custom');
  const custom = filtered.filter((a) => a.category === 'custom');

  if (!builtIn.length && !custom.length) {
    builtinContainer.appendChild(el('div', { class: 'sub' }, [t('actions.noActions')]));
    return;
  }

  for (const a of builtIn) {
    builtinContainer.appendChild(buildActionRow(a));
  }
  if (!builtIn.length) {
    builtinContainer.appendChild(el('div', { class: 'sub' }, [t('actions.noActions')]));
  }

  if (customContainer) {
    if (!custom.length) {
      customContainer.appendChild(el('div', { class: 'sub', style: 'padding:6px 12px;font-size:11px' }, ['No custom scripts uploaded.']));
    }
    for (const a of custom) {
      customContainer.appendChild(buildActionRow(a, true));
    }
  }
}

function buildActionRow(a, isCustom = false) {
  const badges = [];
  if (isCustom) {
    badges.push(el('span', { class: 'chip format-badge' }, [a.scriptFormat]));
  }

  const infoBlock = el('div', {}, [
    el('div', { class: 'name' }, [a.name]),
    el('div', { class: 'desc' }, [a.description]),
  ]);

  const rowChildren = [
    el('div', { class: 'ic' }, [
      el('img', {
        class: 'ic-img',
        src: a.icon,
        alt: '',
        onerror: (e) => {
          e.target.onerror = null;
          e.target.src = '/actions/actions_icons/default.png';
        },
      }),
    ]),
    infoBlock,
    ...badges,
    el('div', { class: `chip ${statusChipClass(a.status)}` }, [statusChipText(a.status)]),
  ];

  if (isCustom) {
    const deleteBtn = el('button', { class: 'al-btn al-delete-btn', type: 'button', title: 'Delete script' }, ['\uD83D\uDDD1']);
    tracker.trackEventListener(deleteBtn, 'click', (ev) => {
      ev.stopPropagation();
      deleteCustomScript(a.bClass);
    });
    rowChildren.push(deleteBtn);
  }

  const row = el('div', { class: `al-row${a.id === activeActionId ? ' selected' : ''}`, draggable: 'true', 'data-action-id': a.id }, rowChildren);

  tracker.trackEventListener(row, 'click', () => onActionSelected(a.id));
  tracker.trackEventListener(row, 'dragstart', (ev) => {
    ev.dataTransfer?.setData('text/plain', a.id);
  });

  return row;
}

async function uploadCustomScript(file) {
  const formData = new FormData();
  formData.append('script_file', file);
  try {
    const resp = await fetch('/upload_custom_script', { method: 'POST', body: formData });
    const data = await resp.json();
    if (data.status === 'success') {
      toast('Custom script uploaded', 1800, 'success');
      await loadActions();
      renderActionsList();
    } else {
      toast(`Upload failed: ${data.message || 'Unknown error'}`, 2600, 'error');
    }
  } catch (err) {
    toast(`Upload error: ${err.message}`, 2600, 'error');
  }
}

async function deleteCustomScript(bClass) {
  if (!confirm(`Delete custom script "${bClass}"?`)) return;
  try {
    const resp = await api.post('/delete_custom_script', { script_name: bClass });
    if (resp.status === 'success') {
      toast('Custom script deleted', 1800, 'success');
      await loadActions();
      renderActionsList();
    } else {
      toast(`Delete failed: ${resp.message || 'Unknown error'}`, 2600, 'error');
    }
  } catch (err) {
    toast(`Delete error: ${err.message}`, 2600, 'error');
  }
}

function statusChipClass(status) {
  if (status === 'running') return 'run';
  if (status === 'success') return 'ok';
  if (status === 'error') return 'err';
  return '';
}

function statusChipText(status) {
  if (status === 'running') return t('actions.running');
  if (status === 'success') return t('common.success');
  if (status === 'error') return t('common.error');
  return t('common.ready');
}

function onActionSelected(actionId) {
  activeActionId = actionId;
  const action = actions.find((a) => a.id === actionId);
  if (!action) return;

  renderActionsList();
  renderArguments(action);

  if (assignTargetPaneIndex != null) {
    panes[assignTargetPaneIndex] = actionId;
    clearAssignTarget();
    renderConsoles();
    return;
  }

  const existing = panes.findIndex((id) => id === actionId);
  if (existing >= 0) {
    highlightPane(existing);
    return;
  }

  const effectiveSplit = isMobile() ? 1 : split;
  let target = panes.slice(0, effectiveSplit).findIndex((id) => !id);
  if (target < 0) target = 0;
  panes[target] = actionId;
  renderConsoles();
  saveState();
}

function renderArguments(action) {
  switchTab('arguments');

  const builder = q('#argBuilder');
  const chips = q('#presetChips');
  if (!builder || !chips) return;
  empty(builder);
  empty(chips);

  builder.appendChild(el('div', { class: 'args-pane-label' }, [
    `Pane ${focusedPaneIndex + 1}: ${action.name}`
  ]));

  const metaBits = [];
  if (action.version) metaBits.push(`v${action.version}`);
  if (action.author) metaBits.push(t('actions.byAuthor', { author: action.author }));

  if (metaBits.length || action.docsUrl) {
    const top = el('div', { style: 'display:flex;justify-content:space-between;gap:8px;align-items:center' }, [
      el('div', { class: 'sub' }, [metaBits.join(' • ')]),
      action.docsUrl
        ? el('a', { class: 'al-btn', href: action.docsUrl, target: '_blank', rel: 'noopener noreferrer' }, [t('actions.docs')])
        : null,
    ]);
    builder.appendChild(top);
  }

  const entries = Object.entries(action.args || {});
  if (!entries.length) {
    builder.appendChild(el('div', { class: 'sub' }, [t('actions.args.none')]));
  }

  for (const [key, cfgRaw] of entries) {
    const cfg = cfgRaw && typeof cfgRaw === 'object' ? cfgRaw : { type: 'text', default: cfgRaw };

    const field = el('div', { class: 'field' }, [
      el('div', { class: 'label' }, [cfg.label || key]),
      createArgControl(key, cfg),
      cfg.help ? el('div', { class: 'sub' }, [cfg.help]) : null,
    ]);
    builder.appendChild(field);
  }

  const presets = Array.isArray(action.examples) ? action.examples : [];
  for (let i = 0; i < presets.length; i++) {
    const p = presets[i];
    const label = p.name || p.title || t('actions.preset', { n: i + 1 });
    const btn = el('button', { class: 'chip2', type: 'button' }, [label]);
    tracker.trackEventListener(btn, 'click', () => applyPreset(p));
    chips.appendChild(btn);
  }
}

function createArgControl(key, cfg) {
  const tpe = cfg.type || 'text';

  if (tpe === 'select') {
    const sel = el('select', { class: 'select', 'data-arg': key });
    const choices = Array.isArray(cfg.choices) ? cfg.choices : [];
    for (const c of choices) {
      const opt = el('option', { value: String(c) }, [String(c)]);
      if (cfg.default != null && String(cfg.default) === String(c)) opt.selected = true;
      sel.appendChild(opt);
    }
    return sel;
  }

  if (tpe === 'checkbox') {
    const ctl = el('input', { type: 'checkbox', class: 'ctl', 'data-arg': key });
    ctl.checked = !!cfg.default;
    return ctl;
  }

  if (tpe === 'number') {
    const attrs = {
      type: 'number',
      class: 'ctl',
      'data-arg': key,
      value: cfg.default != null ? String(cfg.default) : '',
    };
    if (cfg.min != null) attrs.min = String(cfg.min);
    if (cfg.max != null) attrs.max = String(cfg.max);
    if (cfg.step != null) attrs.step = String(cfg.step);
    return el('input', attrs);
  }

  if (tpe === 'range' || tpe === 'slider') {
    const min = cfg.min != null ? Number(cfg.min) : 0;
    const max = cfg.max != null ? Number(cfg.max) : 100;
    const val = cfg.default != null ? Number(cfg.default) : min;

    const wrap = el('div', { style: 'display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center' });
    const range = el('input', {
      type: 'range',
      class: 'range',
      'data-arg': key,
      min: String(min),
      max: String(max),
      step: String(cfg.step != null ? cfg.step : 1),
      value: String(val),
    });
    const out = el('span', { class: 'sub' }, [String(val)]);
    tracker.trackEventListener(range, 'input', () => { out.textContent = range.value; });
    wrap.appendChild(range);
    wrap.appendChild(out);
    return wrap;
  }

  return el('input', {
    type: 'text',
    class: 'ctl',
    'data-arg': key,
    value: cfg.default != null ? String(cfg.default) : '',
    placeholder: cfg.placeholder || '',
  });
}

function applyPreset(preset) {
  const builder = q('#argBuilder');
  if (!builder) return;

  for (const [k, v] of Object.entries(preset || {})) {
    if (k === 'name' || k === 'title') continue;
    const input = builder.querySelector(`[data-arg="${k}"]`);
    if (!input) continue;

    if (input.type === 'checkbox') input.checked = !!v;
    else input.value = String(v ?? '');
  }

  toast(t('actions.toast.presetApplied'), 1400, 'success');
}

function collectArguments() {
  const args = [];
  const builder = q('#argBuilder');
  if (builder) {
    const controls = $$('[data-arg]', builder);
    controls.forEach((ctl) => {
      const key = ctl.getAttribute('data-arg');
      const flag = '--' + String(key).replace(/_/g, '-');

      if (ctl.type === 'checkbox') {
        if (ctl.checked) args.push(flag);
        return;
      }

      const value = String(ctl.value ?? '').trim();
      if (!value) return;
      args.push(flag, value);
    });
  }

  const free = String(q('#freeArgs')?.value || '').trim();
  if (free) args.push(...free.split(/\s+/));

  return args.join(' ');
}

function renderConsoles() {
  const container = q('#multiConsole');
  if (!container) return;

  const effectiveSplit = isMobile() ? 1 : split;
  container.className = `multiConsole split-${effectiveSplit}`;
  container.style.setProperty('--rows', effectiveSplit === 4 ? '2' : '1');
  empty(container);

  for (let i = effectiveSplit; i < panes.length; i++) panes[i] = null;

  for (let i = 0; i < effectiveSplit; i++) {
    const actionId = panes[i];
    const action = actionId ? actions.find((a) => a.id === actionId) : null;

    const pane = el('div', { class: 'pane', 'data-index': String(i) });

    const title = el('div', { class: 'paneTitle' }, [
      el('span', { class: 'dot', style: `background:${statusDotColor(action?.status || 'ready')}` }),
      action ? el('img', {
        class: 'paneIcon',
        src: action.icon,
        alt: '',
        onerror: (e) => {
          e.target.onerror = null;
          e.target.src = '/actions/actions_icons/default.png';
        },
      }) : null,
      el('div', { class: 'titleBlock' }, [
        el('div', { class: 'titleLine' }, [el('strong', {}, [action ? action.name : t('actions.emptyPane')])]),
        action ? el('div', { class: 'metaLine' }, [
          action.version ? el('span', { class: 'chip' }, ['v' + action.version]) : null,
          action.author ? el('span', { class: 'chip' }, [t('actions.byAuthor', { author: action.author })]) : null,
        ]) : null,
      ]),
    ]);

    const paneBtns = el('div', { class: 'paneBtns' });
    if (!action) {
      const assignBtn = el('button', { class: 'al-btn', type: 'button' }, [t('actions.assign')]);
      tracker.trackEventListener(assignBtn, 'click', () => setAssignTarget(i));
      paneBtns.appendChild(assignBtn);
    } else {
      const runBtn = el('button', { class: 'al-btn', type: 'button' }, [t('common.run')]);
      tracker.trackEventListener(runBtn, 'click', () => runActionInPane(i));

      const stopBtn = el('button', { class: 'al-btn warn', type: 'button' }, [t('common.stop')]);
      tracker.trackEventListener(stopBtn, 'click', () => stopActionInPane(i));

      const clearBtn = el('button', { class: 'al-btn', type: 'button' }, [t('common.clear')]);
      tracker.trackEventListener(clearBtn, 'click', () => clearActionLogs(action.id));

      const exportBtn = el('button', { class: 'al-btn', type: 'button' }, ['\u2B07 ' + t('actions.exportLogs')]);
      tracker.trackEventListener(exportBtn, 'click', () => exportActionLogs(action.id, action.name));

      const autoBtn = el('button', { class: 'al-btn', type: 'button' }, [autoClearPane[i] ? t('actions.autoClearOn') : t('actions.autoClearOff')]);
      if (autoClearPane[i]) autoBtn.classList.add('warn');
      tracker.trackEventListener(autoBtn, 'click', () => {
        autoClearPane[i] = !autoClearPane[i];
        renderConsoles();
      });

      paneBtns.appendChild(runBtn);
      paneBtns.appendChild(stopBtn);
      paneBtns.appendChild(clearBtn);
      paneBtns.appendChild(exportBtn);
      paneBtns.appendChild(autoBtn);
    }

    const header = el('div', { class: 'paneHeader' }, [title, paneBtns]);
    const log = el('div', { class: 'paneLog', id: `paneLog-${i}` });

    pane.appendChild(header);
    pane.appendChild(log);
    container.appendChild(pane);

    tracker.trackEventListener(pane, 'dragover', (e) => {
      e.preventDefault();
      pane.classList.add('paneHighlight');
    });
    tracker.trackEventListener(pane, 'dragleave', () => pane.classList.remove('paneHighlight'));
    tracker.trackEventListener(pane, 'drop', (e) => {
      e.preventDefault();
      pane.classList.remove('paneHighlight');
      const dropped = e.dataTransfer?.getData('text/plain');
      if (!dropped) return;
      panes[i] = dropped;
      renderConsoles();
      saveState();
    });

    tracker.trackEventListener(pane, 'click', () => {
      focusedPaneIndex = i;
      $$('.pane', root).forEach((p, idx) => p.classList.toggle('paneFocused', idx === i));
      const pAction = actionId ? actions.find(a => a.id === actionId) : null;
      if (pAction) {
        activeActionId = pAction.id;
        renderArguments(pAction);
        renderActionsList();
      }
      saveState();
    });

    renderPaneLog(i, actionId);
  }
}

function renderPaneLog(index, actionId) {
  const logEl = q(`#paneLog-${index}`);
  if (!logEl) return;
  empty(logEl);

  if (!actionId) {
    logEl.appendChild(el('div', { class: 'logline dim' }, [t('actions.selectAction')]));
    return;
  }

  const lines = logsByAction.get(actionId) || [];
  if (!lines.length) {
    logEl.appendChild(el('div', { class: 'logline dim' }, [t('actions.waitingLogs')]));
    return;
  }

  for (const line of lines) {
    logEl.appendChild(el('div', { class: `logline ${logLineClass(line)}` }, [String(line)]));
  }

  logEl.scrollTop = logEl.scrollHeight;
}

function logLineClass(line) {
  const l = String(line || '').toLowerCase();
  if (l.includes('error') || l.includes('failed') || l.includes('traceback')) return 'err';
  if (l.includes('warn')) return 'warn';
  if (l.includes('success') || l.includes('done') || l.includes('complete')) return 'ok';
  if (l.includes('info') || l.includes('start')) return 'info';
  return 'dim';
}

function statusDotColor(status) {
  if (status === 'running') return 'var(--acid)';
  if (status === 'success') return 'var(--ok)';
  if (status === 'error') return 'var(--danger)';
  return 'var(--accent-2, #18f0ff)';
}

function setAssignTarget(index) {
  assignTargetPaneIndex = index;
  $$('.pane', root).forEach((p) => p.classList.remove('paneHighlight'));
  q(`.pane[data-index="${index}"]`)?.classList.add('paneHighlight');
  switchTab('actions');
}

function clearAssignTarget() {
  assignTargetPaneIndex = null;
  $$('.pane', root).forEach((p) => p.classList.remove('paneHighlight'));
}

function highlightPane(index) {
  const pane = q(`.pane[data-index="${index}"]`);
  if (!pane) return;
  pane.classList.add('paneHighlight');
  if (tracker) tracker.trackTimeout(() => pane.classList.remove('paneHighlight'), 900);
  else setTimeout(() => pane.classList.remove('paneHighlight'), 900);
}

async function runActionInPane(index) {
  const actionId = panes[index] || activeActionId;
  const action = actions.find((a) => a.id === actionId);
  if (!action) {
    toast(t('actions.toast.selectActionFirst'), 1600, 'warning');
    return;
  }

  // Auto-focus pane and render its args before collecting
  if (focusedPaneIndex !== index) {
    focusedPaneIndex = index;
    $$('.pane', root).forEach((p, idx) => p.classList.toggle('paneFocused', idx === index));
    if (action) renderArguments(action);
  }

  if (!panes[index]) panes[index] = action.id;
  if (autoClearPane[index]) clearActionLogs(action.id);

  action.status = 'running';
  renderActionsList();
  renderConsoles();

  const args = collectArguments();
  appendActionLog(action.id, t('actions.toast.startingAction', { name: action.name }));

  try {
    const res = await api.post('/run_script', { script_name: action.module || action.id, args });
    if (res.status !== 'success') throw new Error(res.message || 'Run failed');
    startOutputPolling(action.id);
    saveState();
  } catch (err) {
    action.status = 'error';
    appendActionLog(action.id, `Error: ${err.message}`);
    renderActionsList();
    renderConsoles();
    toast(`${t('common.error')}: ${err.message}`, 2600, 'error');
  }
}

async function stopActionInPane(index) {
  const actionId = panes[index] || activeActionId;
  const action = actions.find((a) => a.id === actionId);
  if (!action) return;

  try {
    const res = await api.post('/stop_script', { script_name: action.path || action.module || action.id });
    if (res.status !== 'success') throw new Error(res.message || 'Stop failed');

    action.status = 'ready';
    stopOutputPolling(action.id);
    appendActionLog(action.id, t('actions.toast.stoppedByUser'));
    renderActionsList();
    renderConsoles();
    saveState();
  } catch (err) {
    toast(`${t('actions.toast.failedToStop')}: ${err.message}`, 2600, 'error');
  }
}

function clearActionLogs(actionId) {
  logsByAction.set(actionId, []);
  for (let i = 0; i < panes.length; i++) if (panes[i] === actionId) renderPaneLog(i, actionId);

  const action = actions.find((a) => a.id === actionId);
  if (action) {
    api.post('/clear_script_output', { script_name: action.path || action.module || action.id }).catch(() => {});
  }
}

function exportActionLogs(actionId, actionName = 'action') {
  const logs = logsByAction.get(actionId) || [];
  if (!logs.length) {
    toast(t('actions.toast.noLogsToExport'), 1600, 'warning');
    return;
  }

  const blob = new Blob([logs.join('\n')], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${actionName}_logs_${Date.now()}.txt`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function appendActionLog(actionId, line) {
  const list = logsByAction.get(actionId) || [];
  list.push(line);
  logsByAction.set(actionId, list);
  for (let i = 0; i < panes.length; i++) if (panes[i] === actionId) renderPaneLog(i, actionId);
}

function startOutputPolling(actionId) {
  stopOutputPolling(actionId);

  const action = actions.find((a) => a.id === actionId);
  if (!action) return;

  const scriptPath = action.path || action.module || action.id;

  const tick = async () => {
    try {
      const res = await api.get(`/get_script_output/${encodeURIComponent(scriptPath)}`, { timeout: 8000, retries: 0 });
      if (res?.status !== 'success') throw new Error('Invalid output payload');

      const data = res.data || {};
      const output = Array.isArray(data.output) ? data.output : [];
      logsByAction.set(actionId, output);

      if (data.is_running) {
        action.status = 'running';
        renderActionsList();
        for (let i = 0; i < panes.length; i++) if (panes[i] === actionId) renderPaneLog(i, actionId);
        const id = setTimeout(tick, 1000);
        pollingTimers.set(actionId, id);
        return;
      }

      if (data.last_error) {
        action.status = 'error';
        appendActionLog(actionId, `Error: ${data.last_error}`);
      } else {
        action.status = 'success';
        appendActionLog(actionId, t('actions.logs.completed'));
      }

      stopOutputPolling(actionId);
      renderActionsList();
      renderConsoles();
    } catch {
      // Keep trying while action is expected running.
      if (action.status === 'running') {
        const id = setTimeout(tick, 1200);
        pollingTimers.set(actionId, id);
      }
    }
  };

  tick();
}

function stopOutputPolling(actionId) {
  const timer = pollingTimers.get(actionId);
  if (timer) {
    clearTimeout(timer);
    pollingTimers.delete(actionId);
  }
}

/* ── Package Management ────────────────────────────── */

async function installPackage() {
  const input = document.getElementById('pkgNameInput');
  const name = (input?.value || '').trim();
  if (!name) return;

  if (!/^[a-zA-Z0-9._-]+$/.test(name)) {
    toast('Invalid package name', 3000, 'error');
    return;
  }

  const consoleEl = document.getElementById('pkgConsole');
  if (consoleEl) {
    consoleEl.classList.add('active');
    consoleEl.textContent = '';
  }

  const evtSource = new EventSource(`/api/packages/install?name=${encodeURIComponent(name)}`);
  evtSource.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.line && consoleEl) {
      consoleEl.textContent += data.line + '\n';
      consoleEl.scrollTop = consoleEl.scrollHeight;
    }
    if (data.done) {
      evtSource.close();
      if (data.success) {
        toast(`${name} installed successfully`, 3000, 'success');
        loadPackages();
      } else {
        toast(`Failed to install ${name}`, 3000, 'error');
      }
    }
  };
  evtSource.onerror = () => {
    evtSource.close();
    toast('Install connection lost', 3000, 'error');
  };
}

async function loadPackages() {
  try {
    const resp = await api.post('/api/packages/list', {});
    if (resp.status === 'success') {
      const list = document.getElementById('pkgList');
      if (!list) return;
      empty(list);
      for (const pkg of resp.data) {
        list.appendChild(el('li', { class: 'pkg-item' }, [
          el('span', {}, [
            el('span', { class: 'pkg-name' }, [pkg.name]),
            el('span', { class: 'pkg-version' }, [pkg.version || '']),
          ]),
          el('button', { class: 'pkg-uninstall-btn', type: 'button', onClick: () => uninstallPackage(pkg.name) }, ['Uninstall']),
        ]));
      }
    }
  } catch (err) {
    toast(`Failed to load packages: ${err.message}`, 2600, 'error');
  }
}

async function uninstallPackage(name) {
  if (!confirm(`Uninstall ${name}?`)) return;
  try {
    const resp = await api.post('/api/packages/uninstall', { name });
    if (resp.status === 'success') {
      toast(`${name} uninstalled`, 3000, 'success');
      loadPackages();
    } else {
      toast(resp.message || 'Failed', 3000, 'error');
    }
  } catch (err) {
    toast(`Uninstall error: ${err.message}`, 2600, 'error');
  }
}
