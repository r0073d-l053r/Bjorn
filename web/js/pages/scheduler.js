/**
 * Scheduler page module.
 * Kanban-style board with 6 lanes, live refresh, countdown timers, search, history modal.
 * Endpoints: GET /action_queue, POST /queue_cmd, GET /attempt_history
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { buildConditionEditor, getConditions } from '../core/condition-builder.js';

const PAGE = 'scheduler';
const PAGE_SIZE = 100;
const LANES = ['running', 'pending', 'upcoming', 'success', 'failed', 'cancelled'];
const LANE_LABELS = {
  running: () => t('sched.running'),
  pending: () => t('sched.pending'),
  upcoming: () => t('sched.upcoming'),
  success: () => t('sched.success'),
  failed: () => t('sched.failed'),
  cancelled: () => t('sched.cancelled')
};

/* ── state ── */
let tracker = null;
let poller = null;
let clockTimer = null;
let LIVE = true;
let FOCUS = false;
let COMPACT = false;
let COLLAPSED = false;
let INCLUDE_SUPERSEDED = false;
let lastBuckets = null;
let showCount = null;
let lastFilterKey = '';
let iconCache = new Map();
/** Map<lane, Map<cardKey, DOM element>> for incremental updates */
let laneCardMaps = new Map();

/* ── tab state ── */
let activeTab = 'queue';
let schedulePoller = null;
let triggerPoller = null;
let scriptsList = [];

/* ── lifecycle ── */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  container.appendChild(buildShell());
  tracker.trackEventListener(window, 'keydown', (e) => {
    if (e.key === 'Escape') closeModal();
  });
  fetchScriptsList();
  switchTab('queue');
}

export function unmount() {
  clearTimeout(searchDeb);
  searchDeb = null;
  if (poller) { poller.stop(); poller = null; }
  if (schedulePoller) { schedulePoller.stop(); schedulePoller = null; }
  if (triggerPoller) { triggerPoller.stop(); triggerPoller = null; }
  if (clockTimer) { clearInterval(clockTimer); clockTimer = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  lastBuckets = null;
  showCount = null;
  lastFilterKey = '';
  iconCache.clear();
  laneCardMaps.clear();
  scriptsList = [];
  activeTab = 'queue';
  LIVE = true; FOCUS = false; COMPACT = false;
  COLLAPSED = false; INCLUDE_SUPERSEDED = false;
}

/* ── shell ── */
function buildShell() {
  return el('div', { class: 'scheduler-container' }, [
    el('div', { id: 'sched-errorBar', class: 'notice', style: 'display:none' }),
    /* ── tab bar ── */
    el('div', { class: 'sched-tabs' }, [
      el('button', { class: 'sched-tab sched-tab-active', 'data-tab': 'queue', onclick: () => switchTab('queue') }, ['Queue']),
      el('button', { class: 'sched-tab', 'data-tab': 'schedules', onclick: () => switchTab('schedules') }, ['Schedules']),
      el('button', { class: 'sched-tab', 'data-tab': 'triggers', onclick: () => switchTab('triggers') }, ['Triggers']),
    ]),
    /* ── Queue tab content (existing kanban) ── */
    el('div', { id: 'sched-tab-queue', class: 'sched-tab-content' }, [
      el('div', { class: 'controls' }, [
        el('input', {
          type: 'text', id: 'sched-search', placeholder: t('sched.filterPlaceholder'),
          oninput: onSearch
        }),
        pill('sched-liveBtn', t('common.on'), true, () => setLive(!LIVE)),
        pill('sched-refBtn', t('common.refresh'), false, () => tick()),
        pill('sched-focBtn', t('sched.focusActive'), false, () => { FOCUS = !FOCUS; $('#sched-focBtn')?.classList.toggle('active', FOCUS); lastFilterKey = ''; tick(); }),
        pill('sched-cmpBtn', t('sched.compact'), false, () => { COMPACT = !COMPACT; $('#sched-cmpBtn')?.classList.toggle('active', COMPACT); lastFilterKey = ''; tick(); }),
        pill('sched-colBtn', t('sched.collapse'), false, toggleCollapse),
        pill('sched-supBtn', INCLUDE_SUPERSEDED ? t('sched.hideSuperseded') : t('sched.showSuperseded'), false, toggleSuperseded),
        el('span', { id: 'sched-stats', class: 'stats' }),
      ]),
      el('div', { id: 'sched-boardWrap', class: 'boardWrap' }, [
        el('div', { id: 'sched-board', class: 'board' }),
      ]),
    ]),
    /* ── Schedules tab content ── */
    el('div', { id: 'sched-tab-schedules', class: 'sched-tab-content', style: 'display:none' }, [
      buildSchedulesPanel(),
    ]),
    /* ── Triggers tab content ── */
    el('div', { id: 'sched-tab-triggers', class: 'sched-tab-content', style: 'display:none' }, [
      buildTriggersPanel(),
    ]),
    /* history modal */
    el('div', {
      id: 'sched-histModal', class: 'modalOverlay', style: 'display:none', 'aria-hidden': 'true',
      onclick: (e) => { if (e.target.id === 'sched-histModal') closeModal(); }
    }, [
      el('div', { class: 'modal' }, [
        el('div', { class: 'modalHeader' }, [
          el('div', { class: 'title' }, [t('sched.history')]),
          el('div', { id: 'sched-histTitle', class: 'muted' }),
          el('div', { class: 'spacer' }),
          el('button', { class: 'xBtn', onclick: closeModal }, [t('common.close')]),
        ]),
        el('div', { id: 'sched-histBody', class: 'modalBody' }),
        el('div', { class: 'modalFooter' }, [
          el('small', {}, [t('sched.historyColorCoded')]),
        ]),
      ]),
    ]),
  ]);
}

/* ── tab switching ── */
function switchTab(tab) {
  activeTab = tab;

  /* update tab buttons */
  $$('.sched-tab').forEach(btn => {
    btn.classList.toggle('sched-tab-active', btn.dataset.tab === tab);
  });

  /* show/hide tab content */
  ['queue', 'schedules', 'triggers'].forEach(id => {
    const panel = $(`#sched-tab-${id}`);
    if (panel) panel.style.display = id === tab ? '' : 'none';
  });

  /* stop all pollers first */
  if (poller) { poller.stop(); poller = null; }
  if (schedulePoller) { schedulePoller.stop(); schedulePoller = null; }
  if (triggerPoller) { triggerPoller.stop(); triggerPoller = null; }

  /* start relevant pollers */
  if (tab === 'queue') {
    tick();
    setLive(true);
  } else if (tab === 'schedules') {
    refreshScheduleList();
    schedulePoller = new Poller(refreshScheduleList, 10000, { immediate: false });
    schedulePoller.start();
  } else if (tab === 'triggers') {
    refreshTriggerList();
    triggerPoller = new Poller(refreshTriggerList, 10000, { immediate: false });
    triggerPoller.start();
  }
}

/* ── fetch scripts list ── */
async function fetchScriptsList() {
  try {
    const data = await api.get('/list_scripts', { timeout: 12000 });
    scriptsList = Array.isArray(data) ? data : (data?.scripts || data?.actions || []);
  } catch (e) {
    scriptsList = [];
  }
}

function populateScriptSelect(selectEl) {
  empty(selectEl);
  selectEl.appendChild(el('option', { value: '' }, ['-- Select script --']));
  scriptsList.forEach(s => {
    const name = typeof s === 'string' ? s : (s.name || s.action_name || '');
    if (name) selectEl.appendChild(el('option', { value: name }, [name]));
  });
}

/* ══════════════════════════════════════════════════════════════════
   SCHEDULES TAB
   ══════════════════════════════════════════════════════════════════ */

function buildSchedulesPanel() {
  return el('div', { class: 'schedules-panel' }, [
    buildScheduleForm(),
    el('div', { id: 'sched-schedule-list' }),
  ]);
}

function buildScheduleForm() {
  const typeToggle = el('select', { id: 'sched-sform-type', onchange: onScheduleTypeChange }, [
    el('option', { value: 'recurring' }, ['Recurring']),
    el('option', { value: 'oneshot' }, ['One-shot']),
  ]);

  const presets = [
    { label: '60s', val: 60 }, { label: '5m', val: 300 }, { label: '15m', val: 900 },
    { label: '30m', val: 1800 }, { label: '1h', val: 3600 }, { label: '6h', val: 21600 },
    { label: '24h', val: 86400 },
  ];

  const intervalRow = el('div', { id: 'sched-sform-interval-row' }, [
    el('label', {}, ['Interval (seconds): ']),
    el('input', { type: 'number', id: 'sched-sform-interval', min: '1', value: '300', style: 'width:100px' }),
    el('span', { style: 'margin-left:8px' },
      presets.map(p =>
        el('button', {
          class: 'pill', type: 'button', style: 'margin:0 2px',
          onclick: () => { const inp = $('#sched-sform-interval'); if (inp) inp.value = p.val; }
        }, [p.label])
      )
    ),
  ]);

  const runAtRow = el('div', { id: 'sched-sform-runat-row', style: 'display:none' }, [
    el('label', {}, ['Run at: ']),
    el('input', { type: 'datetime-local', id: 'sched-sform-runat' }),
  ]);

  return el('div', { class: 'schedules-form' }, [
    el('h3', {}, ['Create Schedule']),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Script: ']),
      el('select', { id: 'sched-sform-script' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Type: ']),
      typeToggle,
    ]),
    intervalRow,
    runAtRow,
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Args (optional): ']),
      el('input', { type: 'text', id: 'sched-sform-args', placeholder: 'CLI arguments' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('button', { class: 'btn', onclick: createSchedule }, ['Create']),
    ]),
  ]);
}

function onScheduleTypeChange() {
  const type = $('#sched-sform-type')?.value;
  const intervalRow = $('#sched-sform-interval-row');
  const runAtRow = $('#sched-sform-runat-row');
  if (intervalRow) intervalRow.style.display = type === 'recurring' ? '' : 'none';
  if (runAtRow) runAtRow.style.display = type === 'oneshot' ? '' : 'none';
}

async function createSchedule() {
  const script = $('#sched-sform-script')?.value;
  if (!script) { toast('Please select a script', 2600, 'error'); return; }

  const type = $('#sched-sform-type')?.value || 'recurring';
  const args = $('#sched-sform-args')?.value || '';

  const payload = { script, type, args };
  if (type === 'recurring') {
    payload.interval = parseInt($('#sched-sform-interval')?.value || '300', 10);
  } else {
    payload.run_at = $('#sched-sform-runat')?.value || '';
    if (!payload.run_at) { toast('Please set a run time', 2600, 'error'); return; }
  }

  try {
    await api.post('/api/schedules/create', payload);
    toast('Schedule created');
    refreshScheduleList();
  } catch (e) {
    toast('Failed to create schedule: ' + e.message, 3000, 'error');
  }
}

async function refreshScheduleList() {
  const container = $('#sched-schedule-list');
  if (!container) return;

  /* also refresh script selector */
  const sel = $('#sched-sform-script');
  if (sel && sel.children.length <= 1) populateScriptSelect(sel);

  try {
    const data = await api.post('/api/schedules/list', {});
    const schedules = Array.isArray(data) ? data : (data?.schedules || []);
    renderScheduleList(container, schedules);
  } catch (e) {
    empty(container);
    container.appendChild(el('div', { class: 'notice error' }, ['Failed to load schedules: ' + e.message]));
  }
}

function renderScheduleList(container, schedules) {
  empty(container);
  if (!schedules.length) {
    container.appendChild(el('div', { class: 'empty' }, ['No schedules configured']));
    return;
  }

  schedules.forEach(s => {
    const typeBadge = el('span', { class: `badge status-${s.type === 'recurring' ? 'running' : 'upcoming'}` }, [s.type || 'recurring']);
    const timing = s.type === 'oneshot'
      ? `Run at: ${fmt(s.run_at)}`
      : `Every ${ms2str((s.interval || 0) * 1000)}`;

    const nextRun = s.next_run_at ? `Next: ${fmt(s.next_run_at)}` : '';
    const statusBadge = s.last_status
      ? el('span', { class: `badge status-${s.last_status}` }, [s.last_status])
      : el('span', { class: 'badge' }, ['never run']);

    const toggleBtn = el('label', { class: 'toggle-switch' }, [
      el('input', {
        type: 'checkbox',
        checked: s.enabled !== false,
        onchange: () => toggleSchedule(s.id, !s.enabled)
      }),
      el('span', { class: 'toggle-slider' }),
    ]);

    const deleteBtn = el('button', { class: 'btn danger', onclick: () => deleteSchedule(s.id) }, ['Delete']);
    const editBtn = el('button', { class: 'btn', onclick: () => editScheduleInline(s) }, ['Edit']);

    container.appendChild(el('div', { class: 'card', 'data-schedule-id': s.id }, [
      el('div', { class: 'cardHeader' }, [
        el('div', { class: 'actionName' }, [
          el('span', { class: 'chip', style: `--h:${hashHue(s.script || '')}` }, [s.script || '']),
        ]),
        typeBadge,
        toggleBtn,
      ]),
      el('div', { class: 'meta' }, [
        el('span', {}, [timing]),
        nextRun ? el('span', {}, [nextRun]) : null,
        el('span', {}, [`Runs: ${s.run_count || 0}`]),
        statusBadge,
      ].filter(Boolean)),
      s.args ? el('div', { class: 'kv' }, [el('span', {}, [`Args: ${s.args}`])]) : null,
      el('div', { class: 'btns' }, [editBtn, deleteBtn]),
    ].filter(Boolean)));
  });
}

async function toggleSchedule(id, enabled) {
  try {
    await api.post('/api/schedules/toggle', { id, enabled });
    toast(enabled ? 'Schedule enabled' : 'Schedule disabled');
    refreshScheduleList();
  } catch (e) {
    toast('Toggle failed: ' + e.message, 3000, 'error');
  }
}

async function deleteSchedule(id) {
  if (!confirm('Delete this schedule?')) return;
  try {
    await api.post('/api/schedules/delete', { id });
    toast('Schedule deleted');
    refreshScheduleList();
  } catch (e) {
    toast('Delete failed: ' + e.message, 3000, 'error');
  }
}

function editScheduleInline(s) {
  const card = $(`[data-schedule-id="${s.id}"]`);
  if (!card) return;

  empty(card);

  const isRecurring = s.type === 'recurring';

  card.appendChild(el('div', { class: 'schedules-form' }, [
    el('h3', {}, ['Edit Schedule']),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Script: ']),
      (() => {
        const sel = el('select', { id: `sched-edit-script-${s.id}` });
        populateScriptSelect(sel);
        sel.value = s.script || '';
        return sel;
      })(),
    ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Type: ']),
      (() => {
        const sel = el('select', { id: `sched-edit-type-${s.id}` }, [
          el('option', { value: 'recurring' }, ['Recurring']),
          el('option', { value: 'oneshot' }, ['One-shot']),
        ]);
        sel.value = s.type || 'recurring';
        return sel;
      })(),
    ]),
    isRecurring
      ? el('div', { class: 'form-row' }, [
          el('label', {}, ['Interval (seconds): ']),
          el('input', { type: 'number', id: `sched-edit-interval-${s.id}`, value: String(s.interval || 300), min: '1', style: 'width:100px' }),
        ])
      : el('div', { class: 'form-row' }, [
          el('label', {}, ['Run at: ']),
          el('input', { type: 'datetime-local', id: `sched-edit-runat-${s.id}`, value: s.run_at || '' }),
        ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Args: ']),
      el('input', { type: 'text', id: `sched-edit-args-${s.id}`, value: s.args || '' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('button', { class: 'btn', onclick: async () => {
        const payload = {
          id: s.id,
          script: $(`#sched-edit-script-${s.id}`)?.value,
          type: $(`#sched-edit-type-${s.id}`)?.value,
          args: $(`#sched-edit-args-${s.id}`)?.value || '',
        };
        if (payload.type === 'recurring') {
          payload.interval = parseInt($(`#sched-edit-interval-${s.id}`)?.value || '300', 10);
        } else {
          payload.run_at = $(`#sched-edit-runat-${s.id}`)?.value || '';
        }
        try {
          await api.post('/api/schedules/update', payload);
          toast('Schedule updated');
          refreshScheduleList();
        } catch (e) {
          toast('Update failed: ' + e.message, 3000, 'error');
        }
      }}, ['Save']),
      el('button', { class: 'btn warn', onclick: () => refreshScheduleList() }, ['Cancel']),
    ]),
  ]));
}

/* ══════════════════════════════════════════════════════════════════
   TRIGGERS TAB
   ══════════════════════════════════════════════════════════════════ */

function buildTriggersPanel() {
  return el('div', { class: 'triggers-panel' }, [
    buildTriggerForm(),
    el('div', { id: 'sched-trigger-list' }),
  ]);
}

function buildTriggerForm() {
  const conditionContainer = el('div', { id: 'sched-tform-conditions' });

  const form = el('div', { class: 'triggers-form' }, [
    el('h3', {}, ['Create Trigger']),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Script: ']),
      el('select', { id: 'sched-tform-script' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Trigger name: ']),
      el('input', { type: 'text', id: 'sched-tform-name', placeholder: 'Trigger name' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Conditions:']),
      conditionContainer,
    ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Cooldown (seconds): ']),
      el('input', { type: 'number', id: 'sched-tform-cooldown', value: '60', min: '0', style: 'width:100px' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('label', {}, ['Args (optional): ']),
      el('input', { type: 'text', id: 'sched-tform-args', placeholder: 'CLI arguments' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('button', { class: 'btn', onclick: testTriggerConditions }, ['Test Conditions']),
      el('span', { id: 'sched-tform-test-result', style: 'margin-left:8px' }),
    ]),
    el('div', { class: 'form-row' }, [
      el('button', { class: 'btn', onclick: createTrigger }, ['Create Trigger']),
    ]),
  ]);

  /* initialize condition builder after DOM is ready */
  setTimeout(() => {
    const cond = $('#sched-tform-conditions');
    if (cond) buildConditionEditor(cond);
  }, 0);

  return form;
}

async function testTriggerConditions() {
  const condContainer = $('#sched-tform-conditions');
  const resultEl = $('#sched-tform-test-result');
  if (!condContainer || !resultEl) return;

  const conditions = getConditions(condContainer);
  try {
    const data = await api.post('/api/triggers/test', { conditions });
    resultEl.textContent = data?.result ? 'Result: TRUE' : 'Result: FALSE';
    resultEl.style.color = data?.result ? 'var(--green, #0f0)' : 'var(--red, #f00)';
  } catch (e) {
    resultEl.textContent = 'Test failed: ' + e.message;
    resultEl.style.color = 'var(--red, #f00)';
  }
}

async function createTrigger() {
  const script = $('#sched-tform-script')?.value;
  const name = $('#sched-tform-name')?.value;
  if (!script) { toast('Please select a script', 2600, 'error'); return; }
  if (!name) { toast('Please enter a trigger name', 2600, 'error'); return; }

  const condContainer = $('#sched-tform-conditions');
  const conditions = condContainer ? getConditions(condContainer) : [];
  const cooldown = parseInt($('#sched-tform-cooldown')?.value || '60', 10);
  const args = $('#sched-tform-args')?.value || '';

  try {
    await api.post('/api/triggers/create', { script, name, conditions, cooldown, args });
    toast('Trigger created');
    $('#sched-tform-name').value = '';
    refreshTriggerList();
  } catch (e) {
    toast('Failed to create trigger: ' + e.message, 3000, 'error');
  }
}

async function refreshTriggerList() {
  const container = $('#sched-trigger-list');
  if (!container) return;

  /* also refresh script selector */
  const sel = $('#sched-tform-script');
  if (sel && sel.children.length <= 1) populateScriptSelect(sel);

  try {
    const data = await api.post('/api/triggers/list', {});
    const triggers = Array.isArray(data) ? data : (data?.triggers || []);
    renderTriggerList(container, triggers);
  } catch (e) {
    empty(container);
    container.appendChild(el('div', { class: 'notice error' }, ['Failed to load triggers: ' + e.message]));
  }
}

function renderTriggerList(container, triggers) {
  empty(container);
  if (!triggers.length) {
    container.appendChild(el('div', { class: 'empty' }, ['No triggers configured']));
    return;
  }

  triggers.forEach(trig => {
    const condCount = Array.isArray(trig.conditions) ? trig.conditions.length : 0;

    const toggleBtn = el('label', { class: 'toggle-switch' }, [
      el('input', {
        type: 'checkbox',
        checked: trig.enabled !== false,
        onchange: () => toggleTrigger(trig.id, !trig.enabled)
      }),
      el('span', { class: 'toggle-slider' }),
    ]);

    const deleteBtn = el('button', { class: 'btn danger', onclick: () => deleteTrigger(trig.id) }, ['Delete']);

    container.appendChild(el('div', { class: 'card' }, [
      el('div', { class: 'cardHeader' }, [
        el('div', { class: 'actionName' }, [
          el('strong', {}, [trig.name || '']),
          el('span', { style: 'margin-left:8px' }, [' \u2192 ']),
          el('span', { class: 'chip', style: `--h:${hashHue(trig.script || '')}` }, [trig.script || '']),
        ]),
        toggleBtn,
      ]),
      el('div', { class: 'meta' }, [
        el('span', {}, [`${condCount} condition${condCount !== 1 ? 's' : ''}`]),
        el('span', {}, [`Cooldown: ${ms2str(( trig.cooldown || 0) * 1000)}`]),
        el('span', {}, [`Fired: ${trig.fire_count || 0}`]),
        trig.last_fired_at ? el('span', {}, [`Last: ${fmt(trig.last_fired_at)}`]) : null,
      ].filter(Boolean)),
      trig.args ? el('div', { class: 'kv' }, [el('span', {}, [`Args: ${trig.args}`])]) : null,
      el('div', { class: 'btns' }, [deleteBtn]),
    ].filter(Boolean)));
  });
}

async function toggleTrigger(id, enabled) {
  try {
    await api.post('/api/triggers/toggle', { id, enabled });
    toast(enabled ? 'Trigger enabled' : 'Trigger disabled');
    refreshTriggerList();
  } catch (e) {
    toast('Toggle failed: ' + e.message, 3000, 'error');
  }
}

async function deleteTrigger(id) {
  if (!confirm('Delete this trigger?')) return;
  try {
    await api.post('/api/triggers/delete', { id });
    toast('Trigger deleted');
    refreshTriggerList();
  } catch (e) {
    toast('Delete failed: ' + e.message, 3000, 'error');
  }
}

function pill(id, text, active, onclick) {
  return el('span', { id, class: `pill ${active ? 'active' : ''}`, onclick }, [text]);
}

/* ── data fetch ── */
async function fetchQueue() {
  const data = await api.get('/action_queue', { timeout: 8000 });
  const rawRows = Array.isArray(data) ? data : (data?.rows || []);
  return rawRows.map(normalizeRow);
}

function normalizeRow(r) {
  const status = (r.status || '').toLowerCase() === 'expired' ? 'failed' : (r.status || '').toLowerCase();
  const scheduled_ms = isoToMs(r.scheduled_for);
  const created_ms = isoToMs(r.created_at) || Date.now();
  const started_ms = isoToMs(r.started_at);
  const completed_ms = isoToMs(r.completed_at);

  let _computed_status = status;
  if (status === 'scheduled') _computed_status = 'upcoming';
  else if (status === 'pending' && scheduled_ms > Date.now()) _computed_status = 'upcoming';

  const tags = dedupeArr(toArray(r.tags));
  const metadata = typeof r.metadata === 'string' ? parseJSON(r.metadata, {}) : (r.metadata || {});

  return {
    ...r, status, scheduled_ms, created_ms, started_ms, completed_ms,
    _computed_status, tags, metadata,
    mac: r.mac || r.mac_address || '',
    priority_effective: r.priority_effective ?? r.priority ?? 0,
  };
}

/* ── tick / render ── */
async function tick() {
  try {
    const rows = await fetchQueue();
    render(rows);
  } catch (e) {
    showError(t('sched.fetchError') + ': ' + e.message);
  }
}

function render(rows) {
  const q = ($('#sched-search')?.value || '').toLowerCase();

  /* filter */
  let filtered = rows;
  if (q) {
    filtered = filtered.filter(r => {
      const bag = `${r.action_name} ${r.mac} ${r.ip} ${r.hostname} ${r.service} ${r.port} ${(r.tags || []).join(' ')}`.toLowerCase();
      return bag.includes(q);
    });
  }
  if (FOCUS) filtered = filtered.filter(r => ['upcoming', 'pending', 'running'].includes(r._computed_status));

  /* superseded filter */
  if (!INCLUDE_SUPERSEDED) {
    const activeKeys = new Set();
    filtered.forEach(r => {
      if (['upcoming', 'pending', 'running'].includes(r._computed_status)) {
        activeKeys.add(`${r.action_name}|${r.mac}|${r.port || 0}`);
      }
    });
    filtered = filtered.filter(r => {
      if (r._computed_status !== 'failed') return true;
      const key = `${r.action_name}|${r.mac}|${r.port || 0}`;
      return !activeKeys.has(key);
    });
  }

  /* dedupe failed: keep highest retry per key */
  const failMap = new Map();
  filtered.filter(r => r._computed_status === 'failed').forEach(r => {
    const key = `${r.action_name}|${r.mac}|${r.port || 0}`;
    const prev = failMap.get(key);
    if (!prev || (r.retry_count || 0) > (prev.retry_count || 0) || r.created_ms > prev.created_ms) failMap.set(key, r);
  });
  const failIds = new Set(Array.from(failMap.values()).map(r => r.id));
  filtered = filtered.filter(r => r._computed_status !== 'failed' || failIds.has(r.id));

  /* bucket */
  const buckets = {};
  LANES.forEach(l => buckets[l] = []);
  filtered.forEach(r => {
    const lane = buckets[r._computed_status];
    if (lane) lane.push(r);
  });

  /* sort per lane */
  const byNewest = (a, b) => Math.max(b.completed_ms, b.started_ms, b.created_ms) - Math.max(a.completed_ms, a.started_ms, a.created_ms);
  const byPrio = (a, b) => (b.priority_effective - a.priority_effective) || byNewest(a, b);
  buckets.running.sort(byPrio);
  buckets.pending.sort((a, b) => byPrio(a, b) || (a.scheduled_ms || a.created_ms) - (b.scheduled_ms || b.created_ms));
  buckets.upcoming.sort((a, b) => (a.scheduled_ms || Infinity) - (b.scheduled_ms || Infinity));
  buckets.success.sort((a, b) => (b.completed_ms || b.started_ms || b.created_ms) - (a.completed_ms || a.started_ms || a.created_ms));
  buckets.failed.sort((a, b) => (b.completed_ms || b.started_ms || b.created_ms) - (a.completed_ms || a.started_ms || a.created_ms));
  buckets.cancelled.sort(byPrio);

  if (COMPACT) {
    LANES.forEach(l => {
      buckets[l] = keepLatest(buckets[l], r => `${r.action_name}|${r.mac}|${r.port || 0}`, r => Math.max(r.completed_ms, r.started_ms, r.created_ms));
    });
  }

  /* stats */
  const total = filtered.length;
  const statsEl = $('#sched-stats');
  if (statsEl) statsEl.textContent = `${total} ${t('sched.entries')} | R:${buckets.running.length} P:${buckets.pending.length} U:${buckets.upcoming.length} S:${buckets.success.length} F:${buckets.failed.length}`;

  /* pagination */
  const fk = filterKey(q);
  if (fk !== lastFilterKey) { showCount = {}; LANES.forEach(l => showCount[l] = PAGE_SIZE); lastFilterKey = fk; }
  if (!showCount) { showCount = {}; LANES.forEach(l => showCount[l] = PAGE_SIZE); }
  lastBuckets = buckets;

  renderBoard(buckets);
}

/* ── cardKey: stable identifier for a card row ── */
function cardKey(r) {
  return `${r.id || ''}|${r.action_name}|${r.mac}|${r.port || 0}|${r._computed_status}`;
}

/* ── card fingerprint for detecting data changes ── */
function cardFingerprint(r) {
  return `${r.status}|${r.retry_count || 0}|${r.priority_effective}|${r.started_at || ''}|${r.completed_at || ''}|${r.error_message || ''}|${r.result_summary || ''}|${(r.tags || []).join(',')}`;
}

/**
 * Incremental board rendering — updates DOM in-place instead of destroying/recreating.
 * This prevents flickering of countdown timers and progress bars.
 */
function renderBoard(buckets) {
  const board = $('#sched-board');
  if (!board) return;

  /* First render: build full structure */
  if (!board.children.length) {
    laneCardMaps.clear();
    LANES.forEach(lane => {
      const items = buckets[lane] || [];
      const visible = items.slice(0, showCount?.[lane] || PAGE_SIZE);
      const cardMap = new Map();
      laneCardMaps.set(lane, cardMap);

      const laneBody = el('div', { class: 'laneBody' });
      if (visible.length === 0) {
        laneBody.appendChild(el('div', { class: 'empty' }, [t('sched.noEntries')]));
      } else {
        visible.forEach(r => {
          const card = cardEl(r);
          card.dataset.cardKey = cardKey(r);
          card.dataset.fp = cardFingerprint(r);
          cardMap.set(cardKey(r), card);
          laneBody.appendChild(card);
        });
        if (items.length > visible.length) {
          laneBody.appendChild(moreBtn(lane));
        }
      }

      const laneEl = el('div', { class: `lane status-${lane}`, 'data-lane': lane }, [
        el('div', { class: 'laneHeader' }, [
          el('span', { class: 'dot' }),
          el('strong', {}, [LANE_LABELS[lane]()]),
          el('span', { class: 'count' }, [String(items.length)]),
        ]),
        laneBody,
      ]);
      board.appendChild(laneEl);
    });

    if (COLLAPSED) $$('.card', board).forEach(c => c.classList.add('collapsed'));
    startClock();
    return;
  }

  /* Incremental update: patch each lane in-place */
  LANES.forEach(lane => {
    const items = buckets[lane] || [];
    const visible = items.slice(0, showCount?.[lane] || PAGE_SIZE);
    const laneEl = board.querySelector(`[data-lane="${lane}"]`);
    if (!laneEl) return;

    /* Update header count */
    const countEl = laneEl.querySelector('.laneHeader .count');
    if (countEl) countEl.textContent = String(items.length);

    const laneBody = laneEl.querySelector('.laneBody');
    if (!laneBody) return;

    const oldMap = laneCardMaps.get(lane) || new Map();
    const newMap = new Map();
    const desiredKeys = visible.map(r => cardKey(r));
    const desiredSet = new Set(desiredKeys);

    /* Remove cards no longer present */
    for (const [key, cardDom] of oldMap) {
      if (!desiredSet.has(key)) {
        cardDom.remove();
      }
    }

    /* Remove "more" button and empty message (will re-add if needed) */
    laneBody.querySelectorAll('.moreBtn, .empty').forEach(n => n.remove());

    /* Add/update cards in order */
    let prevNode = null;
    for (let i = 0; i < visible.length; i++) {
      const r = visible[i];
      const key = cardKey(r);
      const fp = cardFingerprint(r);
      let cardDom = oldMap.get(key);

      if (cardDom) {
        /* Card exists - check if data changed */
        if (cardDom.dataset.fp !== fp) {
          /* Data changed - replace with fresh card */
          const newCard = cardEl(r);
          newCard.dataset.cardKey = key;
          newCard.dataset.fp = fp;
          if (COLLAPSED) newCard.classList.add('collapsed');
          cardDom.replaceWith(newCard);
          cardDom = newCard;
        }
        newMap.set(key, cardDom);
      } else {
        /* New card */
        cardDom = cardEl(r);
        cardDom.dataset.cardKey = key;
        cardDom.dataset.fp = fp;
        if (COLLAPSED) cardDom.classList.add('collapsed');
        newMap.set(key, cardDom);
      }

      /* Ensure correct order in DOM */
      const expectedAfter = prevNode;
      const actualPrev = cardDom.previousElementSibling;
      if (actualPrev !== expectedAfter || !cardDom.parentNode) {
        if (expectedAfter) {
          expectedAfter.after(cardDom);
        } else {
          laneBody.prepend(cardDom);
        }
      }
      prevNode = cardDom;
    }

    /* Empty state */
    if (visible.length === 0) {
      laneBody.appendChild(el('div', { class: 'empty' }, [t('sched.noEntries')]));
    }

    /* "More" button */
    if (items.length > visible.length) {
      laneBody.appendChild(moreBtn(lane));
    }

    laneCardMaps.set(lane, newMap);
  });

  startClock();
}

function moreBtn(lane) {
  return el('button', {
    class: 'moreBtn', onclick: () => {
      showCount[lane] = (showCount[lane] || PAGE_SIZE) + PAGE_SIZE;
      if (lastBuckets) renderBoard(lastBuckets);
    }
  }, [t('sched.displayMore')]);
}

function startClock() {
  if (clockTimer) clearInterval(clockTimer);
  clockTimer = setInterval(updateCountdowns, 1000);
}

/* ── origin badge resolver ── */
function _resolveOrigin(r) {
  const md = r.metadata || {};
  const trigger = (r.trigger_source || md.trigger_source || '').toLowerCase();
  const method = (md.decision_method || '').toLowerCase();
  const origin = (md.decision_origin || '').toLowerCase();

  // LLM orchestrator (autonomous or advisor)
  if (trigger === 'llm_autonomous' || origin === 'llm' || method === 'llm_autonomous')
    return { label: 'LLM', cls: 'llm' };
  if (trigger === 'llm_advisor' || method === 'llm_advisor')
    return { label: 'LLM Advisor', cls: 'llm' };
  // AI model (ML-based decision)
  if (method === 'ai_confirmed' || method === 'ai_boosted' || origin === 'ai_confirmed')
    return { label: 'AI', cls: 'ai' };
  // MCP (external tool call)
  if (trigger === 'mcp' || trigger === 'mcp_tool')
    return { label: 'MCP', cls: 'mcp' };
  // Manual (UI or API)
  if (trigger === 'ui' || trigger === 'manual' || trigger === 'api')
    return { label: 'Manual', cls: 'manual' };
  // Scheduler heuristic (default)
  if (trigger === 'scheduler' || trigger === 'trigger_event' || method === 'heuristic')
    return { label: 'Heuristic', cls: 'heuristic' };
  // Fallback: show trigger if known
  if (trigger) return { label: trigger, cls: 'heuristic' };
  return null;
}

/* ── card ── */
function cardEl(r) {
  const cs = r._computed_status;
  const children = [];

  /* info button */
  children.push(el('button', {
    class: 'infoBtn', title: t('sched.history'),
    onclick: () => openHistory(r.action_name, r.mac, r.port || 0)
  }, ['i']));

  /* header */
  children.push(el('div', { class: 'cardHeader' }, [
    el('div', { class: 'actionIconWrap' }, [
      el('img', {
        class: 'actionIcon', src: resolveIconSync(r.action_name),
        width: '80', height: '80', onerror: (e) => { e.target.src = '/actions/actions_icons/default.png'; }
      }),
    ]),
    el('div', { class: 'actionName' }, [
      el('span', { class: 'chip', style: `--h:${hashHue(r.action_name)}` }, [r.action_name]),
    ]),
    el('span', { class: `badge status-${cs}` }, [cs]),
  ]));

  /* origin badge — shows who queued this action */
  const origin = _resolveOrigin(r);
  if (origin) {
    children.push(el('div', { class: 'originBadge origin-' + origin.cls }, [origin.label]));
  }

  /* chips */
  const chips = [];
  if (r.hostname) chips.push(chipEl(r.hostname, 195));
  if (r.ip) chips.push(chipEl(r.ip, 195));
  if (r.port) chips.push(chipEl(`${t('sched.port')} ${r.port}`, 210, t('sched.port')));
  if (r.mac) chips.push(chipEl(r.mac, 195));
  if (chips.length) children.push(el('div', { class: 'chips' }, chips));

  /* service kv */
  if (r.service) children.push(el('div', { class: 'kv' }, [el('span', {}, [`${t('sched.service')}: ${r.service}`])]));

  /* tags */
  if (r.tags?.length) {
    children.push(el('div', { class: 'tags' },
      r.tags.map(tag => el('span', { class: 'tag' }, [tag]))));
  }

  /* timer */
  if ((cs === 'upcoming' || (cs === 'pending' && r.scheduled_ms > Date.now())) && r.scheduled_ms) {
    children.push(el('div', { class: 'timer', 'data-type': 'start', 'data-ts': String(r.scheduled_ms) }, [
      t('sched.eligibleIn') + ' ', el('span', { class: 'cd' }, ['-']),
    ]));
    children.push(el('div', { class: 'progress' }, [
      el('div', { class: 'bar', 'data-start': String(r.created_ms), 'data-end': String(r.scheduled_ms), style: 'width:0%' }),
    ]));
  } else if (cs === 'running' && r.started_ms) {
    children.push(el('div', { class: 'timer', 'data-type': 'elapsed', 'data-ts': String(r.started_ms) }, [
      t('sched.elapsed') + ' ', el('span', { class: 'cd' }, ['-']),
    ]));
  }

  /* meta */
  const meta = [el('span', {}, [`${t('sched.created')}: ${fmt(r.created_at)}`])];
  if (r.started_at) meta.push(el('span', {}, [`${t('sched.started')}: ${fmt(r.started_at)}`]));
  if (r.completed_at) meta.push(el('span', {}, [`${t('sched.done')}: ${fmt(r.completed_at)}`]));
  if (r.retry_count > 0) meta.push(el('span', { class: 'chip', style: '--h:30' }, [
    `${t('sched.retries')} ${r.retry_count}${r.max_retries != null ? '/' + r.max_retries : ''}`]));
  if (r.priority_effective) meta.push(el('span', {}, [`${t('sched.priority')}: ${r.priority_effective}`]));
  children.push(el('div', { class: 'meta' }, meta));

  /* buttons */
  const btns = [];
  if (['upcoming', 'scheduled', 'pending', 'running'].includes(r.status)) {
    btns.push(el('button', { class: 'btn warn', onclick: () => queueCmd(r.id, 'cancel') }, [t('common.cancel')]));
  }
  if (!['running', 'pending', 'scheduled'].includes(r.status)) {
    btns.push(el('button', { class: 'btn danger', onclick: () => queueCmd(r.id, 'delete') }, [t('common.delete')]));
  }
  if (btns.length) children.push(el('div', { class: 'btns' }, btns));

  /* error / result */
  if (r.error_message) children.push(el('div', { class: 'notice error' }, [r.error_message]));
  if (r.result_summary) children.push(el('div', { class: 'notice success' }, [r.result_summary]));

  return el('div', { class: `card status-${cs}` }, children);
}

function chipEl(text, hue, prefix) {
  const parts = [];
  if (prefix) parts.push(el('span', { class: 'k' }, [prefix]), '\u00A0');
  parts.push(text);
  return el('span', { class: 'chip', style: `--h:${hue}` }, parts);
}

/* ── countdown / progress ── */
function updateCountdowns() {
  const now = Date.now();
  $$('.timer').forEach(timer => {
    const type = timer.dataset.type;
    const ts = parseInt(timer.dataset.ts);
    const cd = timer.querySelector('.cd');
    if (!cd || !ts) return;
    if (type === 'start') {
      const diff = ts - now;
      cd.textContent = diff <= 0 ? t('sched.due') : ms2str(diff);
    } else if (type === 'elapsed') {
      cd.textContent = ms2str(now - ts);
    }
  });
  $$('.progress .bar').forEach(bar => {
    const start = parseInt(bar.dataset.start);
    const end = parseInt(bar.dataset.end);
    if (!start || !end || end <= start) return;
    const pct = Math.min(100, Math.max(0, ((now - start) / (end - start)) * 100));
    bar.style.width = pct + '%';
  });
}

/* ── queue command ── */
async function queueCmd(id, cmd) {
  try {
    await api.post('/queue_cmd', { id, cmd });
    tick();
  } catch (e) {
    showError(t('sched.cmdFailed') + ': ' + e.message);
  }
}

/* ── history modal ── */
async function openHistory(action, mac, port) {
  const modal = $('#sched-histModal');
  const title = $('#sched-histTitle');
  const body = $('#sched-histBody');
  if (!modal || !body) return;

  if (title) title.textContent = `\u2014 ${action} \u00B7 ${mac}${port && port !== 0 ? ` \u00B7 port ${port}` : ''}`;
  empty(body);
  body.appendChild(el('div', { class: 'empty' }, [t('common.loading')]));
  modal.style.display = 'flex';
  modal.setAttribute('aria-hidden', 'false');

  try {
    const url = `/attempt_history?action=${encodeURIComponent(action)}&mac=${encodeURIComponent(mac)}&port=${encodeURIComponent(port)}&limit=100`;
    const data = await api.get(url, { timeout: 8000 });
    const rows = Array.isArray(data) ? data : (data?.rows || data || []);

    empty(body);
    if (!rows.length) {
      body.appendChild(el('div', { class: 'empty' }, [t('sched.noHistory')]));
      return;
    }

    const norm = rows.map(x => ({
      status: (x.status || '').toLowerCase(),
      retry_count: Number(x.retry_count || 0),
      max_retries: x.max_retries,
      ts: x.ts || x.completed_at || x.started_at || x.scheduled_for || x.created_at || '',
    })).sort((a, b) => (b.ts > a.ts ? 1 : -1));

    norm.forEach(hr => {
      const st = hr.status || 'unknown';
      const retry = (hr.retry_count || hr.max_retries != null)
        ? el('span', { style: 'color:var(--ink)' }, [`${t('sched.retry')} ${hr.retry_count}${hr.max_retries != null ? '/' + hr.max_retries : ''}`])
        : null;

      body.appendChild(el('div', { class: `histRow hist-${st}` }, [
        el('span', { class: 'ts' }, [fmt(hr.ts)]),
        retry,
        el('span', { style: 'margin-left:auto' }),
        el('span', { class: 'st' }, [st]),
      ].filter(Boolean)));
    });
  } catch (e) {
    empty(body);
    body.appendChild(el('div', { class: 'empty' }, [`${t('common.error')}: ${e.message}`]));
  }
}

function closeModal() {
  const modal = $('#sched-histModal');
  if (!modal) return;
  modal.style.display = 'none';
  modal.setAttribute('aria-hidden', 'true');
}

/* ── controls ── */
function setLive(on) {
  LIVE = on;
  const btn = $('#sched-liveBtn');
  if (btn) btn.classList.toggle('active', LIVE);
  if (poller) { poller.stop(); poller = null; }
  if (LIVE) {
    poller = new Poller(tick, 2500, { immediate: false });
    poller.start();
  }
}

function toggleCollapse() {
  COLLAPSED = !COLLAPSED;
  const btn = $('#sched-colBtn');
  if (btn) btn.textContent = COLLAPSED ? t('sched.expand') : t('sched.collapse');
  $$('#sched-board .card').forEach(c => c.classList.toggle('collapsed', COLLAPSED));
}

function toggleSuperseded() {
  INCLUDE_SUPERSEDED = !INCLUDE_SUPERSEDED;
  const btn = $('#sched-supBtn');
  if (btn) {
    btn.classList.toggle('active', INCLUDE_SUPERSEDED);
    btn.textContent = INCLUDE_SUPERSEDED ? t('sched.hideSuperseded') : t('sched.showSuperseded');
  }
  lastFilterKey = '';
  tick();
}

let searchDeb = null;
function onSearch() {
  clearTimeout(searchDeb);
  searchDeb = setTimeout(() => { lastFilterKey = ''; tick(); }, 180);
}

function showError(msg) {
  const bar = $('#sched-errorBar');
  if (!bar) return;
  bar.textContent = msg;
  bar.style.display = 'block';
  setTimeout(() => { bar.style.display = 'none'; }, 5000);
}

/* ── icon resolution ── */
function resolveIconSync(name) {
  if (iconCache.has(name)) return iconCache.get(name);
  resolveIconAsync(name);
  return '/actions/actions_icons/default.png';
}

async function resolveIconAsync(name) {
  if (iconCache.has(name)) return;
  const candidates = [
    `/actions/actions_icons/${name}.png`,
    `/resources/images/status/${name}/${name}.bmp`,
  ];
  for (const url of candidates) {
    try {
      const r = await fetch(url, { method: 'HEAD', cache: 'force-cache' });
      if (r.ok) { iconCache.set(name, url); updateIconsInDOM(name, url); return; }
    } catch { /* next */ }
  }
  iconCache.set(name, '/actions/actions_icons/default.png');
}

function updateIconsInDOM(name, url) {
  $$(`img.actionIcon`).forEach(img => {
    if (img.closest('.cardHeader')?.querySelector('.actionName')?.textContent?.trim() === name) {
      if (img.src !== url) img.src = url;
    }
  });
}

/* ── helpers ── */
function isoToMs(ts) { if (!ts) return 0; return new Date(ts + (ts.includes('Z') || ts.includes('+') ? '' : 'Z')).getTime() || 0; }
function fmt(ts) { if (!ts) return '-'; try { return new Date(ts + (ts.includes('Z') || ts.includes('+') ? '' : 'Z')).toLocaleString(); } catch { return ts; } }
function ms2str(ms) {
  if (ms < 0) ms = 0;
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m ${String(sec).padStart(2, '0')}s`;
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`;
  return `${sec}s`;
}
function toArray(v) {
  if (!v) return [];
  if (Array.isArray(v)) return v.map(String).filter(Boolean);
  try { const p = JSON.parse(v); if (Array.isArray(p)) return p.map(String).filter(Boolean); } catch { /* noop */ }
  return String(v).split(',').map(s => s.trim()).filter(Boolean);
}
function dedupeArr(a) { return [...new Set(a)]; }
function parseJSON(s, fb) { try { return JSON.parse(s); } catch { return fb; } }
function hashHue(str) { let h = 0; for (let i = 0; i < str.length; i++) h = ((h << 5) - h + str.charCodeAt(i)) | 0; return ((h % 360) + 360) % 360; }
function filterKey(q) { return `${q}|${FOCUS}|${COMPACT}|${INCLUDE_SUPERSEDED}`; }
function keepLatest(rows, keyFn, dateFn) {
  const map = new Map();
  rows.forEach(r => {
    const k = keyFn(r);
    const prev = map.get(k);
    if (!prev || dateFn(r) > dateFn(prev)) map.set(k, r);
  });
  return Array.from(map.values());
}
