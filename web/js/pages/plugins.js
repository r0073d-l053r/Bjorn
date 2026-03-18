/**
 * Plugins page - Install, configure, enable/disable, and uninstall plugins.
 * @module pages/plugins
 */

import { api } from '../core/api.js';
import { $, el, escapeHtml, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

/* ------------------------------------------------------------------ */
/*  State                                                              */
/* ------------------------------------------------------------------ */

let root = null;
let plugins = [];
let activeConfigId = null;  // plugin ID whose config modal is open

const TYPE_BADGES = {
  action:   { label: 'Action',   cls: 'badge-action' },
  notifier: { label: 'Notifier', cls: 'badge-notifier' },
  enricher: { label: 'Enricher', cls: 'badge-enricher' },
  exporter: { label: 'Exporter', cls: 'badge-exporter' },
  ui_widget:{ label: 'Widget',   cls: 'badge-widget' },
};

const STATUS_LABELS = {
  loaded:   'Loaded',
  disabled: 'Disabled',
  error:    'Error',
  missing:  'Missing',
  not_installed: 'Not installed',
};

/* ------------------------------------------------------------------ */
/*  Lifecycle                                                          */
/* ------------------------------------------------------------------ */

export async function mount(container) {
  root = el('div', { class: 'plugins-page' });
  container.appendChild(root);
  await loadPlugins();
  render();
}

export function unmount() {
  // Close config modal if open
  const modal = document.getElementById('pluginConfigModal');
  if (modal) modal.remove();

  // Clear DOM reference (listeners on removed DOM are GC'd by browser)
  if (root && root.parentNode) {
    root.parentNode.removeChild(root);
  }
  root = null;
  plugins = [];
  activeConfigId = null;
}

/* ------------------------------------------------------------------ */
/*  Data                                                               */
/* ------------------------------------------------------------------ */

async function loadPlugins() {
  try {
    const res = await api.get('/api/plugins/list', { timeout: 10000, retries: 0 });
    plugins = Array.isArray(res?.data) ? res.data : [];
  } catch {
    plugins = [];
  }
}

/* ------------------------------------------------------------------ */
/*  Rendering                                                          */
/* ------------------------------------------------------------------ */

function render() {
  if (!root) return;
  root.innerHTML = '';

  // Header
  const header = el('div', { class: 'plugins-header' }, [
    el('h1', {}, ['Plugins']),
    el('div', { class: 'plugins-actions' }, [
      buildInstallButton(),
      el('button', {
        class: 'btn btn-sm',
        onclick: async () => { await loadPlugins(); render(); },
      }, ['Reload']),
    ]),
  ]);
  root.appendChild(header);

  // Plugin count
  const loaded = plugins.filter(p => p.status === 'loaded').length;
  root.appendChild(el('p', { class: 'plugins-count' }, [
    `${plugins.length} plugin(s) installed, ${loaded} active`
  ]));

  // Cards
  if (plugins.length === 0) {
    root.appendChild(el('div', { class: 'plugins-empty' }, [
      el('p', {}, ['No plugins installed.']),
      el('p', {}, ['Drop a .zip plugin archive or use the Install button above.']),
    ]));
  } else {
    const grid = el('div', { class: 'plugins-grid' });
    for (const p of plugins) {
      grid.appendChild(buildPluginCard(p));
    }
    root.appendChild(grid);
  }

  // Config modal (if open)
  if (activeConfigId) {
    renderConfigModal(activeConfigId);
  }
}

function buildPluginCard(p) {
  const typeBadge = TYPE_BADGES[p.type] || { label: p.type, cls: '' };
  const statusLabel = STATUS_LABELS[p.status] || p.status;
  const statusCls = `status-${p.status}`;

  const card = el('div', { class: `plugin-card ${p.enabled ? '' : 'plugin-disabled'}` }, [
    // Top row: name + toggle
    el('div', { class: 'plugin-card-head' }, [
      el('div', { class: 'plugin-card-title' }, [
        el('strong', {}, [escapeHtml(p.name || p.id)]),
        el('span', { class: `plugin-type-badge ${typeBadge.cls}` }, [typeBadge.label]),
        el('span', { class: `plugin-status ${statusCls}` }, [statusLabel]),
      ]),
      buildToggle(p),
    ]),

    // Info
    el('div', { class: 'plugin-card-info' }, [
      el('p', { class: 'plugin-desc' }, [escapeHtml(p.description || '')]),
      el('div', { class: 'plugin-meta' }, [
        el('span', {}, [`v${escapeHtml(p.version || '?')}`]),
        p.author ? el('span', {}, [`by ${escapeHtml(p.author)}`]) : null,
      ]),
    ]),

    // Hooks
    p.hooks && p.hooks.length ? el('div', { class: 'plugin-hooks' },
      p.hooks.map(h => el('span', { class: 'hook-badge' }, [h]))
    ) : null,

    // Error message
    p.error ? el('div', { class: 'plugin-error' }, [escapeHtml(p.error)]) : null,

    // Dependencies warning
    p.dependencies && !p.dependencies.ok
      ? el('div', { class: 'plugin-deps-warn' }, [
          'Missing: ' + p.dependencies.missing.join(', ')
        ])
      : null,

    // Actions
    el('div', { class: 'plugin-card-actions' }, [
      p.has_config ? el('button', {
        class: 'btn btn-sm',
        onclick: () => openConfig(p.id),
      }, ['Configure']) : null,
      el('button', {
        class: 'btn btn-sm btn-danger',
        onclick: () => confirmUninstall(p.id, p.name),
      }, ['Uninstall']),
    ]),
  ]);

  return card;
}

function buildToggle(p) {
  const toggle = el('label', { class: 'plugin-toggle' }, [
    el('input', {
      type: 'checkbox',
      ...(p.enabled ? { checked: 'checked' } : {}),
      onchange: async (e) => {
        const enabled = e.target.checked;
        try {
          await api.post('/api/plugins/toggle', { id: p.id, enabled: enabled ? 1 : 0 });
          toast(`${p.name} ${enabled ? 'enabled' : 'disabled'}`, 2000, 'success');
          await loadPlugins();
          render();
        } catch {
          toast('Failed to toggle plugin', 2500, 'error');
          e.target.checked = !enabled;
        }
      },
    }),
    el('span', { class: 'toggle-slider' }),
  ]);
  return toggle;
}

function buildInstallButton() {
  const fileInput = el('input', {
    type: 'file',
    accept: '.zip',
    style: 'display:none',
    onchange: async (e) => {
      const file = e.target.files?.[0];
      if (!file) return;
      await installPlugin(file);
      e.target.value = '';
    },
  });

  const btn = el('button', {
    class: 'btn btn-sm btn-primary',
    onclick: () => fileInput.click(),
  }, ['+ Install Plugin']);

  return el('div', { style: 'display:inline-block' }, [fileInput, btn]);
}

/* ------------------------------------------------------------------ */
/*  Config Modal                                                       */
/* ------------------------------------------------------------------ */

async function openConfig(pluginId) {
  activeConfigId = pluginId;
  renderConfigModal(pluginId);
}

async function renderConfigModal(pluginId) {
  // Remove existing modal
  const existing = $('#pluginConfigModal');
  if (existing) existing.remove();

  let schema = {};
  let values = {};

  try {
    const res = await api.get(`/api/plugins/config?id=${encodeURIComponent(pluginId)}`, { timeout: 5000 });
    if (res?.status === 'ok') {
      schema = res.schema || {};
      values = res.values || {};
    }
  } catch { /* keep defaults */ }

  const fields = Object.entries(schema);
  if (fields.length === 0) {
    toast('No configurable settings', 2000, 'info');
    activeConfigId = null;
    return;
  }

  const form = el('div', { class: 'config-form' });

  for (const [key, spec] of fields) {
    const current = values[key] ?? spec.default ?? '';
    const label = spec.label || key;
    const inputType = spec.secret ? 'password' : 'text';

    let input;
    if (spec.type === 'bool' || spec.type === 'boolean') {
      input = el('input', {
        type: 'checkbox',
        id: `cfg_${key}`,
        'data-key': key,
        ...(current ? { checked: 'checked' } : {}),
      });
    } else if (spec.type === 'select' && Array.isArray(spec.choices)) {
      input = el('select', { id: `cfg_${key}`, 'data-key': key },
        spec.choices.map(c => el('option', {
          value: c,
          ...(c === current ? { selected: 'selected' } : {}),
        }, [String(c)]))
      );
    } else if (spec.type === 'number' || spec.type === 'int' || spec.type === 'float') {
      input = el('input', {
        type: 'number',
        id: `cfg_${key}`,
        'data-key': key,
        value: String(current),
        ...(spec.min != null ? { min: String(spec.min) } : {}),
        ...(spec.max != null ? { max: String(spec.max) } : {}),
      });
    } else {
      input = el('input', {
        type: inputType,
        id: `cfg_${key}`,
        'data-key': key,
        value: String(current),
        placeholder: spec.placeholder || '',
      });
    }

    form.appendChild(el('div', { class: 'config-field' }, [
      el('label', { for: `cfg_${key}` }, [label]),
      input,
      spec.help ? el('small', { class: 'config-help' }, [spec.help]) : null,
    ]));
  }

  const modal = el('div', { class: 'modal-overlay', id: 'pluginConfigModal' }, [
    el('div', { class: 'modal-content plugin-config-modal' }, [
      el('div', { class: 'modal-header' }, [
        el('h3', {}, [`Configure: ${escapeHtml(pluginId)}`]),
        el('button', { class: 'modal-close', onclick: closeConfig }, ['X']),
      ]),
      form,
      el('div', { class: 'modal-footer' }, [
        el('button', { class: 'btn', onclick: closeConfig }, ['Cancel']),
        el('button', {
          class: 'btn btn-primary',
          onclick: () => saveConfig(pluginId),
        }, ['Save']),
      ]),
    ]),
  ]);

  (root || document.body).appendChild(modal);
}

function closeConfig() {
  activeConfigId = null;
  const modal = $('#pluginConfigModal');
  if (modal) modal.remove();
}

async function saveConfig(pluginId) {
  const modal = $('#pluginConfigModal');
  if (!modal) return;

  const config = {};
  const inputs = modal.querySelectorAll('[data-key]');
  for (const input of inputs) {
    const key = input.getAttribute('data-key');
    if (input.type === 'checkbox') {
      config[key] = input.checked;
    } else {
      config[key] = input.value;
    }
  }

  try {
    const res = await api.post('/api/plugins/config', { id: pluginId, config });
    if (res?.status === 'ok') {
      toast('Configuration saved', 2000, 'success');
      closeConfig();
    } else {
      toast(res?.message || 'Save failed', 2500, 'error');
    }
  } catch {
    toast('Failed to save configuration', 2500, 'error');
  }
}

/* ------------------------------------------------------------------ */
/*  Install / Uninstall                                                */
/* ------------------------------------------------------------------ */

async function installPlugin(file) {
  try {
    toast('Installing plugin...', 3000, 'info');
    const formData = new FormData();
    formData.append('plugin', file);

    const res = await fetch('/api/plugins/install', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();

    if (data?.status === 'ok') {
      toast(`Plugin "${data.name || data.plugin_id}" installed`, 3000, 'success');
      await loadPlugins();
      render();
    } else {
      toast(data?.message || 'Install failed', 4000, 'error');
    }
  } catch (e) {
    toast(`Install error: ${e.message}`, 4000, 'error');
  }
}

function confirmUninstall(pluginId, name) {
  if (!confirm(`Uninstall plugin "${name || pluginId}"? This will remove all plugin files.`)) {
    return;
  }
  uninstallPlugin(pluginId);
}

async function uninstallPlugin(pluginId) {
  try {
    const res = await api.post('/api/plugins/uninstall', { id: pluginId });
    if (res?.status === 'ok') {
      toast('Plugin uninstalled', 2000, 'success');
      await loadPlugins();
      render();
    } else {
      toast(res?.message || 'Uninstall failed', 3000, 'error');
    }
  } catch {
    toast('Failed to uninstall plugin', 3000, 'error');
  }
}
