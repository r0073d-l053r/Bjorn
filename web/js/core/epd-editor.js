/**
 * EPD Layout Editor — visual drag-and-drop layout editor for e-paper displays.
 *
 * Features: drag/resize elements, grid/snap, display modes (Color/NB/BN),
 * add/delete elements, import/export JSON, undo, font size editing,
 * real icon previews, live EPD preview, rotation, invert,
 * and multiple EPD types.
 */
import { el, toast, empty } from './dom.js';
import { api } from './api.js';
import { t as i18n } from './i18n.js';

/* ── Helpers ─────────────────────────────────────────────── */
const L = (k, v) => i18n(k, v);
const Lx = (k, fb) => { const o = i18n(k); return o && o !== k ? o : fb; };
const SVG_NS = 'http://www.w3.org/2000/svg';
const XLINK_NS = 'http://www.w3.org/1999/xlink';
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const snapVal = (v, g) => g > 1 ? Math.round(v / g) * g : v;
const deepClone = (o) => JSON.parse(JSON.stringify(o));
const isLine = (name) => name.startsWith('line_');

/* ── Icon name → BMP filename mapping ────────────────────── */
const ICON_FILES = {
  wifi_icon: 'wifi.bmp',
  bt_icon: 'bluetooth.bmp',
  usb_icon: 'usb.bmp',
  eth_icon: 'ethernet.bmp',
  battery_icon: '100.bmp',
  status_image: 'bjorn1.bmp',
  main_character: 'bjorn1.bmp',
  frise: 'frise.bmp',
  // Stats row icons (used inside stats_row representative content)
  _stat_target: 'target.bmp',
  _stat_port: 'port.bmp',
  _stat_vuln: 'vuln.bmp',
  _stat_cred: 'cred.bmp',
  _stat_zombie: 'zombie.bmp',
  _stat_data: 'data.bmp',
};

/* ── Element type → color mapping ────────────────────────── */
const TYPE_COLORS = {
  icon:      { fill: 'rgba(66,133,244,0.22)',  stroke: '#4285f4' },
  text:      { fill: 'rgba(52,168,83,0.22)',   stroke: '#34a853' },
  bar:       { fill: 'rgba(251,188,4,0.22)',   stroke: '#fbbc04' },
  character: { fill: 'rgba(156,39,176,0.22)',  stroke: '#9c27b0' },
  area:      { fill: 'rgba(255,87,34,0.18)',   stroke: '#ff5722' },
  line:      { fill: 'none',                    stroke: '#ea4335' },
  default:   { fill: 'rgba(158,158,158,0.16)', stroke: '#9e9e9e' },
};

function guessType(name) {
  if (isLine(name)) return 'line';
  if (/icon|bt_|wifi|usb|eth|battery/.test(name)) return 'icon';
  if (/text|title|status_line|ip_/.test(name)) return 'text';
  if (/bar|progress|histogram/.test(name)) return 'bar';
  if (/character|frise/.test(name)) return 'character';
  if (/area|comment|lvl|box|row|count|network/.test(name)) return 'area';
  return 'default';
}

function colorFor(name, displayMode) {
  const type = guessType(name);
  if (displayMode === 'nb') return { fill: 'rgba(30,30,30,0.22)', stroke: '#222' };
  if (displayMode === 'bn') return { fill: 'rgba(220,220,220,0.22)', stroke: '#ccc' };
  return TYPE_COLORS[type] || TYPE_COLORS.default;
}

/* ── State ───────────────────────────────────────────────── */
let _tracker = null;
let _sidebarEl = null;
let _mainEl = null;
let _svg = null;
let _layout = null;
let _originalLayout = null;
let _layouts = null;
let _selectedKey = null;
let _zoom = 2;
let _gridSize = 10;
let _snapEnabled = true;
let _labelsVisible = true;
let _displayMode = 'color';   // 'color' | 'nb' | 'bn'
let _rotation = 0;            // 0, 90, 180, 270
let _invertColors = false;
let _undoStack = [];
let _dragging = null;
let _mounted = false;
let _activated = false;
let _iconCache = new Map();    // name → dataURL
let _liveTimer = null;

/* ── Public API ──────────────────────────────────────────── */
export function mount(tracker) {
  _tracker = tracker;
  _mounted = true;
  _activated = false;
}

export function unmount() {
  stopLivePreview();
  _selectedKey = null;
  _dragging = null;
  _layout = null;
  _originalLayout = null;
  _layouts = null;
  _undoStack = [];
  _svg = null;
  _sidebarEl = null;
  _mainEl = null;
  _mounted = false;
  _activated = false;
  _iconCache.clear();
}

export async function activate(sidebarEl, mainEl) {
  _sidebarEl = sidebarEl;
  _mainEl = mainEl;
  // Ensure focusable for arrow key navigation
  if (_mainEl && !_mainEl.getAttribute('tabindex')) _mainEl.setAttribute('tabindex', '0');
  if (_activated && _layout) {
    renderAll();
    startLivePreview();
    return;
  }
  _activated = true;
  await loadFromServer();
  preloadIcons();
  startLivePreview();
}

/* ── Icon Preloading ─────────────────────────────────────── */
function preloadIcons() {
  for (const [elemName, filename] of Object.entries(ICON_FILES)) {
    if (_iconCache.has(elemName)) continue;
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => {
      // Convert to data URL via canvas for SVG embedding
      const c = document.createElement('canvas');
      c.width = img.naturalWidth;
      c.height = img.naturalHeight;
      const ctx = c.getContext('2d');
      ctx.drawImage(img, 0, 0);
      try {
        _iconCache.set(elemName, c.toDataURL('image/png'));
        // Re-render to show icons once loaded
        if (_svg && _layout) renderAll();
      } catch { /* CORS fallback: just skip icon preview */ }
    };
    img.src = `/static_images/${filename}`;
  }
}

/* ── Live EPD Preview ────────────────────────────────────── */
function startLivePreview() {
  stopLivePreview();
  _liveTimer = setInterval(() => {
    const img = _mainEl?.querySelector?.('.epd-live-img');
    if (img) img.src = `/web/screen.png?t=${Date.now()}`;
  }, 4000);
}

function stopLivePreview() {
  if (_liveTimer) { clearInterval(_liveTimer); _liveTimer = null; }
}

/* ── Server IO ───────────────────────────────────────────── */
async function loadFromServer(epdType) {
  try {
    const [layoutsRes, layoutRes] = await Promise.all([
      api.get('/api/epd/layouts', { timeout: 10000, retries: 0 }),
      api.get(epdType ? `/api/epd/layout?epd_type=${epdType}` : '/api/epd/layout', { timeout: 10000, retries: 0 }),
    ]);
    _layouts = layoutsRes;
    _layout = deepClone(layoutRes);
    _originalLayout = deepClone(layoutRes);
    _undoStack = [];
    _selectedKey = null;
    renderAll();
  } catch (err) {
    toast(`EPD Layout: ${err.message}`, 3000, 'error');
  }
}

async function saveToServer() {
  if (!_layout) return;
  try {
    await api.post('/api/epd/layout', _layout, { timeout: 15000, retries: 0 });
    _originalLayout = deepClone(_layout);
    toast(Lx('epd.saved', 'Layout saved'), 2000, 'success');
  } catch (err) {
    toast(`Save failed: ${err.message}`, 3000, 'error');
  }
}

async function resetToDefault() {
  if (!confirm(Lx('epd.confirmReset', 'Reset layout to built-in defaults?'))) return;
  try {
    await api.post('/api/epd/layout/reset', {}, { timeout: 15000, retries: 0 });
    await loadFromServer();
    toast(Lx('epd.reset', 'Layout reset to defaults'), 2000, 'success');
  } catch (err) {
    toast(`Reset failed: ${err.message}`, 3000, 'error');
  }
}

/* ── Undo ────────────────────────────────────────────────── */
function pushUndo() {
  if (!_layout) return;
  _undoStack.push(deepClone(_layout));
  if (_undoStack.length > 50) _undoStack.shift();
}

function undo() {
  if (!_undoStack.length) return;
  _layout = _undoStack.pop();
  renderAll();
}

/* ── Render All ──────────────────────────────────────────── */
function renderAll() {
  if (!_sidebarEl || !_mainEl || !_layout) return;
  renderMain();
  renderSidebar();
}

/* ── Main Area ───────────────────────────────────────────── */
function renderMain() {
  empty(_mainEl);
  const meta = _layout.meta || {};
  const W = meta.ref_width || 122;
  const H = meta.ref_height || 250;

  // Toolbar
  _mainEl.appendChild(buildToolbar());

  // Content row: canvas + live preview side by side
  const contentRow = el('div', { class: 'epd-content-row' });

  // Canvas wrapper — NO explicit width/height on wrapper, let SVG size it
  const wrapper = el('div', { class: `epd-canvas-wrapper mode-${_displayMode}${_invertColors ? ' inverted' : ''}` });

  // SVG
  const isRotated = _rotation === 90 || _rotation === 270;
  const svgW = isRotated ? H : W;
  const svgH = isRotated ? W : H;

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${svgW} ${svgH}`);
  svg.setAttribute('width', String(svgW * _zoom));
  svg.setAttribute('height', String(svgH * _zoom));
  svg.style.display = 'block';
  _svg = svg;

  // Rotation transform group
  const rotG = document.createElementNS(SVG_NS, 'g');
  if (_rotation === 90) rotG.setAttribute('transform', `rotate(90 ${svgW / 2} ${svgH / 2}) translate(${(svgW - svgH) / 2} ${(svgH - svgW) / 2})`);
  else if (_rotation === 180) rotG.setAttribute('transform', `rotate(180 ${W / 2} ${H / 2})`);
  else if (_rotation === 270) rotG.setAttribute('transform', `rotate(270 ${svgW / 2} ${svgH / 2}) translate(${(svgW - svgH) / 2} ${(svgH - svgW) / 2})`);

  // Background rect
  let bgFill = '#fff';
  if (_displayMode === 'bn') bgFill = '#111';
  if (_invertColors) bgFill = bgFill === '#fff' ? '#111' : '#fff';

  const bgRect = document.createElementNS(SVG_NS, 'rect');
  bgRect.setAttribute('width', String(W));
  bgRect.setAttribute('height', String(H));
  bgRect.setAttribute('fill', bgFill);
  rotG.appendChild(bgRect);

  // Grid
  if (_gridSize > 1) {
    const gridG = document.createElementNS(SVG_NS, 'g');
    const isDark = (_displayMode === 'bn') !== _invertColors;
    const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.06)';
    for (let x = _gridSize; x < W; x += _gridSize) {
      const l = document.createElementNS(SVG_NS, 'line');
      l.setAttribute('x1', String(x)); l.setAttribute('y1', '0');
      l.setAttribute('x2', String(x)); l.setAttribute('y2', String(H));
      l.setAttribute('stroke', gridColor); l.setAttribute('stroke-width', '0.3');
      gridG.appendChild(l);
    }
    for (let y = _gridSize; y < H; y += _gridSize) {
      const l = document.createElementNS(SVG_NS, 'line');
      l.setAttribute('x1', '0'); l.setAttribute('y1', String(y));
      l.setAttribute('x2', String(W)); l.setAttribute('y2', String(y));
      l.setAttribute('stroke', gridColor); l.setAttribute('stroke-width', '0.3');
      gridG.appendChild(l);
    }
    rotG.appendChild(gridG);
  }

  // Elements — sorted: lines behind, then largest area first
  const elements = _layout.elements || {};
  const sortedKeys = Object.keys(elements).sort((a, b) => {
    if (isLine(a) && !isLine(b)) return -1;
    if (!isLine(a) && isLine(b)) return 1;
    const aA = (elements[a].w || W) * (elements[a].h || 1);
    const bA = (elements[b].w || W) * (elements[b].h || 1);
    return bA - aA;
  });

  const elemsG = document.createElementNS(SVG_NS, 'g');
  for (const key of sortedKeys) {
    elemsG.appendChild(createSvgElement(key, elements[key], W, H));
  }
  rotG.appendChild(elemsG);

  // Resize handles (on top, only for selected non-line element)
  if (_selectedKey && !isLine(_selectedKey) && elements[_selectedKey]) {
    const e = elements[_selectedKey];
    const handlesG = document.createElementNS(SVG_NS, 'g');
    const hs = 2.5;
    const corners = [
      { cx: e.x, cy: e.y, cursor: 'nw-resize', corner: 'nw' },
      { cx: e.x + (e.w || 0), cy: e.y, cursor: 'ne-resize', corner: 'ne' },
      { cx: e.x, cy: e.y + (e.h || 0), cursor: 'sw-resize', corner: 'sw' },
      { cx: e.x + (e.w || 0), cy: e.y + (e.h || 0), cursor: 'se-resize', corner: 'se' },
    ];
    for (const c of corners) {
      const r = document.createElementNS(SVG_NS, 'rect');
      r.setAttribute('x', String(c.cx - hs));
      r.setAttribute('y', String(c.cy - hs));
      r.setAttribute('width', String(hs * 2));
      r.setAttribute('height', String(hs * 2));
      r.setAttribute('fill', '#fff');
      r.setAttribute('stroke', '#4285f4');
      r.setAttribute('stroke-width', '0.8');
      r.setAttribute('data-handle', c.corner);
      r.setAttribute('data-key', _selectedKey);
      r.style.cursor = c.cursor;
      handlesG.appendChild(r);
    }
    rotG.appendChild(handlesG);
  }

  svg.appendChild(rotG);
  wrapper.appendChild(svg);
  contentRow.appendChild(wrapper);

  // Live EPD preview panel
  const livePanel = el('div', { class: 'epd-live-panel' });
  livePanel.appendChild(el('h4', { style: 'margin:0 0 8px;text-align:center' }, ['Live EPD']));
  const liveImg = el('img', {
    class: 'epd-live-img',
    src: `/web/screen.png?t=${Date.now()}`,
    alt: 'Live EPD',
  });
  liveImg.onerror = () => { liveImg.style.opacity = '0.3'; };
  liveImg.onload = () => { liveImg.style.opacity = '1'; };
  livePanel.appendChild(liveImg);
  livePanel.appendChild(el('p', { style: 'text-align:center;font-size:11px;opacity:.5;margin:4px 0 0' }, [
    `${W}x${H}px — refreshes every 4s`
  ]));
  contentRow.appendChild(livePanel);

  _mainEl.appendChild(contentRow);

  // Bind pointer events on SVG
  bindCanvasEvents(svg, W, H);
}

/* ── Representative content for preview ──────────────────── */
const PREVIEW_TEXT = {
  title:        'BJORN',
  ip_text:      '192.168.x.x',
  status_line1: 'IDLE',
  status_line2: 'Ready',
  lvl_box:      'LVL\n20',
  network_kb:   'M\n0',
  attacks_count: 'X\n0',
};

/* Stats row: 6 icons at hardcoded x offsets inside the row bounds */
const STATS_ICONS = ['target', 'port', 'vuln', 'cred', 'zombie', 'data'];
const STATS_X_OFFSETS = [0, 20, 40, 60, 80, 100]; // ref-space offsets from stats_row.x

function svgText(x, y, text, fontSize, fill, opts = {}) {
  const t = document.createElementNS(SVG_NS, 'text');
  t.setAttribute('x', String(x));
  t.setAttribute('y', String(y));
  t.setAttribute('font-size', String(fontSize));
  t.setAttribute('fill', fill);
  t.setAttribute('pointer-events', 'none');
  t.setAttribute('font-family', opts.font || 'monospace');
  if (opts.anchor) t.setAttribute('text-anchor', opts.anchor);
  if (opts.weight) t.setAttribute('font-weight', opts.weight);
  t.textContent = text;
  return t;
}

function addRepresentativeContent(g, key, x, y, w, h, isDark) {
  const textFill = isDark ? '#ccc' : '#222';
  const mutedFill = isDark ? '#888' : '#999';

  // Title — large centered text "BJORN"
  if (key === 'title') {
    const fs = Math.min(h * 0.75, 10);
    g.appendChild(svgText(x + w / 2, y + h * 0.78, 'BJORN', fs, textFill, { anchor: 'middle', weight: 'bold', font: 'sans-serif' }));
    return;
  }

  // Stats row — 6 stat icons with count text below each
  if (key === 'stats_row') {
    const iconSize = Math.min(h * 0.6, 12);
    const statNames = ['target', 'port', 'vuln', 'cred', 'zombie', 'data'];
    for (let i = 0; i < 6; i++) {
      const ox = x + STATS_X_OFFSETS[i] * (w / 118);
      // Try to show actual stat icon
      const statUrl = _iconCache.get(`_stat_${statNames[i]}`);
      if (statUrl) {
        const img = document.createElementNS(SVG_NS, 'image');
        img.setAttributeNS(XLINK_NS, 'href', statUrl);
        img.setAttribute('x', String(ox));
        img.setAttribute('y', String(y + 1));
        img.setAttribute('width', String(iconSize));
        img.setAttribute('height', String(iconSize));
        img.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        img.setAttribute('pointer-events', 'none');
        if (_invertColors) img.setAttribute('filter', 'invert(1)');
        g.appendChild(img);
      } else {
        // Fallback: mini box placeholder
        const sr = document.createElementNS(SVG_NS, 'rect');
        sr.setAttribute('x', String(ox));
        sr.setAttribute('y', String(y + 1));
        sr.setAttribute('width', String(iconSize));
        sr.setAttribute('height', String(iconSize));
        sr.setAttribute('fill', isDark ? 'rgba(200,200,200,0.15)' : 'rgba(0,0,0,0.08)');
        sr.setAttribute('stroke', mutedFill);
        sr.setAttribute('stroke-width', '0.3');
        sr.setAttribute('rx', '0.5');
        sr.setAttribute('pointer-events', 'none');
        g.appendChild(sr);
      }
      // Count text below icon
      g.appendChild(svgText(ox + iconSize / 2, y + iconSize + 5, '0', 3, mutedFill, { anchor: 'middle' }));
    }
    return;
  }

  // IP text
  if (key === 'ip_text') {
    const fs = Math.min(h * 0.7, 6);
    g.appendChild(svgText(x + 1, y + fs + 0.5, '192.168.x.x', fs, textFill));
    return;
  }

  // Status lines
  if (key === 'status_line1') {
    const fs = Math.min(h * 0.7, 6);
    g.appendChild(svgText(x + 1, y + fs + 0.5, 'IDLE', fs, textFill, { weight: 'bold' }));
    return;
  }
  if (key === 'status_line2') {
    const fs = Math.min(h * 0.7, 5);
    g.appendChild(svgText(x + 1, y + fs + 0.5, 'Ready', fs, mutedFill));
    return;
  }

  // Progress bar — filled portion
  if (key === 'progress_bar') {
    const fill = document.createElementNS(SVG_NS, 'rect');
    fill.setAttribute('x', String(x));
    fill.setAttribute('y', String(y));
    fill.setAttribute('width', String(w * 0.65));
    fill.setAttribute('height', String(h));
    fill.setAttribute('fill', isDark ? 'rgba(200,200,200,0.3)' : 'rgba(0,0,0,0.15)');
    fill.setAttribute('pointer-events', 'none');
    fill.setAttribute('rx', '0.5');
    g.appendChild(fill);
    return;
  }

  // Comment area — multiline text preview
  if (key === 'comment_area') {
    const fs = 4;
    const lines = ['Feeling like a', 'cyber-sleuth in', "\'Sneakers\'."];
    for (let i = 0; i < lines.length; i++) {
      g.appendChild(svgText(x + 2, y + 6 + i * (fs + 1.5), lines[i], fs, mutedFill, { font: 'sans-serif' }));
    }
    return;
  }

  // LVL box — label + number
  if (key === 'lvl_box') {
    const fs = Math.min(w * 0.35, 5);
    g.appendChild(svgText(x + w / 2, y + fs + 1, 'LvL', fs, mutedFill, { anchor: 'middle', font: 'sans-serif' }));
    g.appendChild(svgText(x + w / 2, y + h * 0.8, '20', fs * 1.1, textFill, { anchor: 'middle', weight: 'bold', font: 'sans-serif' }));
    return;
  }

  // Network KB
  if (key === 'network_kb') {
    const fs = Math.min(w * 0.35, 5);
    g.appendChild(svgText(x + w / 2, y + fs + 1, 'M', fs, mutedFill, { anchor: 'middle', font: 'sans-serif' }));
    g.appendChild(svgText(x + w / 2, y + h * 0.8, '0', fs * 1.1, textFill, { anchor: 'middle', weight: 'bold', font: 'sans-serif' }));
    return;
  }

  // Attacks count
  if (key === 'attacks_count') {
    const fs = Math.min(w * 0.35, 5);
    g.appendChild(svgText(x + w / 2, y + fs + 1, 'X', fs, mutedFill, { anchor: 'middle', font: 'sans-serif' }));
    g.appendChild(svgText(x + w / 2, y + h * 0.8, '29', fs * 1.1, textFill, { anchor: 'middle', weight: 'bold', font: 'sans-serif' }));
    return;
  }

  // CPU / Memory histograms — simple bar preview
  if (key === 'cpu_histogram' || key === 'mem_histogram') {
    const label = key === 'cpu_histogram' ? 'C' : 'M';
    const barH = h * 0.6;
    const bar = document.createElementNS(SVG_NS, 'rect');
    bar.setAttribute('x', String(x));
    bar.setAttribute('y', String(y + h - barH));
    bar.setAttribute('width', String(w));
    bar.setAttribute('height', String(barH));
    bar.setAttribute('fill', isDark ? 'rgba(200,200,200,0.2)' : 'rgba(0,0,0,0.1)');
    bar.setAttribute('pointer-events', 'none');
    g.appendChild(bar);
    g.appendChild(svgText(x + w / 2, y + h + 4, label, 3, mutedFill, { anchor: 'middle' }));
    return;
  }

  // Main character — note: display.py auto-centers at bottom,
  // layout rect is a bounding hint only
  if (key === 'main_character' && !_iconCache.has(key)) {
    const fs = 3;
    g.appendChild(svgText(x + w / 2, y + h / 2 - 2, '\u2699 auto-placed', fs, mutedFill, { anchor: 'middle', font: 'sans-serif' }));
    g.appendChild(svgText(x + w / 2, y + h / 2 + 3, 'by renderer', fs, mutedFill, { anchor: 'middle', font: 'sans-serif' }));
    return;
  }
}

function createSvgElement(key, elem, W, H) {
  const colors = colorFor(key, _displayMode);
  const selected = key === _selectedKey;
  const isDark = (_displayMode === 'bn') !== _invertColors;

  if (isLine(key)) {
    const g = document.createElementNS(SVG_NS, 'g');
    g.setAttribute('data-key', key);
    g.style.cursor = 'ns-resize';
    const y = elem.y || 0;
    // Hit area
    const hitLine = document.createElementNS(SVG_NS, 'line');
    hitLine.setAttribute('x1', '0'); hitLine.setAttribute('y1', String(y));
    hitLine.setAttribute('x2', String(W)); hitLine.setAttribute('y2', String(y));
    hitLine.setAttribute('stroke', 'transparent'); hitLine.setAttribute('stroke-width', '6');
    g.appendChild(hitLine);
    // Visible line
    const visLine = document.createElementNS(SVG_NS, 'line');
    visLine.setAttribute('x1', '0'); visLine.setAttribute('y1', String(y));
    visLine.setAttribute('x2', String(W)); visLine.setAttribute('y2', String(y));
    visLine.setAttribute('stroke', selected ? '#4285f4' : colors.stroke);
    visLine.setAttribute('stroke-width', selected ? '1.5' : '0.8');
    visLine.setAttribute('stroke-dasharray', selected ? '4,2' : '3,3');
    g.appendChild(visLine);
    // Label
    if (_labelsVisible) {
      const txt = document.createElementNS(SVG_NS, 'text');
      txt.setAttribute('x', '2');
      txt.setAttribute('y', String(y - 1.5));
      txt.setAttribute('font-size', '3.5');
      txt.setAttribute('fill', isDark ? '#aaa' : '#666');
      txt.setAttribute('pointer-events', 'none');
      txt.textContent = key.replace('line_', '');
      g.appendChild(txt);
    }
    return g;
  }

  // Rectangle element
  const g = document.createElementNS(SVG_NS, 'g');
  g.setAttribute('data-key', key);
  g.style.cursor = 'move';

  const x = elem.x || 0;
  const y = elem.y || 0;
  const w = elem.w || 10;
  const h = elem.h || 10;

  const r = document.createElementNS(SVG_NS, 'rect');
  r.setAttribute('x', String(x));
  r.setAttribute('y', String(y));
  r.setAttribute('width', String(w));
  r.setAttribute('height', String(h));
  r.setAttribute('fill', colors.fill);
  r.setAttribute('stroke', selected ? '#4285f4' : colors.stroke);
  r.setAttribute('stroke-width', selected ? '1.2' : '0.5');
  r.setAttribute('rx', '0.5');
  if (selected) {
    r.setAttribute('stroke-dasharray', '3,1');
  }
  g.appendChild(r);

  // Icon image overlay (if available)
  const iconUrl = _iconCache.get(key);
  if (iconUrl) {
    const img = document.createElementNS(SVG_NS, 'image');
    img.setAttributeNS(XLINK_NS, 'href', iconUrl);
    img.setAttribute('x', String(x));
    img.setAttribute('y', String(y));
    img.setAttribute('width', String(w));
    img.setAttribute('height', String(h));
    img.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    img.setAttribute('pointer-events', 'none');
    if (_invertColors) img.setAttribute('filter', 'invert(1)');
    g.appendChild(img);
  }

  // Representative content preview (text, bars, stat icons)
  addRepresentativeContent(g, key, x, y, w, h, isDark);

  // Label (name badge — top-left corner, small)
  if (_labelsVisible) {
    const fontSize = Math.min(3, Math.max(1.8, h * 0.2));
    const txt = document.createElementNS(SVG_NS, 'text');
    txt.setAttribute('x', String(x + 1));
    txt.setAttribute('y', String(y + fontSize + 0.3));
    txt.setAttribute('font-size', fontSize.toFixed(1));
    txt.setAttribute('fill', isDark ? 'rgba(180,180,180,0.6)' : 'rgba(60,60,60,0.5)');
    txt.setAttribute('pointer-events', 'none');
    txt.setAttribute('font-family', 'monospace');
    txt.textContent = key;
    g.appendChild(txt);
  }

  return g;
}

/* ── Toolbar ─────────────────────────────────────────────── */
function buildToolbar() {
  const bar = el('div', { class: 'epd-editor-toolbar' });

  // Row 1
  const row1 = el('div', { class: 'epd-toolbar-row' });

  // EPD Type selector
  const epdSelect = el('select', { class: 'select', title: 'EPD Type' });
  if (_layouts?.layouts) {
    const currentType = _layouts.current_epd_type || 'epd2in13_V4';
    for (const [epdType, info] of Object.entries(_layouts.layouts)) {
      const opt = el('option', { value: epdType }, [
        `${epdType} (${info.meta?.ref_width || '?'}x${info.meta?.ref_height || '?'})`
      ]);
      if (epdType === currentType) opt.selected = true;
      epdSelect.appendChild(opt);
    }
  }
  epdSelect.addEventListener('change', async () => {
    pushUndo();
    await loadFromServer(epdSelect.value);
  });
  row1.appendChild(epdSelect);

  // Display mode selector
  const modeSelect = el('select', { class: 'select', title: 'Display Mode' });
  [['color', 'Color'], ['nb', 'NB (Black/White)'], ['bn', 'BN (White/Black)']].forEach(([val, label]) => {
    const opt = el('option', { value: val }, [label]);
    if (val === _displayMode) opt.selected = true;
    modeSelect.appendChild(opt);
  });
  modeSelect.addEventListener('change', () => {
    _displayMode = modeSelect.value;
    renderAll();
  });
  row1.appendChild(modeSelect);

  // Rotation selector
  const rotSelect = el('select', { class: 'select', title: 'Rotation' });
  [[0, '0\u00b0'], [90, '90\u00b0'], [180, '180\u00b0'], [270, '270\u00b0']].forEach(([val, label]) => {
    const opt = el('option', { value: String(val) }, [label]);
    if (val === _rotation) opt.selected = true;
    rotSelect.appendChild(opt);
  });
  rotSelect.addEventListener('change', () => {
    _rotation = parseInt(rotSelect.value) || 0;
    renderAll();
  });
  row1.appendChild(rotSelect);

  // Invert toggle
  const invertBtn = el('button', {
    class: `btn${_invertColors ? ' active' : ''}`,
    type: 'button', title: 'Invert Colors',
  }, ['Invert']);
  invertBtn.addEventListener('click', () => {
    _invertColors = !_invertColors;
    invertBtn.classList.toggle('active', _invertColors);
    renderAll();
  });
  row1.appendChild(invertBtn);

  // Zoom
  const zoomWrap = el('span', { class: 'epd-zoom-wrap' });
  const zoomLabel = el('span', { class: 'epd-zoom-label' }, [`${Math.round(_zoom * 100)}%`]);
  const zoomRange = el('input', {
    type: 'range', class: 'range epd-zoom-range',
    min: '1', max: '6', step: '0.5', value: String(_zoom),
  });
  zoomRange.addEventListener('input', () => {
    _zoom = parseFloat(zoomRange.value) || 2;
    zoomLabel.textContent = `${Math.round(_zoom * 100)}%`;
    renderAll();
  });
  zoomWrap.append(el('span', {}, ['Zoom:']), zoomRange, zoomLabel);
  row1.appendChild(zoomWrap);

  bar.appendChild(row1);

  // Row 2
  const row2 = el('div', { class: 'epd-toolbar-row' });

  // Grid size
  const gridSelect = el('select', { class: 'select', title: 'Grid Size' });
  [0, 5, 10, 15, 20].forEach(g => {
    const opt = el('option', { value: String(g) }, [g === 0 ? 'No grid' : `${g}px`]);
    if (g === _gridSize) opt.selected = true;
    gridSelect.appendChild(opt);
  });
  gridSelect.addEventListener('change', () => {
    _gridSize = parseInt(gridSelect.value) || 0;
    renderAll();
  });
  row2.appendChild(gridSelect);

  // Snap
  const snapBtn = el('button', {
    class: `btn${_snapEnabled ? ' active' : ''}`, type: 'button',
  }, [_snapEnabled ? 'Snap ON' : 'Snap OFF']);
  snapBtn.addEventListener('click', () => {
    _snapEnabled = !_snapEnabled;
    snapBtn.textContent = _snapEnabled ? 'Snap ON' : 'Snap OFF';
    snapBtn.classList.toggle('active', _snapEnabled);
  });
  row2.appendChild(snapBtn);

  // Labels
  const labelsBtn = el('button', {
    class: `btn${_labelsVisible ? ' active' : ''}`, type: 'button',
  }, [_labelsVisible ? 'Labels ON' : 'Labels OFF']);
  labelsBtn.addEventListener('click', () => {
    _labelsVisible = !_labelsVisible;
    labelsBtn.textContent = _labelsVisible ? 'Labels ON' : 'Labels OFF';
    labelsBtn.classList.toggle('active', _labelsVisible);
    renderAll();
  });
  row2.appendChild(labelsBtn);

  // Undo
  row2.appendChild(mkBtn('Undo', undo, 'Undo (Ctrl+Z)'));

  // Add element
  row2.appendChild(mkBtn('+ Add', showAddModal, 'Add Element'));

  // Import / Export
  row2.appendChild(mkBtn('Import', importLayout, 'Import Layout JSON'));
  row2.appendChild(mkBtn('Export', exportLayout, 'Export Layout JSON'));

  // Save
  const saveBtn = mkBtn('Save', saveToServer, 'Save to Device');
  saveBtn.style.fontWeight = '800';
  row2.appendChild(saveBtn);

  // Reset
  const resetBtn = el('button', { class: 'btn danger', type: 'button', title: 'Reset to Defaults' }, ['Reset']);
  resetBtn.addEventListener('click', resetToDefault);
  row2.appendChild(resetBtn);

  bar.appendChild(row2);
  return bar;
}

function mkBtn(text, onClick, title = '') {
  const b = el('button', { class: 'btn', type: 'button', title }, [text]);
  b.addEventListener('click', onClick);
  return b;
}

/* ── Sidebar ─────────────────────────────────────────────── */
function renderSidebar() {
  if (!_sidebarEl || !_layout) return;
  empty(_sidebarEl);

  // Properties panel
  const propsPanel = el('div', { class: 'epd-props-panel' });
  if (_selectedKey && _layout.elements?.[_selectedKey]) {
    const elem = _layout.elements[_selectedKey];
    const isL = isLine(_selectedKey);

    propsPanel.appendChild(el('h4', { style: 'margin:0 0 8px' }, [_selectedKey]));

    const makeHandler = (prop, minVal) => (v) => {
      pushUndo();
      _layout.elements[_selectedKey][prop] = minVal != null ? Math.max(minVal, v) : v;
      renderAll();
    };

    if (isL) {
      propsPanel.appendChild(propRow('Y', elem.y || 0, makeHandler('y')));
    } else {
      propsPanel.appendChild(propRow('X', elem.x || 0, makeHandler('x')));
      propsPanel.appendChild(propRow('Y', elem.y || 0, makeHandler('y')));
      propsPanel.appendChild(propRow('W', elem.w || 0, makeHandler('w', 4)));
      propsPanel.appendChild(propRow('H', elem.h || 0, makeHandler('h', 4)));
    }

    const delBtn = el('button', { class: 'btn danger epd-delete-btn', type: 'button' }, ['Delete Element']);
    delBtn.addEventListener('click', () => {
      if (!confirm(`Delete "${_selectedKey}"?`)) return;
      pushUndo();
      delete _layout.elements[_selectedKey];
      _selectedKey = null;
      renderAll();
    });
    propsPanel.appendChild(delBtn);
  } else {
    propsPanel.appendChild(el('p', { class: 'epd-hint' }, ['Click an element on the canvas']));
  }
  _sidebarEl.appendChild(propsPanel);

  // Elements list
  const listSection = el('div', { class: 'epd-elements-list' });
  listSection.appendChild(el('h4', { style: 'margin:8px 0 4px' }, ['Elements']));

  const elements = _layout.elements || {};
  const rects = Object.keys(elements).filter(k => !isLine(k)).sort();
  const lines = Object.keys(elements).filter(k => isLine(k)).sort();

  const ul = el('ul', { class: 'unified-list' });
  for (const key of rects) {
    const e = elements[key];
    ul.appendChild(makeElementListItem(key, e, false));
  }
  if (lines.length) {
    ul.appendChild(el('li', { class: 'epd-list-divider' }, ['Lines']));
    for (const key of lines) {
      ul.appendChild(makeElementListItem(key, elements[key], true));
    }
  }
  listSection.appendChild(ul);
  _sidebarEl.appendChild(listSection);

  // Fonts section
  const fonts = _layout.fonts;
  if (fonts && Object.keys(fonts).length) {
    const fontsSection = el('div', { class: 'epd-fonts-section' });
    fontsSection.appendChild(el('h4', { style: 'margin:12px 0 4px' }, ['Font Sizes']));
    for (const [fk, fv] of Object.entries(fonts)) {
      fontsSection.appendChild(propRow(fk, fv, (v) => {
        pushUndo();
        _layout.fonts[fk] = Math.max(4, v);
        renderSidebar();
      }));
    }
    _sidebarEl.appendChild(fontsSection);
  }

  // Meta info
  const meta = _layout.meta || {};
  _sidebarEl.appendChild(el('p', { style: 'margin:12px 0 2px;opacity:.5;font-size:11px' }, [
    `${meta.name || '?'} \u2014 ${meta.ref_width || '?'}\u00d7${meta.ref_height || '?'}px`
  ]));
}

function makeElementListItem(key, e, isL) {
  const li = el('li', {
    class: `card epd-element-item${key === _selectedKey ? ' selected' : ''}`,
  });
  if (isL) {
    li.append(
      el('span', { class: 'epd-line-dash' }, ['\u2500\u2500']),
      el('span', { style: 'flex:1;font-weight:700' }, [key]),
      el('span', { class: 'epd-coords' }, [`y=${e.y}`]),
    );
  } else {
    // Show icon thumbnail if available
    const iconUrl = _iconCache.get(key);
    if (iconUrl) {
      const thumb = el('img', { src: iconUrl, class: 'epd-list-icon' });
      li.appendChild(thumb);
    } else {
      li.appendChild(el('span', {
        class: 'epd-type-dot',
        style: `background:${(TYPE_COLORS[guessType(key)] || TYPE_COLORS.default).stroke}`
      }));
    }
    li.append(
      el('span', { style: 'flex:1;font-weight:700' }, [key]),
      el('span', { class: 'epd-coords' }, [`(${e.x},${e.y})`]),
    );
  }
  li.addEventListener('click', () => { _selectedKey = key; renderAll(); _mainEl?.focus(); });
  return li;
}

function propRow(label, value, onChange) {
  const row = el('div', { class: 'epd-prop-row' });
  const lbl = el('label', {}, [label]);
  const inp = el('input', {
    type: 'number', class: 'input epd-prop-input',
    value: String(value), step: '1',
  });
  inp.addEventListener('change', () => {
    const v = parseInt(inp.value);
    if (Number.isFinite(v)) onChange(v);
  });
  row.append(lbl, inp);
  return row;
}

/* ── Canvas Events (Drag & Drop) ─────────────────────────── */
function bindCanvasEvents(svg, W, H) {
  const toRef = (clientX, clientY) => {
    const rect = svg.getBoundingClientRect();
    const rawX = (clientX - rect.left) / _zoom;
    const rawY = (clientY - rect.top) / _zoom;
    // Account for rotation
    if (_rotation === 90) return { x: rawY, y: W - rawX };
    if (_rotation === 180) return { x: W - rawX, y: H - rawY };
    if (_rotation === 270) return { x: H - rawY, y: rawX };
    return { x: rawX, y: rawY };
  };

  svg.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return;
    const pt = toRef(ev.clientX, ev.clientY);

    // Resize handle hit
    const handleEl = ev.target.closest('[data-handle]');
    if (handleEl && handleEl.dataset.key) {
      const key = handleEl.dataset.key;
      const elem = _layout.elements?.[key];
      if (!elem) return;
      pushUndo();
      _dragging = { key, corner: handleEl.dataset.handle, type: 'resize', startElem: { ...elem } };
      _selectedKey = key;
      svg.setPointerCapture(ev.pointerId);
      ev.preventDefault();
      renderSidebar();
      return;
    }

    // Element hit
    const gEl = ev.target.closest('[data-key]');
    if (gEl && gEl.dataset.key) {
      const key = gEl.dataset.key;
      const elem = _layout.elements?.[key];
      if (!elem) return;
      pushUndo();
      _selectedKey = key;
      _dragging = {
        key, type: 'move',
        offsetX: isLine(key) ? 0 : pt.x - (elem.x || 0),
        offsetY: pt.y - (elem.y || 0),
      };
      svg.setPointerCapture(ev.pointerId);
      ev.preventDefault();
      renderSidebar();
      return;
    }

    // Deselect — keep focus on main for arrow keys
    _selectedKey = null;
    _mainEl?.focus();
    renderAll();
  });

  svg.addEventListener('pointermove', (ev) => {
    if (!_dragging || !_layout) return;
    const pt = toRef(ev.clientX, ev.clientY);
    const key = _dragging.key;
    const elem = _layout.elements[key];
    if (!elem) return;
    const g = _snapEnabled ? _gridSize : 0;

    if (_dragging.type === 'move') {
      if (isLine(key)) {
        elem.y = clamp(snapVal(pt.y - _dragging.offsetY, g), 0, H);
      } else {
        elem.x = clamp(snapVal(pt.x - _dragging.offsetX, g), 0, W - (elem.w || 1));
        elem.y = clamp(snapVal(pt.y - _dragging.offsetY, g), 0, H - (elem.h || 1));
      }
    } else if (_dragging.type === 'resize') {
      const se = _dragging.startElem;
      const corner = _dragging.corner;
      let nx = se.x, ny = se.y, nw = se.w, nh = se.h;
      if (corner.includes('e')) nw = Math.max(4, snapVal(pt.x - se.x, g));
      if (corner.includes('w')) { const newX = snapVal(pt.x, g); nw = Math.max(4, se.x + se.w - newX); nx = se.x + se.w - nw; }
      if (corner.includes('s')) nh = Math.max(4, snapVal(pt.y - se.y, g));
      if (corner.includes('n')) { const newY = snapVal(pt.y, g); nh = Math.max(4, se.y + se.h - newY); ny = se.y + se.h - nh; }
      elem.x = clamp(nx, 0, W - 4);
      elem.y = clamp(ny, 0, H - 4);
      elem.w = Math.min(nw, W - elem.x);
      elem.h = Math.min(nh, H - elem.y);
    }

    updateSvgElement(key, elem, W, H);
    updateHandles(key, elem);
    renderSidebar();
  });

  svg.addEventListener('pointerup', (ev) => {
    if (_dragging) {
      svg.releasePointerCapture(ev.pointerId);
      _dragging = null;
      renderAll();
      // Focus main for keyboard navigation
      _mainEl?.focus();
    }
  });

  // Keyboard
  if (!_mainEl._kbBound) {
    _mainEl._kbBound = true;
    _mainEl.setAttribute('tabindex', '0');
    _mainEl.addEventListener('keydown', (ev) => {
      if ((ev.ctrlKey || ev.metaKey) && ev.key === 'z') { ev.preventDefault(); undo(); return; }
      if (!_selectedKey || !_layout?.elements?.[_selectedKey]) return;
      const step = _snapEnabled && _gridSize > 1 ? _gridSize : 1;
      const m = _layout.meta || {};
      const mW = m.ref_width || 122, mH = m.ref_height || 250;
      const elem = _layout.elements[_selectedKey];
      let moved = false;
      if (ev.key === 'ArrowLeft')  { pushUndo(); elem.x = Math.max(0, (elem.x || 0) - step); moved = true; }
      if (ev.key === 'ArrowRight') { pushUndo(); elem.x = Math.min(mW - (elem.w || 1), (elem.x || 0) + step); moved = true; }
      if (ev.key === 'ArrowUp')    { pushUndo(); elem.y = Math.max(0, (elem.y || 0) - step); moved = true; }
      if (ev.key === 'ArrowDown')  { pushUndo(); elem.y = Math.min(mH - (elem.h || 1), (elem.y || 0) + step); moved = true; }
      if (ev.key === 'Delete' || ev.key === 'Backspace') {
        if (ev.target.tagName === 'INPUT') return; // don't interfere with input fields
        if (confirm(`Delete "${_selectedKey}"?`)) { pushUndo(); delete _layout.elements[_selectedKey]; _selectedKey = null; moved = true; }
      }
      if (moved) { ev.preventDefault(); renderAll(); }
    });
  }
}

/* ── Live SVG Updates ────────────────────────────────────── */
function updateSvgElement(key, elem, W, H) {
  if (!_svg) return;
  const g = _svg.querySelector(`[data-key="${key}"]`);
  if (!g) return;

  if (isLine(key)) {
    g.querySelectorAll('line').forEach(l => { l.setAttribute('y1', String(elem.y || 0)); l.setAttribute('y2', String(elem.y || 0)); });
    const txt = g.querySelector('text');
    if (txt) txt.setAttribute('y', String((elem.y || 0) - 1.5));
  } else {
    const r = g.querySelector('rect');
    if (r) { r.setAttribute('x', String(elem.x || 0)); r.setAttribute('y', String(elem.y || 0)); r.setAttribute('width', String(elem.w || 10)); r.setAttribute('height', String(elem.h || 10)); }
    const img = g.querySelector('image');
    if (img) { img.setAttribute('x', String(elem.x || 0)); img.setAttribute('y', String(elem.y || 0)); img.setAttribute('width', String(elem.w || 10)); img.setAttribute('height', String(elem.h || 10)); }
    const txt = g.querySelector('text');
    if (txt) { const fs = Math.min(3.5, Math.max(2, (elem.h || 10) * 0.28)); txt.setAttribute('x', String((elem.x || 0) + 1)); txt.setAttribute('y', String((elem.y || 0) + fs + 0.5)); }
  }
}

function updateHandles(key, elem) {
  if (!_svg || isLine(key)) return;
  const hs = 2.5;
  const corners = {
    nw: [elem.x, elem.y], ne: [elem.x + (elem.w || 0), elem.y],
    sw: [elem.x, elem.y + (elem.h || 0)], se: [elem.x + (elem.w || 0), elem.y + (elem.h || 0)],
  };
  _svg.querySelectorAll(`[data-key="${key}"][data-handle]`).forEach(h => {
    const c = corners[h.dataset.handle];
    if (c) { h.setAttribute('x', String(c[0] - hs)); h.setAttribute('y', String(c[1] - hs)); }
  });
}

/* ── Add Element Modal ───────────────────────────────────── */
function showAddModal() {
  if (!_mainEl || !_layout) return;
  const meta = _layout.meta || {};
  const W = meta.ref_width || 122;
  const H = meta.ref_height || 250;

  const overlay = el('div', { class: 'epd-add-modal' });
  const modal = el('div', { class: 'modal-content' });
  modal.innerHTML = `
    <h3 style="margin:0 0 12px">Add Element</h3>
    <div class="form-group">
      <label>Name (snake_case)</label>
      <input type="text" id="epd-add-name" class="input" placeholder="my_element" style="width:100%">
    </div>
    <div class="form-group">
      <label>Type</label>
      <select id="epd-add-type" class="select" style="width:100%">
        <option value="rect">Rectangle</option>
        <option value="line">Horizontal Line</option>
      </select>
    </div>
    <div class="modal-footer">
      <button class="btn" id="epd-add-cancel">Cancel</button>
      <button class="btn" id="epd-add-confirm" style="font-weight:800">Add</button>
    </div>`;
  overlay.appendChild(modal);
  overlay.style.display = 'flex';
  _mainEl.appendChild(overlay);

  const nameInp = overlay.querySelector('#epd-add-name');
  const typeInp = overlay.querySelector('#epd-add-type');
  overlay.querySelector('#epd-add-cancel').addEventListener('click', () => overlay.remove());
  overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
  overlay.querySelector('#epd-add-confirm').addEventListener('click', () => {
    const name = (nameInp.value || '').trim().replace(/[^a-z0-9_]/gi, '_').toLowerCase();
    if (!name) { toast('Name is required', 2000, 'error'); return; }
    if (_layout.elements[name]) { toast('Element already exists', 2000, 'error'); return; }
    pushUndo();
    _layout.elements[name] = typeInp.value === 'line'
      ? { y: Math.round(H / 2) }
      : { x: Math.round(W / 2 - 10), y: Math.round(H / 2 - 10), w: 20, h: 20 };
    _selectedKey = name;
    overlay.remove();
    renderAll();
  });
  nameInp.focus();
}

/* ── Import / Export ─────────────────────────────────────── */
function exportLayout() {
  if (!_layout) return;
  const blob = new Blob([JSON.stringify(_layout, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${_layout.meta?.name || 'layout'}.json`;
  a.click();
  URL.revokeObjectURL(url);
  toast(Lx('epd.exported', 'Layout exported'), 1800, 'success');
}

function importLayout() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.json';
  inp.onchange = async () => {
    const f = inp.files?.[0];
    if (!f) return;
    try {
      const text = await f.text();
      const data = JSON.parse(text);
      if (!data.meta || !data.elements) { toast('Invalid layout: needs "meta" + "elements"', 3000, 'error'); return; }
      pushUndo();
      _layout = data;
      _selectedKey = null;
      toast(Lx('epd.imported', 'Layout imported'), 1800, 'success');
      renderAll();
    } catch (err) {
      toast(`Import failed: ${err.message}`, 3000, 'error');
    }
  };
  inp.click();
}
