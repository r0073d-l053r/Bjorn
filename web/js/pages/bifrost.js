/**
 * Bifrost — Pwnagotchi Mode SPA page
 * Real-time WiFi recon dashboard with face, mood, activity feed, networks, plugins.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty, toast, escapeHtml, confirmT } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'bifrost';

/* ── State ─────────────────────────────────────────────── */

let tracker  = null;
let poller   = null;
let root     = null;

let bifrostEnabled = false;
let status   = {};
let stats    = {};
let networks = [];
let activity = [];
let plugins  = [];
let epochs   = [];
let sideTab  = 'networks';  // 'networks' | 'plugins' | 'history'

/* ── Lifecycle ─────────────────────────────────────────── */

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  bindEvents();
  await refresh();
  poller = new Poller(refresh, 4000);
  poller.start();
}

export function unmount() {
  if (poller) { poller.stop(); poller = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  root = null;
  networks = [];
  activity = [];
  plugins  = [];
  epochs   = [];
}

/* ── Shell ─────────────────────────────────────────────── */

function buildShell() {
  return el('div', { class: 'bifrost-page' }, [

    /* ── Header ───────────────────────────────────────── */
    el('div', { class: 'bifrost-header' }, [
      el('h1', { class: 'bifrost-title' }, [
        el('span', { class: 'bifrost-title-icon' }, ['🌈']),
        el('span', { 'data-i18n': 'bifrost.title' }, [t('bifrost.title')]),
      ]),
      el('div', { class: 'bifrost-controls' }, [
        el('button', { class: 'bifrost-btn', id: 'bifrost-toggle' }, [
          el('span', { class: 'dot' }),
          el('span', { class: 'bifrost-toggle-label', 'data-i18n': 'bifrost.disabled' }, [t('bifrost.disabled')]),
        ]),
        el('button', { class: 'bifrost-btn', id: 'bifrost-mode' }, [
          el('span', { class: 'bifrost-mode-label' }, ['Auto']),
        ]),
      ]),
    ]),

    /* ── Stats bar ────────────────────────────────────── */
    el('div', { class: 'bifrost-stats', id: 'bifrost-stats' }),

    /* ── Main grid ────────────────────────────────────── */
    el('div', { class: 'bifrost-grid' }, [

      /* Left: Live view */
      el('div', { class: 'bifrost-panel bifrost-live' }, [
        el('div', { class: 'bifrost-face-wrap' }, [
          el('div', { class: 'bifrost-face', id: 'bifrost-face' }, ['(. .)']),
          el('div', { class: 'bifrost-mood', id: 'bifrost-mood' }, ['sleeping']),
          el('div', { class: 'bifrost-voice', id: 'bifrost-voice' }),
        ]),
        el('div', { class: 'bifrost-info-row', id: 'bifrost-info' }),
        el('div', { class: 'bifrost-panel-head' }, [
          el('span', { 'data-i18n': 'bifrost.activityFeed' }, [t('bifrost.activityFeed')]),
          el('button', {
            class: 'bifrost-btn', id: 'bifrost-clear-activity',
            style: 'padding:3px 8px;font-size:0.65rem',
          }, [t('bifrost.clearActivity')]),
        ]),
        el('div', { class: 'bifrost-activity', id: 'bifrost-activity' }),
      ]),

      /* Right: sidebar */
      el('div', { class: 'bifrost-panel' }, [
        el('div', { class: 'bifrost-side-tabs' }, [
          sideTabBtn('networks', t('bifrost.networks')),
          sideTabBtn('plugins',  t('bifrost.plugins')),
          sideTabBtn('history',  t('bifrost.history')),
        ]),
        el('div', { class: 'bifrost-sidebar', id: 'bifrost-sidebar' }),
      ]),
    ]),
  ]);
}

function sideTabBtn(id, label) {
  return el('button', {
    class: `bifrost-side-tab${sideTab === id ? ' active' : ''}`,
    'data-btab': id,
  }, [label]);
}

/* ── Events ────────────────────────────────────────────── */

function bindEvents() {
  root.addEventListener('click', async (e) => {
    // Toggle enable/disable — BIFROST is a 4th exclusive mode
    const toggle = e.target.closest('#bifrost-toggle');
    if (toggle) {
      const willEnable = !bifrostEnabled;
      // Warn user: enabling puts WiFi in monitor mode, kills network
      if (willEnable && !confirmT(t('bifrost.confirmEnable'))) return;
      try {
        const res = await api.post('/api/bifrost/toggle', { enabled: willEnable });
        bifrostEnabled = res.enabled;
        paintToggle();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Toggle mode
    const modeBtn = e.target.closest('#bifrost-mode');
    if (modeBtn) {
      const newMode = status.mode === 'auto' ? 'manual' : 'auto';
      try {
        await api.post('/api/bifrost/mode', { mode: newMode });
        status.mode = newMode;
        paintMode();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Clear activity
    if (e.target.closest('#bifrost-clear-activity')) {
      try {
        await api.post('/api/bifrost/activity/clear', {});
        toast(t('bifrost.activityCleared'), 2000, 'success');
        activity = [];
        paintActivity();
      } catch (err) { toast(err.message, 3000, 'error'); }
      return;
    }

    // Side tab switch
    const stab = e.target.closest('[data-btab]');
    if (stab) {
      sideTab = stab.dataset.btab;
      $$('.bifrost-side-tab', root).forEach(b =>
        b.classList.toggle('active', b.dataset.btab === sideTab));
      paintSidebar();
      return;
    }

    // Plugin toggle
    const pluginToggle = e.target.closest('[data-plugin-toggle]');
    if (pluginToggle) {
      const name = pluginToggle.dataset.pluginToggle;
      const plugin = plugins.find(p => p.name === name);
      if (plugin) {
        try {
          await api.post('/api/bifrost/plugin/toggle', { name, enabled: !plugin.enabled });
          await refreshPlugins();
        } catch (err) { toast(err.message, 3000, 'error'); }
      }
      return;
    }
  });
}

/* ── Data refresh ──────────────────────────────────────── */

async function refresh() {
  try {
    const [statusData, statsData, actData] = await Promise.all([
      api.get('/api/bifrost/status'),
      api.get('/api/bifrost/stats'),
      api.get('/api/bifrost/activity?limit=50'),
    ]);
    status = statusData || {};
    stats = statsData || {};
    bifrostEnabled = status.enabled || false;
    activity = actData.activity || [];
    paint();
  } catch (err) {
    console.warn('[bifrost] refresh error:', err.message);
  }

  // Lazy-load sidebar data
  if (sideTab === 'networks') refreshNetworks();
  else if (sideTab === 'plugins') refreshPlugins();
  else if (sideTab === 'history') refreshEpochs();
}

async function refreshNetworks() {
  try {
    const data = await api.get('/api/bifrost/networks');
    networks = data.networks || [];
    paintSidebar();
  } catch (err) { console.warn('[bifrost] networks error:', err.message); }
}

async function refreshPlugins() {
  try {
    const data = await api.get('/api/bifrost/plugins');
    plugins = data.plugins || [];
    paintSidebar();
  } catch (err) { console.warn('[bifrost] plugins error:', err.message); }
}

async function refreshEpochs() {
  try {
    const data = await api.get('/api/bifrost/epochs');
    epochs = data.epochs || [];
    paintSidebar();
  } catch (err) { console.warn('[bifrost] epochs error:', err.message); }
}

/* ── Paint ─────────────────────────────────────────────── */

function paint() {
  paintToggle();
  paintMode();
  paintStats();
  paintFace();
  paintInfo();
  paintActivity();
  paintSidebar();
}

function paintToggle() {
  const btn = $('#bifrost-toggle', root);
  if (!btn) return;
  btn.classList.toggle('active', bifrostEnabled);
  const lbl = $('.bifrost-toggle-label', btn);
  if (lbl) {
    const key = bifrostEnabled ? 'bifrost.enabled' : 'bifrost.disabled';
    lbl.textContent = t(key);
    lbl.setAttribute('data-i18n', key);
  }
}

function paintMode() {
  const lbl = $('.bifrost-mode-label', root);
  if (lbl) {
    const mode = status.mode || 'auto';
    lbl.textContent = mode === 'auto' ? 'Auto' : 'Manual';
  }
}

function paintStats() {
  const container = $('#bifrost-stats', root);
  if (!container) return;

  const items = [
    { val: stats.total_networks || 0,   lbl: t('bifrost.statNetworks') },
    { val: stats.total_handshakes || 0,  lbl: t('bifrost.statHandshakes') },
    { val: stats.total_deauths || 0,     lbl: t('bifrost.statDeauths') },
    { val: stats.total_assocs || 0,      lbl: t('bifrost.statAssocs') },
    { val: stats.total_epochs || 0,      lbl: t('bifrost.statEpochs') },
    { val: stats.total_peers || 0,       lbl: t('bifrost.statPeers') },
  ];

  empty(container);
  for (const s of items) {
    container.appendChild(
      el('div', { class: 'bifrost-stat' }, [
        el('div', { class: 'bifrost-stat-val' }, [String(s.val)]),
        el('div', { class: 'bifrost-stat-lbl' }, [s.lbl]),
      ])
    );
  }
}

function paintFace() {
  const faceEl = $('#bifrost-face', root);
  const moodEl = $('#bifrost-mood', root);
  const voiceEl = $('#bifrost-voice', root);
  if (faceEl) {
    if (status.monitor_failed) {
      faceEl.textContent = '(X_X)';
      faceEl.className = 'bifrost-face mood-angry';
    } else {
      faceEl.textContent = status.face || '(. .)';
      faceEl.className = 'bifrost-face';
      if (status.mood) faceEl.classList.add('mood-' + status.mood);
    }
  }
  if (moodEl) {
    if (status.monitor_failed) {
      moodEl.textContent = t('bifrost.monitorFailed');
      moodEl.className = 'bifrost-mood mood-badge-angry';
    } else {
      const mood = status.mood || 'sleeping';
      moodEl.textContent = mood;
      moodEl.className = 'bifrost-mood mood-badge-' + mood;
    }
  }
  if (voiceEl) {
    if (status.monitor_failed) {
      voiceEl.textContent = t('bifrost.monitorFailedHint');
    } else {
      voiceEl.textContent = status.voice || '';
    }
  }
}

function paintInfo() {
  const container = $('#bifrost-info', root);
  if (!container) return;
  empty(container);

  const items = [
    { label: 'Ch', value: status.channel || 0 },
    { label: 'APs', value: status.num_aps || 0 },
    { label: '🤝', value: status.num_handshakes || 0 },
    { label: '⏱', value: formatUptime(status.uptime || 0) },
    { label: 'Ep', value: status.epoch || 0 },
  ];

  for (const item of items) {
    container.appendChild(
      el('span', { class: 'bifrost-info-chip' }, [
        el('span', { class: 'bifrost-info-label' }, [item.label]),
        el('span', { class: 'bifrost-info-value' }, [String(item.value)]),
      ])
    );
  }

  if (status.last_pwnd) {
    container.appendChild(
      el('span', { class: 'bifrost-info-chip pwnd' }, [
        el('span', { class: 'bifrost-info-label' }, ['🏆']),
        el('span', { class: 'bifrost-info-value' }, [escapeHtml(status.last_pwnd)]),
      ])
    );
  }
}

function paintActivity() {
  const container = $('#bifrost-activity', root);
  if (!container) return;
  empty(container);

  if (activity.length === 0) {
    container.appendChild(
      el('div', { class: 'bifrost-empty' }, [t('bifrost.noActivity')])
    );
    return;
  }

  for (const ev of activity) {
    const icon = eventIcon(ev.event_type);
    container.appendChild(
      el('div', { class: 'bifrost-activity-item' }, [
        el('span', { class: 'bifrost-act-time' }, [formatTime(ev.timestamp)]),
        el('span', { class: 'bifrost-act-icon' }, [icon]),
        el('span', { class: 'bifrost-act-title' }, [escapeHtml(ev.title || '')]),
        ev.details ? el('span', { class: 'bifrost-act-detail' }, [escapeHtml(ev.details)]) : '',
      ].filter(Boolean))
    );
  }
}

/* ── Sidebar panels ────────────────────────────────────── */

function paintSidebar() {
  const container = $('#bifrost-sidebar', root);
  if (!container) return;
  empty(container);

  switch (sideTab) {
    case 'networks': paintNetworks(container); break;
    case 'plugins':  paintPlugins(container);  break;
    case 'history':  paintEpochs(container);   break;
  }
}

/* ── Networks ─────────────────────────────────────────── */

function paintNetworks(container) {
  if (networks.length === 0) {
    container.appendChild(el('div', { class: 'bifrost-empty' }, [t('bifrost.noNetworks')]));
    return;
  }

  for (const net of networks) {
    const signal = signalBars(net.rssi);
    const encBadge = encryptionBadge(net.encryption || 'OPEN');
    container.appendChild(
      el('div', { class: 'bifrost-net-row' }, [
        el('div', { class: 'bifrost-net-main' }, [
          el('span', { class: 'bifrost-net-signal' }, [signal]),
          el('span', { class: 'bifrost-net-essid' }, [escapeHtml(net.essid || '<hidden>')]),
          el('span', { class: 'bifrost-net-enc' }, [encBadge]),
        ]),
        el('div', { class: 'bifrost-net-meta' }, [
          `ch${net.channel || '?'}`,
          net.rssi ? ` ${net.rssi}dB` : '',
          net.clients ? ` · ${net.clients} sta` : '',
          net.handshake ? ' · ✅' : '',
        ].join('')),
      ])
    );
  }
}

/* ── Plugins ──────────────────────────────────────────── */

function paintPlugins(container) {
  if (plugins.length === 0) {
    container.appendChild(el('div', { class: 'bifrost-empty' }, [t('bifrost.noPlugins')]));
    return;
  }

  for (const plug of plugins) {
    container.appendChild(
      el('div', { class: 'bifrost-plugin-row' }, [
        el('div', { class: 'bifrost-plugin-info' }, [
          el('span', { class: 'bifrost-plugin-name' }, [escapeHtml(plug.name)]),
          el('span', { class: 'bifrost-plugin-desc' }, [escapeHtml(plug.description || '')]),
        ]),
        el('button', {
          class: `bifrost-btn${plug.enabled ? ' active' : ''}`,
          'data-plugin-toggle': plug.name,
          style: 'padding:2px 8px;font-size:0.6rem',
        }, [plug.enabled ? '⏸' : '▶']),
      ])
    );
  }
}

/* ── Epoch History ────────────────────────────────────── */

function paintEpochs(container) {
  if (epochs.length === 0) {
    container.appendChild(el('div', { class: 'bifrost-empty' }, [t('bifrost.noEpochs')]));
    return;
  }

  const table = el('div', { class: 'bifrost-epoch-table' }, [
    el('div', { class: 'bifrost-epoch-header' }, [
      el('span', {}, ['#']),
      el('span', {}, ['🤝']),
      el('span', {}, ['💀']),
      el('span', {}, ['📡']),
      el('span', {}, [t('bifrost.mood')]),
      el('span', {}, ['⭐']),
    ]),
  ]);

  for (const ep of epochs.slice(0, 50)) {
    const rewardStr = typeof ep.reward === 'number' ? ep.reward.toFixed(2) : '—';
    table.appendChild(
      el('div', { class: 'bifrost-epoch-row' }, [
        el('span', {}, [String(ep.epoch_num ?? ep.id ?? '?')]),
        el('span', {}, [String(ep.num_handshakes ?? 0)]),
        el('span', {}, [String(ep.num_deauths ?? 0)]),
        el('span', {}, [String(ep.num_hops ?? 0)]),
        el('span', { class: `mood-badge-${ep.mood || 'sleeping'}` }, [ep.mood || '—']),
        el('span', {}, [rewardStr]),
      ])
    );
  }

  container.appendChild(table);
}

/* ── Helpers ───────────────────────────────────────────── */

function eventIcon(type) {
  const icons = {
    handshake: '🤝', deauth: '💀', association: '📡',
    new_ap: '📶', channel_hop: '📻', epoch: '🔄',
    plugin: '🧩', error: '❌', start: '▶️', stop: '⏹️',
  };
  return icons[type] || '📝';
}

function signalBars(rssi) {
  if (!rssi) return '▂';
  const val = Math.abs(rssi);
  if (val < 50) return '▂▄▆█';
  if (val < 60) return '▂▄▆';
  if (val < 70) return '▂▄';
  return '▂';
}

function encryptionBadge(enc) {
  if (!enc || enc === 'OPEN' || enc === '') return 'OPEN';
  if (enc.includes('WPA3')) return 'WPA3';
  if (enc.includes('WPA2')) return 'WPA2';
  if (enc.includes('WPA')) return 'WPA';
  if (enc.includes('WEP')) return 'WEP';
  return enc;
}

function formatUptime(secs) {
  if (!secs || secs < 0) return '0s';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (h > 0) return `${h}h${m.toString().padStart(2, '0')}m`;
  return `${m}m`;
}

function formatTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts.includes('Z') || ts.includes('+') ? ts : ts + 'Z');
    const now = Date.now();
    const diff = now - d.getTime();
    if (diff < 60000) return t('bifrost.justNow');
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m`;
    if (diff < 86400000) return `${Math.floor(diff / 3600000)}h`;
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  } catch { return ts; }
}
