/**
 * Zombieland page module — C2 (Command & Control) agent management.
 * Uses Server-Sent Events (SSE) via /c2/events for real-time updates.
 * The EventSource connection is closed in unmount() to prevent leaks.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { initSharedSidebarLayout } from '../core/sidebar-layout.js';

const PAGE = 'zombieland';
const L = (key, fallback, vars = {}) => {
  const v = t(key, vars);
  return v === key ? fallback : v;
};

/* ——— Presence thresholds (ms) ——— */
const PRESENCE = { GRACE: 30000, WARN: 60000, ORANGE: 100000, RED: 160000 };

/* ——— ECG waveform paths ——— */
const ECG_PQRST = 'M0,21 L15,21 L18,19 L20,21 L30,21 L32,23 L34,21 L40,21 L42,12 L44,30 L46,8 L48,35 L50,21 L60,21 L65,21 L70,19 L72,21 L85,21 L90,21 L100,21 L110,21 L115,19 L118,21 L130,21 L132,23 L134,21 L140,21 L142,12 L144,30 L146,8 L148,35 L150,21 L160,21 L170,21 L180,21 L190,21 L200,21';
const ECG_FLAT = 'M0,21 L200,21';

/* ——— State ——— */
let tracker = null;
let poller = null;
let disposeSidebarLayout = null;
let eventSource = null;
let agents = new Map();   // id -> agent object
let selectedAgents = new Set();
let searchTerm = '';
let c2Running = false;
let c2Port = null;
let sseHealthy = false;
let commandHistory = [];
let historyIndex = -1;

function loadStylesheet(path, id) {
  const link = el('link', {
    rel: 'stylesheet',
    href: path,
    id: `style-${id}`
  });
  document.head.appendChild(link);
  return () => {
    const styleElement = document.getElementById(`style-${id}`);
    if (styleElement) {
      styleElement.remove();
    }
  };
}

/* ================================================================
 * Lifecycle
 * ================================================================ */

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);

  // Load page-specific styles and track them for cleanup
  const unloadStyles = loadStylesheet('/web/css/zombieland.css', PAGE);
  tracker.trackResource(unloadStyles);

  agents.clear();
  selectedAgents.clear();
  searchTerm = '';
  c2Running = false;
  c2Port = null;
  sseHealthy = false;
  commandHistory = [];
  historyIndex = -1;

  const shell = buildShell();
  container.appendChild(shell);
  container.appendChild(buildGenerateClientModal());
  container.appendChild(buildFileBrowserModal());

  disposeSidebarLayout = initSharedSidebarLayout(shell, {
    sidebarSelector: '.zl-sidebar',
    mainSelector: '.zl-main',
    storageKey: 'sidebar:zombieland',
    mobileBreakpoint: 900,
    toggleLabel: t('common.menu'),
  });
  await refreshState();
  syncSearchClearButton();
  connectSSE();

  poller = new Poller(refreshState, 10000, { immediate: false });
  poller.start();
  tracker.trackInterval(tickPresence, 1000);
}

export function unmount() {
  if (disposeSidebarLayout) { try { disposeSidebarLayout(); } catch { } disposeSidebarLayout = null; }
  if (eventSource) { eventSource.close(); eventSource = null; }
  sseHealthy = false;
  if (poller) { poller.stop(); poller = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  agents.clear();
  selectedAgents.clear();
  searchTerm = '';
  commandHistory = [];
  historyIndex = -1;
}

/* ================================================================
 * Shell & Modals
 * ================================================================ */

function buildShell() {
  return el('div', { class: 'zombieland-container page-with-sidebar' }, [
    el('aside', { class: 'zl-sidebar page-sidebar' }, [
      el('div', { class: 'sidehead' }, [
        el('div', { class: 'sidetitle' }, [t('nav.zombieland')]),
        el('div', { class: 'spacer' }),
        el('button', { class: 'btn', id: 'hideSidebar', 'data-hide-sidebar': '1', type: 'button' }, [t('common.hide')]),
      ]),
      el('div', { class: 'sidecontent' }, [
        el('div', { class: 'zl-stats-grid' }, [
          statItem('zl-stat-total', t('zombie.total')),
          statItem('zl-stat-alive', t('zombie.online')),
          statItem('zl-stat-avg-cpu', t('zombie.avgCpu')),
          statItem('zl-stat-avg-ram', t('zombie.avgRam')),
          statItem('zl-stat-c2', L('zombieland.c2Status', 'C2 Port')),
        ]),
        el('div', { class: 'zl-toolbar' }, [
          el('button', { class: 'btn btn-icon', onclick: onRefresh, title: t('common.refresh') }, [el('i', { 'data-lucide': 'refresh-cw' })]),
          el('button', { class: 'btn', onclick: onGenerateClient }, [el('i', { 'data-lucide': 'plus-circle' }), ' ' + t('zombie.generateClient')]),
          el('button', { class: 'btn btn-primary', onclick: onStartC2 }, [el('i', { 'data-lucide': 'play' }), ' ' + t('zombie.startC2')]),
          el('button', { class: 'btn btn-danger', onclick: onStopC2 }, [el('i', { 'data-lucide': 'square' }), ' ' + t('zombie.stopC2')]),
          el('button', { class: 'btn', onclick: onCheckStale }, [el('i', { 'data-lucide': 'search' }), ' ' + t('zombie.checkStale')]),
          el('button', { class: 'btn btn-danger', onclick: onPurgeStale, title: t('zombie.purgeStaleHint') }, [el('i', { 'data-lucide': 'trash-2' }), ' ' + t('zombie.purgeStale')]),
        ]),
      ]),
    ]),
    el('div', { class: 'zl-main page-main' }, [
      el('div', { class: 'zl-main-grid' }, [
        el('div', { class: 'zl-console-panel' }, [
          el('div', { class: 'zl-panel-header' }, [
            el('span', { class: 'zl-panel-title' }, [t('console.title')]),
            el('div', { class: 'zl-quickbar' }, [
              quickCmd('sysinfo'), quickCmd('pwd'), quickCmd('ls -la'), quickCmd('ps aux'), quickCmd('ip a'),
            ]),
            el('button', { class: 'btn btn-sm btn-icon', onclick: clearConsole, title: t('zombie.clearConsole') }, [el('i', { 'data-lucide': 'trash-2' })]),
          ]),
          el('div', { class: 'zl-console-output', id: 'zl-console-output' }),
          el('div', { class: 'zl-console-input-row' }, [
            el('select', { class: 'zl-target-select', id: 'zl-target-select' }, [
              el('option', { value: 'broadcast' }, [t('zombie.allAgents')]),
              el('option', { value: 'selected' }, [t('zombie.selectedAgents')]),
            ]),
            el('input', { type: 'text', class: 'zl-cmd-input', id: 'zl-cmd-input', placeholder: t('zombie.enterCommand'), onkeydown: onCmdKeyDown }),
            el('button', { class: 'btn btn-primary', onclick: onSendCommand }, [el('i', { 'data-lucide': 'send' }), ' ' + t('common.send')]),
          ]),
        ]),
        el('div', { class: 'zl-agents-panel' }, [
          el('div', { class: 'zl-panel-header' }, [
            el('span', { class: 'zl-panel-title' }, [t('zombie.agents'), ' (', el('span', { id: 'zl-agent-count' }, ['0']), ')']),
            el('div', { class: 'zl-toolbar-left' }, [
              el('input', { type: 'text', class: 'zl-search-input', id: 'zl-search', placeholder: t('zombie.fileBrowser'), oninput: onSearch }),
              el('button', { class: 'zl-search-clear', onclick: clearSearch }, [el('i', { 'data-lucide': 'x' })]),
            ]),
            el('button', { class: 'btn btn-sm btn-icon', onclick: onSelectAll, title: t('zombie.selectAll') }, [el('i', { 'data-lucide': 'check-square' })]),
            el('button', { class: 'btn btn-sm btn-icon', onclick: onDeselectAll, title: t('zombie.deselectAll') }, [el('i', { 'data-lucide': 'square' })]),
          ]),
          el('div', { class: 'zl-agents-list', id: 'zl-agents-list', onclick: onAgentListClick }),
        ]),
      ]),
      el('div', { class: 'zl-logs-panel' }, [
        el('div', { class: 'zl-panel-header' }, [
          el('span', { class: 'zl-panel-title' }, [el('i', { 'data-lucide': 'file-text' }), ' ' + t('zombie.systemLogs')]),
          el('button', { class: 'btn btn-sm btn-icon', onclick: clearLogs, title: t('zombie.clearLogs') }, [el('i', { 'data-lucide': 'trash-2' })]),
        ]),
        el('div', { class: 'zl-logs-output', id: 'zl-logs-output' }),
      ]),
    ]),
  ]);
}

function statItem(id, label) {
  return el('div', { class: 'stat-item' }, [
    el('span', { class: 'stat-value', id }, ['0']),
    el('span', { class: 'stat-label' }, [label]),
  ]);
}

function quickCmd(cmd) {
  return el('button', {
    class: 'quick-cmd', onclick: () => {
      const input = $('#zl-cmd-input');
      if (input) { input.value = cmd; input.focus(); }
    }
  }, [cmd]);
}

function buildGenerateClientModal() {
  return el('div', { id: 'generateModal', class: 'modal', style: 'display:none;' }, [
    el('div', { class: 'modal-content' }, [
      el('h3', { class: 'modal-title' }, [t('zombie.generateClient')]),
      el('div', { class: 'form-grid' }, [
        el('label', {}, [t('zombie.clientId')]),
        el('input', { id: 'clientId', type: 'text', class: 'input', placeholder: 'zombie01' }),
        el('label', {}, [t('common.platform')]),
        el('select', { id: 'clientPlatform', class: 'select' }, [
          el('option', { value: 'linux' }, ['Linux']),
          el('option', { value: 'windows' }, ['Windows']),
          el('option', { value: 'macos' }, ['macOS']),
          el('option', { value: 'universal' }, ['Universal (Python)']),
        ]),
        el('label', {}, [t('zombie.labCreds')]),
        el('div', { class: 'grid-col-2' }, [
          el('input', { id: 'labUser', type: 'text', class: 'input', placeholder: t('common.username') }),
          el('input', { id: 'labPass', type: 'password', class: 'input', placeholder: t('common.password') }),
        ]),
      ]),
      el('div', { class: 'deploy-options' }, [
        el('h4', {}, [t('zombie.deployOptions')]),
        el('label', { class: 'checkbox-label' }, [
          el('input', { type: 'checkbox', id: 'deploySSH', onchange: (e) => { $('#sshOptions').classList.toggle('hidden', !e.target.checked); } }),
          el('span', {}, [t('zombie.deployViaSSH')]),
        ]),
        el('div', { id: 'sshOptions', class: 'hidden form-grid' }, [
          el('label', {}, [t('zombie.sshHost')]), el('input', { id: 'sshHost', type: 'text', class: 'input' }),
          el('label', {}, [t('zombie.sshUser')]), el('input', { id: 'sshUser', type: 'text', class: 'input' }),
          el('label', {}, [t('zombie.sshPass')]), el('input', { id: 'sshPass', type: 'password', class: 'input' }),
        ]),
      ]),
      el('div', { class: 'modal-actions' }, [
        el('button', { class: 'btn', onclick: () => $('#generateModal').style.display = 'none' }, [t('common.cancel')]),
        el('button', { class: 'btn btn-primary', onclick: onConfirmGenerate }, [t('common.generate')]),
      ]),
    ]),
  ]);
}

function buildFileBrowserModal() {
  return el('div', { id: 'fileBrowserModal', class: 'modal', style: 'display:none;' }, [
    el('div', { class: 'modal-content' }, [
      el('h3', { class: 'modal-title' }, [t('zombie.fileBrowser'), ' - ', el('span', { id: 'browserAgent' })]),
      el('div', { class: 'file-browser-nav' }, [
        el('input', { id: 'browserPath', type: 'text', class: 'input flex-grow' }),
        el('button', { class: 'btn', onclick: browseDirectory }, [t('common.browse')]),
        el('button', { class: 'btn', onclick: onUploadFile }, [t('common.upload')]),
      ]),
      el('div', { id: 'fileList', class: 'file-list' }),
      el('div', { class: 'modal-actions' }, [
        el('button', { class: 'btn', onclick: () => $('#fileBrowserModal').style.display = 'none' }, [t('common.close')]),
      ]),
    ]),
  ]);
}

/* ================================================================
 * Data fetching & SSE
 * ================================================================ */

async function refreshState() {
  try {
    if (sseHealthy && eventSource && eventSource.readyState === EventSource.OPEN) return;
    const [status, agentList] = await Promise.all([
      api.get('/c2/status').catch(() => null),
      api.get('/c2/agents').catch(() => null),
    ]);
    if (!tracker) return; /* unmounted while awaiting */
    if (status) { c2Running = !!status.running; c2Port = status.port || null; }
    if (Array.isArray(agentList)) {
      for (const a of agentList) {
        const id = a.id || a.agent_id || a.client_id;
        if (!id) continue;
        const existing = agents.get(id) || {};
        const merged = { ...existing, ...a, id, last_seen: maxTimestamp(existing.last_seen, a.last_seen) };
        agents.set(id, merged);
      }
    }
    renderAgents();
    updateStats();
  } catch (err) { console.warn(`[${PAGE}] refreshState error:`, err.message); }
}

function connectSSE() {
  if (eventSource) eventSource.close();
  eventSource = new EventSource('/c2/events');
  eventSource.onopen = () => { sseHealthy = true; systemLog('info', t('zombie.connectedToC2')); };
  eventSource.onerror = () => { sseHealthy = false; systemLog('error', t('zombie.c2ConnectionLost')); };
  eventSource.addEventListener('status', (e) => {
    try { const data = JSON.parse(e.data); c2Running = !!data.running; c2Port = data.port || null; updateStats(); } catch { }
  });
  eventSource.addEventListener('telemetry', (e) => {
    try {
      const data = JSON.parse(e.data);
      const id = data.id || data.agent_id; if (!id) return;
      const now = Date.now();
      const existing = agents.get(id) || {};
      const agent = { ...existing, ...data, id, last_seen: now };
      agents.set(id, agent);
      if (computePresence(existing, now).status !== computePresence(agent, now).status) {
        systemLog('success', t('zombie.telemetryReceived', { name: agent.hostname || id }));
      }
      const card = $('[data-agent-id="' + id + '"]');
      if (card) { card.classList.add('pulse'); tracker.trackTimeout(() => card.classList.remove('pulse'), 600); }
      renderAgents();
      updateStats();
    } catch { }
  });
  eventSource.addEventListener('log', (e) => { try { const d = JSON.parse(e.data); systemLog(d.level || 'info', d.text || ''); } catch { } });
  eventSource.addEventListener('console', (e) => { try { const d = JSON.parse(e.data); consoleLog(d.kind || 'RX', d.text || '', d.target || null); } catch { } });
}

/* ================================================================
 * Presence, Ticking, and Rendering
 * ================================================================ */

function computePresence(agent, now) {
  if (!agent || !agent.last_seen) return { status: 'offline', delta: null, color: 'red', bpm: 0 };
  const last = parseTs(agent.last_seen);
  if (isNaN(last)) return { status: 'offline', delta: null, color: 'red', bpm: 0 };
  const delta = now - last;
  if (delta < PRESENCE.GRACE) return { status: 'online', delta, color: 'green', bpm: 55 };
  if (delta < PRESENCE.WARN) return { status: 'online', delta, color: 'green', bpm: 40 };
  if (delta < PRESENCE.ORANGE) return { status: 'idle', delta, color: 'yellow', bpm: 22 };
  if (delta < PRESENCE.RED) return { status: 'idle', delta, color: 'orange', bpm: 12 };
  return { status: 'offline', delta, color: 'red', bpm: 0 };
}

function tickPresence() {
  const now = Date.now();
  document.querySelectorAll('.zl-agent-card').forEach(card => {
    const agentId = card.dataset.agentId;
    const agent = agents.get(agentId);
    if (!agent) return;
    const pres = computePresence(agent, now);
    const counter = $('#zl-ecg-counter-' + agentId);
    if (counter) counter.textContent = pres.delta != null ? Math.floor(pres.delta / 1000) + 's' : '--';
    const ecgEl = $('#zl-ecg-' + agentId);
    if (ecgEl) {
      ecgEl.className = `ecg ${pres.color} ${pres.bpm === 0 ? 'flat' : ''}`;
      const wrapper = ecgEl.querySelector('.ecg-wrapper');
      if (wrapper) wrapper.style.animationDuration = `${pres.bpm > 0 ? 72 / pres.bpm : 3.2}s`;
    }
    const pill = card.querySelector('.zl-pill');
    if (pill) { pill.className = `zl-pill ${pres.status}`; pill.textContent = pres.status; }
    card.classList.toggle('agent-stale-yellow', pres.status === 'idle' && pres.color === 'yellow');
    card.classList.toggle('agent-stale-orange', pres.status === 'idle' && pres.color === 'orange');
    card.classList.toggle('agent-stale-red', pres.status === 'offline');
  });
  updateStats();
}

function renderAgents() {
  const list = $('#zl-agents-list');
  if (!list) return;
  const now = Date.now();
  const needle = searchTerm.toLowerCase();
  const deduped = dedupeAgents(Array.from(agents.values()));
  const filtered = deduped.filter(a => !needle || [a.id, a.hostname, a.ip, a.os, a.mac].filter(Boolean).join(' ').toLowerCase().includes(needle));
  filtered.sort((a, b) => {
    const pa = computePresence(a, now), pb = computePresence(b, now);
    const rank = { online: 0, idle: 1, offline: 2 };
    if (rank[pa.status] !== rank[pb.status]) return rank[pa.status] - rank[pb.status];
    return (a.hostname || a.id || '').localeCompare(b.hostname || b.id || '');
  });
  empty(list);
  if (filtered.length === 0) {
    list.appendChild(el('div', { class: 'zl-empty' }, [searchTerm ? t('zombie.noAgentsMatchSearch') : t('zombie.noAgentsConnected')]));
  } else {
    filtered.forEach(agent => list.appendChild(createAgentCard(agent, now)));
  }
  updateTargetSelect();
  const countEl = $('#zl-agent-count');
  if (countEl) {
    const onlineCount = filtered.filter(a => computePresence(a, now).status === 'online').length;
    countEl.textContent = `${onlineCount}/${filtered.length}`;
  }
  if (window.lucide) window.lucide.createIcons();
}

function createAgentCard(agent, now) {
  const id = agent.id;
  const pres = computePresence(agent, now);
  let staleClass = pres.status === 'idle' ? ` agent-stale-${pres.color}` : (pres.status === 'offline' ? ' agent-stale-red' : '');
  const isSelected = selectedAgents.has(id);

  return el('div', { class: `zl-agent-card ${isSelected ? 'selected' : ''}${staleClass}`, 'data-agent-id': id }, [
    el('div', { class: 'zl-card-header' }, [
      el('input', { type: 'checkbox', class: 'agent-checkbox', checked: isSelected, 'data-agent-id': id }),
      el('div', { class: 'zl-card-identity' }, [
        el('div', { class: 'zl-card-hostname' }, [agent.hostname || t('common.unknown')]),
        el('div', { class: 'zl-card-id' }, [id]),
      ]),
      el('span', { class: 'zl-pill ' + pres.status }, [pres.status]),
    ]),
    el('div', { class: 'zl-card-info' }, [
      infoRow(t('common.os'), agent.os || t('common.unknown')),
      infoRow(t('common.ip'), agent.ip || 'N/A'),
      infoRow(t('zombie.cpuRam'), `${agent.cpu || 0}% / ${agent.mem || 0}%`),
    ]),
    el('div', { class: 'zl-ecg-row' }, [
      createECG(id, pres.color, pres.bpm),
      el('span', { class: 'zl-ecg-counter', id: 'zl-ecg-counter-' + id }, [pres.delta != null ? Math.floor(pres.delta / 1000) + 's' : '--']),
    ]),
    el('div', { class: 'zl-card-actions' }, [
      el('button', { class: 'btn btn-sm btn-icon', 'data-action': 'shell', title: t('zombie.terminal') }, [el('i', { 'data-lucide': 'terminal' })]),
      el('button', { class: 'btn btn-sm btn-icon', 'data-action': 'browse', title: t('zombie.fileBrowser') }, [el('i', { 'data-lucide': 'folder' })]),
      el('button', { class: 'btn btn-sm btn-icon btn-danger', 'data-action': 'remove', title: t('zombie.removeAgent') }, [el('i', { 'data-lucide': 'x' })]),
    ]),
  ]);
}

function createECG(id, colorClass, bpm) {
  const ns = 'http://www.w3.org/2000/svg';
  const path = document.createElementNS(ns, 'path');
  path.setAttribute('d', bpm > 0 ? ECG_PQRST : ECG_FLAT);
  const svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 200 42');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.appendChild(path);
  const wrapper = el('div', { class: 'ecg-wrapper', style: `animation-duration: ${bpm > 0 ? 72 / bpm : 3.2}s` }, [svg, svg.cloneNode(true), svg.cloneNode(true)]);
  return el('div', { class: `ecg ${colorClass} ${bpm === 0 ? 'flat' : ''}`, id: 'zl-ecg-' + id }, [wrapper]);
}

function updateStats() {
  const now = Date.now();
  const all = Array.from(agents.values());
  const onlineAgents = all.filter(a => computePresence(a, now).status === 'online');
  const sv = (id, v) => { const e = $(`#${id}`); if (e) e.textContent = v; };
  sv('zl-stat-total', String(all.length));
  sv('zl-stat-alive', String(onlineAgents.length));
  const avgCPU = onlineAgents.length ? Math.round(onlineAgents.reduce((s, a) => s + (a.cpu || 0), 0) / onlineAgents.length) : 0;
  const avgRAM = onlineAgents.length ? Math.round(onlineAgents.reduce((s, a) => s + (a.mem || 0), 0) / onlineAgents.length) : 0;
  sv('zl-stat-avg-cpu', `${avgCPU}%`);
  sv('zl-stat-avg-ram', `${avgRAM}%`);
  const c2El = $('#zl-stat-c2');
  if (c2El) {
    c2El.textContent = c2Running ? `${t('status.online')} :${c2Port || '?'}` : t('status.offline');
    c2El.className = `stat-value ${c2Running ? 'stat-online' : 'stat-offline'}`;
  }
}

/* ================================================================
 * Event Handlers
 * ================================================================ */

function onAgentListClick(e) {
  const card = e.target.closest('.zl-agent-card');
  if (!card) return;
  const agentId = card.dataset.agentId;
  const agent = agents.get(agentId);

  if (e.target.matches('.agent-checkbox')) {
    if (e.target.checked) selectedAgents.add(agentId);
    else selectedAgents.delete(agentId);
    renderAgents();
  } else if (e.target.dataset.action) {
    switch (e.target.dataset.action) {
      case 'shell': focusGlobalConsole(agentId); break;
      case 'browse': openFileBrowser(agentId); break;
      case 'remove': onRemoveAgent(agentId, agent.hostname || agentId); break;
    }
  }
}

function onSelectAll() {
  document.querySelectorAll('.agent-checkbox').forEach(cb => {
    selectedAgents.add(cb.dataset.agentId);
    cb.checked = true;
  });
  renderAgents();
}

function onDeselectAll() {
  selectedAgents.clear();
  renderAgents();
}

function onSearch(e) {
  searchTerm = (e.target.value || '').trim();
  syncSearchClearButton();
  renderAgents();
}

function clearSearch() {
  const input = $('#zl-search');
  if (input) input.value = '';
  searchTerm = '';
  renderAgents();
  syncSearchClearButton();
}

function syncSearchClearButton() {
  const clearBtn = $('.zl-search-clear');
  if (clearBtn) clearBtn.style.display = searchTerm.length > 0 ? 'inline-block' : 'none';
}

function onRefresh() {
  const wasSseHealthy = sseHealthy;
  sseHealthy = false;
  refreshState().finally(() => { sseHealthy = wasSseHealthy; });
  toast(t('common.refreshed'));
}

async function onStartC2() {
  const port = prompt(t('zombie.enterC2Port'), '5555');
  if (!port) return;
  try {
    await api.post('/c2/start', { port: parseInt(port) });
    toast(t('zombie.c2StartedOnPort', { port }), 2600, 'success');
    await refreshState();
  } catch (err) { toast(t('zombie.failedStartC2'), 2600, 'error'); }
}

async function onStopC2() {
  if (!confirm(t('zombie.confirmStopC2'))) return;
  try {
    await api.post('/c2/stop');
    toast(t('zombie.c2Stopped'), 2600, 'warning');
    await refreshState();
  } catch (err) { toast(t('zombie.failedStopC2'), 2600, 'error'); }
}

async function onCheckStale() {
  try {
    const result = await api.get('/c2/stale_agents?threshold=300');
    toast(t('zombie.staleFound', { count: result.count }));
    systemLog('info', t('zombie.staleCheck', { count: result.count }));
  } catch (err) { toast('Failed to fetch stale agents', 'error'); }
}

async function onPurgeStale() {
  if (!confirm(t('zombie.confirmPurgeStale'))) return;
  try {
    const result = await api.post('/c2/purge_agents', { threshold: 86400 });
    toast(t('zombie.agentsPurged', { count: result.purged || 0 }), 2600, 'warning');
    await refreshState();
  } catch (err) { toast(t('zombie.failedPurgeStale'), 2600, 'error'); }
}

function onGenerateClient() {
  $('#generateModal').style.display = 'flex';
}

async function onConfirmGenerate() {
  const clientId = $('#clientId').value.trim() || `zombie_${Date.now()}`;
  const data = {
    client_id: clientId,
    platform: $('#clientPlatform').value,
    lab_user: $('#labUser').value.trim(),
    lab_password: $('#labPass').value.trim(),
  };
  try {
    const result = await api.post('/c2/generate_client', data);
    toast(t('zombie.clientGenerated', { id: clientId }), 'success');
    if ($('#deploySSH').checked) {
      await api.post('/c2/deploy', {
        client_id: clientId,
        ssh_host: $('#sshHost').value,
        ssh_user: $('#sshUser').value,
        ssh_pass: $('#sshPass').value,
        lab_user: data.lab_user,
        lab_password: data.lab_password,
      });
      toast(t('zombie.deployStarted', { host: $('#sshHost').value }));
    }
    $('#generateModal').style.display = 'none';
    if (result.filename) {
      const a = el('a', { href: `/c2/download_client/${result.filename}`, download: result.filename });
      a.click();
    }
  } catch (err) { toast(`Failed to generate: ${err.message}`, 'error'); }
}

/* ================================================================
 * Console and Commands
 * ================================================================ */

function consoleLog(type, message, target) {
  const output = $('#zl-console-output'); if (!output) return;
  const time = new Date().toLocaleTimeString('en-US', { hour12: false });
  if (typeof message === 'object') message = JSON.stringify(message, null, 2);
  const line = el('div', { class: 'console-line' }, [
    el('span', { class: 'console-time' }, [time]),
    el('span', { class: 'console-type ' + String(type).toLowerCase() }, [type]),
    target ? el('span', { class: 'console-target' }, ['[' + target + ']']) : null,
    el('div', { class: 'console-content' }, [el('pre', {}, [message])]),
  ]);
  output.appendChild(line);
  output.scrollTop = output.scrollHeight;
}

function systemLog(level, message) {
  const output = $('#zl-logs-output'); if (!output) return;
  const time = new Date().toLocaleTimeString('en-US', { hour12: false });
  output.appendChild(el('div', { class: 'zl-log-line' }, [
    el('span', { class: 'console-time' }, [time]),
    el('span', { class: 'console-type ' + level.toLowerCase() }, [level.toUpperCase()]),
    el('div', { class: 'zl-log-text' }, [message]),
  ]));
  output.scrollTop = output.scrollHeight;
}

function clearConsole() { const e = $('#zl-console-output'); if (e) empty(e); }
function clearLogs() { const e = $('#zl-logs-output'); if (e) empty(e); }

function onCmdKeyDown(e) {
  if (e.key === 'Enter') onSendCommand();
  else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (historyIndex > 0) { historyIndex--; e.target.value = commandHistory[historyIndex] || ''; }
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (historyIndex < commandHistory.length - 1) { historyIndex++; e.target.value = commandHistory[historyIndex] || ''; }
    else { historyIndex = commandHistory.length; e.target.value = ''; }
  }
}

async function onSendCommand() {
  const input = $('#zl-cmd-input');
  const cmd = input.value.trim();
  if (!cmd) return;
  const target = $('#zl-target-select').value;
  let targets = [];
  if (target === 'broadcast') { /* targets remains empty for broadcast */ }
  else if (target === 'selected') { targets = Array.from(selectedAgents); }
  else { targets = [target]; }

  if (target !== 'broadcast' && targets.length === 0) {
    toast(t('zombie.noAgentsSelected'), 'warning');
    return;
  }

  await sendCommand(cmd, targets);
  input.value = '';
}

async function sendCommand(command, targets = []) {
  if (!command) return;
  try {
    const endpoint = targets.length === 0 ? '/c2/broadcast' : '/c2/command';
    const payload = targets.length === 0 ? { command } : { command, targets };
    consoleLog('TX', command, targets.length > 0 ? targets.join(',') : 'ALL');
    await api.post(endpoint, payload);
    toast(t(targets.length === 0 ? 'zombie.commandBroadcasted' : 'zombie.commandSent'), 2600, 'success');
    commandHistory.push(command);
    historyIndex = commandHistory.length;
  } catch (err) { toast(t('zombie.failedSendCommand'), 2600, 'error'); systemLog('error', err.message); }
}

async function onRemoveAgent(agentId, name) {
  if (!confirm(t('zombie.confirmRemoveAgent', { name }))) return;
  try {
    await api.post('/c2/remove_client', { client_id: agentId });
    agents.delete(agentId); selectedAgents.delete(agentId);
    renderAgents();
    toast(t('zombie.agentRemoved', { name }), 2600, 'warning');
  } catch (err) { toast(t('zombie.failedRemoveAgent', { name }), 2600, 'error'); }
}

/* ================================================================
 * File Browser
 * ================================================================ */

function openFileBrowser(agentId) {
  const modal = $('#fileBrowserModal');
  modal.style.display = 'flex';
  modal.dataset.agentId = agentId;
  $('#browserAgent').textContent = agentId;
  $('#browserPath').value = '/';
  browseDirectory();
}

async function browseDirectory() {
  const agentId = $('#fileBrowserModal').dataset.agentId;
  const path = $('#browserPath').value || '/';
  const fileList = $('#fileList');
  empty(fileList);
  fileList.textContent = t('common.loading');
  try {
    await sendCommand(`ls -la ${path}`, [agentId]);
    // The result will arrive via SSE and be handled by the 'console' event listener.
    // For now, we assume it's coming to the main console. A better way would be a dedicated event.
    // This is a limitation of the current design. We can refine it later.
    toast(t('zombie.browseCommandSent'));
  } catch (err) {
    toast(t('zombie.browseCommandFailed'), 'error');
    fileList.textContent = t('common.error');
  }
}

function onUploadFile() {
  const agentId = $('#fileBrowserModal').dataset.agentId;
  const path = $('#browserPath').value || '/';
  const input = el('input', {
    type: 'file',
    onchange: (e) => {
      const file = e.target.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = async (event) => {
        const base64 = btoa(event.target.result);
        const filePath = `${path.endsWith('/') ? path : path + '/'}${file.name}`;
        try {
          await sendCommand(`upload ${filePath} ${base64}`, [agentId]);
          toast(t('zombie.uploadStarted', { name: file.name }));
        } catch { toast(t('zombie.uploadFailed'), 'error'); }
      };
      reader.readAsBinaryString(file);
    }
  });
  input.click();
}


/* ================================================================
 * Helpers
 * ================================================================ */

function updateTargetSelect() {
  const select = $('#zl-target-select');
  if (!select) return;
  const currentVal = select.value;
  empty(select);
  select.appendChild(el('option', { value: 'broadcast' }, [t('zombie.allAgents')]));
  select.appendChild(el('option', { value: 'selected' }, [t('zombie.selectedAgents'), ` (${selectedAgents.size})`]));
  const now = Date.now();
  for (const agent of agents.values()) {
    if (computePresence(agent, now).status === 'online') {
      select.appendChild(el('option', { value: agent.id }, [agent.hostname || agent.id]));
    }
  }
  select.value = currentVal; // Preserve selection if possible
}

function focusGlobalConsole(agentId) {
  const sel = $('#zl-target-select');
  if (sel) sel.value = agentId;
  $('#zl-cmd-input')?.focus();
}

function infoRow(label, value) {
  return el('div', { class: 'zl-info-row' }, [el('span', { class: 'zl-info-label' }, [label + ':']), el('span', { class: 'zl-info-value' }, [value])]);
}

function dedupeAgents(arr) {
  const byHost = new Map();
  arr.forEach(a => {
    const key = (a.hostname || '').trim().toLowerCase() || a.id;
    const prev = byHost.get(key);
    if (!prev || parseTs(a.last_seen) >= parseTs(prev.last_seen)) byHost.set(key, a);
  });
  return Array.from(byHost.values());
}

function maxTimestamp(a, b) {
  const ta = parseTs(a), tb = parseTs(b);
  if (ta == null) return b; if (tb == null) return a;
  return ta >= tb ? a : b;
}

function parseTs(v) {
  if (v == null) return NaN;
  if (typeof v === 'number') return v;
  return Date.parse(v);
}
