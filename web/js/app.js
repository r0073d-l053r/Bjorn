/**
 * app.js — SPA bootstrap.
 * Initializes core modules, registers routes, starts the router.
 * Wires shell UI: console, quickpanel, actions, settings, launcher, pollers.
 */

import * as router from './core/router.js';
import * as i18n from './core/i18n.js';
import * as theme from './core/theme.js';
import { api, Poller } from './core/api.js';
import { $, el, setText, toast } from './core/dom.js';
import * as consoleSSE from './core/console-sse.js';
import * as quickpanel from './core/quickpanel.js';
import * as actions from './core/actions.js';
import * as settingsConfig from './core/settings-config.js';

/* =========================================
 * 1) Initialize core modules
 * ========================================= */

// Theme: apply saved CSS vars immediately (no flash)
theme.init();

// i18n: load translations, then boot UI
i18n.init().then(() => {
  bootUI();
}).catch(err => {
  console.error('[App] i18n init failed:', err);
  bootUI(); // Boot anyway with fallback keys
});

function bootUI() {
  // Runtime i18n wrappers for legacy hardcoded dialogs.
  if (!window.__bjornDialogsPatched) {
    const nativeConfirm = window.confirm.bind(window);
    const nativePrompt = window.prompt.bind(window);
    window.confirm = (msg) => nativeConfirm(i18n.trLoose(String(msg ?? '')));
    window.prompt = (msg, def = '') => nativePrompt(i18n.trLoose(String(msg ?? '')), def);
    window.__bjornDialogsPatched = true;
  }

  /* =========================================
   * 2) Register all routes (lazy-loaded)
   * ========================================= */
  router.route('/dashboard', () => import('./pages/dashboard.js'));
  router.route('/netkb', () => import('./pages/netkb.js'));
  router.route('/network', () => import('./pages/network.js'));
  router.route('/credentials', () => import('./pages/credentials.js'));
  router.route('/vulnerabilities', () => import('./pages/vulnerabilities.js'));
  router.route('/attacks', () => import('./pages/attacks.js'));
  router.route('/scheduler', () => import('./pages/scheduler.js'));
  router.route('/database', () => import('./pages/database.js'));
  router.route('/files', () => import('./pages/files.js'));
  router.route('/loot', () => import('./pages/loot.js'));
  router.route('/actions', () => import('./pages/actions.js'));
  router.route('/actions-studio', () => import('./pages/actions-studio.js'));
  router.route('/backup', () => import('./pages/backup.js'));
  router.route('/web-enum', () => import('./pages/web-enum.js'));
  router.route('/zombieland', () => import('./pages/zombieland.js'));
  router.route('/ai-dashboard', () => import('./pages/rl-dashboard.js'));
  router.route('/bjorn-debug', () => import('./pages/bjorn-debug.js'));
  router.route('/sentinel', () => import('./pages/sentinel.js'));
  router.route('/bifrost', () => import('./pages/bifrost.js'));
  router.route('/loki', () => import('./pages/loki.js'));
  router.route('/bjorn', () => import('./pages/bjorn.js'));

  // 404 fallback
  router.setNotFound((container, path) => {
    container.appendChild(
      el('div', { class: 'not-found' }, [
        el('h2', {}, [i18n.t('common.notFound')]),
        el('p', {}, [`${i18n.t('common.notFound')}: ${path}`]),
        el('a', { href: '#/dashboard' }, [i18n.t('nav.dashboard')])
      ])
    );
  });

  /* =========================================
   * 3) Mount language selector in topbar
   * ========================================= */
  const langContainer = $('#langSelect');
  if (langContainer) {
    i18n.mountLangSelector(langContainer);
  }

  /* =========================================
   * 4) Initialize router (reads hash, loads first page)
   * ========================================= */
  const appContainer = $('#app');
  router.init(appContainer);
  window.addEventListener('i18n:changed', () => {
    i18n.updateDOM(document);
    router.reloadCurrent?.();
  });

  /* =========================================
   * 5) Wire up topbar buttons
   * ========================================= */
  wireTopbar();

  /* =========================================
   * 6) Start global pollers (status, character, say)
   * ========================================= */
  ensureBjornProgress();
  startGlobalPollers();

  /* =========================================
   * 7) Wire page launcher overlay
   * ========================================= */
  wireLauncher();

  /* =========================================
   * 8) Initialize shell modules
   * ========================================= */
  consoleSSE.init();
  quickpanel.init();
  actions.init();

  /* =========================================
   * 9) Wire bottombar extras (liveview, footer fit)
   * ========================================= */
  wireLiveview();
  setupFooterFit();

  /* =========================================
   * 10) Wire settings modal
   * ========================================= */
  wireSettingsModal();

  /* =========================================
   * 11) Wire chip editor
   * ========================================= */
  wireChipEditor();

  /* =========================================
   * 12) Global toast bridge
   * ========================================= */
  window.toast = (msg, ms = 2600) => toast(msg, ms);

  console.info('[App] Bjorn SPA initialized');
}

/* =========================================
 * Global pollers — status bar updates
 * OPTIMIZED: Staggered timings to reduce CPU load
 * ========================================= */
function ensureBjornProgress() {
  const host = document.querySelector('.status-left .status-text');
  if (!host) return;

  if (document.getElementById('bjornProgress')) return; // déjà là

  const progress = el('div', {
    id: 'bjornProgress',
    class: 'bjorn-progress',
    style: 'display:none;'
  }, [
    el('div', { class: 'bjorn-progress-bar' }),
    el('span', { class: 'bjorn-progress-text' })
  ]);

  host.appendChild(progress);
}

function startGlobalPollers() {
  // Status (Toutes les 6s)
  const statusPoller = new Poller(async () => {
    try {
      const data = await api.get('/bjorn_status', { timeout: 5000, retries: 0 });

      const statusEl = $('#bjornStatus');
      const status2El = $('#bjornStatus2');

      const progressEl = $('#bjornProgress');
      const progressBar = progressEl?.querySelector('.bjorn-progress-bar');
      const progressText = progressEl?.querySelector('.bjorn-progress-text');

      const imgEl = $('#bjornStatusImage');

      if (statusEl && data.status) setText(statusEl, data.status);

      if (status2El) {
        if (data.status2) {
          setText(status2El, data.status2);
          status2El.style.display = '';
        } else {
          status2El.style.display = 'none';
        }
      }

      // 🟢 PROGRESS — show only when actively running (1-100)
      if (progressEl) {
        const pct = Number(data.progress) || 0;
        if (pct > 0) {
          progressEl.style.display = '';
          progressBar.style.setProperty('--progress', `${pct}%`);
          progressText.textContent = `${pct}%`;
        } else {
          progressEl.style.display = 'none';
        }
      }

      if (imgEl && data.image_path) {
        imgEl.src = data.image_path + '?t=' + Date.now();
      }
    } catch (e) { }
  }, 6000);

  // Character (Toutes les 10s - C'est suffisant pour une icône)
  const charPoller = new Poller(async () => {
    try {
      const imgEl = $('#bjorncharacter');
      if (!imgEl) return;
      const res = await fetch('/bjorn_character');
      if (!res.ok) return;
      const blob = await res.blob();
      if (imgEl.src && imgEl.src.startsWith('blob:')) URL.revokeObjectURL(imgEl.src);
      imgEl.src = URL.createObjectURL(blob);
    } catch (e) { }
  }, 10000);

  // Say (Toutes les 8s)
  const sayPoller = new Poller(async () => {
    try {
      const data = await api.get('/bjorn_say', { timeout: 5000, retries: 0 });
      const sayEl = $('#bjornSay');
      if (sayEl && data?.text) setText(sayEl, data.text);
    } catch (e) { }
  }, 8000);

  statusPoller.start();
  charPoller.start();
  sayPoller.start();
}

/* =========================================
 * Topbar wiring
 * ========================================= */

function wireTopbar() {
  // Logo -> dashboard
  const logo = $('#logoBtn');
  if (logo) {
    logo.addEventListener('click', () => router.navigate('/dashboard'));
    logo.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); router.navigate('/dashboard'); }
    });
  }

  // Settings button
  const settingsBtn = $('#openSettings');
  if (settingsBtn) {
    settingsBtn.addEventListener('click', () => toggleSettings());
  }

  // Launcher button
  const launcherBtn = $('#openLauncher');
  if (launcherBtn) {
    launcherBtn.addEventListener('click', () => toggleLauncher());
  }
}

/* =========================================
 * Liveview dropdown (BÉTON EDITION)
 * Uses recursive setTimeout to prevent thread stacking
 * ========================================= */

function wireLiveview() {
  const character = $('#bjorncharacter');
  const center = $('.status-center');
  if (!character || !center) return;

  const dropdown = el('div', { class: 'bjorn-dropdown' }, [
    el('img', { id: 'screenImage_Home', src: '/web/screen.png', alt: 'Bjorn', style: 'cursor:pointer;max-width:200px;border-radius:6px' })
  ]);
  center.appendChild(dropdown);

  const liveImg = $('#screenImage_Home', dropdown);
  let timer = null;
  const LIVE_DELAY = 4000; // On passe à 4s pour matcher display.py

  function updateLive() {
    if (dropdown.style.display !== 'block') return; // Stop si caché

    const n = new Image();
    n.onload = () => {
      liveImg.src = n.src;
      // On ne planifie la suivante QUE quand celle-ci est affichée
      timer = setTimeout(updateLive, LIVE_DELAY);
    };
    n.onerror = () => {
      // En cas d'erreur, on attend un peu avant de réessayer
      timer = setTimeout(updateLive, LIVE_DELAY * 2);
    };
    n.src = '/web/screen.png?t=' + Date.now();
  }

  const show = () => {
    dropdown.style.display = 'block';
    if (!timer) updateLive();
  };
  const hide = () => {
    dropdown.style.display = 'none';
    clearTimeout(timer);
    timer = null;
  };

  // Events
  character.addEventListener('mouseenter', show);
  character.addEventListener('mouseleave', () => setTimeout(() => {
    if (!dropdown.matches(':hover') && !character.matches(':hover')) hide();
  }, 300));

  character.addEventListener('click', (e) => {
    e.stopPropagation();
    dropdown.style.display === 'block' ? hide() : show();
  });

  document.addEventListener('click', (ev) => {
    if (!dropdown.contains(ev.target) && !character.contains(ev.target)) hide();
  });

  if (liveImg) {
    liveImg.addEventListener('click', () => router.navigate('/bjorn'));
  }
}

/* =========================================
 * Footer text fitting (adaptive font size)
 * ========================================= */

function setupFooterFit() {
  function fitTextById(id, opts = {}) {
    const el = document.getElementById(id);
    if (!el) return;
    const box = el.parentElement || el;
    const max = opts.max || 12;
    const min = opts.min || 7;
    let size = max;
    el.style.fontSize = size + 'px';
    const maxH = parseFloat(getComputedStyle(el).maxHeight) || Infinity;

    while ((el.scrollWidth > box.clientWidth || el.scrollHeight > maxH) && size > min) {
      size--;
      el.style.fontSize = size + 'px';
    }
  }

  function runFooterFit() {
    fitTextById('bjornStatus', { max: 12, min: 7 });
    fitTextById('bjornSay', { max: 12, min: 7 });
    fitTextById('bjornStatus2', { max: 12, min: 7 });
    fitTextById('bjornProgress', { max: 11, min: 7 }); // 🟢
  }

  // Run on load & resize
  window.addEventListener('load', runFooterFit);
  window.addEventListener('resize', runFooterFit);

  // Observe size/content changes
  const left = document.querySelector('.status-left');
  const right = document.querySelector('.status-right');
  const ro = new ResizeObserver(runFooterFit);
  if (left) ro.observe(left);
  if (right) ro.observe(right);

  ['bjornStatus', 'bjornSay', 'bjornStatus2', 'bjornProgress'].forEach(id => {
    const elem = document.getElementById(id);
    if (!elem) return;
    ro.observe(elem);
    new MutationObserver(runFooterFit).observe(elem, {
      childList: true,
      characterData: true,
      subtree: true
    });
  });

  const imgs = [document.getElementById('bjornStatusImage'), document.getElementById('bjorncharacter')];
  imgs.forEach(img => {
    if (!img) return;
    if (img.complete) runFooterFit();
    else img.addEventListener('load', runFooterFit, { once: true });
  });

  // Initial run
  runFooterFit();
}

/* =========================================
 * Page launcher
 * ========================================= */

const NAV_MODE_KEY = 'bjorn.navMode'; // 'rail' or 'grid'
function getNavMode() { return localStorage.getItem(NAV_MODE_KEY) || 'rail'; }
function setNavMode(mode) { localStorage.setItem(NAV_MODE_KEY, mode); }

const PAGES = [
  { path: '/dashboard', icon: 'home.png', label: 'nav.dashboard' },
  { path: '/bjorn', icon: 'bjorn_icon.png', label: 'nav.bjorn' },
  { path: '/netkb', icon: 'netkb.png', label: 'nav.netkb' },
  { path: '/network', icon: 'network.png', label: 'nav.network' },
  { path: '/credentials', icon: 'credentials.png', label: 'nav.credentials' },
  { path: '/vulnerabilities', icon: 'vulnerabilities.png', label: 'nav.vulnerabilities' },
  { path: '/attacks', icon: 'attacks.png', label: 'nav.attacks' },
  { path: '/scheduler', icon: 'scheduler.png', label: 'nav.scheduler' },
  { path: '/database', icon: 'database.png', label: 'nav.database' },
  { path: '/files', icon: 'files_explorer.png', label: 'nav.files' },
  { path: '/loot', icon: 'loot.png', label: 'nav.loot' },
  { path: '/actions', icon: 'actions_launcher.png', label: 'nav.actions' },
  { path: '/actions-studio', icon: 'actions_studio.png', label: 'nav.actionsStudio' },
  { path: '/backup', icon: 'backup_update.png', label: 'nav.backup' },
  { path: '/web-enum', icon: 'web_enum.png', label: 'nav.webEnum' },
  { path: '/zombieland', icon: 'zombieland.png', label: 'nav.zombieland' },
  { path: '/sentinel', icon: 'network.png', label: 'nav.sentinel' },
  { path: '/bifrost', icon: 'network.png', label: 'nav.bifrost' },
  { path: '/loki', icon: 'actions_launcher.png', label: 'nav.loki' },
  { path: '/ai-dashboard', icon: 'ai_dashboard.png', label: 'nav.ai_dashboard' },
  { path: '/bjorn-debug', icon: 'database.png', label: 'Bjorn Debug' },
];

function wireLauncher() {
  const railOverlay = $('#launcher');
  const gridOverlay = $('#navOverlay');
  const navGrid = $('#navGrid');
  if (!railOverlay) return;

  // Build rail launcher
  railOverlay.innerHTML = '';
  const scroll = el('div', { class: 'launcher-scroll' });
  for (const page of PAGES) {
    const card = el('button', {
      class: 'lbtn',
      role: 'button',
      tabindex: '0',
      title: i18n.t(page.label),
      onclick: () => {
        router.navigate(page.path);
        closeLauncher();
      },
    }, [
      el('img', { src: `/web/images/${page.icon}`, alt: '', width: '48', height: '48' }),
      el('span', { class: 'lbtn-label', 'data-i18n': page.label }, [i18n.t(page.label)]),
    ]);
    scroll.appendChild(card);
  }
  railOverlay.appendChild(scroll);

  // Build grid launcher
  if (navGrid) {
    navGrid.innerHTML = '';
    for (const page of PAGES) {
      const card = el('button', {
        class: 'lbtn',
        role: 'button',
        tabindex: '0',
        title: i18n.t(page.label),
        onclick: () => {
          router.navigate(page.path);
          closeNavOverlay();
        },
      }, [
        el('img', { src: `/web/images/${page.icon}`, alt: '', width: '48', height: '48' }),
        el('span', { class: 'lbtn-label', 'data-i18n': page.label }, [i18n.t(page.label)]),
      ]);
      navGrid.appendChild(card);
    }
  }

  // Close rail on outside click
  document.addEventListener('pointerdown', (e) => {
    const btn = $('#openLauncher');
    if (!railOverlay.classList.contains('show')) return;
    if (railOverlay.contains(e.target)) return;
    if (btn && btn.contains(e.target)) return;
    closeLauncher();
  });

  // Close grid overlay on backdrop click
  if (gridOverlay) {
    gridOverlay.addEventListener('click', (e) => {
      if (e.target === gridOverlay) closeNavOverlay();
    });
  }
}

function toggleLauncher() {
  if (getNavMode() === 'grid') {
    toggleNavOverlay();
  } else {
    const overlay = $('#launcher');
    if (!overlay) return;
    const isOpen = overlay.getAttribute('aria-hidden') !== 'false';
    overlay.setAttribute('aria-hidden', String(!isOpen));
    overlay.classList.toggle('show', isOpen);
  }
}

function closeLauncher() {
  const overlay = $('#launcher');
  if (!overlay) return;
  overlay.setAttribute('aria-hidden', 'true');
  overlay.classList.remove('show');
}

function toggleNavOverlay() {
  const overlay = $('#navOverlay');
  if (!overlay) return;
  const isOpen = overlay.classList.contains('show');
  if (isOpen) {
    overlay.classList.remove('show');
    overlay.setAttribute('aria-hidden', 'true');
  } else {
    overlay.classList.add('show');
    overlay.setAttribute('aria-hidden', 'false');
  }
}

function closeNavOverlay() {
  const overlay = $('#navOverlay');
  if (!overlay) return;
  overlay.classList.remove('show');
  overlay.setAttribute('aria-hidden', 'true');
}

/* =========================================
 * Settings modal (tabbed: General, Theme, Config)
 * Uses the old-style modal-backdrop + modal with tabs
 * ========================================= */

function wireSettingsModal() {
  // Build modal content inside #settingsBackdrop
  const backdrop = $('#settingsBackdrop');
  if (!backdrop) return;

  function buildSettings() {
    backdrop.innerHTML = '';
    const modal = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Settings' });

    // Tabs navigation
    const tabs = el('nav', { class: 'tabs', id: 'settingsTabs' });
    const btnGeneral = el('button', { class: 'tabbtn active', 'data-tab': 'general' }, ['General']);
    const btnTheme = el('button', { class: 'tabbtn', 'data-tab': 'theme' }, ['Theme']);
    const btnConfig = el('button', { class: 'tabbtn', 'data-tab': 'config' }, ['Config']);
    tabs.append(btnGeneral, btnTheme, btnConfig);

    // General tab
    const tabGeneral = el('section', { class: 'tabpanel', id: 'tab-general' });
    tabGeneral.append(
      el('h3', {}, [i18n.t('settings.general')]),
      el('div', { class: 'row' }, [
        el('label', {}, ['Notifications']),
        el('div', { class: 'switch', id: 'switchNotifs' })
      ]),
      el('div', { class: 'row' }, [
        el('label', {}, ['Navigation Mode']),
        el('select', { id: 'selectNavMode', class: 'select' }, [
          el('option', { value: 'rail' }, ['Floating Bar']),
          el('option', { value: 'grid' }, ['Grid Overlay']),
        ])
      ]),
      el('div', { class: 'row' }, [
        el('label', {}, [i18n.t('settings.language')]),
      ])
    );
    // Set current nav mode selection
    const navModeSelect = tabGeneral.querySelector('#selectNavMode');
    if (navModeSelect) {
      navModeSelect.value = getNavMode();
      navModeSelect.addEventListener('change', () => setNavMode(navModeSelect.value));
    }
    // Mount language selector inside general tab
    const langRow = tabGeneral.querySelector('.row:last-child');
    if (langRow) i18n.mountLangSelector(langRow);

    // Theme tab
    const tabTheme = el('section', { class: 'tabpanel', id: 'tab-theme', hidden: '' });
    tabTheme.append(el('h3', {}, [i18n.t('settings.theme')]));
    theme.mountEditor(tabTheme);

    // Config tab
    const tabConfig = el('section', { class: 'tabpanel', id: 'tab-config', hidden: '' }, [
      el('div', { class: 'cfg-toolbar' }, [
        el('button', { class: 'btn', id: 'cfgReload' }, [i18n.t('common.refresh')]),
        el('button', { class: 'btn', id: 'cfgRestore' }, [i18n.t('common.reset')]),
        el('button', { class: 'btn btn-primary', id: 'cfgSave' }, [i18n.t('common.save')]),
      ]),
      el('div', { id: 'configFormHost', class: 'cfg-host' }),
    ]);

    modal.append(tabs, tabGeneral, tabTheme, tabConfig);
    backdrop.appendChild(modal);

    const cfgHost = modal.querySelector('#configFormHost');
    settingsConfig.mountConfig(cfgHost);

    // Tab switching
    tabs.addEventListener('click', (e) => {
      const btn = e.target.closest('.tabbtn');
      if (!btn) return;
      const tabId = btn.dataset.tab;
      tabs.querySelectorAll('.tabbtn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      modal.querySelectorAll('.tabpanel').forEach(p => p.hidden = true);
      const panel = modal.querySelector(`#tab-${tabId}`);
      if (panel) panel.hidden = false;
      if (tabId === 'config') settingsConfig.loadConfig(cfgHost);
      if (tabId === 'theme') {
        theme.disableOverlay();
      } else {
        theme.restoreOverlay();
      }
    });

    // Notifications switch
    const notifSwitch = modal.querySelector('#switchNotifs');
    if (notifSwitch) {
      const notifOn = localStorage.getItem('bjorn.notifs') !== 'off';
      if (notifOn) notifSwitch.classList.add('on');
      notifSwitch.addEventListener('click', () => {
        notifSwitch.classList.toggle('on');
        localStorage.setItem('bjorn.notifs', notifSwitch.classList.contains('on') ? 'on' : 'off');
      });
    }

    // Config actions
    modal.querySelector('#cfgReload')?.addEventListener('click', () => settingsConfig.loadConfig(cfgHost));
    modal.querySelector('#cfgSave')?.addEventListener('click', () => settingsConfig.saveConfig());
    modal.querySelector('#cfgRestore')?.addEventListener('click', () => settingsConfig.restoreDefaults(cfgHost));
  }

  // Store build function for reuse
  backdrop._buildSettings = buildSettings;
}

function toggleSettings() {
  const backdrop = $('#settingsBackdrop');
  if (!backdrop) return;

  const isOpen = backdrop.style.display === 'flex';
  if (isOpen) {
    theme.restoreOverlay();
    backdrop.style.display = 'none';
    backdrop.setAttribute('aria-hidden', 'true');
  } else {
    if (backdrop._buildSettings) backdrop._buildSettings();
    backdrop.style.display = 'flex';
    backdrop.setAttribute('aria-hidden', 'false');
    setTimeout(() => {
      const m = backdrop.querySelector('.modal');
      if (m) m.classList.add('show');
    }, 0);
  }
}

/* Close settings on backdrop click (persistent listener) */
document.addEventListener('click', (e) => {
  const backdrop = $('#settingsBackdrop');
  if (!backdrop || backdrop.style.display !== 'flex') return;
  if (e.target === backdrop) toggleSettings();
});

/* Close settings on Escape (persistent listener) */
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const backdrop = $('#settingsBackdrop');
  if (!backdrop || backdrop.style.display !== 'flex') return;
  toggleSettings();
});

/* =========================================
 * Chip Editor (global singleton)
 * Wires the existing #chipEditBackdrop from HTML
 * ========================================= */

function wireChipEditor() {
  const backdrop = $('#chipEditBackdrop');
  if (!backdrop || window.ChipsEditor) return;

  const title = $('#chipEditTitle');
  const label = $('#chipEditLabel');
  const input = $('#chipEditInput');
  const ta = $('#chipEditTextarea');
  const btnSave = $('#chipEditSave');
  const btnCancel = $('#chipEditCancel');
  const btnClose = $('#chipEditClose');
  if (!input || !ta || !btnSave) return;

  let resolver = null;
  function show() { backdrop.classList.add('show'); requestAnimationFrame(() => (input.offsetParent ? input : ta).focus()); }
  function hide() { backdrop.classList.remove('show'); resolver = null; }
  function currentValue() { return (input.offsetParent ? input.value : ta.value).trim(); }
  function resolve(val) { if (resolver) { resolver(val); hide(); } }
  function save() { resolve(currentValue()); }
  function cancel() { resolve(null); }

  btnSave.addEventListener('click', save);
  btnCancel.addEventListener('click', cancel);
  btnClose.addEventListener('click', cancel);
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) cancel(); });
  document.addEventListener('keydown', (e) => {
    if (!backdrop.classList.contains('show')) return;
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    if (e.key === 'Enter' && e.target.closest('#chipEditBackdrop') && e.target.id !== 'chipEditTextarea') {
      e.preventDefault(); save();
    }
  });

  window.ChipsEditor = {
    open(opts = {}) {
      const { value = '', title: ttl = 'Edit value', label: lab = 'Value', placeholder = '', multiline = false, maxLength, confirmLabel = 'Save' } = opts;
      if (title) title.textContent = ttl;
      if (label) label.textContent = lab;
      if (btnSave) btnSave.textContent = confirmLabel;
      if (multiline) {
        ta.style.display = '';
        input.style.display = 'none';
        ta.value = value;
        ta.placeholder = placeholder;
        ta.removeAttribute('maxlength');
        if (maxLength) ta.setAttribute('maxlength', String(maxLength));
      } else {
        input.style.display = '';
        ta.style.display = 'none';
        input.value = value;
        input.placeholder = placeholder;
        input.removeAttribute('maxlength');
        if (maxLength) input.setAttribute('maxlength', String(maxLength));
      }
      show();
      return new Promise(res => { resolver = res; });
    }
  };
}