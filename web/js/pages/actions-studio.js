import { ResourceTracker } from '../core/resource-tracker.js';
import { el } from '../core/dom.js';
import { t } from '../core/i18n.js';
import { mountStudioRuntime } from './actions-studio-runtime.js';

const PAGE = 'actions-studio';

let tracker = null;
let runtimeCleanup = null;

function studioTemplate() {
  return `
<div id="app">
  <header>
    <div class="logo" aria-hidden="true"></div>
    <h1>${t('studio.title')}</h1>
    <div class="sp"></div>

    <button class="btn icon" id="btnPal" title="${t('studio.openPalette')}" aria-controls="left">&#9776;</button>
    <button class="btn icon" id="btnIns" title="${t('studio.openInspector')}" aria-controls="right">&#9881;</button>
    <button class="btn" id="btnAutoLayout" title="${t('studio.autoLayout')}">&#9889; ${t('studio.autoLayout')}</button>
    <button class="btn" id="btnRepel" title="${t('studio.repel')}">${t('studio.repel')}</button>
    <button class="btn primary" id="btnApply" title="${t('studio.apply')}">${t('studio.apply')}</button>
    <button class="btn" id="btnHelp" title="${t('studio.help')}">${t('studio.help')}</button>

    <div class="kebab">
      <button class="btn icon" id="btnMenu" aria-haspopup="true">&#8942;</button>
      <div class="menu studio-kebab-menu" id="mainMenu" role="menu" aria-label="${t('common.actions')}">
        <div class="item" id="mAddHost" role="menuitem">${t('studio.addHost')}</div>
        <div class="item" id="mAutoLayout" role="menuitem">${t('studio.autoLayout')}</div>
        <div class="item" id="mRepel" role="menuitem">${t('studio.repel')}</div>
        <div class="item" id="mFit" role="menuitem">${t('studio.fitGraph')}</div>
        <div class="item" id="mHelp" role="menuitem">${t('studio.help')}</div>
        <div class="item" id="mSave" role="menuitem">${t('studio.saveToDb')}</div>
        <div class="item" id="mImportdbActions" role="menuitem">${t('studio.importActionsDb')}</div>
        <div class="item" id="mImportdbActionsStudio" role="menuitem">${t('studio.importStudioDb')}</div>
      </div>
    </div>
  </header>

  <main>
    <aside id="left" aria-label="${t('studio.palette')}">
      <div class="studio-sidehead">
        <div class="studio-sidehead-title">${t('studio.palette')}</div>
        <button class="btn icon studio-side-close" id="btnCloseLeft" type="button" aria-label="${t('studio.closePanel')}">&times;</button>
      </div>
      <div class="tabs">
        <div class="tab active" data-tab="actions">${t('studio.actionsTab')}</div>
        <div class="tab" data-tab="hosts">${t('studio.hostsTab')}</div>
      </div>

      <div class="tab-content active" id="tab-actions">
        <div class="search-row">
          <input class="search" id="filterActions" placeholder="${t('studio.filterActions')}">
          <button class="search-clear" id="clearFilterActions" aria-label="${t('common.clear')}">&times;</button>
        </div>
        <div class="palette-meta" id="actionsMeta">
          <span class="pill"><span id="actionsTotalCount">0</span> ${t('studio.total')}</span>
          <span class="pill"><span id="actionsPlacedCount">0</span> ${t('studio.placed')}</span>
        </div>
        <h2>${t('studio.availableActions')}</h2>
        <div id="plist"></div>
      </div>

      <div class="tab-content" id="tab-hosts">
        <div class="search-row">
          <input class="search" id="filterHosts" placeholder="${t('studio.filterHosts')}">
          <button class="search-clear" id="clearFilterHosts" aria-label="${t('common.clear')}">&times;</button>
        </div>
        <div class="palette-meta" id="hostsMeta">
          <span class="pill"><span id="hostsTotalCount">0</span> ${t('studio.total')}</span>
          <span class="pill"><span id="hostsAliveCount">0</span> ${t('studio.alive')}</span>
          <span class="pill"><span id="hostsPlacedCount">0</span> ${t('studio.placed')}</span>
        </div>
        <button class="btn studio-create-host-btn" id="btnCreateHost">${t('studio.createTestHost')}</button>
        <h2>${t('studio.realHosts')}</h2>
        <div id="realHosts"></div>
        <h2>${t('studio.testHosts')}</h2>
        <div id="testHosts"></div>
      </div>
    </aside>

    <section id="center" aria-label="${t('studio.canvas')}">
      <div id="bggrid"></div>
      <div id="canvas" style="transform:translate(0px,0px) scale(1)">
        <svg id="links" width="4000" height="3000" aria-label="Graph links"></svg>
        <div id="nodes" aria-live="polite"></div>
      </div>

      <div id="controls">
        <button class="ctrl" id="zIn" title="Zoom in" aria-label="Zoom in">+</button>
        <button class="ctrl" id="zOut" title="Zoom out" aria-label="Zoom out">-</button>
        <button class="ctrl" id="zFit" title="${t('studio.fitGraph')}" aria-label="${t('studio.fitGraph')}">[]</button>
      </div>

      <div id="canvasHint" class="canvas-hint">
        <strong>${t('studio.tips')}</strong>
        <span>${t('studio.tipsText')}</span>
        <button id="btnHideCanvasHint" class="btn icon" aria-label="${t('common.hide')}">&times;</button>
      </div>
    </section>

    <aside id="right" aria-label="${t('studio.inspector')}">
      <div class="studio-sidehead">
        <div class="studio-sidehead-title">${t('studio.inspector')}</div>
        <button class="btn icon studio-side-close" id="btnCloseRight" type="button" aria-label="${t('studio.closePanel')}">&times;</button>
      </div>
      <div class="section" id="actionInspector">
        <h3>${t('studio.selectedAction')}</h3>
        <div id="noSel" class="small">${t('studio.selectNodeToEdit')}</div>
        <div id="edit" style="display:none">
          <label><span>b_class</span><input id="e_class" disabled></label>
          <div class="form-row">
            <label><span>b_module</span><input id="e_module"></label>
            <label><span>b_status</span><input id="e_status"></label>
          </div>
          <div class="form-row">
            <label><span>${t('common.type')}</span>
              <select id="e_type"><option value="normal">normal</option><option value="global">global</option></select>
            </label>
            <label><span>${t('common.enabled')}</span>
              <select id="e_enabled"><option value="1">${t('common.yes')}</option><option value="0">${t('common.no')}</option></select>
            </label>
          </div>
          <div class="form-row">
            <label><span>${t('sched.priority')}</span><input type="number" id="e_prio" min="1" max="100"></label>
            <label><span>Timeout</span><input type="number" id="e_timeout"></label>
          </div>
          <div class="form-row">
            <label><span>Max retries</span><input type="number" id="e_retry"></label>
            <label><span>Cooldown (s)</span><input type="number" id="e_cool"></label>
          </div>
          <div class="form-row">
            <label><span>Rate limit</span><input id="e_rate" placeholder="3/86400"></label>
            <label><span>${t('common.port')}</span><input type="number" id="e_port" placeholder="22"></label>
          </div>
          <label><span>Services (CSV)</span><input id="e_services" placeholder="ssh, http, https"></label>
          <label><span>Tags JSON</span><input id="e_tags" placeholder='["notif"]'></label>
          <hr>
          <h3>${t('studio.trigger')}</h3>
          <div class="form-row">
            <label><span>${t('common.type')}</span>
              <select id="t_type">
                <option>on_start</option><option>on_new_host</option><option>on_host_alive</option><option>on_host_dead</option>
                <option>on_join</option><option>on_leave</option><option>on_port_change</option><option>on_new_port</option>
                <option>on_service</option><option>on_web_service</option><option>on_success</option><option>on_failure</option>
                <option>on_cred_found</option><option>on_mac_is</option><option>on_essid_is</option><option>on_ip_is</option>
                <option>on_has_cve</option><option>on_has_cpe</option><option>on_all</option><option>on_any</option><option>on_interval</option>
              </select>
            </label>
            <label><span>Parameter</span><input id="t_param" placeholder="port / service / ActionName / JSON list" class="mono-input"></label>
          </div>
          <hr>
          <h3>${t('studio.requirement')}</h3>
          <div class="row">
            <label class="flex-1"><span>${t('studio.mode')}</span>
              <select id="r_mode"><option value="all">ALL (AND)</option><option value="any">ANY (OR)</option></select>
            </label>
            <button class="btn" id="r_add">${t('studio.addCondition')}</button>
          </div>
          <div id="r_list" class="small"></div>
          <div class="row studio-action-btns">
            <button class="btn primary" id="btnUpdateAction">${t('studio.apply')}</button>
            <button class="btn" id="btnDeleteNode">${t('studio.removeFromCanvas')}</button>
          </div>
        </div>
      </div>

      <div class="section" id="hostInspector" style="display:none">
        <h3>${t('studio.selectedHost')}</h3>
        <div class="form-row">
          <label><span>${t('common.mac')}</span><input id="h_mac"></label>
          <label><span>${t('common.hostname')}</span><input id="h_hostname"></label>
        </div>
        <div class="form-row">
          <label><span>${t('common.ip')}(s)</span><input id="h_ips" placeholder="192.168.1.10;192.168.1.11"></label>
          <label><span>${t('common.ports')}</span><input id="h_ports" placeholder="22;80;443"></label>
        </div>
        <div class="form-row">
          <label><span>Alive</span>
            <select id="h_alive"><option value="1">${t('common.yes')}</option><option value="0">${t('common.no')}</option></select>
          </label>
          <label><span>ESSID</span><input id="h_essid"></label>
        </div>
        <label><span>Services (JSON)</span><textarea id="h_services" placeholder='[{"port":22,"service":"ssh"},{"port":80,"service":"http"}]'></textarea></label>
        <label><span>Vulns (CSV)</span><input id="h_vulns" placeholder="CVE-2023-..., CVE-2024-..."></label>
        <label><span>Creds (JSON)</span><textarea id="h_creds" placeholder='[{"service":"ssh","user":"admin","password":"pass"}]'></textarea></label>
        <div class="row studio-action-btns">
          <button class="btn primary" id="btnUpdateHost">${t('studio.apply')}</button>
          <button class="btn" id="btnDeleteHost">${t('studio.deleteFromCanvas')}</button>
        </div>
      </div>
    </aside>

    <button id="sideBackdrop" class="studio-side-backdrop" aria-hidden="true" aria-label="${t('studio.closePanel')}"></button>

    <div id="studioMobileDock" class="studio-mobile-dock" aria-label="Studio mobile controls">
      <button class="btn" id="btnPalDock" aria-controls="left" title="${t('studio.openPalette')}">${t('studio.palette')}</button>
      <button class="btn" id="btnFitDock" title="${t('studio.fitGraph')}">Fit</button>
      <div class="studio-mobile-stats"><span id="nodeCountMini">0</span>N | <span id="linkCountMini">0</span>L</div>
      <button class="btn primary" id="btnApplyDock">${t('studio.apply')}</button>
      <button class="btn" id="btnInsDock" aria-controls="right" title="${t('studio.openInspector')}">Inspect</button>
    </div>
  </main>

  <footer>
    <div class="pill"><span class="legend-dot legend-ok"></span> ${t('studio.success')}</div>
    <div class="pill"><span class="legend-dot legend-bad"></span> ${t('studio.failure')}</div>
    <div class="pill"><span class="legend-dot legend-req"></span> ${t('studio.requires')}</div>
    <div class="pill">${t('studio.pinchHint')}</div>
    <div class="pill"><span id="nodeCount">0</span> ${t('studio.nodesCount')}, <span id="linkCount">0</span> ${t('studio.linksCount')}</div>
  </footer>
</div>

<div class="edge-menu" id="edgeMenu">
  <div class="edge-menu-item" data-action="edit">${t('common.edit')}...</div>
  <div class="edge-menu-item" data-action="toggle-success">${t('studio.success')}</div>
  <div class="edge-menu-item" data-action="toggle-failure">${t('studio.failure')}</div>
  <div class="edge-menu-item" data-action="toggle-req">${t('studio.requires')}</div>
  <div class="edge-menu-item danger" data-action="delete">${t('common.delete')}</div>
</div>

<div class="modal" id="linkWizard" aria-hidden="true" aria-labelledby="linkWizardTitle" role="dialog">
  <div class="modal-content">
    <div class="modal-header">
      <h2 class="modal-title" id="linkWizardTitle">${t('studio.link')}</h2>
      <button class="modal-close" id="lwClose" aria-label="${t('common.close')}">x</button>
    </div>
    <div class="modal-body">
      <div class="row studio-link-endpoints">
        <div class="pill">${t('studio.from')}: <b id="lwFromName">-</b></div>
        <div class="pill">${t('studio.to')}: <b id="lwToName">-</b></div>
      </div>
      <p class="small" id="lwContext">${t('studio.linkContext')}</p>
      <hr>
      <div class="form-row">
        <label><span>${t('studio.mode')}</span>
          <select id="lwMode"><option value="trigger">${t('studio.trigger')}</option><option value="requires">${t('studio.requirement')}</option></select>
        </label>
        <label><span>${t('studio.preset')}</span><select id="lwPreset"></select></label>
      </div>
      <div class="form-row" id="lwParamsRow">
        <label><span>${t('studio.param1')}</span><input id="lwParam1" placeholder="ssh / 22 / CVE-..."></label>
        <label><span>${t('studio.param2')}</span><input id="lwParam2" placeholder="optional"></label>
      </div>
      <div class="section studio-preview-row">
        <div class="row"><div class="pill">${t('studio.preview')}:</div><code id="lwPreview">-</code></div>
      </div>
      <div class="row studio-wizard-btns">
        <button class="btn primary" id="lwCreate">${t('studio.validate')}</button>
        <button class="btn" id="lwCancel">${t('common.cancel')}</button>
      </div>
    </div>
  </div>
</div>

<div class="modal" id="hostModal" aria-hidden="true" aria-labelledby="hostModalTitle" role="dialog">
  <div class="modal-content">
    <div class="modal-header">
      <h2 class="modal-title" id="hostModalTitle">${t('studio.addTestHost')}</h2>
      <button class="modal-close" onclick="closeHostModal()" aria-label="${t('common.close')}">x</button>
    </div>
    <div class="modal-body">
      <label><span>${t('common.mac')}</span><input id="new_mac" placeholder="AA:BB:CC:DD:EE:FF"></label>
      <label><span>${t('common.hostname')}</span><input id="new_hostname" placeholder="test-server-01"></label>
      <label><span>${t('common.ip')}(s)</span><input id="new_ips" placeholder="192.168.1.100;192.168.1.101"></label>
      <label><span>${t('common.ports')}</span><input id="new_ports" placeholder="22;80;443;3306"></label>
      <label><span>Services (JSON)</span>
        <textarea id="new_services" placeholder='[{"port":22,"service":"ssh"},{"port":80,"service":"http"}]'>[{"port":22,"service":"ssh"}]</textarea>
      </label>
      <label><span>Vulns (CSV)</span><input id="new_vulns" placeholder="CVE-2023-1234, CVE-2024-5678"></label>
      <label><span>Creds (JSON)</span>
        <textarea id="new_creds" placeholder='[{"service":"ssh","user":"admin","password":"password"}]'>[]</textarea>
      </label>
      <label><span>Alive</span>
        <select id="new_alive"><option value="1">${t('common.yes')}</option><option value="0">${t('common.no')}</option></select>
      </label>
      <div class="row studio-wizard-btns">
        <button class="btn primary" onclick="createTestHost()">${t('studio.createTestHost')}</button>
        <button class="btn" onclick="closeHostModal()">${t('common.cancel')}</button>
      </div>
    </div>
  </div>
</div>

<div class="modal" id="helpModal" aria-hidden="true" aria-labelledby="helpModalTitle" role="dialog">
  <div class="modal-content">
    <div class="modal-header">
      <h2 class="modal-title" id="helpModalTitle">${t('studio.shortcuts')}</h2>
      <button class="modal-close" id="helpClose" aria-label="${t('common.close')}">x</button>
    </div>
    <div class="modal-body">
      <div class="section">
        <h3>${t('studio.navigation')}</h3>
        <div class="small">${t('studio.shortcutZoom')}</div>
        <div class="small">${t('studio.shortcutPan')}</div>
        <div class="small">${t('studio.shortcutDragNode')}</div>
      </div>
      <div class="section">
        <h3>${t('studio.keyboard')}</h3>
        <div class="small"><b>F</b>: ${t('studio.shortcutFit')}</div>
        <div class="small"><b>Ctrl/Cmd + S</b>: ${t('studio.shortcutSave')}</div>
        <div class="small"><b>Esc</b>: ${t('studio.shortcutEsc')}</div>
        <div class="small"><b>Delete</b>: ${t('studio.shortcutDelete')}</div>
      </div>
    </div>
  </div>
</div>
`;
}

export function mount(container) {
  tracker = new ResourceTracker(PAGE);

  const root = el('div', { class: 'studio-container studio-runtime-host' }, [
    el('div', { class: 'studio-loading' }, [t('common.loading')]),
  ]);
  container.appendChild(root);

  try {
    root.innerHTML = studioTemplate();
    runtimeCleanup = mountStudioRuntime(root);
  } catch (err) {
    root.innerHTML = '';
    root.appendChild(el('div', { class: 'card', style: 'margin:12px;padding:12px' }, [
      el('h3', {}, [t('nav.actionsStudio')]),
      el('p', {}, [`Failed to initialize studio: ${err.message}`]),
    ]));
  }
}

export function unmount() {
  if (typeof runtimeCleanup === 'function') {
    try { runtimeCleanup(); } catch { /* noop */ }
  }
  runtimeCleanup = null;

  if (tracker) {
    tracker.cleanupAll();
    tracker = null;
  }
}
