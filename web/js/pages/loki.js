/**
 * Loki — HID Attack Suite SPA page
 * Script editor, library, job management, quick-type.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty, toast, escapeHtml, confirmT } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'loki';

/* ── State ─────────────────────────────────────────────── */

let tracker   = null;
let poller    = null;
let root      = null;

let lokiEnabled = false;
let status    = {};
let scripts   = [];
let payloads  = [];
let jobs      = [];
let layouts   = ['us'];
let currentScript = { id: null, name: '', content: '' };

/* ── Lifecycle ─────────────────────────────────────────── */

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  bindEvents();
  await refresh();
  poller = new Poller(refreshJobs, 4000);
  poller.start();
}

export function unmount() {
  if (poller) { poller.stop(); poller = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  root = null;
  scripts = [];
  payloads = [];
  jobs = [];
}

/* ── Shell ─────────────────────────────────────────────── */

function buildShell() {
  return el('div', { class: 'loki-page' }, [

    /* ── Header ───────────────────────────────────────── */
    el('div', { class: 'loki-header' }, [
      el('h1', { class: 'loki-title' }, [
        el('span', { class: 'loki-title-icon' }, ['\uD83D\uDC0D']),
        el('span', { 'data-i18n': 'loki.title' }, [t('loki.title')]),
      ]),
      el('div', { class: 'loki-controls' }, [
        el('span', { 'data-i18n': 'loki.enable' }, [t('loki.enable')]),
        el('input', { type: 'checkbox', class: 'loki-toggle', id: 'loki-toggle' }),
      ]),
    ]),

    /* ── Status bar ───────────────────────────────────── */
    el('div', { class: 'loki-status-bar', id: 'loki-status-bar' }),

    /* ── Grid: editor + library ───────────────────────── */
    el('div', { class: 'loki-grid', id: 'loki-grid' }, [

      /* Editor column */
      el('div', { class: 'loki-editor-panel' }, [
        el('textarea', {
          class: 'loki-editor',
          id: 'loki-editor',
          spellcheck: 'false',
          placeholder: '// HIDScript editor\nlayout(\'us\');\ndelay(1000);\npress("GUI r");\ndelay(500);\ntype("notepad\\n");\ndelay(1000);\ntype("Hello from Loki!");',
        }),
        el('div', { class: 'loki-editor-toolbar' }, [
          el('button', { class: 'loki-btn primary', id: 'loki-run' }, ['\u25B6 ', t('loki.run')]),
          el('button', { class: 'loki-btn', id: 'loki-save' }, ['\uD83D\uDCBE ', t('loki.save')]),
          el('button', { class: 'loki-btn', id: 'loki-new' }, ['\uD83D\uDCC4 ', t('loki.new')]),
          el('select', { id: 'loki-layout-select' }),
        ]),
        /* Quick type row */
        el('div', { class: 'loki-quick-row' }, [
          el('input', {
            type: 'text', class: 'loki-quick-input', id: 'loki-quick-input',
            placeholder: t('loki.quick_placeholder'),
          }),
          el('button', { class: 'loki-btn', id: 'loki-quick-send' }, [t('loki.quick_send')]),
        ]),
      ]),

      /* Library column */
      el('div', { class: 'loki-library' }, [
        /* Payloads section */
        el('div', { class: 'loki-library-section' }, [
          el('div', { class: 'loki-library-heading', id: 'loki-payloads-heading' }, [t('loki.payloads')]),
          el('ul', { class: 'loki-library-list', id: 'loki-payloads-list' }),
        ]),
        /* Custom scripts section */
        el('div', { class: 'loki-library-section' }, [
          el('div', { class: 'loki-library-heading', id: 'loki-scripts-heading' }, [t('loki.custom_scripts')]),
          el('ul', { class: 'loki-library-list', id: 'loki-scripts-list' }),
        ]),
      ]),
    ]),

    /* ── Jobs panel ───────────────────────────────────── */
    el('div', { class: 'loki-jobs' }, [
      el('div', { class: 'loki-jobs-header' }, [
        el('h3', {}, [t('loki.jobs')]),
        el('button', { class: 'loki-btn', id: 'loki-clear-jobs' }, [t('loki.clear_completed')]),
      ]),
      el('div', { id: 'loki-jobs-body' }),
    ]),
  ]);
}

/* ── Events ────────────────────────────────────────────── */

function bindEvents() {
  // Toggle enable/disable
  const tog = $('#loki-toggle', root);
  if (tog) tog.addEventListener('change', async () => {
    const enabled = tog.checked;
    const res = await api.post('/api/loki/toggle', { enabled });
    if (res?.status === 'ok') {
      lokiEnabled = enabled;
      toast(enabled ? t('loki.enabled_msg') : t('loki.disabled_msg'));
      await refresh();
    }
  });

  // Run
  const runBtn = $('#loki-run', root);
  if (runBtn) runBtn.addEventListener('click', runScript);

  // Save
  const saveBtn = $('#loki-save', root);
  if (saveBtn) saveBtn.addEventListener('click', saveScript);

  // New
  const newBtn = $('#loki-new', root);
  if (newBtn) newBtn.addEventListener('click', newScript);

  // Quick type
  const quickBtn = $('#loki-quick-send', root);
  if (quickBtn) quickBtn.addEventListener('click', quickType);
  const quickInput = $('#loki-quick-input', root);
  if (quickInput) quickInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') quickType();
  });

  // Clear jobs
  const clearBtn = $('#loki-clear-jobs', root);
  if (clearBtn) clearBtn.addEventListener('click', async () => {
    await api.post('/api/loki/jobs/clear', {});
    await refreshJobs();
  });

  // Layout select
  const layoutSel = $('#loki-layout-select', root);
  if (layoutSel) layoutSel.addEventListener('change', () => {
    // Layout is sent per-run, stored in editor state
  });

  // Tab on editor inserts two spaces
  const editor = $('#loki-editor', root);
  if (editor) editor.addEventListener('keydown', e => {
    if (e.key === 'Tab') {
      e.preventDefault();
      const start = editor.selectionStart;
      const end = editor.selectionEnd;
      editor.value = editor.value.substring(0, start) + '  ' + editor.value.substring(end);
      editor.selectionStart = editor.selectionEnd = start + 2;
    }
  });
}

/* ── Data fetch ────────────────────────────────────────── */

async function refresh() {
  const [sRes, scrRes, payRes, jobRes, layRes] = await Promise.all([
    api.get('/api/loki/status'),
    api.get('/api/loki/scripts'),
    api.get('/api/loki/payloads'),
    api.get('/api/loki/jobs'),
    api.get('/api/loki/layouts'),
  ]);

  if (sRes) { status = sRes; lokiEnabled = sRes.enabled; }
  if (scrRes) scripts = scrRes.scripts || [];
  if (payRes) payloads = payRes.payloads || [];
  if (jobRes) jobs = jobRes.jobs || [];
  if (layRes) layouts = layRes.layouts || ['us'];

  paint();
}

async function refreshJobs() {
  const [sRes, jobRes] = await Promise.all([
    api.get('/api/loki/status'),
    api.get('/api/loki/jobs'),
  ]);
  if (sRes) { status = sRes; lokiEnabled = sRes.enabled; }
  if (jobRes) jobs = jobRes.jobs || [];
  paintStatus();
  paintJobs();
}

/* ── Render ────────────────────────────────────────────── */

function paint() {
  paintToggle();
  paintStatus();
  paintLayouts();
  paintPayloads();
  paintScripts();
  paintJobs();
  paintDisabledState();
}

function paintToggle() {
  const tog = $('#loki-toggle', root);
  if (tog) tog.checked = lokiEnabled;
}

function paintStatus() {
  const bar = $('#loki-status-bar', root);
  if (!bar) return;
  empty(bar);

  const running = status.running;
  const gadget = status.gadget_ready;
  const installed = status.gadget_installed !== false;

  if (!installed) {
    bar.append(
      statusItem(t('loki.gadget_label'), t('loki.not_installed') || 'Not installed', false),
    );
    return;
  }

  bar.append(
    statusItem(t('loki.status_label'), running ? t('loki.running') : t('loki.idle'), running),
    statusItem(t('loki.gadget_label'), gadget ? t('loki.ready') : t('loki.not_ready'), gadget),
    statusItem(t('loki.layout_label'), (status.layout || 'us').toUpperCase()),
    statusItem(t('loki.jobs_label'), `${status.jobs_running || 0} ${t('loki.running_lc')}`),
  );
}

function statusItem(label, value, dotState) {
  const children = [];
  if (dotState !== undefined) {
    children.push(el('span', { class: `dot ${dotState ? 'on' : 'off'}` }));
  }
  children.push(el('span', { class: 'label' }, [label + ': ']));
  children.push(el('span', { class: 'value' }, [String(value)]));
  return el('span', { class: 'loki-status-item' }, children);
}

function paintLayouts() {
  const sel = $('#loki-layout-select', root);
  if (!sel) return;
  empty(sel);
  for (const lay of layouts) {
    const opt = el('option', { value: lay }, [lay.toUpperCase()]);
    if (lay === (status.layout || 'us')) opt.selected = true;
    sel.appendChild(opt);
  }
}

function paintPayloads() {
  const list = $('#loki-payloads-list', root);
  if (!list) return;
  empty(list);
  for (const p of payloads) {
    const item = el('li', { class: 'loki-library-item' }, [
      el('span', { class: 'name', title: p.description || '' }, [p.name]),
    ]);
    item.addEventListener('click', () => loadPayload(p));
    list.appendChild(item);
  }
  if (!payloads.length) {
    list.appendChild(el('li', { class: 'loki-library-item' }, [
      el('span', { class: 'name', style: 'color:var(--muted)' }, [t('loki.no_payloads')]),
    ]));
  }
}

function paintScripts() {
  const list = $('#loki-scripts-list', root);
  if (!list) return;
  empty(list);
  for (const s of scripts) {
    const item = el('li', {
      class: `loki-library-item${currentScript.id === s.id ? ' active' : ''}`,
    }, [
      el('span', { class: 'name' }, [s.name]),
      el('button', {
        class: 'loki-btn danger',
        style: 'padding:2px 6px;font-size:0.65rem;',
        title: t('loki.delete'),
      }, ['\u2715']),
    ]);
    // Click name → load
    item.querySelector('.name').addEventListener('click', () => loadScript(s));
    // Click delete
    item.querySelector('.loki-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm(t('loki.confirm_delete', { name: s.name }))) return;
      await api.post('/api/loki/script/delete', { id: s.id });
      await refresh();
    });
    list.appendChild(item);
  }
  if (!scripts.length) {
    list.appendChild(el('li', { class: 'loki-library-item' }, [
      el('span', { class: 'name', style: 'color:var(--muted)' }, [t('loki.no_scripts')]),
    ]));
  }
}

function paintJobs() {
  const body = $('#loki-jobs-body', root);
  if (!body) return;
  empty(body);

  if (!jobs.length) {
    body.appendChild(el('div', { class: 'loki-jobs-empty' }, [t('loki.no_jobs')]));
    return;
  }

  const table = el('table', { class: 'loki-jobs-table' }, [
    el('thead', {}, [
      el('tr', {}, [
        el('th', {}, ['ID']),
        el('th', {}, [t('loki.script')]),
        el('th', {}, [t('loki.status_col')]),
        el('th', {}, [t('loki.started')]),
        el('th', {}, [t('loki.actions')]),
      ]),
    ]),
    el('tbody', {}, jobs.slice(0, 20).map(j => {
      const badge = el('span', { class: `loki-badge ${j.status}` }, [
        statusIcon(j.status), ' ', j.status,
      ]);
      const row = el('tr', {}, [
        el('td', {}, [j.id ? j.id.substring(0, 6) : '...']),
        el('td', {}, [j.script_name || '-']),
        el('td', {}, [badge]),
        el('td', {}, [formatTime(j.started_at)]),
        el('td', {}),
      ]);
      const actions = row.lastChild;
      if (j.status === 'running') {
        const cancelBtn = el('button', { class: 'loki-btn danger', style: 'padding:2px 8px;font-size:0.7rem;' }, [t('loki.cancel')]);
        cancelBtn.addEventListener('click', async () => {
          await api.post('/api/loki/job/cancel', { job_id: j.id });
          await refreshJobs();
        });
        actions.appendChild(cancelBtn);
      }
      if (j.output) {
        const outBtn = el('button', { class: 'loki-btn', style: 'padding:2px 8px;font-size:0.7rem;' }, [t('loki.output')]);
        outBtn.addEventListener('click', () => {
          alert(j.output || t('loki.no_output'));
        });
        actions.appendChild(outBtn);
      }
      return row;
    })),
  ]);
  body.appendChild(table);
}

function paintDisabledState() {
  const grid = $('#loki-grid', root);
  if (!grid) return;

  const installed = status.gadget_installed !== false;

  if (!installed) {
    grid.classList.add('loki-disabled-overlay');
    // Show install banner
    let banner = $('#loki-install-banner', root);
    if (!banner) {
      banner = el('div', { id: 'loki-install-banner', class: 'loki-install-banner' }, [
        el('p', {}, [t('loki.install_msg') || 'HID gadget not installed. Install it and reboot to enable Loki.']),
        el('button', { class: 'loki-btn primary', id: 'loki-install-btn' }, [
          t('loki.install_btn') || 'Install HID Gadget & Reboot',
        ]),
      ]);
      grid.parentNode.insertBefore(banner, grid);
      $('#loki-install-btn', root).addEventListener('click', installGadget);
    }
  } else if (!lokiEnabled) {
    grid.classList.add('loki-disabled-overlay');
    // Remove install banner if present
    const banner = $('#loki-install-banner', root);
    if (banner) banner.remove();
  } else {
    grid.classList.remove('loki-disabled-overlay');
    const banner = $('#loki-install-banner', root);
    if (banner) banner.remove();
  }
}

async function installGadget() {
  const btn = $('#loki-install-btn', root);
  if (btn) { btn.disabled = true; btn.textContent = 'Installing...'; }

  const res = await api.post('/api/loki/install', {});
  if (res?.success) {
    toast(res.message || 'Installed!');
    if (res.reboot_required) {
      if (confirm(t('loki.reboot_confirm') || 'HID gadget installed. Reboot now?')) {
        await api.post('/api/loki/reboot', {});
        toast('Rebooting...');
      }
    }
  } else {
    toast(res?.message || 'Installation failed', 'error');
    if (btn) { btn.disabled = false; btn.textContent = t('loki.install_btn') || 'Install HID Gadget & Reboot'; }
  }
}

/* ── Actions ───────────────────────────────────────────── */

async function runScript() {
  const editor = $('#loki-editor', root);
  if (!editor) return;
  const content = editor.value.trim();
  if (!content) { toast(t('loki.empty_script'), 'warn'); return; }

  const name = currentScript.name || 'editor';
  const res = await api.post('/api/loki/script/run', { content, name });
  if (res?.status === 'ok') {
    toast(t('loki.job_started', { id: res.job_id }));
    await refreshJobs();
  } else {
    toast(res?.message || t('loki.run_error'), 'error');
  }
}

async function saveScript() {
  const editor = $('#loki-editor', root);
  if (!editor) return;
  const content = editor.value.trim();
  if (!content) { toast(t('loki.empty_script'), 'warn'); return; }

  let name = currentScript.name;
  if (!name) {
    name = prompt(t('loki.script_name_prompt'), 'my_script');
    if (!name) return;
  }

  const res = await api.post('/api/loki/script/save', {
    id: currentScript.id || undefined,
    name,
    content,
    description: '',
  });
  if (res?.status === 'ok') {
    toast(t('loki.saved'));
    currentScript.name = name;
    await refresh();
  } else {
    toast(res?.message || t('loki.save_error'), 'error');
  }
}

function newScript() {
  const editor = $('#loki-editor', root);
  if (editor) editor.value = '';
  currentScript = { id: null, name: '', content: '' };
  paintScripts();
}

async function loadScript(s) {
  // Fetch full content
  const res = await api.get(`/api/loki/script?id=${s.id}`);
  if (res?.script) {
    const editor = $('#loki-editor', root);
    if (editor) editor.value = res.script.content || '';
    currentScript = { id: s.id, name: s.name, content: res.script.content };
    paintScripts();
  }
}

function loadPayload(p) {
  const editor = $('#loki-editor', root);
  if (editor) editor.value = p.content || '';
  currentScript = { id: null, name: p.name, content: p.content };
  paintScripts();
}

async function quickType() {
  const input = $('#loki-quick-input', root);
  if (!input) return;
  const text = input.value;
  if (!text) return;

  const res = await api.post('/api/loki/quick', { text });
  if (res?.status === 'ok') {
    toast(t('loki.quick_sent'));
    input.value = '';
    await refreshJobs();
  } else {
    toast(res?.message || t('loki.quick_error'), 'error');
  }
}

/* ── Helpers ───────────────────────────────────────────── */

function statusIcon(status) {
  switch (status) {
    case 'running':   return '\u26A1';
    case 'succeeded': return '\u2705';
    case 'failed':    return '\u274C';
    case 'cancelled': return '\u23F9';
    case 'pending':   return '\u23F3';
    default:          return '\u2022';
  }
}

function formatTime(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString();
  } catch {
    return iso;
  }
}
