/**
 * Network page module.
 * Table view + D3 force-directed map with zoom/drag, search, label toggle.
 * Endpoint /network_data returns HTML, parsed client-side.
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'network';
const L = (key, fallback, vars = {}) => {
  const v = t(key, vars);
  return v === key ? fallback : v;
};
const ICONS = {
  bjorn: '/web/images/boat.png',
  host_active: '/web/images/target.png',
  host_empty: '/web/images/target2.png',
  loot: '/web/images/treasure.png',
  gateway: '/web/images/lighthouse.png',
};

/* ── state ── */
let tracker = null;
let poller = null;
let networkData = [];
let viewMode = 'table';
let showLabels = true;
let searchTerm = '';
let searchDebounce = null;
let currentSortState = { column: -1, direction: 'asc' };
let prevNetworkFingerprint = '';
let stickyLevel = 0; /* 0=off, 1=col1, 2=col1+2, 3=col1+2 (max for 2-col table) */

/* D3 state */
let d3Module = null;
let simulation = null;
let svg = null;
let g = null;
let nodeGroup = null;
let linkGroup = null;
let labelsGroup = null;
let globalNodes = [];
let globalLinks = [];
let currentZoomScale = 1;
let mapInitialized = false;

/* ── prefs ── */
const getPref = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
const setPref = (k, v) => { try { localStorage.setItem(k, v); } catch { /* noop */ } };

/* ── lifecycle ── */
export async function mount(container) {
  tracker = new ResourceTracker(PAGE);

  viewMode = getPref('nv:view', 'table');
  if (!['table', 'map'].includes(viewMode)) viewMode = 'table';
  showLabels = getPref('nv:showHostname', 'true') === 'true';
  const savedSearch = getPref('nv:search', '');
  if (savedSearch) searchTerm = savedSearch.toLowerCase();

  container.appendChild(buildShell(savedSearch));
  syncViewUI();
  syncClearBtn();

  await refresh();
  poller = new Poller(refresh, 5000);
  poller.start();
}

export function unmount() {
  clearTimeout(searchDebounce);
  searchDebounce = null;
  if (poller) { poller.stop(); poller = null; }
  if (simulation) { simulation.stop(); simulation = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  networkData = [];
  globalNodes = [];
  globalLinks = [];
  mapInitialized = false;
  d3Module = null;
  svg = null;
  g = null;
  nodeGroup = null;
  linkGroup = null;
  labelsGroup = null;
  currentSortState = { column: -1, direction: 'asc' };
  searchTerm = '';
  prevNetworkFingerprint = '';
}

/* ── data fetch ── */
async function refresh() {
  try {
    const html = await api.get('/network_data', { timeout: 8000 });
    if (typeof html !== 'string' || !tracker) return;
    const parsed = parseNetworkHTML(html);
    /* Skip DOM rebuild when data unchanged */
    const fp = parsed.map(r => `${r.hostname}|${r.ip}|${r.mac}|${(r.ports||[]).join(',')}`).join(';');
    if (fp === prevNetworkFingerprint) return;
    prevNetworkFingerprint = fp;
    networkData = parsed;
    renderTable();
    applySearchToTable();
    if (mapInitialized && simulation) updateMapFromData(networkData);
  } catch (err) {
    console.warn(`[${PAGE}]`, err.message);
  }
}

/* ── parse HTML response ── */
function parseNetworkHTML(htmlStr) {
  const tmp = document.createElement('div');
  tmp.innerHTML = htmlStr;
  const table = tmp.querySelector('table');
  if (!table) return [];
  const rows = Array.from(table.querySelectorAll('tr')).slice(1);
  return rows.map(tr => {
    const cells = Array.from(tr.querySelectorAll('td'));
    if (cells.length < 6) return null;
    const essid = (cells[0]?.textContent || '').trim();
    const ip = (cells[1]?.textContent || '').trim();
    const hostname = (cells[2]?.textContent || '').trim();
    const mac = (cells[3]?.textContent || '').trim();
    const vendor = (cells[4]?.textContent || '').trim();
    const portsStr = (cells[5]?.textContent || '').trim();
    const ports = portsStr.split(';').map(p => p.trim()).filter(p => p && p.toLowerCase() !== 'none');
    return { essid, ip, hostname, mac, vendor, ports };
  }).filter(Boolean);
}

/* ── shell ── */
function buildShell(savedSearch) {
  return el('div', { class: 'network-container' }, [
    el('div', { class: 'ocean-container' }, [
      el('div', { class: 'ocean-surface' }),
      el('div', { class: 'ocean-caustics' }),
    ]),
    el('div', { class: 'nv-toolbar-wrap' }, [
      el('div', { class: 'nv-toolbar' }, [
        el('div', { class: 'nv-search' }, [
          el('span', { class: 'nv-search-icon', 'aria-hidden': 'true' }, ['\u{1F50D}']),
          el('input', {
            type: 'text', id: 'searchInput', placeholder: t('common.search'),
            value: savedSearch || '', oninput: onSearchInput
          }),
          el('button', {
            class: 'nv-search-clear', id: 'nv-searchClear', type: 'button',
            'aria-label': t('common.clear'), onclick: clearSearch
          }, ['\u2715']),
        ]),
        el('div', { class: 'segmented', id: 'viewSeg' }, [
          el('button', { 'data-view': 'table', onclick: () => setView('table') }, [L('common.table', 'Table')]),
          el('button', { 'data-view': 'map', onclick: () => setView('map') }, [L('common.map', 'Map')]),
        ]),
        el('label', {
          class: 'nv-switch', id: 'hostSwitch',
          'data-on': String(showLabels),
          style: viewMode === 'map' ? '' : 'display:none'
        }, [
          el('input', {
            type: 'checkbox', id: 'toggleHostname',
            ...(showLabels ? { checked: '' } : {}),
            onchange: (e) => toggleLabels(e.target.checked)
          }),
          el('span', {}, [L('network.showHostname', 'Show hostname')]),
          el('span', { class: 'track' }, [el('span', { class: 'thumb' })]),
        ]),
      ]),
    ]),
    el('div', { id: 'table-wrap', class: 'table-wrap' }, [
      el('div', { id: 'network-table' }),
    ]),
    el('div', { id: 'visualization-container', style: 'display:none' }),
    el('div', { id: 'd3-tooltip', class: 'd3-tooltip' }),
  ]);
}

/* ── search ── */
function onSearchInput(e) {
  clearTimeout(searchDebounce);
  searchDebounce = tracker
    ? tracker.trackTimeout(() => {
        searchTerm = e.target.value.trim().toLowerCase();
        setPref('nv:search', searchTerm);
        applySearchToTable();
        applySearchToMap();
        syncClearBtn();
      }, 120)
    : setTimeout(() => {
        searchTerm = e.target.value.trim().toLowerCase();
        setPref('nv:search', searchTerm);
        applySearchToTable();
        applySearchToMap();
        syncClearBtn();
      }, 120);
}

function clearSearch() {
  const inp = $('#searchInput');
  if (inp) { inp.value = ''; inp.focus(); }
  searchTerm = '';
  setPref('nv:search', '');
  applySearchToTable();
  applySearchToMap();
  syncClearBtn();
}

function syncClearBtn() {
  const btn = $('#nv-searchClear');
  if (btn) btn.style.display = searchTerm ? '' : 'none';
}

function applySearchToTable() {
  const table = document.querySelector('#network-table table');
  if (!table) return;
  const rows = Array.from(table.querySelectorAll('tbody tr'));
  rows.forEach(tr => {
    tr.style.display = !searchTerm || tr.textContent.toLowerCase().includes(searchTerm) ? '' : 'none';
  });
}

function applySearchToMap() {
  if (!d3Module || !nodeGroup) return;
  nodeGroup.selectAll('.node').style('opacity', d => {
    if (!searchTerm) return 1;
    const bag = `${d.label} ${d.ip || ''} ${d.vendor || ''}`.toLowerCase();
    return bag.includes(searchTerm) ? 1 : 0.1;
  });
}

/* ── view ── */
function setView(mode) {
  if (!['table', 'map'].includes(mode)) return;
  viewMode = mode;
  setPref('nv:view', mode);
  syncViewUI();
  if (mode === 'map' && !mapInitialized) initMap();
}

function syncViewUI() {
  const root = $('.network-container');
  const tableWrap = $('#table-wrap');
  const mapContainer = $('#visualization-container');
  const hostSwitch = $('#hostSwitch');
  if (root) {
    root.classList.toggle('is-table-view', viewMode === 'table');
    root.classList.toggle('is-map-view', viewMode === 'map');
  }
  if (tableWrap) tableWrap.style.display = viewMode === 'table' ? 'block' : 'none';
  if (mapContainer) mapContainer.style.display = viewMode === 'map' ? 'block' : 'none';
  if (hostSwitch) hostSwitch.style.display = viewMode === 'map' ? 'inline-flex' : 'none';
  $$('#viewSeg button').forEach(b => {
    b.setAttribute('aria-pressed', String(b.dataset.view === viewMode));
  });
}

/* ── labels ── */
function toggleLabels(on) {
  showLabels = on;
  setPref('nv:showHostname', String(on));
  const sw = $('#hostSwitch');
  if (sw) sw.dataset.on = String(on);
  if (labelsGroup) labelsGroup.style('opacity', showLabels ? 1 : 0);
}

/* ── table rendering ── */
function renderTable() {
  const wrap = $('#network-table');
  if (!wrap) return;
  empty(wrap);

  if (networkData.length === 0) {
    wrap.appendChild(el('div', { class: 'network-empty' }, [t('common.noData')]));
    return;
  }

  const pinBtn = el('button', {
    class: 'nv-pin-btn',
    title: L('network.toggleSticky', 'Pin columns'),
    onclick: () => cycleStickyLevel(),
  }, ['\uD83D\uDCCC']);
  if (stickyLevel > 0) pinBtn.classList.add('active');

  const thead = el('thead', {}, [
    el('tr', {}, [
      el('th', { class: 'hosts-header' }, [
        L('common.hosts', 'Hosts'),
        el('span', { style: 'display:inline-flex;margin-left:6px;vertical-align:middle' }, [pinBtn]),
      ]),
      el('th', { class: 'ports-header' }, [L('common.ports', 'Ports')]),
    ]),
  ]);

  const rows = networkData.map(item => {
    const hostBubbles = [];
    if (item.ip) hostBubbles.push(el('span', { class: 'bubble ip-address' }, [item.ip]));
    if (item.hostname) hostBubbles.push(el('span', { class: 'bubble hostname' }, [item.hostname]));
    if (item.mac) hostBubbles.push(el('span', { class: 'bubble mac-address' }, [item.mac]));
    if (item.vendor) hostBubbles.push(el('span', { class: 'bubble vendor' }, [item.vendor]));
    if (item.essid) hostBubbles.push(el('span', { class: 'bubble essid' }, [item.essid]));
    if (hostBubbles.length === 0) hostBubbles.push(el('span', { class: 'bubble bubble-empty' }, [t('network.unknownHost')]));

    const portBubbles = item.ports.length
      ? item.ports.map(p => el('span', { class: 'port-bubble' }, [p]))
      : [el('span', { class: 'port-bubble is-empty' }, [L('common.none', 'None')])];

    return el('tr', {}, [
      el('td', { class: 'hosts-cell' }, [el('div', { class: 'hosts-content' }, hostBubbles)]),
      el('td', { class: 'ports-cell' }, [el('div', { class: 'ports-container' }, portBubbles)]),
    ]);
  });

  const table = el('table', { class: 'network-table' }, [thead, el('tbody', {}, rows)]);
  wrap.appendChild(el('div', { class: 'table-inner' }, [table]));
  applyStickyClasses();

  /* table sort */
  initTableSorting(table);
  applyCurrentSort(table);
}

function cycleStickyLevel() {
  stickyLevel = (stickyLevel + 1) % 3; /* 0 → 1 → 2 → 0 */
  applyStickyClasses();
}

function applyStickyClasses() {
  const table = document.querySelector('#network-table table');
  if (!table) return;
  const headers = table.querySelectorAll('thead th');
  const rows = table.querySelectorAll('tbody tr');

  /* update pin button state */
  const pinBtn = table.querySelector('.nv-pin-btn');
  if (pinBtn) {
    pinBtn.classList.toggle('active', stickyLevel > 0);
    pinBtn.textContent = stickyLevel > 0 ? `\uD83D\uDCCC${stickyLevel}` : '\uD83D\uDCCC';
  }

  /* reset all sticky */
  headers.forEach(th => { th.classList.remove('nv-sticky-col'); th.style.left = ''; });
  rows.forEach(tr => {
    tr.querySelectorAll('td').forEach(td => { td.classList.remove('nv-sticky-col'); td.style.left = ''; });
  });

  if (stickyLevel === 0) return;

  /* measure column widths */
  const firstRow = table.querySelector('tbody tr');
  if (!firstRow) return;
  const cells = firstRow.querySelectorAll('td');
  let leftOffset = 0;

  for (let col = 0; col < Math.min(stickyLevel, headers.length); col++) {
    const th = headers[col];
    const w = cells[col] ? cells[col].offsetWidth : th.offsetWidth;
    th.classList.add('nv-sticky-col');
    th.style.left = leftOffset + 'px';
    rows.forEach(tr => {
      const td = tr.children[col];
      if (td) { td.classList.add('nv-sticky-col'); td.style.left = leftOffset + 'px'; }
    });
    leftOffset += w;
  }
}

function initTableSorting(table) {
  const headers = Array.from(table.querySelectorAll('th'));
  headers.forEach((h, idx) => {
    h.style.cursor = 'pointer';
    h.addEventListener('click', () => {
      headers.forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
      if (currentSortState.column === idx) {
        currentSortState.direction = currentSortState.direction === 'asc' ? 'desc' : 'asc';
      } else {
        currentSortState.column = idx;
        currentSortState.direction = 'asc';
      }
      h.classList.add(`sort-${currentSortState.direction}`);
      sortTable(table, idx, currentSortState.direction);
    });
  });
}

function applyCurrentSort(table) {
  if (!table) return;
  if (currentSortState.column < 0) return;
  const headers = Array.from(table.querySelectorAll('th'));
  headers.forEach(x => x.classList.remove('sort-asc', 'sort-desc'));
  const active = headers[currentSortState.column];
  if (active) active.classList.add(`sort-${currentSortState.direction}`);
  sortTable(table, currentSortState.column, currentSortState.direction);
}

function sortTable(table, colIndex, direction) {
  const tbody = table.querySelector('tbody');
  if (!tbody) return;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    const A = a.querySelectorAll('td')[colIndex]?.textContent.trim().toLowerCase() || '';
    const B = b.querySelectorAll('td')[colIndex]?.textContent.trim().toLowerCase() || '';
    return direction === 'asc' ? A.localeCompare(B) : B.localeCompare(A);
  });
  rows.forEach(r => tbody.appendChild(r));
}

/* ── D3 Map ── */
async function initMap() {
  const container = $('#visualization-container');
  if (!container) return;

  /* lazy load d3 from local static file (CSP-safe) */
  if (!d3Module) {
    try {
      d3Module = window.d3 || null;
      if (!d3Module) {
        await loadScriptOnce('/web/js/d3.v7.min.js');
        d3Module = window.d3 || null;
      }
      if (!d3Module) throw new Error('window.d3 unavailable');
    } catch (e) {
      console.warn('[network] D3 not available:', e.message);
      container.appendChild(el('div', { class: 'network-empty' }, [t('network.d3Unavailable')]));
      return;
    }
  }
  const d3 = d3Module;

  /* Force a layout recalc so clientWidth/clientHeight are up to date */
  void container.offsetHeight;

  const width = container.clientWidth || 800;
  const height = container.clientHeight || 600;
  console.debug('[network] Map init: container', width, 'x', height);

  svg = d3.select(container).append('svg')
    .attr('width', width).attr('height', height)
    .style('width', '100%').style('height', '100%');

  /* click background to hide tooltip */
  svg.on('click', () => {
    const tt = $('#d3-tooltip');
    if (tt) tt.style.opacity = '0';
  });

  g = svg.append('g');

  /* layers */
  g.append('g').attr('class', 'sonar-layer');
  linkGroup = g.append('g').attr('class', 'links-layer');
  nodeGroup = g.append('g').attr('class', 'nodes-layer');
  labelsGroup = g.append('g').attr('class', 'labels-layer node-labels');

  /* zoom */
  const zoom = d3.zoom().scaleExtent([0.2, 6]).on('zoom', (e) => {
    g.attr('transform', e.transform);
    currentZoomScale = e.transform.k;
    requestAnimationFrame(() =>
      labelsGroup.selectAll('.label-group')
        .attr('transform', d => `translate(${d.x},${d.y + d.r + 15}) scale(${1 / currentZoomScale})`)
    );
  });
  svg.call(zoom);

  /* physics */
  simulation = d3.forceSimulation()
    .force('link', d3.forceLink().id(d => d.id).distance(d => d.target?.type === 'loot' ? 30 : 80))
    .force('charge', d3.forceManyBody().strength(d => d.type === 'host_empty' ? -300 : -100))
    .force('collide', d3.forceCollide().radius(d => d.r * 1.5).iterations(2))
    .force('x', d3.forceX(width / 2).strength(0.08))
    .force('y', d3.forceY(height / 2).strength(0.08))
    .alphaMin(0.05)
    .velocityDecay(0.6)
    .on('tick', ticked);

  tracker.trackEventListener(window, 'resize', () => {
    if (viewMode !== 'map') return;
    const w = container.clientWidth;
    const h = container.clientHeight;
    svg.attr('width', w).attr('height', h);
    simulation.force('x', d3.forceX(w / 2).strength(0.08));
    simulation.force('y', d3.forceY(h / 2).strength(0.08));
    simulation.alpha(0.3).restart();
  });

  mapInitialized = true;
  if (networkData.length > 0) updateMapFromData(networkData);
}

function loadScriptOnce(src) {
  const existing = document.querySelector(`script[data-src="${src}"]`);
  if (existing) {
    if (existing.dataset.loaded === '1') return Promise.resolve();
    return new Promise((resolve, reject) => {
      existing.addEventListener('load', () => resolve(), { once: true });
      existing.addEventListener('error', () => reject(new Error('Script load failed')), { once: true });
    });
  }
  return new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = src;
    s.async = true;
    s.dataset.src = src;
    s.addEventListener('load', () => {
      s.dataset.loaded = '1';
      resolve();
    }, { once: true });
    s.addEventListener('error', () => reject(new Error(`Script load failed: ${src}`)), { once: true });
    document.head.appendChild(s);
  });
}

function updateMapFromData(data) {
  if (!d3Module || !simulation) return;

  const incomingNodes = new Map();
  const incomingLinks = [];

  incomingNodes.set('bjorn', { id: 'bjorn', type: 'bjorn', r: 50, label: 'BJORN' });

  data.forEach(h => {
    if (!h?.ip) return;
    const hasPorts = h.ports && h.ports.length > 0;
    const isGateway = h.ip.endsWith('.1') || h.ip.endsWith('.254');
    const type = isGateway ? 'gateway' : (hasPorts ? 'host_active' : 'host_empty');
    const radius = isGateway ? 40 : (hasPorts ? 30 : 20);

    incomingNodes.set(h.ip, {
      id: h.ip, type, ip: h.ip, label: h.hostname || h.ip,
      vendor: h.vendor, r: radius, ports: h.ports,
    });

    if (hasPorts) {
      h.ports.forEach(p => {
        const portId = `${h.ip}_${p}`;
        incomingNodes.set(portId, { id: portId, type: 'loot', label: p, r: 15, parent: h.ip });
        incomingLinks.push({ source: h.ip, target: portId });
      });
    }
  });

  /* reconcile */
  const nextNodes = [];
  let hasStructuralChanges = globalNodes.length !== incomingNodes.size;

  incomingNodes.forEach((data, id) => {
    const existing = globalNodes.find(n => n.id === id);
    if (existing) {
      if (existing.type !== data.type) hasStructuralChanges = true;
      Object.assign(existing, data);
      nextNodes.push(existing);
    } else {
      hasStructuralChanges = true;
      const w = parseInt(svg.attr('width')) || 800;
      const h = parseInt(svg.attr('height')) || 600;
      data.x = w / 2 + (Math.random() - 0.5) * 50;
      data.y = h / 2 + (Math.random() - 0.5) * 50;
      nextNodes.push(data);
    }
  });

  globalNodes = nextNodes;
  globalLinks = incomingLinks.map(l => ({ source: l.source, target: l.target }));
  updateViz(hasStructuralChanges);
}

function updateViz(restartSim) {
  const d3 = d3Module;

  /* nodes */
  const node = nodeGroup.selectAll('.node').data(globalNodes, d => d.id);
  const nodeEnter = node.enter().append('g').attr('class', 'node')
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

  nodeEnter.append('g').attr('class', 'foam-container');
  nodeEnter.append('image').attr('class', 'node-icon')
    .on('error', function () { d3.select(this).style('display', 'none'); });

  const nodeUpdate = nodeEnter.merge(node);
  nodeUpdate.attr('class', d => `node ${d.type === 'host_empty' ? 'empty' : ''}`);
  nodeUpdate.select('.node-icon')
    .attr('xlink:href', d => ICONS[d.type] || ICONS.host_empty)
    .attr('x', d => -d.r).attr('y', d => -d.r)
    .attr('width', d => d.r * 2).attr('height', d => d.r * 2)
    .style('display', 'block');

  nodeUpdate.select('.foam-container').each(function (d) {
    if (!['bjorn', 'gateway', 'host_active'].includes(d.type)) {
      d3.select(this).selectAll('*').remove();
      return;
    }
    if (d3.select(this).selectAll('circle').empty()) {
      const c = d3.select(this);
      [1, 2].forEach(i => c.append('circle').attr('class', 'foam-ring').attr('r', d.r * (1 + i * 0.15)));
    }
  });

  nodeUpdate.on('click', (e, d) => showTooltip(e, d));
  node.exit().transition().duration(500).style('opacity', 0).remove();

  /* links */
  const link = linkGroup.selectAll('.link').data(globalLinks, d =>
    (d.source.id || d.source) + '-' + (d.target.id || d.target));
  link.enter().append('line').attr('class', 'link');
  link.exit().remove();

  /* labels */
  const labelData = globalNodes.filter(d => ['bjorn', 'gateway', 'host_active', 'loot'].includes(d.type));
  const label = labelsGroup.selectAll('.label-group').data(labelData, d => d.id);
  const labelEnter = label.enter().append('g').attr('class', 'label-group');
  labelEnter.append('rect').attr('class', 'label-bg').attr('height', 16);
  labelEnter.append('text').attr('class', 'label-text').attr('text-anchor', 'middle').attr('y', 11);

  const labelUpdate = labelEnter.merge(label);
  labelUpdate.select('text').text(d => d.label).each(function () {
    const w = this.getBBox().width;
    d3.select(this.parentNode).select('rect').attr('x', -w / 2 - 4).attr('width', w + 8);
  });
  label.exit().remove();

  labelsGroup.style('opacity', showLabels ? 1 : 0);

  simulation.nodes(globalNodes);
  simulation.force('link').links(globalLinks);
  if (restartSim) simulation.alpha(0.3).restart();
}

function ticked() {
  linkGroup.selectAll('.link')
    .attr('x1', d => d.source.x).attr('y1', d => d.source.y)
    .attr('x2', d => d.target.x).attr('y2', d => d.target.y);

  nodeGroup.selectAll('.node')
    .attr('transform', d => `translate(${d.x},${d.y})`);

  labelsGroup.selectAll('.label-group')
    .attr('transform', d => `translate(${d.x},${d.y + d.r + 15}) scale(${1 / currentZoomScale})`);

  /* sonar on bjorn */
  const bjorn = globalNodes.find(n => n.type === 'bjorn');
  if (bjorn && g) {
    let sonar = g.select('.sonar-layer').selectAll('.sonar-wave').data([bjorn]);
    sonar.enter().append('circle').attr('class', 'sonar-wave')
      .merge(sonar).attr('cx', d => d.x).attr('cy', d => d.y);
  }
}

function showTooltip(e, d) {
  e.stopPropagation();
  const tt = $('#d3-tooltip');
  if (!tt) return;
  empty(tt);
  if (d.type === 'loot') {
    tt.appendChild(el('div', {}, [`\u{1F4B0} Port ${d.label}`]));
  } else {
    tt.appendChild(el('div', { style: 'color:var(--acid);font-weight:bold;margin-bottom:5px' }, [d.label]));
    if (d.ip && d.ip !== d.label) tt.appendChild(el('div', {}, [d.ip]));
    if (d.vendor) tt.appendChild(el('div', { style: 'opacity:0.8;font-size:0.8em' }, [d.vendor]));
  }
  tt.style.left = (e.pageX + 10) + 'px';
  tt.style.top = (e.pageY - 50) + 'px';
  tt.style.opacity = '1';
}
