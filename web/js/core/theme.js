/**
 * Theme module — CSS variable management, persistence, theme editor UI.
 * Single source of truth: all colors come from :root CSS variables.
 *
 * Supports:
 *  - Preset themes (default Nordic Acid, light, etc.)
 *  - User custom overrides persisted to localStorage
 *  - Theme editor with color pickers + raw CSS textarea
 *  - Icon pack switching via icon registry
 *  - Import / Export themes as JSON
 *  - Live preview: overlay disabled while Theme tab is active
 */

import { t } from './i18n.js';

const STORAGE_KEY = 'bjorn_theme';
const ICON_PACK_KEY = 'bjorn_icon_pack';

/* Default theme tokens — matches global.css :root */
const DEFAULT_THEME = {
  '--bg': '#050709',
  '--bg-2': '#0b0f14',
  '--ink': '#e6fff7',
  '--muted': '#8affc1cc',
  '--acid': '#00ff9a',
  '--acid-2': '#18f0ff',
  '--danger': '#ff3b3b',
  '--warning': '#ffd166',
  '--ok': '#2cff7e',
  '--accent': '#22f0b4',
  '--accent-2': '#18d6ff',
  '--c-border': '#00ffff22',
  '--c-border-strong': '#00ffff33',
  '--c-border-hi': '#00ffff44',
  '--panel': '#0e1717',
  '--panel-2': '#101c1c',
  '--c-panel': '#0b1218',
  '--c-panel-2': '#0a1118',
  '--c-btn': '#0d151c',
  '--switch-track': '#111111',
  '--switch-on-bg': '#022a1a',
  '--sb-track': '#07121a',
  '--sb-thumb': '#09372b',
  '--glass-8': '#00000088',
  '--radius': '14px'
};

/* Editable token groups for the theme editor */
const TOKEN_GROUPS = [
  {
    label: 'theme.group.colors',
    tokens: [
      { key: '--bg', label: 'theme.token.bg', type: 'color' },
      { key: '--bg-2', label: 'theme.token.bg2', type: 'color' },
      { key: '--ink', label: 'theme.token.ink', type: 'color' },
      { key: '--muted', label: 'theme.token.muted', type: 'color' },
      { key: '--acid', label: 'theme.token.accent1', type: 'color' },
      { key: '--acid-2', label: 'theme.token.accent2', type: 'color' },
      { key: '--accent', label: 'theme.token.accent', type: 'color' },
      { key: '--accent-2', label: 'theme.token.accentAlt', type: 'color' },
      { key: '--danger', label: 'theme.token.danger', type: 'color' },
      { key: '--warning', label: 'theme.token.warning', type: 'color' },
      { key: '--ok', label: 'theme.token.ok', type: 'color' },
    ]
  },
  {
    label: 'theme.group.surfaces',
    tokens: [
      { key: '--panel', label: 'theme.token.panel', type: 'color' },
      { key: '--panel-2', label: 'theme.token.panel2', type: 'color' },
      { key: '--c-panel', label: 'theme.token.ctrlPanel', type: 'color' },
      { key: '--c-panel-2', label: 'theme.token.ctrlPanel2', type: 'color' },
      { key: '--c-btn', label: 'theme.token.btnBg', type: 'color' },
    ]
  },
  {
    label: 'theme.group.borders',
    tokens: [
      { key: '--c-border', label: 'theme.token.border', type: 'color' },
      { key: '--c-border-strong', label: 'theme.token.borderStrong', type: 'color' },
      { key: '--c-border-hi', label: 'theme.token.borderHi', type: 'color' },
    ]
  },
  {
    label: 'theme.group.controls',
    tokens: [
      { key: '--switch-track', label: 'theme.token.switchTrack', type: 'color' },
      { key: '--switch-on-bg', label: 'theme.token.switchOnBg', type: 'color' },
      { key: '--sb-track', label: 'theme.token.scrollTrack', type: 'color' },
      { key: '--sb-thumb', label: 'theme.token.scrollThumb', type: 'color' },
      { key: '--glass-8', label: 'theme.token.glass', type: 'color' },
    ]
  },
  {
    label: 'theme.group.layout',
    tokens: [
      { key: '--radius', label: 'theme.token.radius', type: 'text' },
    ]
  }
];

let _userOverrides = {};

/* -- Icon registry -- */
const _iconPacks = {
  default: {} // populated from /web/images/*.png
};
let _currentPack = 'default';

/* Load user theme from localStorage */
function loadSaved() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) _userOverrides = JSON.parse(raw);
  } catch { _userOverrides = {}; }

  try {
    _currentPack = localStorage.getItem(ICON_PACK_KEY) || 'default';
  } catch { _currentPack = 'default'; }
}

/* Apply overrides to :root */
function applyToDOM() {
  const root = document.documentElement;
  // Reset to defaults first
  for (const [k, v] of Object.entries(DEFAULT_THEME)) {
    root.style.setProperty(k, v);
  }
  // Apply user overrides on top
  for (const [k, v] of Object.entries(_userOverrides)) {
    if (v) root.style.setProperty(k, v);
  }
}

/* Save overrides to localStorage */
function persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(_userOverrides));
  } catch { /* storage full or blocked */ }
}

/* -- Public API -- */

export function init() {
  loadSaved();
  applyToDOM();
}

/** Get current value for a token */
export function getToken(key) {
  return _userOverrides[key] || DEFAULT_THEME[key] || '';
}

/** Set a single token override */
export function setToken(key, value) {
  _userOverrides[key] = value;
  document.documentElement.style.setProperty(key, value);
  persist();
}

/** Reset all overrides to default */
export function resetToDefault() {
  _userOverrides = {};
  persist();
  applyToDOM();
}

/** Apply a full theme preset */
export function applyPreset(preset) {
  _userOverrides = { ...preset };
  persist();
  applyToDOM();
}

/** Get current overrides (for display in editor) */
export function getCurrentOverrides() {
  return { ...DEFAULT_THEME, ..._userOverrides };
}

/* -- Import / Export -- */

/** Export current theme as JSON string */
export function exportTheme() {
  return JSON.stringify(_userOverrides, null, 2);
}

/** Import theme from JSON string */
export function importTheme(json) {
  try {
    const parsed = JSON.parse(json);
    if (typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Invalid');
    _userOverrides = parsed;
    persist();
    applyToDOM();
    return true;
  } catch {
    return false;
  }
}

/* -- Overlay management for live preview -- */

let _overlayWasVisible = false;

/** Disable the settings backdrop overlay so theme changes are visible live */
export function disableOverlay() {
  const backdrop = document.getElementById('settingsBackdrop');
  if (!backdrop) return;
  _overlayWasVisible = true;
  backdrop.style.background = 'transparent';
  const modal = backdrop.querySelector('.modal');
  if (modal) {
    modal.style.boxShadow = '0 0 0 2px var(--acid), 0 20px 60px rgba(0,0,0,.6)';
    modal.style.maxHeight = '70vh';
    modal.style.overflow = 'auto';
  }
}

/** Restore the overlay when leaving theme tab */
export function restoreOverlay() {
  if (!_overlayWasVisible) return;
  const backdrop = document.getElementById('settingsBackdrop');
  if (!backdrop) return;
  backdrop.style.background = '';
  const modal = backdrop.querySelector('.modal');
  if (modal) {
    modal.style.boxShadow = '';
    modal.style.maxHeight = '';
    modal.style.overflow = '';
  }
  _overlayWasVisible = false;
}

/* -- Icon registry -- */

/**
 * Register an icon pack.
 * @param {string} name
 * @param {object} icons - { logicalName: svgString | url }
 */
export function registerIconPack(name, icons) {
  _iconPacks[name] = icons;
}

/** Get an icon by logical name from current pack */
export function icon(name) {
  const pack = _iconPacks[_currentPack] || _iconPacks.default;
  return pack[name] || _iconPacks.default[name] || '';
}

/** Switch icon pack */
export function setIconPack(name) {
  if (!_iconPacks[name]) {
    console.warn(`[Theme] Unknown icon pack: ${name}`);
    return;
  }
  _currentPack = name;
  try { localStorage.setItem(ICON_PACK_KEY, name); } catch { /* */ }
}

/* -- Theme Editor UI -- */

/**
 * Mount the theme editor into a container element.
 * @param {HTMLElement} container
 */
export function mountEditor(container) {
  container.innerHTML = '';

  /* Disable overlay for live preview */
  disableOverlay();

  const current = getCurrentOverrides();

  // Color pickers grouped
  for (const group of TOKEN_GROUPS) {
    const section = document.createElement('div');
    section.className = 'theme-group';

    const heading = document.createElement('h4');
    heading.className = 'theme-group-title';
    heading.textContent = t(group.label);
    section.appendChild(heading);

    for (const token of group.tokens) {
      const row = document.createElement('div');
      row.className = 'theme-row';

      const label = document.createElement('label');
      label.textContent = t(token.label);
      label.className = 'theme-label';

      const input = document.createElement('input');
      input.type = token.type === 'color' ? 'color' : 'text';
      input.className = 'theme-input';
      input.value = normalizeColor(current[token.key] || '');
      input.addEventListener('input', () => {
        setToken(token.key, input.value);
      });

      row.appendChild(label);
      row.appendChild(input);
      section.appendChild(row);
    }

    container.appendChild(section);
  }

  // Raw CSS textarea (advanced)
  const advSection = document.createElement('div');
  advSection.className = 'theme-group';

  const advTitle = document.createElement('h4');
  advTitle.className = 'theme-group-title';
  advTitle.textContent = t('theme.advanced');
  advSection.appendChild(advTitle);

  const textarea = document.createElement('textarea');
  textarea.className = 'theme-raw-css';
  textarea.rows = 6;
  textarea.placeholder = '--my-var: #ff0000;\n--other: 12px;';
  textarea.value = Object.entries(_userOverrides)
    .filter(([k]) => !TOKEN_GROUPS.some(g => g.tokens.some(tk => tk.key === k)))
    .map(([k, v]) => `${k}: ${v};`)
    .join('\n');
  advSection.appendChild(textarea);

  const applyBtn = document.createElement('button');
  applyBtn.className = 'btn btn-sm';
  applyBtn.textContent = t('theme.applyRaw');
  applyBtn.addEventListener('click', () => {
    parseAndApplyRawCSS(textarea.value);
  });
  advSection.appendChild(applyBtn);

  container.appendChild(advSection);

  // Import / Export / Reset buttons
  const actionsRow = document.createElement('div');
  actionsRow.className = 'theme-actions';

  const exportBtn = document.createElement('button');
  exportBtn.className = 'btn btn-sm';
  exportBtn.textContent = t('theme.export');
  exportBtn.addEventListener('click', () => {
    const blob = new Blob([exportTheme()], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'bjorn-theme.json';
    a.click();
    URL.revokeObjectURL(url);
  });
  actionsRow.appendChild(exportBtn);

  const importBtn = document.createElement('button');
  importBtn.className = 'btn btn-sm';
  importBtn.textContent = t('theme.import');
  importBtn.addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = '.json';
    input.addEventListener('change', () => {
      const file = input.files?.[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        const ok = importTheme(reader.result);
        if (ok) {
          mountEditor(container);
        } else {
          alert(t('theme.importError'));
        }
      };
      reader.readAsText(file);
    });
    input.click();
  });
  actionsRow.appendChild(importBtn);

  const resetBtn = document.createElement('button');
  resetBtn.className = 'btn btn-sm btn-danger';
  resetBtn.textContent = t('theme.reset');
  resetBtn.addEventListener('click', () => {
    resetToDefault();
    mountEditor(container);
  });
  actionsRow.appendChild(resetBtn);

  container.appendChild(actionsRow);
}

/** Parse raw CSS var declarations from textarea */
function parseAndApplyRawCSS(raw) {
  const lines = raw.split('\n');
  for (const line of lines) {
    const match = line.match(/^\s*(--[\w-]+)\s*:\s*(.+?)\s*;?\s*$/);
    if (match) {
      setToken(match[1], match[2]);
    }
  }
}

/** Normalize a CSS color to #hex for color picker inputs */
function normalizeColor(val) {
  if (!val || val.includes('var(') || val.includes('rgba') || val.includes('color-mix')) {
    return val; // Can't normalize complex values
  }
  // If it's already a hex, return as-is (truncate alpha channel for color picker)
  if (/^#[0-9a-f]{6,8}$/i.test(val)) return val.slice(0, 7);
  return val;
}
