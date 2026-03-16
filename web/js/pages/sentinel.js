/**
 * Sentinel Watchdog — SPA page
 * Real-time network monitoring, event feed, rules engine, device baselines.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty, toast, escapeHtml, confirmT } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'sentinel';

/* ── State ─────────────────────────────────────────────── */

let tracker  = null;
let poller   = null;
let root     = null;

let sentinelEnabled = false;
let events   = [];
let rules    = [];
let devices  = [];
let unreadCount = 0;
let notifierCfg = {};  // { discord_webhook: '...', webhook_url: '...', ... }
let sideTab  = 'rules';   // 'rules' | 'devices' | 'notifiers'

/* ── Lifecycle ─────────────────────────────────────────── */

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  bindEvents();
  await refresh();
  poller = new Poller(refresh, 5000);
  poller.start();
}

export function unmount() {
  if (poller) { poller.stop(); poller = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  root = null;
  events = [];
  rules = [];
  devices = [];
  notifierCfg = {};
}

/* ── Shell ─────────────────────────────────────────────── */

function buildShell() {
  return el('div', { class: 'sentinel-page' }, [

    /* ── Header ───────────────────────────────────────── */
    el('div', { class: 'sentinel-header' }, [
      el('h1', { class: 'sentinel-title' }, [
        el('span', { class: 'sentinel-title-icon' }, ['🛡️']),
        el('span', { 'data-i18n': 'sentinel.title' }, [t('sentinel.title')]),
      ]),
      el('div', { class: 'sentinel-controls' }, [
        el('button', { class: 'sentinel-toggle', id: 'sentinel-toggle' }, [
          el('span', { class: 'dot' }),
          el('span', { class: 'sentinel-toggle-label', 'data-i18n': 'sentinel.disabled' }, [t('sentinel.disabled')]),
        ]),
      ]),
    ]),

    /* ── Stats bar ────────────────────────────────────── */
    el('div', { class: 'sentinel-stats', id: 'sentinel-stats' }),

    /* ── Main grid ────────────────────────────────────── */
    el('div', { class: 'sentinel-grid' }, [

      /* Left: event feed */
      el('div', { class: 'sentinel-panel' }, [
        el('div', { class: 'sentinel-panel-head' }, [
          el('span', { 'data-i18n': 'sentinel.eventFeed' }, [t('sentinel.eventFeed')]),
          el('div', { style: 'display:flex;gap:6px' }, [
            el('button', {
              class: 'sentinel-toggle', id: 'sentinel-ack-all',
              style: 'padding:3px 8px;font-size:0.65rem',
            }, [t('sentinel.ackAll')]),
            el('button', {
              class: 'sentinel-toggle', id: 'sentinel-clear',
              style: 'padding:3px 8px;font-size:0.65rem',
            }, [t('sentinel.clearAll')]),
            el('button', {
              class: 'sentinel-toggle sentinel-ai-btn', id: 'sentinel-ai-summary',
              style: 'padding:3px 8px;font-size:0.65rem;display:none',
            }, ['\uD83E\uDDE0 AI Summary']),
          ]),
        ]),
        el('div', { class: 'sentinel-panel-body', id: 'sentinel-events' }, [
          el('div', { style: 'color:var(--muted);text-align:center;padding:40px 10px;font-size:0.8rem' },
            [t('common.loading')]),
        ]),
      ]),

      /* Right: sidebar */
      el('div', { class: 'sentinel-panel' }, [
        el('div', { class: 'sentinel-side-tabs' }, [
          sideTabBtn('rules',     t('sentinel.rules')),
          sideTabBtn('devices',   t('sentinel.devices')),
          sideTabBtn('notifiers', t('sentinel.notifiers')),
        ]),
        el('div', { class: 'sentinel-panel-body', id: 'sentinel-sidebar' }),
      ]),
    ]),
  ]);
}

function sideTabBtn(id, label) {
  return el('button', {
    class: `sentinel-side-tab${sideTab === id ? ' active' : ''}`,
    'data-stab': id,
  }, [label]);
}

/* ── Events ────────────────────────────────────────────── */

function bindEvents() {
  // Toggle sentinel on/off
  root.addEventListener('click', async (e) => {
    const toggle = e.target.closest('#sentinel-toggle');
    if (toggle) {
      try {
        const res = await api.post('/api/sentinel/toggle', { enabled: !sentinelEnabled });
        sentinelEnabled = res.enabled;
        paintToggle();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Ack all
    if (e.target.closest('#sentinel-ack-all')) {
      try {
        await api.post('/api/sentinel/ack', { all: true });
        toast(t('sentinel.allAcked'), 2000, 'success');
        await refreshEvents();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Clear all
    if (e.target.closest('#sentinel-clear')) {
      if (!confirmT(t('sentinel.confirmClear'))) return;
      try {
        await api.post('/api/sentinel/clear', {});
        toast(t('sentinel.eventsCleared'), 2000, 'success');
        await refreshEvents();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Side tab switch
    const stab = e.target.closest('[data-stab]');
    if (stab) {
      sideTab = stab.dataset.stab;
      $$('.sentinel-side-tab', root).forEach(b =>
        b.classList.toggle('active', b.dataset.stab === sideTab));
      paintSidebar();
      return;
    }

    // Ack single event
    const ackBtn = e.target.closest('[data-ack]');
    if (ackBtn) {
      try {
        await api.post('/api/sentinel/ack', { id: parseInt(ackBtn.dataset.ack) });
        await refreshEvents();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Toggle rule enabled
    const ruleToggle = e.target.closest('[data-rule-toggle]');
    if (ruleToggle) {
      const ruleId = parseInt(ruleToggle.dataset.ruleToggle);
      const rule = rules.find(r => r.id === ruleId);
      if (rule) {
        try {
          await api.post('/api/sentinel/rule', { id: ruleId, name: rule.name, trigger_type: rule.trigger_type, enabled: rule.enabled ? 0 : 1 });
          await refreshRules();
        } catch (err) { toast(err.message, 3000, 'error'); }
      }
      return;
    }

    // Delete rule
    const ruleDel = e.target.closest('[data-rule-del]');
    if (ruleDel) {
      if (!confirmT(t('sentinel.confirmDeleteRule'))) return;
      try {
        await api.post('/api/sentinel/rule/delete', { id: parseInt(ruleDel.dataset.ruleDel) });
        toast(t('sentinel.ruleDeleted'), 2000, 'success');
        await refreshRules();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Add rule
    if (e.target.closest('#sentinel-add-rule')) {
      showRuleEditor();
      return;
    }

    // Edit rule
    const ruleEdit = e.target.closest('[data-rule-edit]');
    if (ruleEdit) {
      const ruleId = parseInt(ruleEdit.dataset.ruleEdit);
      const rule = rules.find(r => r.id === ruleId);
      if (rule) showRuleEditor(rule);
      return;
    }

    // Save notifiers
    if (e.target.closest('#sentinel-save-notifiers')) {
      saveNotifiers();
      return;
    }

    // Save device
    const devSave = e.target.closest('[data-dev-save]');
    if (devSave) {
      saveDevice(devSave.dataset.devSave);
      return;
    }

    // AI Analyze event
    const aiAnalyze = e.target.closest('[data-ai-analyze]');
    if (aiAnalyze) {
      analyzeEvent(parseInt(aiAnalyze.dataset.aiAnalyze));
      return;
    }

    // AI Summary
    if (e.target.closest('#sentinel-ai-summary')) {
      summarizeEvents();
      return;
    }

    // AI Generate rule
    if (e.target.closest('#sentinel-ai-gen-rule')) {
      generateRuleFromAI();
      return;
    }
  });
}

/* ── Data refresh ──────────────────────────────────────── */

async function refresh() {
  try {
    const [statusData, eventsData, rulesData, devicesData, notifData] = await Promise.all([
      api.get('/api/sentinel/status'),
      api.get('/api/sentinel/events?limit=100'),
      api.get('/api/sentinel/rules'),
      api.get('/api/sentinel/devices'),
      api.get('/api/sentinel/notifiers').catch(() => null),
    ]);
    sentinelEnabled = statusData.enabled;
    events = eventsData.events || [];
    unreadCount = eventsData.unread_count || 0;
    rules = rulesData.rules || [];
    devices = devicesData.devices || [];
    if (notifData?.notifiers) notifierCfg = notifData.notifiers;
    paint();
  } catch (err) {
    console.warn('[sentinel] refresh error:', err.message);
  }
}

async function refreshEvents() {
  try {
    const data = await api.get('/api/sentinel/events?limit=100');
    events = data.events || [];
    unreadCount = data.unread_count || 0;
    paintStats();
    paintEvents();
  } catch (err) { console.warn('[sentinel] events error:', err.message); }
}

async function refreshRules() {
  try {
    const data = await api.get('/api/sentinel/rules');
    rules = data.rules || [];
    paintSidebar();
  } catch (err) { console.warn('[sentinel] rules error:', err.message); }
}

/* ── Paint ─────────────────────────────────────────────── */

function paint() {
  paintToggle();
  paintStats();
  paintEvents();
  paintSidebar();
}

function paintToggle() {
  const btn = $('#sentinel-toggle', root);
  if (!btn) return;
  btn.classList.toggle('active', sentinelEnabled);
  const lbl = $('.sentinel-toggle-label', btn);
  if (lbl) {
    const key = sentinelEnabled ? 'sentinel.enabled' : 'sentinel.disabled';
    lbl.textContent = t(key);
    lbl.setAttribute('data-i18n', key);
  }
}

function paintStats() {
  const container = $('#sentinel-stats', root);
  if (!container) return;
  const alive = devices.filter(d => {
    if (!d.last_seen) return false;
    const diff = Date.now() - new Date(d.last_seen + 'Z').getTime();
    return diff < 600000; // 10 min
  }).length;

  const stats = [
    { val: devices.length,  lbl: t('sentinel.statDevices') },
    { val: alive,           lbl: t('sentinel.statAlive') },
    { val: unreadCount,     lbl: t('sentinel.statUnread') },
    { val: events.length,   lbl: t('sentinel.statEvents') },
    { val: rules.filter(r => r.enabled).length, lbl: t('sentinel.statRules') },
  ];

  empty(container);
  for (const s of stats) {
    container.appendChild(
      el('div', { class: 'sentinel-stat' }, [
        el('div', { class: 'sentinel-stat-val' }, [String(s.val)]),
        el('div', { class: 'sentinel-stat-lbl' }, [s.lbl]),
      ])
    );
  }
}

function paintEvents() {
  const container = $('#sentinel-events', root);
  if (!container) return;
  empty(container);

  // Show AI Summary button when there are enough unread events
  const aiSumBtn = $('#sentinel-ai-summary', root);
  if (aiSumBtn) aiSumBtn.style.display = unreadCount > 3 ? '' : 'none';

  if (events.length === 0) {
    container.appendChild(
      el('div', {
        style: 'color:var(--muted);text-align:center;padding:40px 10px;font-size:0.8rem'
      }, [t('sentinel.noEvents')])
    );
    return;
  }

  for (const ev of events) {
    const isUnread = !ev.acknowledged;
    const sevClass = ev.severity === 'critical' ? ' sev-critical'
                   : ev.severity === 'warning'  ? ' sev-warning' : '';
    const card = el('div', {
      class: `sentinel-event${isUnread ? ' unread' : ''}${sevClass}`,
    }, [
      el('div', { class: 'sentinel-event-head' }, [
        el('div', { style: 'display:flex;align-items:center;flex:1;gap:6px;min-width:0' }, [
          el('span', {
            class: `sentinel-event-badge ${ev.event_type}`,
          }, [formatEventType(ev.event_type)]),
          el('span', { class: 'sentinel-event-title' }, [escapeHtml(ev.title)]),
        ]),
        el('div', { style: 'display:flex;align-items:center;gap:6px;flex-shrink:0' }, [
          el('button', {
            class: 'sentinel-toggle sentinel-ai-btn',
            'data-ai-analyze': ev.id,
            style: 'padding:1px 6px;font-size:0.55rem',
            title: 'AI Analyze',
          }, ['\uD83E\uDDE0']),
          el('span', { class: 'sentinel-event-time' }, [formatTime(ev.timestamp)]),
          ...(isUnread ? [
            el('button', {
              class: 'sentinel-toggle',
              'data-ack': ev.id,
              style: 'padding:1px 6px;font-size:0.6rem',
              title: t('sentinel.acknowledge'),
            }, ['✓'])
          ] : []),
        ]),
      ]),
      el('div', { class: 'sentinel-event-body' }, [
        escapeHtml(ev.details || ''),
        ...(ev.mac_address ? [
          el('span', { style: 'margin-left:6px;opacity:0.6;font-family:monospace' },
            [ev.mac_address])
        ] : []),
        ...(ev.ip_address ? [
          el('span', { style: 'margin-left:4px;opacity:0.6;font-family:monospace' },
            [ev.ip_address])
        ] : []),
      ]),
      el('div', { class: 'sentinel-ai-result', id: `ai-result-${ev.id}` }),
    ]);
    container.appendChild(card);
  }
}

/* ── Sidebar panels ────────────────────────────────────── */

function paintSidebar() {
  const container = $('#sentinel-sidebar', root);
  if (!container) return;
  empty(container);

  switch (sideTab) {
    case 'rules':     paintRules(container); break;
    case 'devices':   paintDevices(container); break;
    case 'notifiers': paintNotifiers(container); break;
  }
}

/* ── Rules ─────────────────────────────────────────────── */

function paintRules(container) {
  // Add rule button + AI generate
  container.appendChild(
    el('div', { style: 'display:flex;gap:6px;margin-bottom:4px;flex-wrap:wrap' }, [
      el('button', {
        class: 'sentinel-toggle', id: 'sentinel-add-rule',
      }, ['+ ' + t('sentinel.addRule')]),
      el('button', {
        class: 'sentinel-toggle sentinel-ai-btn', id: 'sentinel-ai-gen-rule',
      }, ['\uD83E\uDDE0 Generate Rule']),
    ])
  );

  if (rules.length === 0) {
    container.appendChild(
      el('div', { style: 'color:var(--muted);text-align:center;padding:20px;font-size:0.75rem' },
        [t('sentinel.noRules')])
    );
    return;
  }

  for (const rule of rules) {
    let conditionsText = '';
    try {
      const conds = typeof rule.conditions === 'string' ? JSON.parse(rule.conditions) : rule.conditions;
      conditionsText = Object.entries(conds || {}).map(([k, v]) => `${k}: ${v}`).join(', ');
    } catch { conditionsText = ''; }

    let actionsText = '';
    try {
      const acts = typeof rule.actions === 'string' ? JSON.parse(rule.actions) : rule.actions;
      actionsText = (acts || []).join(', ');
    } catch { actionsText = ''; }

    container.appendChild(
      el('div', { class: 'sentinel-rule' }, [
        el('div', { class: 'sentinel-rule-info' }, [
          el('div', { class: 'sentinel-rule-name' }, [
            el('span', {
              style: `color:${rule.enabled ? 'var(--acid)' : 'var(--muted)'}`,
            }, [rule.enabled ? '● ' : '○ ']),
            escapeHtml(rule.name),
          ]),
          el('div', { class: 'sentinel-rule-type' }, [
            rule.trigger_type,
            conditionsText ? ` — ${conditionsText}` : '',
          ]),
          el('div', { class: 'sentinel-rule-type' }, [
            `${t('sentinel.ruleLogic')}: ${rule.logic || 'AND'} · ${t('sentinel.ruleActions')}: ${actionsText}`,
          ]),
        ]),
        el('div', { class: 'sentinel-rule-actions' }, [
          el('button', {
            class: 'sentinel-toggle',
            'data-rule-toggle': rule.id,
            style: 'padding:2px 6px;font-size:0.6rem',
            title: rule.enabled ? t('sentinel.disable') : t('sentinel.enable'),
          }, [rule.enabled ? '⏸' : '▶']),
          el('button', {
            class: 'sentinel-toggle',
            'data-rule-edit': rule.id,
            style: 'padding:2px 6px;font-size:0.6rem',
            title: t('sentinel.editRule'),
          }, ['✏️']),
          el('button', {
            class: 'sentinel-toggle',
            'data-rule-del': rule.id,
            style: 'padding:2px 6px;font-size:0.6rem',
            title: t('sentinel.deleteRule'),
          }, ['🗑']),
        ]),
      ])
    );
  }
}

/* ── Rule editor modal ─────────────────────────────────── */

const TRIGGER_TYPES = [
  'new_device', 'device_join', 'device_leave',
  'arp_spoof', 'port_change', 'mac_flood',
  'rogue_dhcp', 'dns_anomaly',
];

const CONDITION_KEYS = [
  'mac_contains', 'mac_not_contains',
  'ip_prefix', 'ip_not_prefix',
  'vendor_contains', 'min_new_devices', 'trusted_only',
];

const ACTION_TYPES = [
  'notify_web', 'notify_discord', 'notify_webhook', 'notify_email',
];

function showRuleEditor(existing = null) {
  const isEdit = !!existing;
  let conditions = {};
  let actions = ['notify_web'];
  if (existing) {
    try { conditions = typeof existing.conditions === 'string' ? JSON.parse(existing.conditions) : (existing.conditions || {}); } catch { conditions = {}; }
    try { actions = typeof existing.actions === 'string' ? JSON.parse(existing.actions) : (existing.actions || ['notify_web']); } catch { actions = ['notify_web']; }
  }

  // Backdrop
  const backdrop = el('div', {
    class: 'sentinel-modal-backdrop',
    style: 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);z-index:100;display:flex;align-items:center;justify-content:center',
  });

  const modal = el('div', {
    class: 'sentinel-modal',
    style: 'background:var(--c-panel);border:1px solid var(--c-border);border-radius:12px;padding:16px;width:340px;max-width:90vw;max-height:80vh;overflow-y:auto;display:flex;flex-direction:column;gap:10px',
  }, [
    el('h3', { style: 'margin:0;font-size:0.95rem;color:var(--ink)' },
      [isEdit ? t('sentinel.editRule') : t('sentinel.addRule')]),

    labelInput(t('sentinel.ruleName'), 'rule-name', existing?.name || ''),
    labelSelect(t('sentinel.triggerType'), 'rule-trigger', TRIGGER_TYPES, existing?.trigger_type || 'new_device'),
    labelSelect(t('sentinel.ruleLogic'), 'rule-logic', ['AND', 'OR'], existing?.logic || 'AND'),
    labelInput(t('sentinel.cooldown') + ' (s)', 'rule-cooldown', String(existing?.cooldown_s ?? 60), 'number'),

    el('div', { style: 'font-size:0.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px' },
      [t('sentinel.conditions')]),
    ...CONDITION_KEYS.map(key =>
      labelInput(key, `rule-cond-${key}`, conditions[key] ?? '', 'text', key === 'trusted_only' ? 'checkbox' : undefined)
    ),

    el('div', { style: 'font-size:0.72rem;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px' },
      [t('sentinel.ruleActions')]),
    ...ACTION_TYPES.map(act =>
      el('label', { style: 'display:flex;align-items:center;gap:6px;font-size:0.75rem;color:var(--ink);cursor:pointer' }, [
        el('input', { type: 'checkbox', 'data-action': act, ...(actions.includes(act) ? { checked: '' } : {}) }),
        act,
      ])
    ),

    el('div', { style: 'display:flex;gap:8px;justify-content:flex-end;margin-top:6px' }, [
      el('button', {
        class: 'sentinel-toggle', id: 'rule-cancel',
        style: 'padding:5px 12px',
      }, [t('sentinel.cancel')]),
      el('button', {
        class: 'sentinel-toggle active', id: 'rule-save',
        style: 'padding:5px 12px',
      }, [t('sentinel.save')]),
    ]),
  ]);

  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);

  // Close on backdrop click
  backdrop.addEventListener('click', (e) => {
    if (e.target === backdrop) backdrop.remove();
  });

  // Cancel
  $('#rule-cancel', modal).addEventListener('click', () => backdrop.remove());

  // Save
  $('#rule-save', modal).addEventListener('click', async () => {
    const name = $('[data-field="rule-name"]', modal)?.value?.trim();
    const triggerType = $('[data-field="rule-trigger"]', modal)?.value;
    const logic = $('[data-field="rule-logic"]', modal)?.value;
    const cooldown = parseInt($('[data-field="rule-cooldown"]', modal)?.value || '60');

    if (!name) { toast(t('sentinel.nameRequired'), 2500, 'error'); return; }

    // Gather conditions
    const conds = {};
    for (const key of CONDITION_KEYS) {
      const input = $(`[data-field="rule-cond-${key}"]`, modal);
      if (!input) continue;
      const val = input.type === 'checkbox' ? (input.checked ? '1' : '') : input.value.trim();
      if (val) conds[key] = val;
    }

    // Gather actions
    const selectedActions = [];
    $$('[data-action]', modal).forEach(cb => {
      if (cb.checked) selectedActions.push(cb.dataset.action);
    });
    if (selectedActions.length === 0) selectedActions.push('notify_web');

    const payload = {
      rule: {
        ...(isEdit ? { id: existing.id } : {}),
        name,
        trigger_type: triggerType,
        logic,
        cooldown_s: cooldown,
        conditions: conds,
        actions: selectedActions,
        enabled: isEdit ? existing.enabled : 1,
      },
    };

    try {
      await api.post('/api/sentinel/rule', payload);
      toast(isEdit ? t('sentinel.ruleUpdated') : t('sentinel.ruleCreated'), 2000, 'success');
      backdrop.remove();
      await refreshRules();
    } catch (err) { toast(err.message, 3000, 'error'); }
  });
}

function labelInput(label, field, value, type = 'text', inputType) {
  const actualType = inputType || type;
  if (actualType === 'checkbox') {
    return el('label', {
      style: 'display:flex;align-items:center;gap:6px;font-size:0.75rem;color:var(--ink);cursor:pointer',
    }, [
      el('input', { type: 'checkbox', 'data-field': field, ...(value === '1' ? { checked: '' } : {}) }),
      label,
    ]);
  }
  return el('div', { style: 'display:flex;flex-direction:column;gap:2px' }, [
    el('label', { style: 'font-size:0.68rem;color:var(--muted);font-weight:600' }, [label]),
    el('input', {
      type: actualType,
      'data-field': field,
      value: value,
      class: 'sentinel-notifier-input',
    }),
  ]);
}

function labelSelect(label, field, options, selected) {
  return el('div', { style: 'display:flex;flex-direction:column;gap:2px' }, [
    el('label', { style: 'font-size:0.68rem;color:var(--muted);font-weight:600' }, [label]),
    el('select', {
      'data-field': field,
      class: 'sentinel-notifier-input',
    }, options.map(o =>
      el('option', { value: o, ...(o === selected ? { selected: '' } : {}) }, [o])
    )),
  ]);
}

/* ── Devices ───────────────────────────────────────────── */

function paintDevices(container) {
  if (devices.length === 0) {
    container.appendChild(
      el('div', { style: 'color:var(--muted);text-align:center;padding:20px;font-size:0.75rem' },
        [t('sentinel.noDevices')])
    );
    return;
  }

  for (const dev of devices) {
    const mac = dev.mac_address;
    container.appendChild(
      el('div', { class: 'sentinel-notifier-row' }, [
        el('div', { style: 'display:flex;justify-content:space-between;align-items:center' }, [
          el('span', {
            class: 'sentinel-rule-name',
            style: 'font-family:monospace;font-size:0.75rem',
          }, [mac]),
          el('span', {
            style: `font-size:0.6rem;padding:1px 6px;border-radius:4px;font-weight:700;${dev.trusted ? 'background:rgba(0,255,154,0.15);color:var(--acid)' : 'background:rgba(255,255,255,0.06);color:var(--muted)'}`,
          }, [dev.trusted ? t('sentinel.trusted') : t('sentinel.untrusted')]),
        ]),
        el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap;align-items:center' }, [
          miniInput(t('sentinel.alias'), `dev-alias-${mac}`, dev.alias || '', '80px'),
          miniInput(t('sentinel.expectedIps'), `dev-ips-${mac}`, dev.expected_ips || '', '100px'),
          el('label', { style: 'display:flex;align-items:center;gap:4px;font-size:0.65rem;color:var(--muted);cursor:pointer' }, [
            el('input', { type: 'checkbox', 'data-field': `dev-trusted-${mac}`, ...(dev.trusted ? { checked: '' } : {}) }),
            t('sentinel.trusted'),
          ]),
          el('button', {
            class: 'sentinel-toggle',
            'data-dev-save': mac,
            style: 'padding:2px 6px;font-size:0.6rem',
          }, ['💾']),
        ]),
        el('div', { class: 'sentinel-rule-type' }, [
          `${t('sentinel.lastSeen')}: ${formatTime(dev.last_seen)}`,
          dev.notes ? ` · ${dev.notes}` : '',
        ]),
      ])
    );
  }
}

function miniInput(placeholder, field, value, width) {
  return el('input', {
    type: 'text',
    placeholder,
    'data-field': field,
    value,
    class: 'sentinel-notifier-input',
    style: `width:${width};padding:3px 5px;font-size:0.68rem`,
  });
}

async function saveDevice(mac) {
  const alias   = $(`[data-field="dev-alias-${mac}"]`, root)?.value || '';
  const ips     = $(`[data-field="dev-ips-${mac}"]`, root)?.value || '';
  const trusted = $(`[data-field="dev-trusted-${mac}"]`, root)?.checked ? 1 : 0;
  try {
    await api.post('/api/sentinel/device', { mac_address: mac, alias, expected_ips: ips, trusted });
    toast(t('sentinel.deviceSaved'), 2000, 'success');
  } catch (err) { toast(err.message, 3000, 'error'); }
}

/* ── Notifiers ─────────────────────────────────────────── */

function paintNotifiers(container) {
  const fields = [
    { key: 'discord_webhook', label: t('sentinel.discordWebhook'), placeholder: 'https://discord.com/api/webhooks/...' },
    { key: 'webhook_url',     label: t('sentinel.webhookUrl'),     placeholder: 'https://example.com/hook' },
    { key: 'email_smtp_host', label: t('sentinel.smtpHost'),       placeholder: 'smtp.gmail.com' },
    { key: 'email_smtp_port', label: t('sentinel.smtpPort'),       placeholder: '587' },
    { key: 'email_username',  label: t('sentinel.smtpUser'),       placeholder: 'user@example.com' },
    { key: 'email_password',  label: t('sentinel.smtpPass'),       placeholder: '••••••••', type: 'password' },
    { key: 'email_from',      label: t('sentinel.emailFrom'),      placeholder: 'sentinel@bjorn.local' },
    { key: 'email_to',        label: t('sentinel.emailTo'),        placeholder: 'admin@example.com' },
  ];

  for (const f of fields) {
    container.appendChild(
      el('div', { class: 'sentinel-notifier-row' }, [
        el('label', { class: 'sentinel-notifier-label' }, [f.label]),
        el('input', {
          type: f.type || 'text',
          'data-notifier': f.key,
          placeholder: f.placeholder,
          value: notifierCfg[f.key] || '',
          class: 'sentinel-notifier-input',
        }),
      ])
    );
  }

  container.appendChild(
    el('button', {
      class: 'sentinel-toggle active',
      id: 'sentinel-save-notifiers',
      style: 'align-self:flex-end;margin-top:6px;padding:5px 14px',
    }, [t('sentinel.saveNotifiers')])
  );
}

async function saveNotifiers() {
  const notifiers = {};
  $$('[data-notifier]', root).forEach(input => {
    const val = input.value.trim();
    if (val) notifiers[input.dataset.notifier] = val;
  });
  try {
    await api.post('/api/sentinel/notifiers', { notifiers });
    toast(t('sentinel.notifiersSaved'), 2000, 'success');
  } catch (err) { toast(err.message, 3000, 'error'); }
}

/* ── AI Functions ──────────────────────────────────────── */

async function analyzeEvent(eventId) {
  const resultEl = $(`#ai-result-${eventId}`, root);
  if (!resultEl) return;

  // Toggle: if already showing, hide
  if (resultEl.classList.contains('active')) {
    resultEl.classList.remove('active');
    return;
  }

  resultEl.textContent = '\u23F3 Analyzing...';
  resultEl.classList.add('active');

  try {
    const res = await api.post('/api/sentinel/analyze', { event_ids: [eventId] });
    if (res?.status === 'ok') {
      resultEl.textContent = res.analysis;
    } else {
      resultEl.textContent = '\u274C ' + (res?.message || 'Analysis failed');
    }
  } catch (e) {
    resultEl.textContent = '\u274C Error: ' + e.message;
  }
}

async function summarizeEvents() {
  const btn = $('#sentinel-ai-summary', root);
  if (btn) btn.textContent = '\u23F3 Summarizing...';

  try {
    const res = await api.post('/api/sentinel/summarize', {});
    if (res?.status === 'ok') {
      // Show summary at the top of the event feed
      const container = $('#sentinel-events', root);
      if (container) {
        const existing = container.querySelector('.sentinel-ai-summary');
        if (existing) existing.remove();
        const summary = el('div', { class: 'sentinel-ai-summary' }, [
          el('div', { style: 'font-weight:600;font-size:0.7rem;margin-bottom:4px;color:var(--acid)' },
            ['\uD83E\uDDE0 AI Summary']),
          el('div', { style: 'font-size:0.7rem;white-space:pre-wrap' }, [res.summary]),
        ]);
        container.insertBefore(summary, container.firstChild);
      }
      toast('Summary generated');
    } else {
      toast('Summary failed: ' + (res?.message || 'unknown'), 3000, 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 3000, 'error');
  } finally {
    if (btn) btn.textContent = '\uD83E\uDDE0 AI Summary';
  }
}

async function generateRuleFromAI() {
  const desc = prompt('Describe the rule you want (e.g. "alert when a new device joins my network"):');
  if (!desc || !desc.trim()) return;

  toast('\u23F3 Generating rule...');
  try {
    const res = await api.post('/api/sentinel/suggest-rule', { description: desc.trim() });
    if (res?.status === 'ok' && res.rule) {
      showRuleEditor(res.rule);
      toast('Rule generated — review and save');
    } else {
      toast('Could not generate rule: ' + (res?.message || res?.raw || 'unknown'), 4000, 'error');
    }
  } catch (e) {
    toast('Error: ' + e.message, 3000, 'error');
  }
}

/* ── Helpers ───────────────────────────────────────────── */

function formatEventType(type) {
  return (type || 'unknown').replace(/_/g, ' ');
}

function formatTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts.includes('Z') || ts.includes('+') ? ts : ts + 'Z');
    const now = Date.now();
    const diff = now - d.getTime();
    if (diff < 60000) return t('sentinel.justNow');
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  } catch { return ts; }
}
