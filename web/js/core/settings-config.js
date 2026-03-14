import { $, el, toast, empty } from './dom.js';
import { api } from './api.js';
import { t } from './i18n.js';

const API = {
  load: '/load_config',
  save: '/save_config',
  restore: '/restore_default_config',
};

const DEFAULT_RANGE = { min: 0, max: 100, step: 1 };
const RANGES = {
  web_delay: { min: 0, max: 10000, step: 1 },
  screen_delay: { min: 0, max: 10, step: 0.1 },
  startup_delay: { min: 0, max: 600, step: 0.1 },
  startup_splash_duration: { min: 0, max: 60, step: 0.1 },
  fullrefresh_delay: { min: 0, max: 3600, step: 1 },
  image_display_delaymin: { min: 0, max: 600, step: 0.1 },
  image_display_delaymax: { min: 0, max: 600, step: 0.1 },
  comment_delaymin: { min: 0, max: 600, step: 0.1 },
  comment_delaymax: { min: 0, max: 600, step: 0.1 },
  shared_update_interval: { min: 1, max: 86400, step: 1 },
  livestatus_delay: { min: 0, max: 600, step: 0.1 },
  ref_width: { min: 32, max: 1024, step: 1 },
  ref_height: { min: 32, max: 1024, step: 1 },
  vuln_max_ports: { min: 1, max: 65535, step: 1 },
  portstart: { min: 0, max: 65535, step: 1 },
  portend: { min: 0, max: 65535, step: 1 },
  frise_default_x: { min: 0, max: 2000, step: 1 },
  frise_default_y: { min: 0, max: 2000, step: 1 },
  frise_epd2in7_x: { min: 0, max: 2000, step: 1 },
  frise_epd2in7_y: { min: 0, max: 2000, step: 1 },
  semaphore_slots: { min: 1, max: 128, step: 1 },
  line_spacing: { min: 0, max: 10, step: 0.1 },
  vuln_update_interval: { min: 1, max: 86400, step: 1 },
  ai_feature_selection_min_variance: { min: 0, max: 1, step: 0.001 },
  ai_model_history_max: { min: 1, max: 10, step: 1 },
  ai_auto_rollback_window: { min: 10, max: 500, step: 10 },
  ai_cold_start_bootstrap_weight: { min: 0, max: 1, step: 0.05 },
  circuit_breaker_threshold: { min: 1, max: 20, step: 1 },
  manual_mode_scan_interval: { min: 30, max: 3600, step: 10 },
};

/* ── Sub-tab grouping: maps __title_* section keys → sub-tab id ── */
const SECTION_TO_TAB = {
  '__title_Bjorn__':          'core',
  '__title_modes__':          'core',
  '__title_web__':            'core',
  '__title_interfaces__':     'network',
  '__title_network__':        'network',
  '__title_actions_studio__': 'actions',
  '__title_timewaits__':      'actions',
  '__title_orchestrator__':   'actions',
  '__title_bruteforce__':     'actions',
  '__title_display__':        'display',
  '__title_epd__':            'display',
  '__title_timing__':         'display',
  '__title_ai__':             'ai',
  '__title_vuln__':           'security',
  '__title_lists__':          'security',
  '__title_runtime__':        'system',
  '__title_power__':          'system',
  '__title_sentinel__':       'security',
  '__title_bifrost__':        'network',
  '__title_loki__':           'security',
};

const SUB_TABS = [
  { id: 'core',     icon: '\u2699',     label: 'Core' },
  { id: 'network',  icon: '\uD83C\uDF10', label: 'Network' },
  { id: 'actions',  icon: '\u26A1',     label: 'Actions' },
  { id: 'display',  icon: '\uD83D\uDDA5', label: 'Display' },
  { id: 'ai',       icon: '\uD83E\uDDE0', label: 'AI / RL' },
  { id: 'security', icon: '\uD83D\uDD12', label: 'Security' },
  { id: 'system',   icon: '\uD83D\uDD27', label: 'System' },
];

let _host = null;
let _lastConfig = null;
let _activeSubTab = 'core';

function resolveTooltips(config) {
  const tips = config?.__tooltips_i18n__;
  if (!tips || typeof tips !== 'object' || Array.isArray(tips)) return {};
  return tips;
}

function createFieldLabel(key, forId = null, tooltipI18nKey = '') {
  const attrs = {};
  if (forId) attrs.for = forId;
  if (tooltipI18nKey) {
    attrs['data-i18n-title'] = tooltipI18nKey;
    attrs.title = t(tooltipI18nKey);
  }
  return el('label', attrs, [key]);
}

function getRangeForKey(key, value) {
  if (RANGES[key]) return RANGES[key];
  const n = Number(value);
  if (Number.isFinite(n)) {
    if (n <= 10) return { min: 0, max: 10, step: 1 };
    if (n <= 100) return { min: 0, max: 100, step: 1 };
    if (n <= 1000) return { min: 0, max: 1000, step: 1 };
    return { min: 0, max: Math.ceil(n * 2), step: Math.max(1, Math.round(n / 100)) };
  }
  return DEFAULT_RANGE;
}

function normalizeNumber(raw) {
  const s = String(raw ?? '').trim().replace(',', '.');
  if (!s || s === '-' || s === '.' || s === '-.') return NaN;
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : NaN;
}

function ensureChipHelpers() {
  if (window.Chips) return;
  const makeChip = (text) => {
    const chip = el('div', { class: 'cfg-chip' }, [
      el('span', {}, [text]),
      el('button', { class: 'cfg-chip-close', type: 'button', 'aria-label': 'Remove' }, ['x']),
    ]);
    return chip;
  };

  document.addEventListener('click', (e) => {
    const close = e.target.closest('.cfg-chip-close');
    if (close) close.closest('.cfg-chip')?.remove();
  });

  document.addEventListener('keydown', async (e) => {
    if (!e.target || !(e.target instanceof HTMLInputElement)) return;
    const input = e.target;
    const wrap = input.closest('.cfg-chip-input');
    if (!wrap) return;
    if (e.key !== 'Enter' && e.key !== ',') return;
    e.preventDefault();

    const list = wrap.parentElement.querySelector('.cfg-chip-list');
    if (!list) return;
    const values = input.value
      .split(',')
      .map(v => v.trim())
      .filter(Boolean);
    if (!values.length) return;
    const existing = new Set(Array.from(list.querySelectorAll('.cfg-chip span')).map(s => s.textContent));
    values.forEach(v => {
      if (existing.has(v)) return;
      list.appendChild(makeChip(v));
    });
    input.value = '';
  });

  document.addEventListener('click', async (e) => {
    const chip = e.target.closest('.cfg-chip');
    if (!chip || e.target.closest('.cfg-chip-close')) return;
    if (!window.ChipsEditor) return;

    const span = chip.querySelector('span');
    const cur = span?.textContent || '';
    const next = await window.ChipsEditor.open({
      value: cur,
      title: t('settings.editValue'),
      label: t('common.value'),
      multiline: false,
    });
    if (next === null) return;
    const val = String(next).trim();
    if (!val) {
      chip.remove();
      return;
    }
    const list = chip.parentElement;
    const exists = Array.from(list.querySelectorAll('.cfg-chip span')).some(s => s !== span && s.textContent === val);
    if (exists) return;
    if (span) span.textContent = val;
  });

  window.Chips = {
    values(root) {
      return Array.from(root.querySelectorAll('.cfg-chip span')).map(s => s.textContent);
    },
    setValues(root, values = []) {
      empty(root);
      values.forEach(v => root.appendChild(makeChip(String(v))));
    },
  };
}

function createBooleanField(key, value, tooltipI18nKey = '') {
  return el('div', { class: 'cfg-field cfg-toggle-row', 'data-key': key, 'data-type': 'boolean' }, [
    createFieldLabel(key, `cfg_${key}`, tooltipI18nKey),
    el('label', { class: 'switch' }, [
      el('input', { id: `cfg_${key}`, type: 'checkbox', ...(value ? { checked: '' } : {}) }),
      el('span', { class: 'slider' }),
    ]),
  ]);
}

function createNumberField(key, value, tooltipI18nKey = '') {
  const range = getRangeForKey(key, value);
  const n = Number.isFinite(Number(value)) ? Number(value) : range.min;
  const row = el('div', { class: 'cfg-field', 'data-key': key, 'data-type': 'number' }, [
    createFieldLabel(key, `cfg_${key}`, tooltipI18nKey),
    el('div', { class: 'cfg-number' }, [
      el('button', { class: 'btn cfg-nudge', type: 'button', 'data-act': 'dec' }, ['-']),
      el('input', {
        id: `cfg_${key}`,
        class: 'input cfg-number-input',
        type: 'text',
        inputmode: 'decimal',
        value: String(n).replace('.', ','),
      }),
      el('button', { class: 'btn cfg-nudge', type: 'button', 'data-act': 'inc' }, ['+']),
    ]),
    el('input', {
      class: 'cfg-range',
      type: 'range',
      min: String(range.min),
      max: String(range.max),
      step: String(range.step),
      value: String(Math.min(range.max, Math.max(range.min, n))),
    }),
  ]);

  const textInput = row.querySelector('.cfg-number-input');
  const slider = row.querySelector('.cfg-range');
  const decBtn = row.querySelector('[data-act="dec"]');
  const incBtn = row.querySelector('[data-act="inc"]');

  const clamp = (v) => Math.max(range.min, Math.min(range.max, v));
  const paint = () => {
    const cur = Number(slider.value);
    const pct = ((cur - range.min) * 100) / (range.max - range.min || 1);
    slider.style.backgroundSize = `${pct}% 100%`;
  };
  const syncFromText = () => {
    const parsed = normalizeNumber(textInput.value);
    if (Number.isFinite(parsed)) {
      slider.value = String(clamp(parsed));
      paint();
    }
  };
  const syncFromRange = () => {
    textInput.value = String(slider.value).replace('.', ',');
    paint();
  };
  const nudge = (dir) => {
    const parsed = normalizeNumber(textInput.value);
    const base = Number.isFinite(parsed) ? parsed : Number(slider.value);
    const next = +(base + dir * range.step).toFixed(10);
    textInput.value = String(next).replace('.', ',');
    slider.value = String(clamp(next));
    paint();
  };

  textInput.addEventListener('input', syncFromText);
  textInput.addEventListener('change', syncFromText);
  slider.addEventListener('input', syncFromRange);
  decBtn.addEventListener('click', () => nudge(-1));
  incBtn.addEventListener('click', () => nudge(1));
  paint();

  return row;
}

function createListField(key, value, tooltipI18nKey = '') {
  const list = Array.isArray(value) ? value : [];
  const node = el('div', { class: 'cfg-field', 'data-key': key, 'data-type': 'list' }, [
    createFieldLabel(key, null, tooltipI18nKey),
    el('div', { class: 'cfg-chip-list' }),
    el('div', { class: 'cfg-chip-input' }, [
      el('input', { class: 'input', type: 'text', placeholder: t('settings.addValues') }),
    ]),
  ]);
  const chipList = node.querySelector('.cfg-chip-list');
  window.Chips.setValues(chipList, list);
  return node;
}

function createStringField(key, value, tooltipI18nKey = '') {
  const node = el('div', { class: 'cfg-field', 'data-key': key, 'data-type': 'string' }, [
    createFieldLabel(key, null, tooltipI18nKey),
    el('div', { class: 'cfg-chip-list' }),
    el('div', { class: 'cfg-chip-input' }, [
      el('input', { class: 'input', type: 'text', placeholder: t('settings.setValue') }),
    ]),
  ]);
  const chipList = node.querySelector('.cfg-chip-list');
  if (value !== undefined && value !== null && String(value) !== '') {
    window.Chips.setValues(chipList, [String(value)]);
  }
  return node;
}

function createSectionCard(title) {
  return el('div', { class: 'card cfg-card' }, [
    el('div', { class: 'head' }, [el('h3', { class: 'title' }, [title])]),
    el('div', { class: 'cfg-card-body' }),
  ]);
}

/* ── Sub-tab navigation bar ── */
function createSubTabBar(onSwitch) {
  const nav = el('nav', { class: 'cfg-subtabs' });
  for (const tab of SUB_TABS) {
    const btn = el('button', {
      class: `cfg-subtab${tab.id === _activeSubTab ? ' active' : ''}`,
      'data-subtab': tab.id,
      type: 'button',
    }, [`${tab.icon}\u00A0${tab.label}`]);
    nav.appendChild(btn);
  }
  nav.addEventListener('click', (e) => {
    const btn = e.target.closest('.cfg-subtab');
    if (!btn) return;
    const id = btn.dataset.subtab;
    if (id === _activeSubTab) return;
    _activeSubTab = id;
    nav.querySelectorAll('.cfg-subtab').forEach(b => b.classList.toggle('active', b.dataset.subtab === id));
    onSwitch(id);
  });
  return nav;
}

function render(config) {
  if (!_host) return;
  empty(_host);
  ensureChipHelpers();
  const tooltips = resolveTooltips(config);

  /* Buckets: one per sub-tab, each with a toggles card + section cards */
  const buckets = {};
  for (const tab of SUB_TABS) {
    buckets[tab.id] = {
      togglesBody: null,
      togglesCard: null,
      cardsGrid: el('div', { class: 'cfg-cards-grid' }),
      currentCard: null,
      pane: el('div', { class: 'cfg-subtab-pane', 'data-pane': tab.id }),
    };
  }

  /* Helper: lazily create the toggles card for a bucket */
  const ensureToggles = (b) => {
    if (!b.togglesCard) {
      b.togglesCard = createSectionCard(t('settings.toggles'));
      b.togglesBody = b.togglesCard.querySelector('.cfg-card-body');
    }
  };

  let currentTabId = 'core';  // default bucket for fields before first __title_*

  for (const [key, value] of Object.entries(config || {})) {
    if (key.startsWith('__')) {
      if (key.startsWith('__title_')) {
        /* Close previous card if any */
        const prevBucket = buckets[currentTabId];
        if (prevBucket.currentCard) {
          prevBucket.cardsGrid.appendChild(prevBucket.currentCard);
          prevBucket.currentCard = null;
        }
        /* Switch to the right bucket */
        currentTabId = SECTION_TO_TAB[key] || 'core';
        const bucket = buckets[currentTabId];
        const sectionName = String(value).replace('__title_', '').replace(/__/g, '');
        bucket.currentCard = createSectionCard(sectionName);
      }
      continue;
    }

    const bucket = buckets[currentTabId];
    const tooltipI18nKey = String(tooltips[key] || '');

    if (typeof value === 'boolean') {
      ensureToggles(bucket);
      bucket.togglesBody.appendChild(createBooleanField(key, value, tooltipI18nKey));
      continue;
    }

    if (!bucket.currentCard) bucket.currentCard = createSectionCard(t('settings.general'));
    const body = bucket.currentCard.querySelector('.cfg-card-body');
    if (Array.isArray(value)) body.appendChild(createListField(key, value, tooltipI18nKey));
    else if (typeof value === 'number') body.appendChild(createNumberField(key, value, tooltipI18nKey));
    else body.appendChild(createStringField(key, value, tooltipI18nKey));
  }

  /* Finalize all buckets */
  for (const tab of SUB_TABS) {
    const b = buckets[tab.id];
    if (b.currentCard) b.cardsGrid.appendChild(b.currentCard);
    if (b.togglesCard) b.pane.appendChild(b.togglesCard);
    if (b.cardsGrid.children.length) b.pane.appendChild(b.cardsGrid);
  }

  /* Build sub-tab bar */
  const showPane = (id) => {
    _host.querySelectorAll('.cfg-subtab-pane').forEach(p => {
      p.hidden = p.dataset.pane !== id;
    });
  };
  const subTabBar = createSubTabBar(showPane);
  _host.appendChild(subTabBar);

  /* Append all panes */
  for (const tab of SUB_TABS) {
    const b = buckets[tab.id];
    b.pane.hidden = tab.id !== _activeSubTab;
    _host.appendChild(b.pane);
  }
}

function collect() {
  const payload = {};
  if (!_host) return payload;

  _host.querySelectorAll('.cfg-field[data-key]').forEach(field => {
    const key = field.getAttribute('data-key');
    const type = field.getAttribute('data-type');
    if (!key || !type) return;

    if (type === 'boolean') {
      payload[key] = !!field.querySelector('input[type="checkbox"]')?.checked;
      return;
    }
    if (type === 'number') {
      const n = normalizeNumber(field.querySelector('.cfg-number-input')?.value);
      payload[key] = Number.isFinite(n) ? n : 0;
      return;
    }
    if (type === 'list') {
      payload[key] = window.Chips.values(field.querySelector('.cfg-chip-list'));
      return;
    }
    if (type === 'string') {
      const values = window.Chips.values(field.querySelector('.cfg-chip-list'));
      payload[key] = values[0] ?? '';
    }
  });

  return payload;
}

export async function loadConfig(host = _host) {
  if (host) _host = host;
  if (!_host) return;
  try {
    const config = await api.get(API.load, { timeout: 15000, retries: 0 });
    _lastConfig = config;
    render(config);
  } catch (err) {
    toast(`${t('settings.errorLoading')}: ${err.message}`, 3200, 'error');
  }
}

export async function saveConfig() {
  if (!_host) return;
  try {
    const payload = collect();
    await api.post(API.save, payload, { timeout: 20000, retries: 0 });
    toast(t('settings.configSaved'), 2200, 'success');
  } catch (err) {
    toast(`${t('settings.errorSaving')}: ${err.message}`, 3200, 'error');
  }
}

export async function restoreDefaults(host = _host) {
  if (host) _host = host;
  if (!_host) return;
  try {
    const config = await api.get(API.restore, { timeout: 20000, retries: 0 });
    _lastConfig = config;
    render(config);
    toast(t('settings.defaultsRestored'), 2200, 'success');
  } catch (err) {
    toast(`${t('settings.errorRestoring')}: ${err.message}`, 3200, 'error');
  }
}

export function mountConfig(host) {
  _host = host || _host;
}

export function hasLoadedConfig() {
  return !!_lastConfig;
}
