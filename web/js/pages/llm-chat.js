/**
 * llm-chat — LLM Chat SPA page
 * Chat interface with LLM bridge + orchestrator reasoning log.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api } from '../core/api.js';
import { el, $, empty, escapeHtml } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'llm-chat';

/* ── State ─────────────────────────────────────────────── */

let tracker    = null;
let root       = null;
let llmEnabled = false;
let orchMode   = false;
const sessionId = 'chat-' + Math.random().toString(36).slice(2, 8);

/* ── Lifecycle ─────────────────────────────────────────── */

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  bindEvents();
  await checkStatus();
  sysMsg(t('llm_chat.session_started'));
}

export function unmount() {
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  root       = null;
  llmEnabled = false;
  orchMode   = false;
}

/* ── Shell ─────────────────────────────────────────────── */

function buildShell() {
  return el('div', { class: 'llmc-page' }, [

    /* Header */
    el('div', { class: 'llmc-header' }, [
      el('span', { class: 'llmc-dot', id: 'llmc-dot' }),
      el('span', { class: 'llmc-title' }, ['BJORN / CHAT']),
      el('span', { class: 'llmc-status', id: 'llmc-status' }, [t('llm_chat.checking')]),
      el('button', { class: 'llmc-btn-ghost', id: 'llmc-orch-btn', title: t('llm_chat.orch_title') },
        [t('llm_chat.orch_log')]),
      el('button', { class: 'llmc-btn-ghost llmc-clear-btn', id: 'llmc-clear-btn' },
        [t('llm_chat.clear_history')]),
      el('button', { class: 'llmc-btn-ghost', id: 'llmc-cfg-btn', title: 'LLM Settings' },
        ['\u2699']),
    ]),

    /* Messages */
    el('div', { class: 'llmc-messages', id: 'llmc-messages' }, [
      el('div', { class: 'llmc-disabled-msg', id: 'llmc-disabled-msg', style: 'display:none' }, [
        t('llm_chat.disabled_msg') + ' ',
        el('a', { href: '#/llm-config' }, [t('llm_chat.settings_link')]),
        '.',
      ]),
    ]),

    /* Thinking */
    el('div', { class: 'llmc-thinking', id: 'llmc-thinking', style: 'display:none' }, [
      '▌ ', t('llm_chat.thinking'),
    ]),

    /* Input row */
    el('div', { class: 'llmc-input-row', id: 'llmc-input-row' }, [
      el('textarea', {
        class: 'llmc-input', id: 'llmc-input',
        placeholder: t('llm_chat.placeholder'),
        rows: '1',
      }),
      el('button', { class: 'llmc-send-btn', id: 'llmc-send-btn' }, [t('llm_chat.send')]),
    ]),
  ]);
}

/* ── Events ────────────────────────────────────────────── */

function bindEvents() {
  const sendBtn  = $('#llmc-send-btn',  root);
  const clearBtn = $('#llmc-clear-btn', root);
  const orchBtn  = $('#llmc-orch-btn',  root);
  const input    = $('#llmc-input',     root);

  const cfgBtn   = $('#llmc-cfg-btn',   root);

  if (sendBtn)  tracker.on(sendBtn,  'click', send);
  if (clearBtn) tracker.on(clearBtn, 'click', clearHistory);
  if (orchBtn)  tracker.on(orchBtn,  'click', toggleOrchLog);
  if (cfgBtn)   tracker.on(cfgBtn,   'click', () => { window.location.hash = '#/llm-config'; });

  if (input) {
    tracker.on(input, 'keydown', (e) => {
      // Auto-resize
      input.style.height = 'auto';
      input.style.height = Math.min(input.scrollHeight, 120) + 'px';
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    });
  }
}

/* ── Status ────────────────────────────────────────────── */

async function checkStatus() {
  try {
    const data = await api.get('/api/llm/status', { timeout: 5000, retries: 0 });
    if (!data) throw new Error('no data');

    llmEnabled = data.enabled === true;
    const dot    = $('#llmc-dot',    root);
    const status = $('#llmc-status', root);
    const disMsg = $('#llmc-disabled-msg', root);
    const sendBtn = $('#llmc-send-btn', root);

    if (!llmEnabled) {
      if (dot) dot.className = 'llmc-dot offline';
      if (status) status.textContent = t('llm_chat.disabled');
      if (disMsg) disMsg.style.display = '';
      if (sendBtn) sendBtn.disabled = true;
    } else {
      if (dot) dot.className = 'llmc-dot online';
      const backend = data.laruche_url
        ? 'LaRuche @ ' + data.laruche_url
        : (data.backend || 'auto');
      if (status) status.textContent = t('llm_chat.online') + ' · ' + backend;
      if (disMsg) disMsg.style.display = 'none';
      if (sendBtn) sendBtn.disabled = false;
    }
  } catch {
    const status = $('#llmc-status', root);
    if (status) status.textContent = t('llm_chat.unavailable');
  }
}

/* ── Chat ──────────────────────────────────────────────── */

async function send() {
  const input   = $('#llmc-input',   root);
  const sendBtn = $('#llmc-send-btn', root);
  if (!input) return;

  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  input.style.height = '44px';

  appendMsg('user', msg);
  setThinking(true);
  if (sendBtn) sendBtn.disabled = true;

  try {
    const data = await api.post('/api/llm/chat', { message: msg, session_id: sessionId });
    setThinking(false);
    if (data?.status === 'ok') {
      appendMsg('assistant', data.response);
    } else {
      sysMsg(t('llm_chat.error') + ': ' + (data?.message || 'unknown'));
    }
  } catch (e) {
    setThinking(false);
    sysMsg(t('llm_chat.net_error') + ': ' + e.message);
  } finally {
    if (sendBtn) sendBtn.disabled = !llmEnabled;
  }
}

async function clearHistory() {
  await api.post('/api/llm/clear_history', { session_id: sessionId });
  const msgs = $('#llmc-messages', root);
  if (!msgs) return;
  empty(msgs);
  const disMsg = $('#llmc-disabled-msg', root);
  if (disMsg) msgs.appendChild(disMsg);
  sysMsg(t('llm_chat.history_cleared'));
}

/* ── Orch log ──────────────────────────────────────────── */

async function toggleOrchLog() {
  orchMode = !orchMode;
  const orchBtn  = $('#llmc-orch-btn',   root);
  const inputRow = $('#llmc-input-row',  root);
  const msgs     = $('#llmc-messages',   root);

  if (orchMode) {
    if (orchBtn) { orchBtn.classList.add('active'); orchBtn.textContent = t('llm_chat.back_chat'); }
    if (inputRow) inputRow.style.display = 'none';
    if (msgs) empty(msgs);
    await loadOrchLog();
  } else {
    if (orchBtn) { orchBtn.classList.remove('active'); orchBtn.textContent = t('llm_chat.orch_log'); }
    if (inputRow) inputRow.style.display = '';
    if (msgs) empty(msgs);
    sysMsg(t('llm_chat.back_to_chat'));
  }
}

async function loadOrchLog() {
  sysMsg(t('llm_chat.loading_log'));
  try {
    const data = await api.get('/api/llm/reasoning', { timeout: 10000, retries: 0 });
    const msgs = $('#llmc-messages', root);
    if (!msgs) return;
    empty(msgs);

    if (!data?.messages?.length) {
      sysMsg(t('llm_chat.no_log'));
      return;
    }
    sysMsg(t('llm_chat.log_header') + ' — ' + data.count + ' message(s)');
    for (const m of data.messages) {
      appendMsg(m.role === 'user' ? 'user' : 'assistant', m.content || '');
    }
  } catch (e) {
    sysMsg(t('llm_chat.log_error') + ': ' + e.message);
  }
}

/* ── Helpers ───────────────────────────────────────────── */

function appendMsg(role, text) {
  const msgs = $('#llmc-messages', root);
  if (!msgs) return;
  const labels = { user: 'YOU', assistant: 'BJORN' };
  const roleLabel = labels[role] || role.toUpperCase();
  const div = el('div', { class: 'llmc-msg ' + role }, [
    el('div', { class: 'llmc-msg-role' }, [roleLabel]),
    document.createTextNode(text),
  ]);
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function sysMsg(text) {
  const msgs = $('#llmc-messages', root);
  if (!msgs) return;
  const div = el('div', { class: 'llmc-msg system' }, [text]);
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
}

function setThinking(on) {
  const el = $('#llmc-thinking', root);
  if (el) el.style.display = on ? '' : 'none';
}
