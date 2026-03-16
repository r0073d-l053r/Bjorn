/**
 * QuickPanel — WiFi & Bluetooth management panel.
 *
 * Slide-down panel with two tabs (WiFi / Bluetooth), scan controls,
 * auto-scan toggles, known-network management with drag-and-drop
 * priority reordering and multi-select batch delete, potfile upload,
 * and Bluetooth pairing/trust/connect with state indicators.
 */

import { $, $$, el, toast, empty } from './dom.js';
import { api } from './api.js';
import { t } from './i18n.js';

/* ---------- API endpoints ---------- */

const API = {
  scanWifi: '/scan_wifi',
  getKnownWifi: '/get_known_wifi',
  connectKnown: '/connect_known_wifi',
  connectWifi: '/connect_wifi',
  updatePriority: '/update_wifi_priority',
  deleteKnown: '/delete_known_wifi',
  importPotfiles: '/import_potfiles',
  uploadPotfile: '/upload_potfile',
  scanBluetooth: '/scan_bluetooth',
  pairBluetooth: '/pair_bluetooth',
  trustBluetooth: '/trust_bluetooth',
  connectBluetooth: '/connect_bluetooth',
  disconnectBluetooth: '/disconnect_bluetooth',
  forgetBluetooth: '/forget_bluetooth',
};

/* ---------- Constants ---------- */

const AUTOSCAN_INTERVAL = 15_000;         // 15 s
const LS_WIFI_AUTO = 'qp_wifi_auto';
const LS_BT_AUTO = 'qp_bt_auto';

/* ---------- Module state ---------- */

let panel;                // #quickpanel element
let wifiList;             // container for wifi scan results
let knownList;            // container for known networks
let knownWrapper;         // wrapper that holds knownList + batch bar
let btList;               // container for bluetooth results
let wifiTab;              // wifi tab content wrapper
let btTab;                // bluetooth tab content wrapper
let tabBtns;              // [wifiTabBtn, btTabBtn]
let wifiAutoTimer = null;
let btAutoTimer = null;
let activeTab = 'wifi';
let scanning = { wifi: false, bt: false };

// Known networks state
let knownNetworks = [];   // current known networks data
let editMode = false;     // multi-select edit mode
let selectedSsids = new Set();
let batchBar = null;

// Drag state
let dragSrcIdx = null;
let dragPlaceholder = null;
let touchDragEl = null;
let touchStartY = 0;

/* =================================================================
   Helpers
   ================================================================= */

function getAutoScan(key) {
  try { return localStorage.getItem(key) === '1'; } catch { return false; }
}
function setAutoScan(key, on) {
  try { localStorage.setItem(key, on ? '1' : '0'); } catch { /* noop */ }
}

/** Signal strength (0-100 percent) to bar count (1-4). */
function signalBars(pct) {
  if (pct >= 75) return 4;
  if (pct >= 50) return 3;
  if (pct >= 25) return 2;
  return 1;
}

/** Signal strength from BT RSSI (dBm) to bar count (1-4). */
function rssiToBars(rssi) {
  if (rssi === undefined || rssi === null || rssi <= -999) return 0;
  if (rssi >= -50) return 4;
  if (rssi >= -65) return 3;
  if (rssi >= -75) return 2;
  return 1;
}

/** Build a `<span class="sig">` with four bar elements + tooltip. */
function sigEl(pct, tooltip) {
  const count = signalBars(pct);
  const bars = [];
  for (let i = 1; i <= 4; i++) {
    const bar = el('i');
    bar.style.height = `${4 + i * 3}px`;
    if (i <= count) bar.className = 'on';
    bars.push(bar);
  }
  return el('span', { class: 'sig', title: tooltip || `${pct}%` }, bars);
}

/** Build signal bars from RSSI dBm. */
function rssiSigEl(rssi) {
  if (rssi === undefined || rssi === null || rssi <= -999) return null;
  const count = rssiToBars(rssi);
  const bars = [];
  for (let i = 1; i <= 4; i++) {
    const bar = el('i');
    bar.style.height = `${4 + i * 3}px`;
    if (i <= count) bar.className = 'on';
    bars.push(bar);
  }
  return el('span', { class: 'sig', title: `${rssi} dBm` }, bars);
}

/** Security type to badge CSS class. */
function secClass(sec) {
  if (!sec) return 'badge-ok';
  const s = sec.toUpperCase();
  if (s === 'OPEN' || s === '' || s === 'NONE' || s === '--') return 'badge-ok';
  if (s.includes('WPA3')) return 'badge-info';
  if (s.includes('WPA')) return 'badge-warn';
  if (s.includes('WEP')) return 'badge-error';
  return 'badge-warn';
}

function secLabel(sec) {
  if (!sec || sec === '--' || sec.toUpperCase() === 'NONE') return 'Open';
  return sec;
}

function secBadge(sec) {
  return el('span', { class: `badge ${secClass(sec)}`, style: 'font-size:11px;padding:1px 6px' }, [secLabel(sec)]);
}

/** State dot element. */
function stateDot(on) {
  return el('span', { class: `state-dot ${on ? 'state-on' : 'state-off'}` });
}

/** BT device type to icon. */
function btIcon(icon) {
  const map = {
    'audio-card': '\u{1F3B5}',     // music
    'audio-headphones': '\u{1F3A7}', // headphones
    'audio-headset': '\u{1F3A7}',
    'phone': '\u{1F4F1}',           // phone
    'computer': '\u{1F4BB}',        // laptop
    'input-keyboard': '\u2328',     // keyboard
    'input-mouse': '\u{1F5B1}',
    'input-gaming': '\u{1F3AE}',    // gamepad
    'input-tablet': '\u{1F4DD}',
    'printer': '\u{1F5A8}',
    'camera-photo': '\u{1F4F7}',
    'camera-video': '\u{1F4F9}',
    'modem': '\u{1F4E1}',
    'network-wireless': '\u{1F4F6}',
  };
  return map[icon] || '\u{1F4E1}';   // default: satellite antenna
}

/** Auto-scan toggle switch. */
function autoScanToggle(key, onChange) {
  const isOn = getAutoScan(key);
  const sw = el('span', { class: `switch${isOn ? ' on' : ''}`, role: 'switch', 'aria-checked': String(isOn), tabindex: '0' });
  const label = el('span', { style: 'font-size:12px;color:var(--muted);user-select:none' }, [t('quick.autoScan')]);
  const wrap = el('label', { style: 'display:inline-flex;align-items:center;gap:8px;cursor:pointer' }, [label, sw]);

  function toggle() {
    const next = !sw.classList.contains('on');
    sw.classList.toggle('on', next);
    sw.setAttribute('aria-checked', String(next));
    setAutoScan(key, next);
    onChange(next);
  }

  sw.addEventListener('click', toggle);
  sw.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });

  return { wrap, isOn };
}

/* =================================================================
   System Dialog (WiFi password prompt)
   ================================================================= */

function openSysDialog(title, fields, onSubmit) {
  const backdrop = $('#sysDialogBackdrop');
  if (!backdrop) return;
  empty(backdrop);

  const modal = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', style: 'padding:20px;max-width:400px;width:90vw;border-radius:16px;background:var(--grad-quickpanel,#0a1116);border:1px solid var(--c-border-strong)' });

  const heading = el('h3', { style: 'margin:0 0 16px;color:var(--ink)' }, [title]);
  modal.appendChild(heading);

  const form = el('form', { style: 'display:flex;flex-direction:column;gap:12px' });

  const inputs = {};
  for (const f of fields) {
    const input = el('input', {
      class: 'input',
      type: f.type || 'text',
      placeholder: f.placeholder || '',
      autocomplete: f.autocomplete || 'off',
      style: 'width:100%;padding:10px 12px;border-radius:8px;border:1px solid var(--c-border-strong);background:var(--c-panel);color:var(--ink);font-size:14px',
    });
    if (f.value) input.value = f.value;
    if (f.readonly) input.readOnly = true;
    inputs[f.name] = input;

    const label = el('label', { style: 'display:flex;flex-direction:column;gap:4px' }, [
      el('span', { style: 'font-size:12px;color:var(--muted)' }, [f.label]),
      input,
    ]);
    form.appendChild(label);
  }

  const btnRow = el('div', { style: 'display:flex;gap:8px;justify-content:flex-end;margin-top:8px' });
  const cancelBtn = el('button', { class: 'btn', type: 'button' }, [t('common.cancel')]);
  const submitBtn = el('button', { class: 'btn', type: 'submit', style: 'background:var(--acid);color:var(--ink-invert,#001014)' }, [t('common.connect')]);
  btnRow.appendChild(cancelBtn);
  btnRow.appendChild(submitBtn);
  form.appendChild(btnRow);
  modal.appendChild(form);
  backdrop.appendChild(modal);

  backdrop.style.display = 'flex';
  backdrop.classList.add('show');

  function closeDlg() {
    backdrop.style.display = 'none';
    backdrop.classList.remove('show');
    empty(backdrop);
  }

  cancelBtn.addEventListener('click', closeDlg);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) closeDlg(); });

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const values = {};
    for (const [name, inp] of Object.entries(inputs)) values[name] = inp.value;
    closeDlg();
    onSubmit(values);
  });

  const firstInput = Object.values(inputs).find(i => !i.readOnly);
  if (firstInput) requestAnimationFrame(() => firstInput.focus());
}

function closeSysDialog() {
  const backdrop = $('#sysDialogBackdrop');
  if (!backdrop) return;
  backdrop.style.display = 'none';
  backdrop.classList.remove('show');
  empty(backdrop);
}

/* =================================================================
   WiFi — scan, connect
   ================================================================= */

async function scanWifi() {
  if (scanning.wifi) return;
  scanning.wifi = true;
  try {
    const data = await api.get(API.scanWifi);
    renderWifiResults(data);
  } catch (err) {
    toast(t('quick.btScanFailed') + ': ' + (err.message || t('common.unknown')), 3000, 'error');
  } finally {
    scanning.wifi = false;
  }
}

function renderWifiResults(data) {
  if (!wifiList) return;
  empty(wifiList);

  const networks = Array.isArray(data) ? data : (data?.networks || data?.results || []);
  if (!networks.length) {
    wifiList.appendChild(el('div', { style: 'padding:12px;color:var(--muted);text-align:center' }, [t('common.noData')]));
    return;
  }

  const currentSsid = data?.current_ssid || null;
  networks.sort((a, b) => (b.signal ?? 0) - (a.signal ?? 0));

  networks.forEach((net, idx) => {
    const ssid = net.ssid || '(Hidden)';
    const signal = net.signal ?? 0;
    const sec = net.security || 'Open';
    const isConnected = net.in_use || ssid === currentSsid;

    const infoEl = el('div', { class: 'wifi-card-info' }, [
      el('span', { class: 'wifi-card-ssid' + (isConnected ? '' : '') , style: isConnected ? 'color:var(--acid)' : '' }, [ssid]),
      el('div', { class: 'wifi-card-meta' }, [
        sigEl(signal),
        secBadge(sec),
        ...(isConnected ? [el('span', { class: 'wifi-connected-chip' }, ['\u2713 Connected'])] : []),
      ]),
    ]);

    const actionsEl = el('div', { class: 'wifi-card-actions' }, [
      el('button', {
        class: 'btn',
        style: 'font-size:12px;padding:4px 12px;border-radius:99px',
        onclick: () => promptWifiConnect(ssid, sec),
      }, [isConnected ? 'Reconnect' : t('common.connect')]),
    ]);

    const row = el('div', {
      class: `qprow wifi-card${isConnected ? ' connected' : ''}`,
      style: `--i:${idx}`,
    }, [infoEl, actionsEl]);

    wifiList.appendChild(row);
  });
}

function promptWifiConnect(ssid, sec) {
  const s = (sec || '').toUpperCase();
  const isOpen = !sec || s === 'OPEN' || s === 'NONE' || s === '--' || s === '';
  if (isOpen) {
    connectWifi(ssid, '');
    return;
  }

  openSysDialog(t('quick.connectWifi'), [
    { name: 'ssid', label: t('network.title'), value: ssid, readonly: true },
    { name: 'password', label: t('creds.password'), type: 'password', placeholder: t('creds.password'), autocomplete: 'current-password' },
  ], (vals) => {
    connectWifi(vals.ssid, vals.password);
  });
}

async function connectWifi(ssid, password) {
  try {
    toast(t('quick.connectingTo', { ssid }), 2000, 'info');
    await api.post(API.connectWifi, { ssid, password });
    toast(t('quick.connectedTo', { ssid }), 3000, 'success');
    scanWifi();
  } catch (err) {
    toast(t('quick.connectionFailed') + ': ' + (err.message || t('common.unknown')), 3500, 'error');
  }
}

/* =================================================================
   Known networks — load, render, drag-drop, multi-select, upload
   ================================================================= */

async function loadKnownWifi() {
  if (!knownList) return;
  empty(knownList);
  knownList.appendChild(el('div', { style: 'padding:8px;color:var(--muted);text-align:center' }, [t('common.loading')]));

  try {
    const data = await api.get(API.getKnownWifi);
    parseAndRenderKnown(data);
  } catch (err) {
    empty(knownList);
    toast(t('quick.loadKnownFailed') + ': ' + (err.message || t('common.unknown')), 3000, 'error');
  }
}

function parseKnownData(data) {
  let networks = [];
  if (Array.isArray(data)) {
    networks = data;
  } else if (data && typeof data === 'object') {
    networks = data.networks || data.known || data.data || data.results || [];
    if (!networks.length) {
      const keys = Object.keys(data);
      if (keys.length === 1 && Array.isArray(data[keys[0]])) {
        networks = data[keys[0]];
      }
    }
  }
  return networks.map((n, i) => ({
    ssid: n.ssid || n.SSID || '(Unknown)',
    priority: n.priority ?? i,
  }));
}

function parseAndRenderKnown(data) {
  knownNetworks = parseKnownData(data);
  selectedSsids.clear();
  renderKnownNetworks();
}

function renderKnownNetworks() {
  if (!knownList) return;
  empty(knownList);
  removeBatchBar();

  if (!knownNetworks.length) {
    knownList.appendChild(el('div', { style: 'padding:12px;color:var(--muted);text-align:center' }, [t('common.noData')]));
    return;
  }

  knownNetworks.forEach((net, idx) => {
    const ssid = net.ssid;
    const priority = net.priority;

    // Checkbox (visible in edit mode)
    const cb = el('input', {
      type: 'checkbox',
      class: 'known-select-cb',
      ...(selectedSsids.has(ssid) ? { checked: '' } : {}),
    });
    cb.addEventListener('change', () => {
      if (cb.checked) selectedSsids.add(ssid);
      else selectedSsids.delete(ssid);
      updateBatchBar();
    });

    // Drag grip
    const grip = el('span', { class: 'known-card-grip', title: 'Drag to reorder' }, ['\u2807']);

    const info = el('div', { class: 'known-card-info' }, [
      el('span', { class: 'known-card-ssid' }, [ssid]),
      el('span', { class: 'known-card-priority' }, [`Priority: ${priority}`]),
    ]);

    const connectBtn = el('button', {
      class: 'qp-icon-btn',
      title: t('common.connect'),
      onclick: () => connectKnownWifi(ssid),
    }, ['\u21C8']);

    const deleteBtn = el('button', {
      class: 'qp-icon-btn danger',
      title: t('common.delete'),
      onclick: () => deleteKnown(ssid),
    }, ['\u2715']);

    const actions = el('div', { class: 'known-card-actions' }, [connectBtn, deleteBtn]);

    const row = el('div', {
      class: `qprow known-card${editMode ? ' edit-mode' : ''}`,
      style: `--i:${idx}`,
      draggable: editMode ? 'false' : 'true',
      'data-idx': String(idx),
    }, [
      editMode ? cb : grip,
      info,
      actions,
    ]);

    // HTML5 Drag events (desktop)
    if (!editMode) {
      row.addEventListener('dragstart', (e) => onDragStart(e, idx));
      row.addEventListener('dragover', (e) => onDragOver(e, idx));
      row.addEventListener('dragend', onDragEnd);
      row.addEventListener('drop', (e) => onDrop(e, idx));

      // Touch drag on grip
      const gripEl = row.querySelector('.known-card-grip');
      if (gripEl) {
        gripEl.addEventListener('touchstart', (e) => onTouchDragStart(e, idx, row), { passive: false });
      }
    }

    knownList.appendChild(row);
  });

  if (editMode && selectedSsids.size > 0) {
    updateBatchBar();
  }
}

/* ---- Drag & Drop (Desktop) ---- */

function onDragStart(e, idx) {
  dragSrcIdx = idx;
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', String(idx));
  requestAnimationFrame(() => e.target.classList.add('dragging'));
}

function onDragOver(e, idx) {
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';

  // Remove old placeholder
  if (dragPlaceholder && dragPlaceholder.parentNode) {
    dragPlaceholder.parentNode.removeChild(dragPlaceholder);
  }

  const row = e.currentTarget;
  const rect = row.getBoundingClientRect();
  const midY = rect.top + rect.height / 2;

  dragPlaceholder = el('div', { class: 'drag-placeholder' });

  if (e.clientY < midY) {
    row.parentNode.insertBefore(dragPlaceholder, row);
  } else {
    row.parentNode.insertBefore(dragPlaceholder, row.nextSibling);
  }
}

function onDrop(e, targetIdx) {
  e.preventDefault();
  if (dragSrcIdx === null || dragSrcIdx === targetIdx) return;

  // Calculate insertion index based on placeholder position
  const row = e.currentTarget;
  const rect = row.getBoundingClientRect();
  const insertBefore = e.clientY < rect.top + rect.height / 2;
  let newIdx = insertBefore ? targetIdx : targetIdx + 1;
  if (dragSrcIdx < newIdx) newIdx--;

  // Reorder array
  const [moved] = knownNetworks.splice(dragSrcIdx, 1);
  knownNetworks.splice(newIdx, 0, moved);

  onDragEnd();
  commitPriorityOrder();
}

function onDragEnd() {
  dragSrcIdx = null;
  if (dragPlaceholder && dragPlaceholder.parentNode) {
    dragPlaceholder.parentNode.removeChild(dragPlaceholder);
  }
  dragPlaceholder = null;
  // Remove dragging class
  if (knownList) {
    knownList.querySelectorAll('.dragging').forEach(r => r.classList.remove('dragging'));
  }
  renderKnownNetworks();
}

/* ---- Touch Drag (Mobile) ---- */

function onTouchDragStart(e, idx, row) {
  if (e.touches.length !== 1) return;
  e.preventDefault();

  dragSrcIdx = idx;
  touchStartY = e.touches[0].clientY;

  // Create a floating clone
  const rect = row.getBoundingClientRect();
  touchDragEl = row.cloneNode(true);
  touchDragEl.style.cssText = `
    position:fixed;left:${rect.left}px;top:${rect.top}px;
    width:${rect.width}px;opacity:.8;z-index:999;
    pointer-events:none;transform:scale(1.02);
    box-shadow:0 8px 24px rgba(0,0,0,.4);
    border-radius:12px;
  `;
  document.body.appendChild(touchDragEl);
  row.classList.add('dragging');

  const onMove = (ev) => {
    if (!touchDragEl) return;
    const dy = ev.touches[0].clientY - touchStartY;
    touchDragEl.style.transform = `scale(1.02) translateY(${dy}px)`;

    // Determine target
    const touchY = ev.touches[0].clientY;
    const rows = [...knownList.querySelectorAll('.known-card:not(.dragging)')];
    if (dragPlaceholder && dragPlaceholder.parentNode) {
      dragPlaceholder.parentNode.removeChild(dragPlaceholder);
    }
    dragPlaceholder = el('div', { class: 'drag-placeholder' });

    for (const r of rows) {
      const rr = r.getBoundingClientRect();
      if (touchY < rr.top + rr.height / 2) {
        r.parentNode.insertBefore(dragPlaceholder, r);
        return;
      }
    }
    // After all rows
    if (rows.length) {
      knownList.appendChild(dragPlaceholder);
    }
  };

  const onEnd = () => {
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onEnd);

    if (touchDragEl && touchDragEl.parentNode) {
      touchDragEl.parentNode.removeChild(touchDragEl);
    }
    touchDragEl = null;

    // Calculate new position from placeholder
    if (dragPlaceholder && dragPlaceholder.parentNode && dragSrcIdx !== null) {
      const allChildren = [...knownList.children];
      let newIdx = allChildren.indexOf(dragPlaceholder);
      // Adjust: items before placeholder that aren't the placeholder itself
      if (newIdx > dragSrcIdx) newIdx--;
      if (newIdx < 0) newIdx = 0;
      if (newIdx !== dragSrcIdx) {
        const [moved] = knownNetworks.splice(dragSrcIdx, 1);
        knownNetworks.splice(newIdx, 0, moved);
        commitPriorityOrder();
      }
    }

    dragSrcIdx = null;
    if (dragPlaceholder && dragPlaceholder.parentNode) {
      dragPlaceholder.parentNode.removeChild(dragPlaceholder);
    }
    dragPlaceholder = null;
    renderKnownNetworks();
  };

  document.addEventListener('touchmove', onMove, { passive: false });
  document.addEventListener('touchend', onEnd);
}

/* ---- Priority commit ---- */

async function commitPriorityOrder() {
  // Assign descending priorities: first item gets N, last gets 1
  const total = knownNetworks.length;
  for (let i = 0; i < total; i++) {
    knownNetworks[i].priority = total - i;
  }
  renderKnownNetworks();

  // Send updates to backend
  for (const net of knownNetworks) {
    try {
      await api.post(API.updatePriority, { ssid: net.ssid, priority: net.priority });
    } catch (err) {
      toast(`Failed to update priority for ${net.ssid}`, 2000, 'error');
    }
  }
  toast(t('quick.priorityUpdated') || 'Priorities updated', 2000, 'success');
}

/* ---- Multi-select ---- */

function toggleEditMode() {
  editMode = !editMode;
  if (!editMode) {
    selectedSsids.clear();
    removeBatchBar();
  }
  renderKnownNetworks();
}

function selectAll() {
  if (selectedSsids.size === knownNetworks.length) {
    selectedSsids.clear();
  } else {
    for (const net of knownNetworks) selectedSsids.add(net.ssid);
  }
  renderKnownNetworks();
  updateBatchBar();
}

function updateBatchBar() {
  removeBatchBar();
  if (!editMode || selectedSsids.size === 0) return;

  batchBar = el('div', { class: 'qp-batch-bar' }, [
    el('span', { style: 'font-size:13px;color:var(--ink)' }, [`${selectedSsids.size} selected`]),
    el('button', {
      class: 'btn',
      style: 'font-size:12px;padding:4px 14px;color:var(--danger,#ff3b3b);border-color:var(--danger,#ff3b3b)',
      onclick: batchDelete,
    }, [`Delete ${selectedSsids.size}`]),
  ]);

  if (knownWrapper) knownWrapper.appendChild(batchBar);
}

function removeBatchBar() {
  if (batchBar && batchBar.parentNode) {
    batchBar.parentNode.removeChild(batchBar);
  }
  batchBar = null;
}

async function batchDelete() {
  const ssids = [...selectedSsids];
  if (!ssids.length) return;

  toast(`Deleting ${ssids.length} network(s)...`, 2000, 'info');

  let ok = 0, fail = 0;
  for (const ssid of ssids) {
    try {
      await api.post(API.deleteKnown, { ssid });
      ok++;
    } catch {
      fail++;
    }
  }

  selectedSsids.clear();
  editMode = false;
  toast(`Deleted ${ok}${fail ? `, ${fail} failed` : ''}`, 2500, ok ? 'success' : 'error');
  loadKnownWifi();
}

/* ---- Known network actions ---- */

async function connectKnownWifi(ssid) {
  try {
    toast(t('quick.connectingTo', { ssid }), 2000, 'info');
    await api.post(API.connectKnown, { ssid });
    toast(t('quick.connectedTo', { ssid }), 3000, 'success');
  } catch (err) {
    toast(t('quick.connectionFailed') + ': ' + (err.message || t('common.unknown')), 3500, 'error');
  }
}

async function deleteKnown(ssid) {
  openSysDialog(t('common.delete'), [
    { name: 'ssid', label: t('quick.forgetNetworkPrompt') || 'Forget this network?', value: ssid, readonly: true },
  ], async (vals) => {
    try {
      await api.post(API.deleteKnown, { ssid: vals.ssid });
      toast('Network removed', 2000, 'success');
      loadKnownWifi();
    } catch (err) {
      toast('Delete failed: ' + (err.message || 'Unknown error'), 3000, 'error');
    }
  });
}

/* ---- Potfile import & upload ---- */

async function importPotfiles() {
  try {
    toast(t('quick.importingPotfiles') || 'Importing potfiles...', 2000, 'info');
    const res = await api.post(API.importPotfiles);
    const count = res?.imported ?? res?.networks_added?.length ?? '?';
    const skipped = res?.skipped ?? 0;
    const failed = res?.failed ?? 0;
    let msg = t('quick.importedCount', { count }) || `Imported ${count} network(s)`;
    if (skipped) msg += `, ${skipped} skipped`;
    if (failed) msg += `, ${failed} failed`;
    toast(msg, 3000, 'success');
    loadKnownWifi();
  } catch (err) {
    toast(t('studio.importFailed') + ': ' + (err.message || t('common.unknown')), 3000, 'error');
  }
}

function uploadPotfile() {
  const input = el('input', { type: 'file', accept: '.pot,.potfile', style: 'display:none' });
  input.addEventListener('change', async () => {
    const file = input.files?.[0];
    if (!file) return;

    const fd = new FormData();
    fd.append('potfile', file);

    try {
      toast(`Uploading ${file.name}...`, 2000, 'info');
      const resp = await fetch(API.uploadPotfile, { method: 'POST', body: fd });
      const data = await resp.json();
      if (data.status === 'success') {
        toast(`Uploaded ${file.name}`, 2000, 'success');
        // Auto-import after upload
        await importPotfiles();
      } else {
        toast(`Upload failed: ${data.message || 'Unknown'}`, 3000, 'error');
      }
    } catch (err) {
      toast('Upload failed: ' + (err.message || 'Unknown error'), 3000, 'error');
    }
    input.remove();
  });

  document.body.appendChild(input);
  input.click();
}

/* =================================================================
   Bluetooth — scan, pair, trust, connect, disconnect, forget
   ================================================================= */

async function scanBluetooth() {
  if (scanning.bt) return;
  scanning.bt = true;
  try {
    const data = await api.get(API.scanBluetooth);
    renderBtResults(data);
  } catch (err) {
    toast(t('quick.btScanFailed') + ': ' + (err.message || t('common.unknown')), 3000, 'error');
  } finally {
    scanning.bt = false;
  }
}

function renderBtResults(data) {
  if (!btList) return;
  empty(btList);

  const devices = Array.isArray(data) ? data : (data?.devices || data?.results || []);
  if (!devices.length) {
    btList.appendChild(el('div', { style: 'padding:12px;color:var(--muted);text-align:center' }, [t('common.noData')]));
    return;
  }

  // Sort: connected first, then paired, then by name
  devices.sort((a, b) => {
    if (a.connected !== b.connected) return a.connected ? -1 : 1;
    if (a.paired !== b.paired) return a.paired ? -1 : 1;
    return (a.name || '').localeCompare(b.name || '');
  });

  devices.forEach((dev, idx) => {
    const name = dev.name || dev.Name || '(Unknown)';
    const mac = dev.mac || dev.address || dev.MAC || '';
    const iconHint = dev.icon || dev.Icon || '';
    const paired = !!(dev.paired || dev.Paired);
    const trusted = !!(dev.trusted || dev.Trusted);
    const connected = !!(dev.connected || dev.Connected);
    const rssi = dev.rssi ?? dev.RSSI ?? null;

    // Icon
    const iconEl = el('span', { class: 'bt-icon' }, [btIcon(iconHint)]);

    // Device info
    const stateChips = [];
    if (paired) stateChips.push(el('span', { class: 'bt-chip bt-chip-paired' }, ['Paired']));
    if (trusted) stateChips.push(el('span', { class: 'bt-chip bt-chip-trusted' }, ['Trusted']));
    if (connected) stateChips.push(el('span', { class: 'bt-chip bt-chip-connected' }, ['\u2713 Connected']));

    const infoChildren = [
      el('span', { class: 'bt-device-name' }, [name]),
      el('span', { class: 'bt-device-mac' }, [mac]),
    ];
    if (stateChips.length) {
      infoChildren.push(el('div', { class: 'bt-state-chips' }, stateChips));
    }

    const deviceEl = el('div', { class: 'bt-device' }, [
      iconEl,
      el('div', { class: 'bt-device-info' }, infoChildren),
      ...(rssi !== null && rssi > -999 ? [rssiSigEl(rssi)] : []),
    ]);

    // Actions (contextual)
    const actions = [];

    if (!paired) {
      actions.push(el('button', {
        class: 'btn',
        style: 'font-size:12px;padding:4px 12px;border-radius:99px;background:var(--acid);color:var(--ink-invert,#001014)',
        onclick: () => btAction('pair', mac, name),
      }, [t('quick.pair') || 'Pair']));
    } else {
      if (!trusted) {
        actions.push(el('button', {
          class: 'btn',
          style: 'font-size:12px;padding:4px 10px;border-radius:99px',
          onclick: () => btAction('trust', mac, name),
        }, [t('quick.trust') || 'Trust']));
      }
      if (connected) {
        actions.push(el('button', {
          class: 'btn',
          style: 'font-size:12px;padding:4px 10px;border-radius:99px',
          onclick: () => btAction('disconnect', mac, name),
        }, [t('common.disconnect') || 'Disconnect']));
      } else {
        actions.push(el('button', {
          class: 'btn',
          style: 'font-size:12px;padding:4px 10px;border-radius:99px',
          onclick: () => btAction('connect', mac, name),
        }, [t('common.connect') || 'Connect']));
      }
      actions.push(el('button', {
        class: 'qp-icon-btn danger',
        title: t('common.remove') || 'Forget',
        onclick: () => btForget(mac, name),
      }, ['\u2715']));
    }

    const row = el('div', {
      class: `qprow${connected ? ' connected' : ''}`,
      style: `grid-template-columns:1fr auto;align-items:center;--i:${idx}`,
    }, [
      deviceEl,
      el('div', { class: 'bt-actions' }, actions),
    ]);

    btList.appendChild(row);
  });
}

async function btAction(action, mac, name) {
  const endpoints = {
    pair: API.pairBluetooth,
    trust: API.trustBluetooth,
    connect: API.connectBluetooth,
    disconnect: API.disconnectBluetooth,
  };

  const url = endpoints[action];
  if (!url) return;

  try {
    toast(t('quick.btActioning', { action, name }) || `${action}ing ${name}...`, 2000, 'info');
    await api.post(url, { address: mac, mac });
    toast(t('quick.btActionDone', { action, name }) || `${action} successful for ${name}`, 3000, 'success');
    scanBluetooth();
  } catch (err) {
    toast(t('quick.btActionFailed', { action }) + ': ' + (err.message || t('common.unknown')), 3500, 'error');
  }
}

function btForget(mac, name) {
  openSysDialog(t('quick.forgetDevice') || 'Forget Device', [
    { name: 'mac', label: t('quick.forgetDevicePrompt', { name }) || `Remove ${name}?`, value: mac, readonly: true },
  ], async (vals) => {
    try {
      await api.post(API.forgetBluetooth, { address: vals.mac, mac: vals.mac });
      toast(t('quick.btForgotten', { name }) || `${name} forgotten`, 2000, 'success');
      scanBluetooth();
    } catch (err) {
      toast(t('common.deleteFailed') + ': ' + (err.message || t('common.unknown')), 3000, 'error');
    }
  });
}

/* =================================================================
   Auto-scan timers
   ================================================================= */

function startWifiAutoScan() {
  stopWifiAutoScan();
  wifiAutoTimer = setInterval(() => {
    if (panel && panel.classList.contains('open') && activeTab === 'wifi') scanWifi();
  }, AUTOSCAN_INTERVAL);
  scanWifi();
}

function stopWifiAutoScan() {
  if (wifiAutoTimer) { clearInterval(wifiAutoTimer); wifiAutoTimer = null; }
}

function startBtAutoScan() {
  stopBtAutoScan();
  btAutoTimer = setInterval(() => {
    if (panel && panel.classList.contains('open') && activeTab === 'bt') scanBluetooth();
  }, AUTOSCAN_INTERVAL);
  scanBluetooth();
}

function stopBtAutoScan() {
  if (btAutoTimer) { clearInterval(btAutoTimer); btAutoTimer = null; }
}

/* =================================================================
   Tab switching
   ================================================================= */

function switchTab(tab) {
  const prevTab = activeTab;
  activeTab = tab;

  if (tabBtns) {
    tabBtns.forEach(btn => btn.classList.toggle('active', btn.dataset.tab === tab));
  }

  if (wifiTab) wifiTab.style.display = (tab === 'wifi') ? '' : 'none';
  if (btTab) btTab.style.display = (tab === 'bt') ? '' : 'none';

  // Stop the inactive tab's timer to save resources
  if (prevTab === 'wifi' && tab !== 'wifi') stopWifiAutoScan();
  if (prevTab === 'bt' && tab !== 'bt') stopBtAutoScan();

  // Start the active tab's timer if enabled
  if (tab === 'wifi' && getAutoScan(LS_WIFI_AUTO) && panel?.classList.contains('open')) {
    startWifiAutoScan();
  }
  if (tab === 'bt' && getAutoScan(LS_BT_AUTO) && panel?.classList.contains('open')) {
    startBtAutoScan();
  }
}

/* =================================================================
   Panel open / close / toggle
   ================================================================= */

export function open() {
  if (!panel) return;
  panel.classList.add('open');
  panel.setAttribute('aria-hidden', 'false');

  loadKnownWifi();

  // Start auto-scan only for the active tab
  if (activeTab === 'wifi' && getAutoScan(LS_WIFI_AUTO)) startWifiAutoScan();
  if (activeTab === 'bt' && getAutoScan(LS_BT_AUTO)) startBtAutoScan();
}

export function close() {
  if (!panel) return;
  panel.classList.remove('open');
  panel.setAttribute('aria-hidden', 'true');

  stopWifiAutoScan();
  stopBtAutoScan();
  closeSysDialog();

  // Exit edit mode on close
  if (editMode) {
    editMode = false;
    selectedSsids.clear();
    removeBatchBar();
  }
}

export function toggle() {
  if (!panel) return;
  if (panel.classList.contains('open')) close();
  else open();
}

/* =================================================================
   Build panel content (init)
   ================================================================= */

export function init() {
  panel = $('#quickpanel');
  if (!panel) {
    console.warn('[QuickPanel] #quickpanel not found in DOM');
    return;
  }

  /* ---- Header ---- */
  const closeBtn = el('button', { class: 'qp-close', 'aria-label': t('quick.close'), onclick: close }, ['\u2715']);
  const header = el('div', { class: 'qp-header', style: 'padding:20px 16px 8px' }, [
    el('div', { class: 'qp-head-left' }, [
      el('strong', { style: 'font-size:16px' }, [t('nav.shortcuts')]),
      el('span', { style: 'font-size:11px;color:var(--muted)' }, [t('quick.subtitle')]),
    ]),
    closeBtn,
  ]);

  /* ---- Tab bar ---- */
  const wifiTabBtn = el('div', { class: 'qp-tab active', 'data-tab': 'wifi', onclick: () => switchTab('wifi') }, [
    el('span', { class: 'qp-tab-icon' }, ['\u{1F4F6}']),
    t('dash.wifi'),
  ]);
  const btTabBtn = el('div', { class: 'qp-tab', 'data-tab': 'bt', onclick: () => switchTab('bt') }, [
    el('span', { class: 'qp-tab-icon' }, ['\u{1F4E1}']),
    t('dash.bluetooth'),
  ]);
  tabBtns = [wifiTabBtn, btTabBtn];

  const tabBar = el('div', { class: 'qp-tabs' }, [wifiTabBtn, btTabBtn]);

  /* ---- WiFi tab content ---- */
  wifiList = el('div', { class: 'wifilist', style: 'max-height:40vh;overflow-y:auto;padding:0 16px' });
  knownList = el('div', { class: 'knownlist', style: 'max-height:30vh;overflow-y:auto;padding:0 16px' });

  const wifiScanBtn = el('button', { class: 'btn', style: 'font-size:13px', onclick: scanWifi }, [t('common.refresh')]);
  const potfileBtn = el('button', { class: 'btn', style: 'font-size:13px', onclick: importPotfiles }, [t('quick.importPotfiles') || 'Import Potfiles']);
  const uploadBtn = el('button', { class: 'btn', style: 'font-size:13px', onclick: uploadPotfile }, ['\u2191 Upload']);

  const wifiAutoCtrl = autoScanToggle(LS_WIFI_AUTO, (on) => {
    if (on && panel.classList.contains('open') && activeTab === 'wifi') startWifiAutoScan();
    else stopWifiAutoScan();
  });

  const wifiToolbar = el('div', { class: 'qp-toolbar' }, [
    wifiScanBtn, potfileBtn, uploadBtn,
    el('span', { class: 'qp-toolbar-spacer' }),
    wifiAutoCtrl.wrap,
  ]);

  // Known networks section header with edit toggle
  const editBtn = el('button', {
    class: 'qp-icon-btn',
    title: 'Select multiple',
    onclick: toggleEditMode,
  }, ['\u2611']);

  const selectAllBtn = el('button', {
    class: 'qp-icon-btn',
    title: 'Select all',
    onclick: selectAll,
    style: 'display:none',
  }, ['\u2610']);

  const knownHeader = el('div', { class: 'qp-section-header' }, [
    el('span', {}, [t('quick.knownNetworks') || 'Saved Networks']),
    el('div', { class: 'qp-section-actions' }, [selectAllBtn, editBtn]),
  ]);

  // Show select all button only in edit mode
  const origToggle = toggleEditMode;

  knownWrapper = el('div', { style: 'position:relative' }, [knownList]);

  wifiTab = el('div', { 'data-panel': 'wifi' }, [wifiToolbar, wifiList, knownHeader, knownWrapper]);

  /* ---- Bluetooth tab content ---- */
  btList = el('div', { class: 'btlist', style: 'max-height:50vh;overflow-y:auto;padding:0 16px' });

  const btScanBtn = el('button', { class: 'btn', style: 'font-size:13px', onclick: scanBluetooth }, [t('common.refresh')]);

  const btAutoCtrl = autoScanToggle(LS_BT_AUTO, (on) => {
    if (on && panel.classList.contains('open') && activeTab === 'bt') startBtAutoScan();
    else stopBtAutoScan();
  });

  const btToolbar = el('div', { class: 'qp-toolbar' }, [
    btScanBtn,
    el('span', { class: 'qp-toolbar-spacer' }),
    btAutoCtrl.wrap,
  ]);

  btTab = el('div', { 'data-panel': 'bt', style: 'display:none' }, [btToolbar, btList]);

  /* ---- Assemble into panel (after the grip) ---- */
  panel.appendChild(header);
  panel.appendChild(tabBar);
  panel.appendChild(wifiTab);
  panel.appendChild(btTab);

  /* ---- Global keyboard shortcuts ---- */
  document.addEventListener('keydown', onKeyDown);

  /* ---- Click outside to close ---- */
  document.addEventListener('pointerdown', onOutsideClick);

  /* ---- Wire topbar trigger button ---- */
  const openBtn = $('#openQuick');
  if (openBtn) openBtn.addEventListener('click', toggle);

  /* ---- Page visibility: pause scans when tab hidden ---- */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      stopWifiAutoScan();
      stopBtAutoScan();
    } else if (panel && panel.classList.contains('open')) {
      if (activeTab === 'wifi' && getAutoScan(LS_WIFI_AUTO)) startWifiAutoScan();
      if (activeTab === 'bt' && getAutoScan(LS_BT_AUTO)) startBtAutoScan();
    }
  });
}

/* =================================================================
   Event handlers
   ================================================================= */

function onKeyDown(e) {
  if (e.ctrlKey && e.key === '\\') {
    e.preventDefault();
    toggle();
    return;
  }
  if (e.key === 'Escape' && panel && panel.classList.contains('open')) {
    const dlg = $('#sysDialogBackdrop');
    if (dlg && (dlg.style.display === 'flex' || dlg.classList.contains('show'))) {
      closeSysDialog();
      return;
    }
    close();
  }
}

function onOutsideClick(e) {
  if (!panel || !panel.classList.contains('open')) return;
  if (panel.contains(e.target)) return;
  const openBtn = $('#openQuick');
  if (openBtn && openBtn.contains(e.target)) return;
  const dlg = $('#sysDialogBackdrop');
  if (dlg && dlg.contains(e.target)) return;
  close();
}
