/**
 * Bjorn Debug — Real-time process profiler.
 * Shows CPU, RSS, FD, threads over time + per-thread / per-file tables.
 * v2: rich thread info, line-level tracemalloc, open files, graph tooltip.
 */

import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, setText, empty } from '../core/dom.js';

let tracker = null;
let snapshotPoller = null;

// Ring buffers for graph
const MAX_PTS = 200;
const history = { ts: [], cpu: [], rss: [], fd: [], threads: [], swap: [] };

// Canvas refs
let graphCanvas = null;
let graphCtx = null;
let graphRAF = null;

// Tooltip state
let hoverIndex = -1;
let tooltipEl = null;

// State
let latestSnapshot = null;
let isPaused = false;

/* ============================================================
 * mount / unmount
 * ============================================================ */

export async function mount(container) {
  tracker = new ResourceTracker('bjorn-debug');
  container.innerHTML = '';
  container.appendChild(buildLayout());

  graphCanvas = document.getElementById('debugGraph');
  tooltipEl = document.getElementById('dbgTooltip');
  if (graphCanvas) {
    graphCtx = graphCanvas.getContext('2d');
    resizeCanvas();
    tracker.trackEventListener(window, 'resize', resizeCanvas);
    tracker.trackEventListener(graphCanvas, 'mousemove', onGraphMouseMove);
    tracker.trackEventListener(graphCanvas, 'mouseleave', onGraphMouseLeave);
  }

  // Seed with server history
  try {
    const h = await api.get('/api/debug/history');
    if (h && h.history) {
      for (const pt of h.history) {
        pushPoint(pt.ts, pt.proc_cpu_pct, pt.rss_kb, pt.fd_open, pt.py_thread_count, pt.vm_swap_kb || 0);
      }
    }
  } catch (e) { /* first load */ }

  snapshotPoller = new Poller(fetchSnapshot, 2000);
  snapshotPoller.start();
  drawLoop();
}

export function unmount() {
  if (snapshotPoller) { snapshotPoller.stop(); snapshotPoller = null; }
  if (graphRAF) { cancelAnimationFrame(graphRAF); graphRAF = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  graphCanvas = null;
  graphCtx = null;
  tooltipEl = null;
  latestSnapshot = null;
  hoverIndex = -1;
  for (const k of Object.keys(history)) history[k].length = 0;
}

/* ============================================================
 * Data fetching
 * ============================================================ */

async function fetchSnapshot() {
  if (isPaused) return;
  try {
    const data = await api.get('/api/debug/snapshot', { timeout: 5000, retries: 0 });
    latestSnapshot = data;
    pushPoint(data.ts, data.proc_cpu_pct, data.rss_kb, data.fd_open, data.py_thread_count, data.vm_swap_kb || 0);
    updateCards(data);
    updateThreadTable(data);
    updatePyThreadTable(data);
    updateTracemallocByLine(data);
    updateTracemallocByFile(data);
    updateOpenFilesTable(data);
  } catch (e) { /* skip */ }
}

function pushPoint(ts, cpu, rss, fd, threads, swap) {
  history.ts.push(ts);
  history.cpu.push(cpu);
  history.rss.push(rss);
  history.fd.push(fd);
  history.threads.push(threads);
  history.swap.push(swap);
  if (history.ts.length > MAX_PTS) {
    for (const k of Object.keys(history)) history[k].shift();
  }
}

/* ============================================================
 * Layout
 * ============================================================ */

function buildLayout() {
  const page = el('div', { class: 'dbg-page' });

  // -- Header --
  const header = el('div', { class: 'dbg-header' });
  header.appendChild(el('h2', { class: 'dbg-title' }, ['Bjorn Debug']));

  const controls = el('div', { class: 'dbg-controls' });
  const pauseBtn = el('button', { class: 'btn dbg-btn', id: 'dbgPause' }, ['Pause']);
  tracker.trackEventListener(pauseBtn, 'click', () => {
    isPaused = !isPaused;
    pauseBtn.textContent = isPaused ? 'Resume' : 'Pause';
    pauseBtn.classList.toggle('active', isPaused);
  });
  const gcBtn = el('button', { class: 'btn dbg-btn', id: 'dbgGC' }, ['Force GC']);
  tracker.trackEventListener(gcBtn, 'click', async () => {
    try {
      const r = await api.post('/api/debug/gc/collect', {});
      if (window.toast) window.toast(`GC collected ${r.collected} objects`);
    } catch (e) { if (window.toast) window.toast('GC failed'); }
  });
  const tmBtn = el('button', { class: 'btn dbg-btn', id: 'dbgTracemalloc' }, ['tracemalloc: ?']);
  tracker.trackEventListener(tmBtn, 'click', async () => {
    const tracing = latestSnapshot?.tracemalloc_active;
    try {
      const r = await api.post('/api/debug/tracemalloc', { action: tracing ? 'stop' : 'start' });
      tmBtn.textContent = `tracemalloc: ${r.tracing ? 'ON' : 'OFF'}`;
      tmBtn.classList.toggle('active', r.tracing);
    } catch (e) { if (window.toast) window.toast('tracemalloc toggle failed'); }
  });
  controls.append(pauseBtn, gcBtn, tmBtn);
  header.appendChild(controls);
  page.appendChild(header);

  // -- KPI cards --
  const cards = el('div', { class: 'dbg-cards', id: 'dbgCards' });
  for (const cd of [
    { id: 'cardCPU', label: 'CPU %', value: '--' },
    { id: 'cardRSS', label: 'RSS (MB)', value: '--' },
    { id: 'cardSwap', label: 'Swap (MB)', value: '--' },
    { id: 'cardFD', label: 'Open FDs', value: '--' },
    { id: 'cardThreads', label: 'Threads', value: '--' },
    { id: 'cardPeak', label: 'RSS Peak (MB)', value: '--' },
  ]) {
    const c = el('div', { class: 'dbg-card', id: cd.id });
    c.appendChild(el('div', { class: 'dbg-card-value' }, [cd.value]));
    c.appendChild(el('div', { class: 'dbg-card-label' }, [cd.label]));
    cards.appendChild(c);
  }
  page.appendChild(cards);

  // -- Graph with tooltip --
  const graphWrap = el('div', { class: 'dbg-graph-wrap' });
  const legend = el('div', { class: 'dbg-legend' });
  for (const li of [
    { color: '#00d4ff', label: 'CPU %' },
    { color: '#00ff6a', label: 'RSS (MB)' },
    { color: '#ff4169', label: 'FDs' },
    { color: '#ffaa00', label: 'Threads' },
    { color: '#b44dff', label: 'Swap (MB)' },
  ]) {
    const item = el('span', { class: 'dbg-legend-item' });
    item.appendChild(el('span', { class: 'dbg-legend-dot', style: `background:${li.color}` }));
    item.appendChild(document.createTextNode(li.label));
    legend.appendChild(item);
  }
  graphWrap.appendChild(legend);
  const canvasContainer = el('div', { class: 'dbg-canvas-container' });
  canvasContainer.appendChild(el('canvas', { id: 'debugGraph', class: 'dbg-canvas' }));
  canvasContainer.appendChild(el('div', { id: 'dbgTooltip', class: 'dbg-tooltip' }));
  graphWrap.appendChild(canvasContainer);
  page.appendChild(graphWrap);

  // -- Tables --
  const tables = el('div', { class: 'dbg-tables' });

  // 1. Kernel threads (with Python mapping)
  tables.appendChild(el('h3', { class: 'dbg-section-title' }, ['Kernel Threads (CPU %) — mapped to Python']));
  tables.appendChild(makeTable('threadTable', 'threadBody',
    ['TID', 'Kernel', 'Python Name', 'Target / Current', 'State', 'CPU %', 'Bar']));

  // 2. Python threads (rich)
  tables.appendChild(el('h3', { class: 'dbg-section-title' }, ['Python Threads — Stack Trace']));
  tables.appendChild(makeTable('pyThreadTable', 'pyThreadBody',
    ['Name', 'Target Function', 'Source File', 'Current Frame', 'Daemon', 'Alive']));

  // 3. tracemalloc by LINE (the leak finder)
  tables.appendChild(el('h3', { class: 'dbg-section-title' }, ['tracemalloc — Top Allocations by Line']));
  const tmInfo = el('div', { class: 'dbg-tm-info', id: 'tmInfo' }, ['tracemalloc not active — click the button to start']);
  tables.appendChild(tmInfo);
  tables.appendChild(makeTable('tmLineTable', 'tmLineBody',
    ['File', 'Line', 'Size (KB)', 'Count', 'Bar']));

  // 4. tracemalloc by FILE (overview)
  tables.appendChild(el('h3', { class: 'dbg-section-title' }, ['tracemalloc — Aggregated by File']));
  tables.appendChild(makeTable('tmFileTable', 'tmFileBody',
    ['File', 'Size (KB)', 'Count', 'Bar']));

  // 5. Open file descriptors
  tables.appendChild(el('h3', { class: 'dbg-section-title' }, ['Open File Descriptors']));
  tables.appendChild(makeTable('fdTable', 'fdBody',
    ['Target', 'Type', 'Count', 'FDs', 'Bar']));

  page.appendChild(tables);

  // CSS
  const style = document.createElement('style');
  style.textContent = SCOPED_CSS;
  page.appendChild(style);

  return page;
}

function makeTable(tableId, bodyId, headers) {
  const wrap = el('div', { class: 'dbg-table-wrap' });
  const table = el('table', { class: 'dbg-table', id: tableId });
  table.appendChild(el('thead', {}, [
    el('tr', {}, headers.map(h => el('th', {}, [h])))
  ]));
  table.appendChild(el('tbody', { id: bodyId }));
  wrap.appendChild(table);
  return wrap;
}

/* ============================================================
 * Card updates
 * ============================================================ */

function updateCards(d) {
  setCardVal('cardCPU', d.proc_cpu_pct.toFixed(1), d.proc_cpu_pct > 80 ? 'hot' : d.proc_cpu_pct > 40 ? 'warm' : '');
  setCardVal('cardRSS', (d.rss_kb / 1024).toFixed(1), d.rss_kb > 400000 ? 'hot' : d.rss_kb > 200000 ? 'warm' : '');
  setCardVal('cardSwap', ((d.vm_swap_kb || 0) / 1024).toFixed(1), d.vm_swap_kb > 50000 ? 'hot' : d.vm_swap_kb > 10000 ? 'warm' : '');
  setCardVal('cardFD', d.fd_open, d.fd_open > 500 ? 'hot' : d.fd_open > 200 ? 'warm' : '');
  setCardVal('cardThreads', `${d.py_thread_count} / ${d.kernel_threads}`, d.py_thread_count > 50 ? 'hot' : d.py_thread_count > 20 ? 'warm' : '');
  setCardVal('cardPeak', ((d.vm_peak_kb || 0) / 1024).toFixed(1), '');

  const tmBtn = document.getElementById('dbgTracemalloc');
  if (tmBtn) {
    tmBtn.textContent = `tracemalloc: ${d.tracemalloc_active ? 'ON' : 'OFF'}`;
    tmBtn.classList.toggle('active', d.tracemalloc_active);
  }
}

function setCardVal(id, val, level) {
  const card = document.getElementById(id);
  if (!card) return;
  const valEl = card.querySelector('.dbg-card-value');
  if (valEl) valEl.textContent = val;
  card.classList.remove('hot', 'warm');
  if (level) card.classList.add(level);
}

/* ============================================================
 * Tables
 * ============================================================ */

function updateThreadTable(d) {
  const body = document.getElementById('threadBody');
  if (!body || !d.threads) return;
  body.innerHTML = '';
  const maxCpu = Math.max(1, ...d.threads.map(t => t.cpu_pct));
  for (const t of d.threads.slice(0, 40)) {
    const pct = t.cpu_pct;
    const barW = Math.max(1, (pct / maxCpu) * 100);
    const barColor = pct > 50 ? '#ff4169' : pct > 15 ? '#ffaa00' : '#00d4ff';

    // Build target/current cell
    let targetText = '';
    if (t.py_target) {
      targetText = t.py_target;
      if (t.py_module) targetText = `${t.py_module}.${targetText}`;
    }
    if (t.py_current) {
      targetText += targetText ? ` | ${t.py_current}` : t.py_current;
    }

    const row = el('tr', { class: pct > 30 ? 'dbg-row-hot' : '' }, [
      el('td', { class: 'dbg-num' }, [String(t.tid)]),
      el('td', { class: 'dbg-mono' }, [t.name]),
      el('td', { class: 'dbg-mono' }, [t.py_name || '--']),
      el('td', { class: 'dbg-mono dbg-target', title: targetText }, [targetText || '--']),
      el('td', {}, [t.state]),
      el('td', { class: 'dbg-num' }, [pct.toFixed(1)]),
      el('td', {}, [el('div', { class: 'dbg-bar', style: `width:${barW}%;background:${barColor}` })]),
    ]);
    body.appendChild(row);
  }
}

function updatePyThreadTable(d) {
  const body = document.getElementById('pyThreadBody');
  if (!body || !d.py_threads) return;
  body.innerHTML = '';
  for (const t of d.py_threads) {
    // Format current frame as "file:line func()"
    let currentFrame = '--';
    if (t.stack_top && t.stack_top.length > 0) {
      const f = t.stack_top[0];
      currentFrame = `${f.file}:${f.line} ${f.func}()`;
    }

    // Build full stack tooltip
    let stackTooltip = '';
    if (t.stack_top) {
      stackTooltip = t.stack_top.map(f => `${f.file}:${f.line} ${f.func}()`).join('\n');
    }

    const targetFile = t.target_file || t.target_module || '';
    const shortFile = targetFile.split('/').slice(-2).join('/');

    const row = el('tr', {}, [
      el('td', { class: 'dbg-mono dbg-name' }, [t.name]),
      el('td', { class: 'dbg-mono' }, [t.target_func || '--']),
      el('td', { class: 'dbg-mono dbg-file', title: targetFile }, [shortFile || '--']),
      el('td', { class: 'dbg-mono dbg-target', title: stackTooltip }, [currentFrame]),
      el('td', {}, [t.daemon ? 'Yes' : 'No']),
      el('td', {}, [t.alive ? 'Yes' : 'No']),
    ]);
    body.appendChild(row);
  }
}

function updateTracemallocByLine(d) {
  const info = document.getElementById('tmInfo');
  const body = document.getElementById('tmLineBody');
  if (!body) return;

  if (!d.tracemalloc_active) {
    if (info) info.textContent = 'tracemalloc not active — click the button to start tracing';
    body.innerHTML = '';
    return;
  }
  if (info) info.textContent = `Traced: ${d.tracemalloc_current_kb.toFixed(0)} KB — Peak: ${d.tracemalloc_peak_kb.toFixed(0)} KB`;

  body.innerHTML = '';
  const items = d.tracemalloc_by_line || [];
  if (!items.length) return;
  const maxSize = Math.max(1, ...items.map(t => t.size_kb));
  for (const t of items) {
    const barW = Math.max(1, (t.size_kb / maxSize) * 100);
    const sizeColor = t.size_kb > 100 ? '#ff4169' : t.size_kb > 30 ? '#ffaa00' : '#b44dff';
    const row = el('tr', { class: t.size_kb > 100 ? 'dbg-row-hot' : '' }, [
      el('td', { class: 'dbg-mono dbg-file', title: t.full_path }, [t.file]),
      el('td', { class: 'dbg-num' }, [String(t.line)]),
      el('td', { class: 'dbg-num' }, [t.size_kb.toFixed(1)]),
      el('td', { class: 'dbg-num' }, [String(t.count)]),
      el('td', {}, [el('div', { class: 'dbg-bar', style: `width:${barW}%;background:${sizeColor}` })]),
    ]);
    body.appendChild(row);
  }
}

function updateTracemallocByFile(d) {
  const body = document.getElementById('tmFileBody');
  if (!body) return;
  body.innerHTML = '';
  const items = d.tracemalloc_by_file || [];
  if (!items.length || !d.tracemalloc_active) return;
  const maxSize = Math.max(1, ...items.map(t => t.size_kb));
  for (const t of items) {
    const barW = Math.max(1, (t.size_kb / maxSize) * 100);
    const row = el('tr', {}, [
      el('td', { class: 'dbg-mono dbg-file', title: t.full_path }, [t.file]),
      el('td', { class: 'dbg-num' }, [t.size_kb.toFixed(1)]),
      el('td', { class: 'dbg-num' }, [String(t.count)]),
      el('td', {}, [el('div', { class: 'dbg-bar', style: `width:${barW}%;background:#b44dff` })]),
    ]);
    body.appendChild(row);
  }
}

function updateOpenFilesTable(d) {
  const body = document.getElementById('fdBody');
  if (!body) return;
  body.innerHTML = '';
  const items = d.open_files || [];
  if (!items.length) return;
  const maxCount = Math.max(1, ...items.map(f => f.count));
  for (const f of items) {
    const barW = Math.max(1, (f.count / maxCount) * 100);
    const typeColors = {
      file: '#00d4ff', socket: '#ff4169', pipe: '#ffaa00',
      device: '#888', proc: '#666', temp: '#b44dff', anon: '#555', other: '#444'
    };
    const barColor = typeColors[f.type] || '#444';
    const fdStr = f.fds.join(', ') + (f.count > f.fds.length ? '...' : '');
    const row = el('tr', { class: f.count > 5 ? 'dbg-row-warn' : '' }, [
      el('td', { class: 'dbg-mono dbg-target', title: f.target }, [f.target]),
      el('td', {}, [el('span', { class: `dbg-type-badge dbg-type-${f.type}` }, [f.type])]),
      el('td', { class: 'dbg-num' }, [String(f.count)]),
      el('td', { class: 'dbg-mono dbg-fds' }, [fdStr]),
      el('td', {}, [el('div', { class: 'dbg-bar', style: `width:${barW}%;background:${barColor}` })]),
    ]);
    body.appendChild(row);
  }
}

/* ============================================================
 * Graph + tooltip
 * ============================================================ */

function getGraphLayout() {
  if (!graphCanvas) return null;
  const W = graphCanvas.width;
  const H = graphCanvas.height;
  const dpr = window.devicePixelRatio || 1;
  const pad = { l: 50 * dpr, r: 60 * dpr, t: 10 * dpr, b: 25 * dpr };
  return { W, H, dpr, pad, gW: W - pad.l - pad.r, gH: H - pad.t - pad.b };
}

function onGraphMouseMove(e) {
  if (!graphCanvas || history.ts.length < 2) return;
  const rect = graphCanvas.getBoundingClientRect();
  const L = getGraphLayout();
  if (!L) return;

  const mouseX = (e.clientX - rect.left) * L.dpr;
  const frac = (mouseX - L.pad.l) / L.gW;
  const idx = Math.round(frac * (history.ts.length - 1));

  if (idx < 0 || idx >= history.ts.length) {
    hoverIndex = -1;
    if (tooltipEl) tooltipEl.style.display = 'none';
    return;
  }

  hoverIndex = idx;

  // Position & populate tooltip
  if (tooltipEl) {
    const ago = history.ts[history.ts.length - 1] - history.ts[idx];
    const ts = new Date(history.ts[idx] * 1000);
    const timeStr = ts.toLocaleTimeString();

    tooltipEl.innerHTML = `
      <div class="dbg-tt-time">${timeStr} (-${formatTimeAgo(ago)})</div>
      <div class="dbg-tt-row"><span class="dbg-tt-dot" style="background:#00d4ff"></span>CPU: <b>${history.cpu[idx].toFixed(1)}%</b></div>
      <div class="dbg-tt-row"><span class="dbg-tt-dot" style="background:#00ff6a"></span>RSS: <b>${(history.rss[idx] / 1024).toFixed(1)} MB</b></div>
      <div class="dbg-tt-row"><span class="dbg-tt-dot" style="background:#ff4169"></span>FDs: <b>${history.fd[idx]}</b></div>
      <div class="dbg-tt-row"><span class="dbg-tt-dot" style="background:#ffaa00"></span>Threads: <b>${history.threads[idx]}</b></div>
      <div class="dbg-tt-row"><span class="dbg-tt-dot" style="background:#b44dff"></span>Swap: <b>${(history.swap[idx] / 1024).toFixed(1)} MB</b></div>
    `;
    tooltipEl.style.display = 'block';

    // Tooltip positioning (CSS pixels)
    const cssX = (L.pad.l / L.dpr) + (idx / (history.ts.length - 1)) * (L.gW / L.dpr);
    const containerW = graphCanvas.parentElement.clientWidth;
    const ttW = tooltipEl.offsetWidth;
    let left = cssX + 12;
    if (left + ttW > containerW - 10) left = cssX - ttW - 12;
    tooltipEl.style.left = `${Math.max(0, left)}px`;
    tooltipEl.style.top = '10px';
  }
}

function onGraphMouseLeave() {
  hoverIndex = -1;
  if (tooltipEl) tooltipEl.style.display = 'none';
}

function resizeCanvas() {
  if (!graphCanvas) return;
  const wrap = graphCanvas.parentElement;
  const dpr = window.devicePixelRatio || 1;
  graphCanvas.width = wrap.clientWidth * dpr;
  graphCanvas.height = 240 * dpr;
  graphCanvas.style.width = wrap.clientWidth + 'px';
  graphCanvas.style.height = '240px';
}

function drawLoop() {
  drawGraph();
  graphRAF = requestAnimationFrame(drawLoop);
}

function drawGraph() {
  const L = getGraphLayout();
  if (!L || !graphCtx) return;
  const { W, H, dpr, pad, gW, gH } = L;
  const ctx = graphCtx;
  ctx.clearRect(0, 0, W, H);

  const pts = history.ts.length;
  if (pts < 2) return;

  // Grid
  ctx.strokeStyle = 'rgba(255,255,255,0.06)';
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (gH * i) / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
  }

  // Series
  const series = [
    { data: history.cpu, color: '#00d4ff', label: 'CPU %' },
    { data: history.rss.map(v => v / 1024), color: '#00ff6a', label: 'RSS MB' },
    { data: history.fd, color: '#ff4169', label: 'FDs' },
    { data: history.threads, color: '#ffaa00', label: 'Threads' },
    { data: history.swap.map(v => v / 1024), color: '#b44dff', label: 'Swap MB' },
  ];

  for (const s of series) {
    if (!s.data.length) continue;
    const max = Math.max(1, ...s.data) * 1.15;
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.5 * dpr;
    ctx.globalAlpha = 0.85;
    ctx.beginPath();
    for (let i = 0; i < s.data.length; i++) {
      const x = pad.l + (i / (s.data.length - 1)) * gW;
      const y = pad.t + gH - (s.data[i] / max) * gH;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;

    // Right-edge label
    const lastVal = s.data[s.data.length - 1];
    const lastY = pad.t + gH - (lastVal / max) * gH;
    ctx.fillStyle = s.color;
    ctx.font = `${10 * dpr}px monospace`;
    ctx.textAlign = 'left';
    ctx.fillText(`${lastVal.toFixed(1)}`, W - pad.r + 4 * dpr, lastY + 3 * dpr);
  }

  // Time axis
  const timeSpan = history.ts[pts - 1] - history.ts[0];
  ctx.fillStyle = 'rgba(255,255,255,0.3)';
  ctx.font = `${9 * dpr}px monospace`;
  ctx.textAlign = 'center';
  for (let i = 0; i <= 4; i++) {
    const frac = i / 4;
    const x = pad.l + frac * gW;
    ctx.fillText(`-${formatTimeAgo(timeSpan - timeSpan * frac)}`, x, H - 5 * dpr);
  }

  // Hover crosshair
  if (hoverIndex >= 0 && hoverIndex < pts) {
    const hx = pad.l + (hoverIndex / (pts - 1)) * gW;
    ctx.strokeStyle = 'rgba(255,255,255,0.3)';
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(hx, pad.t); ctx.lineTo(hx, pad.t + gH); ctx.stroke();
    ctx.setLineDash([]);

    // Dots on each series at hoverIndex
    for (const s of series) {
      if (!s.data.length || hoverIndex >= s.data.length) continue;
      const max = Math.max(1, ...s.data) * 1.15;
      const val = s.data[hoverIndex];
      const y = pad.t + gH - (val / max) * gH;
      ctx.fillStyle = s.color;
      ctx.beginPath();
      ctx.arc(hx, y, 4 * dpr, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

function formatTimeAgo(secs) {
  if (secs < 60) return `${Math.round(secs)}s`;
  return `${Math.floor(secs / 60)}m${Math.round(secs % 60)}s`;
}

/* ============================================================
 * Scoped CSS
 * ============================================================ */

const SCOPED_CSS = `
.dbg-page { padding: 12px; max-width: 1600px; margin: 0 auto; }

.dbg-header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
.dbg-title { margin: 0; font-size: 1.3em; color: var(--text, #e0e0e0); }
.dbg-controls { display: flex; gap: 6px; flex-wrap: wrap; }
.dbg-btn { font-size: 0.78em; padding: 4px 10px; border: 1px solid rgba(255,255,255,0.15); border-radius: 4px; background: rgba(255,255,255,0.04); color: var(--text, #ccc); cursor: pointer; transition: all .15s; }
.dbg-btn:hover { background: rgba(255,255,255,0.1); }
.dbg-btn.active { background: rgba(0,212,255,0.15); border-color: #00d4ff; color: #00d4ff; }

/* KPI cards */
.dbg-cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 14px; }
.dbg-card { background: rgba(255,255,255,0.03); border: 1px solid rgba(255,255,255,0.08); border-radius: 8px; padding: 10px 12px; text-align: center; transition: border-color .3s, background .3s; }
.dbg-card.warm { border-color: #ffaa00; background: rgba(255,170,0,0.06); }
.dbg-card.hot { border-color: #ff4169; background: rgba(255,65,105,0.08); }
.dbg-card-value { font-size: 1.6em; font-weight: 700; font-family: monospace; color: var(--text, #fff); line-height: 1.2; }
.dbg-card-label { font-size: 0.72em; color: rgba(255,255,255,0.45); margin-top: 2px; text-transform: uppercase; letter-spacing: .5px; }
.dbg-card.hot .dbg-card-value { color: #ff4169; }
.dbg-card.warm .dbg-card-value { color: #ffaa00; }

/* Graph */
.dbg-graph-wrap { background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 8px; margin-bottom: 14px; }
.dbg-canvas-container { position: relative; }
.dbg-canvas { width: 100%; height: 240px; display: block; cursor: crosshair; }
.dbg-legend { display: flex; gap: 14px; padding: 0 4px 6px; flex-wrap: wrap; }
.dbg-legend-item { display: inline-flex; align-items: center; gap: 4px; font-size: 0.72em; color: rgba(255,255,255,0.55); }
.dbg-legend-dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }

/* Tooltip */
.dbg-tooltip { display: none; position: absolute; top: 10px; left: 0; background: rgba(10,10,20,0.92); border: 1px solid rgba(255,255,255,0.15); border-radius: 6px; padding: 8px 12px; font-size: 0.76em; color: #ddd; pointer-events: none; z-index: 10; white-space: nowrap; backdrop-filter: blur(8px); box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
.dbg-tt-time { color: rgba(255,255,255,0.5); margin-bottom: 4px; font-size: 0.9em; }
.dbg-tt-row { display: flex; align-items: center; gap: 6px; line-height: 1.6; }
.dbg-tt-dot { width: 6px; height: 6px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.dbg-tt-row b { color: #fff; }

/* Tables */
.dbg-section-title { font-size: 0.95em; color: var(--text, #ccc); margin: 16px 0 6px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 4px; }
.dbg-table-wrap { overflow-x: auto; margin-bottom: 10px; max-height: 350px; overflow-y: auto; }
.dbg-table { width: 100%; border-collapse: collapse; font-size: 0.76em; }
.dbg-table th { position: sticky; top: 0; background: rgba(20,20,30,0.95); text-align: left; padding: 5px 8px; color: rgba(255,255,255,0.5); font-weight: 600; text-transform: uppercase; font-size: 0.82em; letter-spacing: .3px; border-bottom: 1px solid rgba(255,255,255,0.1); z-index: 1; }
.dbg-table td { padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,0.04); color: var(--text, #bbb); }
.dbg-table tr:hover td { background: rgba(255,255,255,0.04); }
.dbg-row-hot td { color: #ff4169 !important; }
.dbg-row-warn td { color: #ffaa00 !important; }
.dbg-mono { font-family: monospace; font-size: 0.9em; }
.dbg-num { text-align: right; font-family: monospace; }
.dbg-name { font-weight: 600; }
.dbg-file { max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dbg-target { max-width: 400px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dbg-fds { max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.85em; color: rgba(255,255,255,0.4); }
.dbg-bar { height: 10px; border-radius: 3px; min-width: 2px; transition: width .3s; }
.dbg-tm-info { font-size: 0.78em; color: rgba(255,255,255,0.4); margin-bottom: 6px; font-style: italic; }

/* Type badges */
.dbg-type-badge { display: inline-block; padding: 1px 6px; border-radius: 3px; font-size: 0.82em; font-weight: 600; }
.dbg-type-file { background: rgba(0,212,255,0.12); color: #00d4ff; }
.dbg-type-socket { background: rgba(255,65,105,0.12); color: #ff4169; }
.dbg-type-pipe { background: rgba(255,170,0,0.12); color: #ffaa00; }
.dbg-type-device { background: rgba(136,136,136,0.15); color: #aaa; }
.dbg-type-proc { background: rgba(100,100,100,0.15); color: #888; }
.dbg-type-temp { background: rgba(180,77,255,0.12); color: #b44dff; }
.dbg-type-anon { background: rgba(80,80,80,0.15); color: #777; }
.dbg-type-other { background: rgba(60,60,60,0.15); color: #666; }
`;
