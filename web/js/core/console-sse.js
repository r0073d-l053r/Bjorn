/**
 * Console SSE — streaming log viewer with SSE, scroll management,
 * font sizing, resize dragging, and floating UI indicators.
 *
 * Replaces the legacy BjornUI.ConsoleSSE IIFE from global.js.
 *
 * @module core/console-sse
 */

import { $, el, toast } from './dom.js';
import { api } from './api.js';
import { t } from './i18n.js';

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */

const MAX_VISIBLE_LINES = 200;
const MAX_RECONNECT = 5;
const RECONNECT_DELAY_MS = 2000;
const LS_FONT_KEY = 'Console.fontPx';
const LS_DOCK_KEY = 'Console.docked';
const DEFAULT_FONT_PX = 12;
const MOBILE_FONT_PX = 11;
const MOBILE_BREAKPOINT = 768;

/** Map canonical log-level tokens to CSS class names. */
const LEVEL_CLASSES = {
  DEBUG: 'debug',
  INFO: 'info',
  WARNING: 'warning',
  ERROR: 'error',
  CRITICAL: 'critical',
  SUCCESS: 'success',
};

/* ------------------------------------------------------------------ */
/*  Module state                                                      */
/* ------------------------------------------------------------------ */

let evtSource = null;
let reconnectCount = 0;
let reconnectTimer = null;
let healthyMessageCount = 0;
const HEALTHY_THRESHOLD = 5;  // messages needed before resetting reconnect counter

let isUserScrolling = false;
let autoScroll = true;
let lineBuffer = [];     // lines held while user is scrolled up
let isDocked = false;

/* Cached DOM refs (populated in init) */
let elConsole = null;
let elLogout = null;
let elFontInput = null;
let elModePill = null;
let elModeToggle = null;
let elAttackToggle = null;
let elDockBtn = null;
let elSelIp = null;
let elSelPort = null;
let elSelAction = null;
let elBtnScan = null;
let elBtnAttack = null;
let elScrollBtn = null;   // floating scroll-to-bottom button
let elBufferBadge = null;   // floating buffer count indicator

/* Resize drag state */
let resizeDragging = false;
let resizeStartY = 0;
let resizeStartH = 0;

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

/**
 * Deterministic hue from a string (0-359).
 * @param {string} str
 * @returns {number}
 */
function hueFromString(str) {
  let h = 0;
  for (let i = 0; i < str.length; i++) {
    h = (h * 31 + str.charCodeAt(i)) >>> 0;
  }
  return h % 360;
}

/**
 * Return the default font size based on viewport width.
 * @returns {number}
 */
function defaultFontPx() {
  return window.innerWidth <= MOBILE_BREAKPOINT ? MOBILE_FONT_PX : DEFAULT_FONT_PX;
}

/**
 * Update the range input's background gradient so the filled portion
 * matches the current thumb position.
 * @param {HTMLInputElement} input
 */
function paintRangeTrack(input) {
  if (!input) return;
  const min = Number(input.min) || 0;
  const max = Number(input.max) || 100;
  const val = Number(input.value);
  const pct = ((val - min) / (max - min)) * 100;
  input.style.backgroundSize = `${pct}% 100%`;
}

/* ------------------------------------------------------------------ */
/*  Dock / Anchor                                                     */
/* ------------------------------------------------------------------ */

function readDockPref() {
  try {
    return localStorage.getItem(LS_DOCK_KEY) === '1';
  } catch {
    return false;
  }
}

function writeDockPref(on) {
  try {
    localStorage.setItem(LS_DOCK_KEY, on ? '1' : '0');
  } catch { /* ignore */ }
}

function syncDockSpace() {
  if (!elConsole) return;

  const open = elConsole.classList.contains('open');
  const active = !!isDocked && !!open;

  document.body.classList.toggle('console-docked', active);
  elConsole.classList.toggle('docked', active);

  if (elDockBtn) {
    elDockBtn.classList.toggle('on', !!isDocked);
    elDockBtn.setAttribute('aria-pressed', String(!!isDocked));
    elDockBtn.title = isDocked ? 'Unanchor console' : 'Anchor console';
  }

  const root = document.documentElement;
  if (!active) {
    root.style.setProperty('--console-dock-h', '0px');
    return;
  }

  // Reserve space equal to console height so the app container doesn't sit under it.
  const h = Math.max(0, Math.round(elConsole.getBoundingClientRect().height));
  root.style.setProperty('--console-dock-h', `${h}px`);
}

function ensureDockButton() {
  if (!elConsole || elDockBtn) return;

  const head = elConsole.querySelector('.console-head');
  const closeBtn = $('#closeConsole');
  if (!head || !closeBtn) return;

  elDockBtn = el('button', {
    class: 'btn console-dock-btn',
    id: 'consoleDock',
    type: 'button',
    title: 'Anchor console',
    'aria-label': 'Anchor console',
    'aria-pressed': 'false',
    onclick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      isDocked = !isDocked;
      writeDockPref(isDocked);
      syncDockSpace();
    },
  }, ['PIN']);

  head.insertBefore(elDockBtn, closeBtn);
}

/* ------------------------------------------------------------------ */
/*  Log-line processing                                               */
/* ------------------------------------------------------------------ */

/**
 * Transform a raw log line into an HTML string with highlighted
 * filenames, log levels, and numbers.
 *
 * NOTE: The log content originates from the server's own log stream;
 *       it is NOT user-supplied input, so innerHTML is acceptable here.
 *
 * @param {string} line
 * @returns {string} HTML string
 */
function processLogLine(line) {
  // 1. Highlight *.py filenames
  line = line.replace(
    /\b([\w\-]+\.py)\b/g,
    (_match, name) => {
      const hue = hueFromString(name);
      return `<span class="logfile" style="--h:${hue}">${name}</span>`;
    }
  );

  // 2. Highlight canonical log levels
  const levelPattern = /\b(DEBUG|INFO|WARNING|ERROR|CRITICAL|SUCCESS)\b/g;
  line = line.replace(levelPattern, (_match, lvl) => {
    const cls = LEVEL_CLASSES[lvl] || lvl.toLowerCase();
    return `<span class="loglvl ${cls}">${lvl}</span>`;
  });

  // 3. Highlight special-case tokens
  line = line.replace(
    /\b(failed)\b/gi,
    (_m, tok) => `<span class="loglvl failed">${tok}</span>`
  );
  line = line.replace(
    /\b(Connected)\b/g,
    (_m, tok) => `<span class="loglvl connected">${tok}</span>`
  );
  line = line.replace(
    /(SSE stream closed)/g,
    (_m, tok) => `<span class="loglvl sseclosed">${tok}</span>`
  );

  // 4. Highlight numbers that are NOT inside HTML tags
  //    Strategy: split on HTML tags, process only the text segments.
  line = line.replace(
    /(<[^>]*>)|(\b\d+(?:\.\d+)?\b)/g,
    (match, tag, num) => {
      if (tag) return tag;                       // pass tags through
      return `<span class="number">${num}</span>`;
    }
  );

  return line;
}

/* ------------------------------------------------------------------ */
/*  Scroll management                                                 */
/* ------------------------------------------------------------------ */

function scrollToBottom() {
  if (!elLogout) return;
  elLogout.scrollTop = elLogout.scrollHeight;
}

/**
 * Determine whether the console body is scrolled to (or near) the bottom.
 * @returns {boolean}
 */
function isAtBottom() {
  if (!elLogout) return true;
  return elLogout.scrollTop + elLogout.clientHeight >= elLogout.scrollHeight - 8;
}

/** Flush any buffered lines into the visible log. */
function flushBuffer() {
  if (!elLogout || lineBuffer.length === 0) return;

  for (const html of lineBuffer) {
    appendLogHtml(html, false);
  }
  lineBuffer = [];
  updateBufferBadge();
  trimLines();
  scrollToBottom();
}

/** Trim oldest lines if the visible count exceeds the maximum. */
function trimLines() {
  if (!elLogout) return;
  while (elLogout.childElementCount > MAX_VISIBLE_LINES) {
    elLogout.removeChild(elLogout.firstElementChild);
  }
}

/**
 * Append one processed HTML line into the console body.
 * @param {string} html
 * @param {boolean} shouldAutoScroll
 */
function appendLogHtml(html, shouldAutoScroll = true) {
  if (!elLogout) return;

  const div = document.createElement('div');
  div.className = 'log-line';
  div.innerHTML = html;
  elLogout.appendChild(div);

  if (shouldAutoScroll) {
    trimLines();
    if (autoScroll) scrollToBottom();
  }
}

/** Handle scroll events on the console body. */
function onLogScroll() {
  const atBottom = isAtBottom();

  if (!atBottom) {
    isUserScrolling = true;
    autoScroll = false;
  } else {
    isUserScrolling = false;
    autoScroll = true;
    flushBuffer();
  }

  updateFloatingUI();
}

/* ------------------------------------------------------------------ */
/*  Floating UI (scroll-to-bottom button & buffer badge)              */
/* ------------------------------------------------------------------ */

function ensureFloatingUI() {
  if (elScrollBtn) return;

  // Scroll-to-bottom button
  elScrollBtn = el('button', {
    class: 'console-scroll-btn hidden',
    title: t('console.scrollToBottom'),
    onclick: () => forceBottom(),
  }, ['\u2193']);

  // Buffer badge
  elBufferBadge = el('span', { class: 'console-buffer-badge hidden' }, ['0']);

  if (elConsole) {
    elConsole.appendChild(elScrollBtn);
    elConsole.appendChild(elBufferBadge);
  }
}

function updateFloatingUI() {
  if (!elScrollBtn || !elBufferBadge) return;

  if (!autoScroll && !isAtBottom()) {
    elScrollBtn.classList.remove('hidden');
  } else {
    elScrollBtn.classList.add('hidden');
  }

  updateBufferBadge();
}

function updateBufferBadge() {
  if (!elBufferBadge) return;
  if (lineBuffer.length > 0) {
    elBufferBadge.textContent = String(lineBuffer.length);
    elBufferBadge.classList.remove('hidden');
  } else {
    elBufferBadge.classList.add('hidden');
  }
}

/* ------------------------------------------------------------------ */
/*  SSE connection                                                    */
/* ------------------------------------------------------------------ */

function connectSSE() {
  if (evtSource) return;

  evtSource = new EventSource('/stream_logs');

  evtSource.onmessage = (evt) => {
    // Only reset reconnect counter after sustained healthy connection
    healthyMessageCount++;
    if (healthyMessageCount >= HEALTHY_THRESHOLD) {
      reconnectCount = 0;
    }

    const raw = evt.data;
    if (!raw) return;

    // Detect Mode Change Logs (Server -> Client Push)
    // Log format: "... - Operation mode switched to: AI"
    if (raw.includes('Operation mode switched to:')) {
      const parts = raw.split('Operation mode switched to:');
      if (parts.length > 1) {
        const newMode = parts[1].trim().split(' ')[0]; // Take first word just in case
        setModeUI(newMode);
      }
    }

    // --- NEW: AI Dashboard Real-time Events ---
    if (raw.includes('[AI_EXEC]')) {
      try {
        const json = raw.split('[AI_EXEC]')[1].trim();
        const data = JSON.parse(json);
        window.dispatchEvent(new CustomEvent('bjorn:ai_exec', { detail: data }));
      } catch (e) { console.warn('[ConsoleSSE] Failed to parse AI_EXEC:', e); }
    }
    if (raw.includes('[AI_DONE]')) {
      try {
        const json = raw.split('[AI_DONE]')[1].trim();
        const data = JSON.parse(json);
        window.dispatchEvent(new CustomEvent('bjorn:ai_done', { detail: data }));
      } catch (e) { console.warn('[ConsoleSSE] Failed to parse AI_DONE:', e); }
    }

    const html = processLogLine(raw);
    if (isUserScrolling && !autoScroll) {
      lineBuffer.push(html);
      updateBufferBadge();
    } else {
      appendLogHtml(html);
    }
  };

  evtSource.onerror = () => {
    healthyMessageCount = 0;
    disconnectSSE();
    scheduleReconnect();
  };
}

function disconnectSSE() {
  if (evtSource) {
    evtSource.close();
    evtSource = null;
  }
}

function scheduleReconnect() {
  if (reconnectTimer) return;
  if (reconnectCount >= MAX_RECONNECT) {
    toast(t('console.maxReconnect'), 4000, 'warning');
    return;
  }

  reconnectCount++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    // Only reconnect if console is still open
    if (elConsole && elConsole.classList.contains('open')) {
      connectSSE();
    }
  }, RECONNECT_DELAY_MS);
}

/* ------------------------------------------------------------------ */
/*  Font size                                                         */
/* ------------------------------------------------------------------ */

/**
 * Set the console font size in pixels. Clamped to the range input's
 * min/max bounds. Persisted to localStorage.
 * @param {number|string} px
 */
export function setFont(px) {
  if (!elConsole || !elFontInput) return;

  const min = Number(elFontInput.min) || 2;
  const max = Number(elFontInput.max) || 24;
  let val = Math.round(Number(px));
  if (Number.isNaN(val)) val = defaultFontPx();
  val = Math.max(min, Math.min(max, val));

  elConsole.style.setProperty('--console-font', `${val}px`);
  elFontInput.value = val;
  paintRangeTrack(elFontInput);

  try {
    localStorage.setItem(LS_FONT_KEY, String(val));
  } catch { /* storage full / blocked */ }
}

/** Load saved font size or apply sensible default. */
function loadFont() {
  let saved = null;
  try {
    saved = localStorage.getItem(LS_FONT_KEY);
  } catch { /* blocked */ }

  const px = saved !== null ? Number(saved) : defaultFontPx();
  setFont(px);
}

/* ------------------------------------------------------------------ */
/*  Console resize (drag)                                             */
/* ------------------------------------------------------------------ */

function onResizeStart(e) {
  e.preventDefault();
  resizeDragging = true;
  resizeStartY = e.type.startsWith('touch') ? e.touches[0].clientY : e.clientY;
  resizeStartH = elConsole ? elConsole.offsetHeight : 0;

  document.addEventListener('mousemove', onResizeMove);
  document.addEventListener('mouseup', onResizeEnd);
  document.addEventListener('touchmove', onResizeMove, { passive: false });
  document.addEventListener('touchend', onResizeEnd);
}

function onResizeMove(e) {
  if (!resizeDragging || !elConsole) return;
  e.preventDefault();

  const clientY = e.type.startsWith('touch') ? e.touches[0].clientY : e.clientY;
  const delta = resizeStartY - clientY;             // drag up = larger
  const newH = Math.max(80, resizeStartH + delta);  // floor at 80px
  elConsole.style.height = `${newH}px`;
  if (isDocked) syncDockSpace();
}

function onResizeEnd() {
  resizeDragging = false;
  document.removeEventListener('mousemove', onResizeMove);
  document.removeEventListener('mouseup', onResizeEnd);
  document.removeEventListener('touchmove', onResizeMove);
  document.removeEventListener('touchend', onResizeEnd);
  if (isDocked) syncDockSpace();
}

/* ------------------------------------------------------------------ */
/*  Mode / Attack toggles                                             */
/* ------------------------------------------------------------------ */


/**
 * Set the Mode UI based on the mode string: 'MANUAL', 'AUTO', or 'AI'.
 * @param {string} mode
 */
function setModeUI(mode) {
  if (!elModePill || !elModeToggle) return;

  // Normalize
  mode = String(mode || 'AUTO').toUpperCase().trim();
  if (mode === 'TRUE') mode = 'MANUAL';     // Legacy fallback
  if (mode === 'FALSE') mode = 'AUTO';      // Legacy fallback

  // Default to AUTO if unrecognized
  if (!['MANUAL', 'AUTO', 'AI'].includes(mode)) {
    mode = 'AUTO';
  }

  const isManual = mode === 'MANUAL';
  const isAi = mode === 'AI';

  // Pill classes
  elModePill.classList.remove('manual', 'auto', 'ai');
  if (isManual) {
    elModePill.classList.add('manual');
  } else if (isAi) {
    elModePill.classList.add('ai');
  } else {
    elModePill.classList.add('auto');
  }

  // Pill Text
  let pillText = t('console.auto');
  if (isManual) pillText = t('console.manual');
  if (isAi) pillText = 'AI Mode';

  elModePill.innerHTML = `<span class="dot"></span> ${pillText}`;

  // Toggle Button Text (Show what NEXT click does)
  // Cycle: MANUAL -> AUTO -> AI -> MANUAL
  if (isManual) {
    elModeToggle.textContent = 'Enable Auto';
  } else if (isAi) {
    elModeToggle.textContent = 'Stop (Manual)'; // AI -> Manual is safer "Stop"
  } else {
    // Auto
    elModeToggle.textContent = 'Enable AI';
  }

  elModeToggle.setAttribute('aria-pressed', String(isManual));
  showAttackForMode(isManual);
}

function showAttackForMode(isManual) {
  const attackBar = $('#attackBar');
  if (!elConsole || !attackBar) return;
  const visible = !!isManual && window.innerWidth > 700;
  elConsole.classList.toggle('with-attack', visible);
  attackBar.style.display = visible ? 'flex' : 'none';
  if (elAttackToggle) elAttackToggle.setAttribute('aria-expanded', String(visible));
}


async function refreshModeFromServer() {
  try {
    // Returns "MANUAL", "AUTO", or "AI" string (text/plain)
    // We must await .text() if the api wrapper returns the fetch response, 
    // but the 'api' helper usually returns parsed JSON or text based on content-type.
    // Let's assume api.get returns the direct body.
    // We'll treat it as string and trim it.
    let mode = await api.get('/check_manual_mode', { timeout: 5000, retries: 0 });

    if (typeof mode === 'string') {
      mode = mode.trim().replace(/^"|"$/g, ''); // Remove quotes if JSON encoded
    }

    setModeUI(mode);
  } catch (e) {
    // Keep UI as-is
  }
}


async function loadManualTargets() {
  if (!elSelIp || !elSelPort || !elSelAction) return;
  try {
    const data = await api.get('/netkb_data_json', { timeout: 10000, retries: 0 });
    const ips = Array.isArray(data?.ips) ? data.ips : [];
    const actions = Array.isArray(data?.actions) ? data.actions : [];
    const portsByIp = data?.ports && typeof data.ports === 'object' ? data.ports : {};

    const currentIp = elSelIp.value;
    const currentAction = elSelAction.value;

    elSelIp.innerHTML = '';
    if (!ips.length) {
      const op = document.createElement('option');
      op.value = '';
      op.textContent = t('console.noTarget');
      elSelIp.appendChild(op);
    } else {
      for (const ip of ips) {
        const op = document.createElement('option');
        op.value = String(ip);
        op.textContent = String(ip);
        elSelIp.appendChild(op);
      }
      if (currentIp && ips.includes(currentIp)) elSelIp.value = currentIp;
    }

    elSelAction.innerHTML = '';
    if (!actions.length) {
      const op = document.createElement('option');
      op.value = '';
      op.textContent = t('console.noAction');
      elSelAction.appendChild(op);
    } else {
      for (const action of actions) {
        const op = document.createElement('option');
        op.value = String(action);
        op.textContent = String(action);
        elSelAction.appendChild(op);
      }
      if (currentAction && actions.includes(currentAction)) elSelAction.value = currentAction;
    }

    updatePortsForSelectedIp(portsByIp);
  } catch {
    // Keep existing options if loading fails.
  }
}

function updatePortsForSelectedIp(cachedPortsByIp = null) {
  if (!elSelIp || !elSelPort) return;
  const render = (ports) => {
    elSelPort.innerHTML = '';
    const list = Array.isArray(ports) ? ports : [];
    if (!list.length) {
      const op = document.createElement('option');
      op.value = '';
      op.textContent = t('console.auto');
      elSelPort.appendChild(op);
      return;
    }
    for (const p of list) {
      const op = document.createElement('option');
      op.value = String(p);
      op.textContent = String(p);
      elSelPort.appendChild(op);
    }
  };

  if (cachedPortsByIp && typeof cachedPortsByIp === 'object') {
    render(cachedPortsByIp[elSelIp.value]);
    return;
  }

  api.get('/netkb_data_json', { timeout: 10000, retries: 0 })
    .then((data) => render(data?.ports?.[elSelIp.value]))
    .catch(() => render([]));
}

async function runManualScan() {
  if (!elBtnScan) return;
  elBtnScan.classList.add('scanning');
  try {
    await api.post('/manual_scan');
    toast(t('console.scanStarted'), 1600, 'success');
  } catch {
    toast(t('console.scanFailed'), 2500, 'error');
  } finally {
    setTimeout(() => elBtnScan?.classList.remove('scanning'), 800);
  }
}

async function runManualAttack() {
  if (!elBtnAttack) return;
  elBtnAttack.classList.add('attacking');
  try {
    await api.post('/manual_attack', {
      ip: elSelIp?.value || '',
      port: elSelPort?.value || '',
      action: elSelAction?.value || '',
    });
    toast(t('console.attackStarted'), 1600, 'success');
  } catch {
    toast(t('console.attackFailed'), 2500, 'error');
  } finally {
    setTimeout(() => elBtnAttack?.classList.remove('attacking'), 900);
  }
}

async function toggleMode() {
  if (!elModePill) return;

  // Determine current mode from class
  let current = 'AUTO';
  if (elModePill.classList.contains('manual')) current = 'MANUAL';
  if (elModePill.classList.contains('ai')) current = 'AI';

  // Cycle: MANUAL -> AUTO -> AI -> MANUAL
  let next = 'AUTO';
  if (current === 'MANUAL') next = 'AUTO';
  else if (current === 'AUTO') next = 'AI';
  else if (current === 'AI') next = 'MANUAL';

  try {
    // Use the new centralized config endpoint
    const res = await api.post('/api/rl/config', { mode: next });
    if (res && res.status === 'ok') {
      setModeUI(res.mode);
      toast(`Mode: ${res.mode}`, 2000, 'success');
    } else {
      toast('Failed to change mode', 3000, 'error');
    }
  } catch (e) {
    console.error(e);
    toast(t('console.failedToggleMode'), 3000, 'error');
  }
}

async function toggleModeQuick() {
  if (!elModePill) return;

  // Quick toggle intended for the pill:
  // AI <-> AUTO (MANUAL -> AUTO).
  let current = 'AUTO';
  if (elModePill.classList.contains('manual')) current = 'MANUAL';
  if (elModePill.classList.contains('ai')) current = 'AI';

  let next = 'AUTO';
  if (current === 'AI') next = 'AUTO';
  else if (current === 'AUTO') next = 'AI';
  else if (current === 'MANUAL') next = 'AUTO';

  try {
    const res = await api.post('/api/rl/config', { mode: next });
    if (res && res.status === 'ok') {
      setModeUI(res.mode);
      toast(`Mode: ${res.mode}`, 2000, 'success');
    } else {
      toast('Failed to change mode', 3000, 'error');
    }
  } catch (e) {
    console.error(e);
    toast(t('console.failedToggleMode'), 3000, 'error');
  }
}

function toggleAttackBar() {
  const attackBar = $('#attackBar');
  if (!elConsole || !attackBar) return;
  const on = !elConsole.classList.contains('with-attack');
  elConsole.classList.toggle('with-attack', on);
  attackBar.style.display = on ? 'flex' : 'none';
  if (elAttackToggle) elAttackToggle.setAttribute('aria-expanded', String(on));
}

/* ------------------------------------------------------------------ */
/*  Console open / close                                              */
/* ------------------------------------------------------------------ */

/**
 * Open the console panel and start the SSE stream.
 */
export function openConsole() {
  if (!elConsole) return;
  elConsole.classList.add('open');
  reconnectCount = 0;
  start();
  syncDockSpace();
}

/**
 * Close the console panel and stop the SSE stream.
 */
export function closeConsole() {
  if (!elConsole) return;
  elConsole.classList.remove('open');
  stop();
  syncDockSpace();
}

/**
 * Toggle the console between open and closed states.
 */
export function toggleConsole() {
  if (!elConsole) return;
  if (elConsole.classList.contains('open')) {
    closeConsole();
  } else {
    openConsole();
  }
}

/* ------------------------------------------------------------------ */
/*  Public API                                                        */
/* ------------------------------------------------------------------ */

/**
 * Start the SSE log stream (idempotent).
 */
export function start() {
  connectSSE();
}

/**
 * Stop the SSE log stream and clear reconnect state.
 */
export function stop() {
  disconnectSSE();
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectCount = 0;
}

/**
 * Toggle the SSE stream on/off.
 */
export function toggle() {
  if (evtSource) {
    stop();
  } else {
    start();
  }
}

/**
 * Force the console to scroll to the bottom, flushing any buffered
 * lines and re-enabling auto-scroll.
 */
export function forceBottom() {
  autoScroll = true;
  isUserScrolling = false;
  flushBuffer();
  scrollToBottom();
  updateFloatingUI();
}

/* ------------------------------------------------------------------ */
/*  Initialisation                                                    */
/* ------------------------------------------------------------------ */

/**
 * Initialise the console SSE module.
 * Wires up all event listeners, loads persisted state, and checks
 * whether the console should auto-start.
 */
export function init() {
  /* Cache DOM references */
  elConsole = $('#console');
  elLogout = $('#logout');
  elFontInput = $('#consoleFont');
  elModePill = $('#modePill');
  elModeToggle = $('#modeToggle');
  elAttackToggle = $('#attackToggle');
  elSelIp = $('#selIP');
  elSelPort = $('#selPort');
  elSelAction = $('#selAction');
  elBtnScan = $('#btnScan');
  elBtnAttack = $('#btnAttack');

  if (!elConsole || !elLogout) {
    console.warn('[ConsoleSSE] Required DOM elements not found — aborting init.');
    return;
  }

  /* Floating UI (scroll-to-bottom btn, buffer badge) */
  ensureFloatingUI();
  isDocked = readDockPref();
  ensureDockButton();
  syncDockSpace();

  /* -- Font size --------------------------------------------------- */
  loadFont();
  if (elFontInput) {
    elFontInput.addEventListener('input', () => setFont(elFontInput.value));
  }

  /* -- Close / Clear ----------------------------------------------- */
  const btnClose = $('#closeConsole');
  if (btnClose) btnClose.addEventListener('click', closeConsole);

  const btnClear = $('#clearLogs');
  if (btnClear) {
    btnClear.addEventListener('click', () => {
      if (elLogout) {
        while (elLogout.firstChild) elLogout.removeChild(elLogout.firstChild);
      }
      lineBuffer = [];
      updateBufferBadge();
    });
  }

  /* -- Old behavior: click bottombar to toggle console ------------- */
  const bottomBar = $('#bottombar');
  if (bottomBar) {
    bottomBar.addEventListener('click', (e) => {
      const target = e.target;
      if (!(target instanceof Element)) return;
      // Avoid hijacking liveview interactions.
      if (target.closest('#bjorncharacter') || target.closest('.bjorn-dropdown')) return;
      toggleConsole();
    });
  }

  /* -- Mode toggle ------------------------------------------------- */
  if (elModeToggle) {
    elModeToggle.addEventListener('click', toggleMode);
  }
  if (elModePill) {
    elModePill.addEventListener('click', (e) => {
      // Prevent bubbling to bottom bar toggle (if nested)
      e.preventDefault();
      e.stopPropagation();
      toggleModeQuick();
    });
  }

  /* -- Attack bar toggle ------------------------------------------- */
  if (elAttackToggle) {
    elAttackToggle.addEventListener('click', toggleAttackBar);
  }

  if (elSelIp) {
    elSelIp.addEventListener('change', () => updatePortsForSelectedIp());
  }
  if (elBtnScan) {
    elBtnScan.addEventListener('click', (e) => {
      e.preventDefault();
      runManualScan();
    });
  }
  if (elBtnAttack) {
    elBtnAttack.addEventListener('click', (e) => {
      e.preventDefault();
      runManualAttack();
    });
  }

  /* -- Scroll tracking --------------------------------------------- */
  elLogout.addEventListener('scroll', onLogScroll);

  /* -- Console resize ---------------------------------------------- */
  const elResize = $('#consoleResize');
  if (elResize) {
    elResize.addEventListener('mousedown', onResizeStart);
    elResize.addEventListener('touchstart', onResizeStart, { passive: false });
  }

  /* -- Keyboard shortcut: Ctrl + ` to toggle console --------------- */
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.key === '`') {
      e.preventDefault();
      toggleConsole();
    }
  });

  /* -- Autostart check --------------------------------------------- */
  loadManualTargets();
  refreshModeFromServer();
  window.addEventListener('resize', () => refreshModeFromServer());

  // BroadcastChannel for instant Tab-to-Tab sync
  const bc = new BroadcastChannel('bjorn_mode_sync');
  bc.onmessage = (ev) => {
    if (ev.data && ev.data.mode) {
      setModeUI(ev.data.mode);
    }
  };

  checkAutostart();
}

/**
 * Query the server to determine if the console should auto-start.
 */
async function checkAutostart() {
  // Keep console closed by default when the web UI loads.
  // It can still be opened manually by the user.
  closeConsole();
}
