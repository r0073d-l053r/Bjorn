/**
 * RL Dashboard - Mode-aware visualization.
 * MANUAL → static overlay, AUTO → heuristic flow graph, AI → neural network cloud.
 */

import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, setText, empty } from '../core/dom.js';

let tracker = null;
let statsPoller = null;
let historyPoller = null;
let metricsGraph = null;
let modelCloud = null;
let heuristicGraph = null;
let _currentMode = 'AUTO';

export async function mount(container) {
  tracker = new ResourceTracker('rl-dashboard');
  container.innerHTML = '';
  container.appendChild(buildLayout());

  await fetchStats();
  await fetchHistory();
  await fetchExperiences();

  statsPoller = new Poller(fetchStats, 5000);
  historyPoller = new Poller(async () => {
    await fetchHistory();
    await fetchExperiences();
  }, 10000);

  statsPoller.start();
  historyPoller.start();
}

export function unmount() {
  if (statsPoller) { statsPoller.stop(); statsPoller = null; }
  if (historyPoller) { historyPoller.stop(); historyPoller = null; }
  if (metricsGraph) { metricsGraph.destroy(); metricsGraph = null; }
  if (modelCloud) { modelCloud.destroy(); modelCloud = null; }
  if (heuristicGraph) { heuristicGraph.destroy(); heuristicGraph = null; }
  if (tracker) { tracker.cleanupAll(); tracker = null; }
}

/* ======================== Mini Metrics Canvas ======================== */

class MultiMetricGraph {
  constructor(canvasId) {
    this.data = {
      epsilon: new Array(100).fill(0),
      reward: new Array(100).fill(0),
      loss: new Array(100).fill(0),
    };
    this.colors = { epsilon: '#00d4ff', reward: '#00ff6a', loss: '#ff4169' };
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');
    this._onResize = () => this.resize();
    window.addEventListener('resize', this._onResize);
    this.resize();
    this.animate();
  }
  destroy() {
    window.removeEventListener('resize', this._onResize);
    if (this._raf) cancelAnimationFrame(this._raf);
  }
  resize() {
    const p = this.canvas.parentElement;
    this.canvas.width = Math.max(1, p.offsetWidth);
    this.canvas.height = Math.max(1, p.offsetHeight);
    this.width = this.canvas.width;
    this.height = this.canvas.height;
  }
  update(stats) {
    if (!stats) return;
    this.data.epsilon.shift();
    this.data.reward.shift();
    this.data.loss.shift();
    this.data.epsilon.push(Number(stats.epsilon || 0));
    const recent = Array.isArray(stats.recent_activity) ? stats.recent_activity : [];
    const r = recent.length ? Number(recent[0].reward || 0) : 0;
    const prevR = this.data.reward[this.data.reward.length - 1] || 0;
    this.data.reward.push(prevR * 0.8 + r * 0.2);
    const l = Number(stats.last_loss || 0);
    const prevL = this.data.loss[this.data.loss.length - 1] || 0;
    this.data.loss.push(prevL * 0.9 + l * 0.1);
  }
  animate() {
    this._raf = requestAnimationFrame(() => this.animate());
    this.ctx.clearRect(0, 0, this.width, this.height);
    this.drawLine(this.data.epsilon, this.colors.epsilon, 1.0);
    this.drawLine(this.data.reward, this.colors.reward, 10.0);
    this.drawLine(this.data.loss, this.colors.loss, 5.0);
  }
  drawLine(data, color, maxVal) {
    if (data.length < 2) return;
    const stepX = this.width / (data.length - 1);
    this.ctx.beginPath();
    data.forEach((val, i) => {
      const x = i * stepX;
      const y = this.height - (Math.max(0, val) / Math.max(0.001, maxVal)) * this.height * 0.8 - 5;
      if (i === 0) this.ctx.moveTo(x, y); else this.ctx.lineTo(x, y);
    });
    this.ctx.strokeStyle = color;
    this.ctx.lineWidth = 2;
    this.ctx.stroke();
  }
}

/* ======================== Heuristic Flow Graph (AUTO mode) ======================== */

class HeuristicGraph {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');
    this.tick = 0;
    this.signals = [];
    this.recentActions = [];

    /* Flow nodes representing the heuristic pipeline */
    this.flowNodes = [
      { id: 'scan',    label: 'SCAN',    icon: '\uD83D\uDD0D', color: [0, 200, 255] },
      { id: 'analyze', label: 'ANALYZE', icon: '\uD83E\uDDE0', color: [140, 100, 255] },
      { id: 'decide',  label: 'DECIDE',  icon: '\u2696\uFE0F', color: [255, 200, 60] },
      { id: 'execute', label: 'EXECUTE', icon: '\u26A1',       color: [0, 255, 120] },
      { id: 'result',  label: 'RESULT',  icon: '\uD83C\uDFAF', color: [255, 100, 180] },
    ];

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.canvas.parentElement);
    this.resize();
    this.animate();
  }

  destroy() {
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.raf) cancelAnimationFrame(this.raf);
  }

  resize() {
    const p = this.canvas.parentElement;
    this.width = Math.max(1, p.offsetWidth);
    this.height = Math.max(1, p.offsetHeight);
    this.canvas.width = this.width;
    this.canvas.height = this.height;
    this.layoutNodes();
  }

  layoutNodes() {
    const n = this.flowNodes.length;
    const padX = 60, padY = 50;
    const spaceX = (this.width - 2 * padX) / Math.max(1, n - 1);
    const cy = this.height / 2;
    this.flowNodes.forEach((node, i) => {
      node.x = padX + i * spaceX;
      node.y = cy;
      node.r = Math.min(30, this.width / (n * 3));
    });
  }

  triggerActivity(activityList) {
    for (const act of activityList) {
      const reward = Number(act.reward || 0);
      this.signals.push({
        progress: 0,
        speed: 0.006 + Math.random() * 0.004,
        color: reward > 0 ? '#00ffa0' : '#ff3333',
        action: act.action || '',
        reward,
      });
      this.recentActions.unshift({ action: act.action || '', reward, time: Date.now() });
      if (this.recentActions.length > 8) this.recentActions.pop();
    }
  }

  animate() {
    this.raf = requestAnimationFrame(() => this.animate());
    this.tick += 0.015;
    this.ctx.clearRect(0, 0, this.width, this.height);
    this.drawBackground();
    this.drawConnections();
    this.drawSignals();
    this.drawNodes();
    this.drawLabels();
    this.drawActionFeed();
    this.updateSignals();
  }

  drawBackground() {
    const ctx = this.ctx;
    /* Subtle radial gradient */
    const grad = ctx.createRadialGradient(this.width / 2, this.height / 2, 0, this.width / 2, this.height / 2, this.width / 2);
    grad.addColorStop(0, 'rgba(0, 255, 160, 0.03)');
    grad.addColorStop(1, 'transparent');
    ctx.fillStyle = grad;
    ctx.fillRect(0, 0, this.width, this.height);

    /* "HEURISTIC ENGINE" watermark */
    ctx.save();
    ctx.font = `bold ${Math.min(16, this.width / 30)}px monospace`;
    ctx.fillStyle = 'rgba(0, 255, 160, 0.08)';
    ctx.textAlign = 'center';
    ctx.fillText('HEURISTIC ENGINE', this.width / 2, 24);
    ctx.restore();
  }

  drawConnections() {
    const ctx = this.ctx;
    const nodes = this.flowNodes;
    for (let i = 0; i < nodes.length - 1; i++) {
      const a = nodes[i], b = nodes[i + 1];
      const pulse = 0.3 + Math.sin(this.tick * 2 + i) * 0.2;

      /* Animated dashed line */
      ctx.save();
      ctx.strokeStyle = `rgba(0, 255, 160, ${pulse})`;
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 6]);
      ctx.lineDashOffset = -this.tick * 30;
      ctx.beginPath();
      ctx.moveTo(a.x + a.r + 4, a.y);
      ctx.lineTo(b.x - b.r - 4, b.y);
      ctx.stroke();
      ctx.restore();

      /* Arrow head */
      const ax = b.x - b.r - 8;
      ctx.fillStyle = `rgba(0, 255, 160, ${pulse + 0.2})`;
      ctx.beginPath();
      ctx.moveTo(ax, a.y - 5);
      ctx.lineTo(ax + 8, a.y);
      ctx.lineTo(ax, a.y + 5);
      ctx.closePath();
      ctx.fill();
    }
  }

  drawNodes() {
    const ctx = this.ctx;
    for (let i = 0; i < this.flowNodes.length; i++) {
      const n = this.flowNodes[i];
      const pulse = 0.7 + Math.sin(this.tick * 2.5 + i * 1.2) * 0.3;
      const [r, g, b] = n.color;

      /* Outer glow */
      ctx.save();
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r + 6, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${r},${g},${b},${0.08 * pulse})`;
      ctx.shadowBlur = 20;
      ctx.shadowColor = `rgba(${r},${g},${b},0.4)`;
      ctx.fill();
      ctx.restore();

      /* Node circle */
      ctx.beginPath();
      ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
      const grad = ctx.createRadialGradient(n.x, n.y - n.r * 0.3, 0, n.x, n.y, n.r);
      grad.addColorStop(0, `rgba(${r},${g},${b},${0.3 * pulse})`);
      grad.addColorStop(1, `rgba(${r},${g},${b},${0.08})`);
      ctx.fillStyle = grad;
      ctx.fill();

      /* Border ring */
      ctx.strokeStyle = `rgba(${r},${g},${b},${0.5 * pulse})`;
      ctx.lineWidth = 1.5;
      ctx.stroke();

      /* Icon */
      ctx.save();
      ctx.font = `${n.r * 0.7}px sans-serif`;
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillStyle = `rgba(${r},${g},${b},${0.9})`;
      ctx.fillText(n.icon, n.x, n.y);
      ctx.restore();
    }
  }

  drawLabels() {
    const ctx = this.ctx;
    ctx.save();
    ctx.font = `bold ${Math.min(11, this.width / 50)}px monospace`;
    ctx.textAlign = 'center';
    for (const n of this.flowNodes) {
      const [r, g, b] = n.color;
      ctx.fillStyle = `rgba(${r},${g},${b},0.8)`;
      ctx.fillText(n.label, n.x, n.y + n.r + 16);
    }
    ctx.restore();
  }

  drawSignals() {
    const ctx = this.ctx;
    const nodes = this.flowNodes;
    for (const sig of this.signals) {
      /* Map progress (0→1) across the full pipeline */
      const totalSegments = nodes.length - 1;
      const segFloat = sig.progress * totalSegments;
      const segIdx = Math.min(Math.floor(segFloat), totalSegments - 1);
      const segT = segFloat - segIdx;
      const a = nodes[segIdx];
      const b = nodes[Math.min(segIdx + 1, nodes.length - 1)];
      const x = a.x + (b.x - a.x) * segT;
      const y = a.y + Math.sin(segT * Math.PI) * -15; /* arc upward */

      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fillStyle = sig.color;
      ctx.shadowBlur = 16;
      ctx.shadowColor = sig.color;
      ctx.fill();
      ctx.shadowBlur = 0;

      /* Trail */
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fillStyle = '#fff';
      ctx.fill();
    }
  }

  drawActionFeed() {
    if (!this.recentActions.length) return;
    const ctx = this.ctx;
    ctx.save();
    const fs = Math.min(11, this.width / 50);
    ctx.font = `${fs}px monospace`;
    ctx.textAlign = 'left';
    const x = 12, startY = this.height - 12;
    const lineH = fs + 4;
    const maxShow = Math.min(this.recentActions.length, Math.floor((this.height * 0.3) / lineH));
    for (let i = 0; i < maxShow; i++) {
      const act = this.recentActions[i];
      const age = (Date.now() - act.time) / 1000;
      const alpha = Math.max(0.2, 1 - age / 30);
      const color = act.reward > 0 ? `rgba(0,255,160,${alpha})` : `rgba(255,60,60,${alpha})`;
      ctx.fillStyle = color;
      const prefix = act.reward > 0 ? '\u2713' : '\u2717';
      ctx.fillText(`${prefix} ${act.action}`, x, startY - i * lineH);
    }
    ctx.restore();
  }

  updateSignals() {
    for (const sig of this.signals) sig.progress += sig.speed;
    this.signals = this.signals.filter(s => s.progress < 1);
  }
}

/* ======================== Abstract Model Cloud (AI mode) ======================== */

class ModelCloud {
  constructor(canvasId) {
    this.canvas = document.getElementById(canvasId);
    if (!this.canvas) return;
    this.ctx = this.canvas.getContext('2d');
    this.tooltip = document.getElementById('brain-tooltip');
    this.nodes = [];
    this.links = [];
    this.signals = [];
    this.seenActivities = new Set();
    this.tick = 0;
    this.hoverIndex = -1;
    this.meta = {
      model_loaded: false, model_version: null,
      model_param_count: 0, model_layer_count: 0, model_feature_count: 0,
    };

    this.resizeObserver = new ResizeObserver(() => this.resize());
    this.resizeObserver.observe(this.canvas.parentElement);
    this.resize();

    this.onMouseMove = (e) => this.handleMouseMove(e);
    this.onMouseLeave = () => { this.hoverIndex = -1; if (this.tooltip) this.tooltip.style.display = 'none'; };
    this.canvas.addEventListener('mousemove', this.onMouseMove);
    this.canvas.addEventListener('mouseleave', this.onMouseLeave);
    this.generateNetwork();
    this.animate();
  }

  destroy() {
    if (this.resizeObserver) this.resizeObserver.disconnect();
    if (this.canvas) {
      if (this.onMouseMove) this.canvas.removeEventListener('mousemove', this.onMouseMove);
      if (this.onMouseLeave) this.canvas.removeEventListener('mouseleave', this.onMouseLeave);
    }
    if (this.raf) cancelAnimationFrame(this.raf);
  }

  resize() {
    const p = this.canvas.parentElement;
    this.width = Math.max(1, p.offsetWidth);
    this.height = Math.max(1, p.offsetHeight);
    this.canvas.width = this.width;
    this.canvas.height = this.height;
    this.generateNetwork();
  }

  triggerActivity(activityList) {
    if (!this.seenActivities) this.seenActivities = new Set();
    for (const act of activityList) {
      const actHash = act.action + '_' + act.reward + '_' + (act.timestamp || Date.now() + Math.random());
      if (this.seenActivities.has(actHash)) continue;
      this.seenActivities.add(actHash);
      if (this.seenActivities.size > 150) {
        const iter = this.seenActivities.values();
        this.seenActivities.delete(iter.next().value);
      }
      const inputNodes = this.nodes.filter(n => n.layer === 0);
      if (!inputNodes.length) continue;
      const startNode = inputNodes[Math.floor(Math.random() * inputNodes.length)];
      this.signals.push({
        sourceNode: startNode, targetNode: null, progress: 0,
        speed: 0.008 + Math.random() * 0.004,
        reward: Number(act.reward || 0), action: act.action || 'Unknown', layer: 0,
        color: Number(act.reward || 0) > 0 ? '#00ffa0' : '#ff3333'
      });
    }
  }

  updateFromStats(stats) {
    const oldMeta = JSON.stringify(this.meta);
    this.meta = {
      model_loaded: !!stats.model_loaded, model_version: stats.model_version || null,
      model_param_count: Number(stats.model_param_count || 0),
      model_layer_count: Number(stats.model_layer_count || 0),
      model_feature_count: Number(stats.model_feature_count || 0),
    };
    if (oldMeta !== JSON.stringify(this.meta)) this.generateNetwork();
  }

  generateNetwork() {
    this.nodes = [];
    this.links = [];
    if (this.width < 50 || this.height < 50) return;
    let numLayers = Math.max(3, Math.min(10, this.meta.model_layer_count || 3));
    if (!this.meta.model_loaded) numLayers = 3;
    const maxNodesPerLayer = Math.max(4, Math.min(15, Math.ceil(Math.log10(Math.max(10, this.meta.model_param_count)) * 2)));
    const paddingX = 60, paddingY = 60;
    const layerSpacing = (this.width - 2 * paddingX) / Math.max(1, numLayers - 1);
    const layers = [];
    for (let i = 0; i < numLayers; i++) {
      let nodeCount = maxNodesPerLayer;
      if (i === 0) nodeCount = Math.max(3, Math.ceil(maxNodesPerLayer * 0.6));
      if (i === numLayers - 1) nodeCount = Math.max(2, Math.ceil(maxNodesPerLayer * 0.4));
      if (i > 0 && i < numLayers - 1) nodeCount = Math.max(3, nodeCount - Math.floor(Math.random() * 3));
      const layerNodes = [];
      const nodeSpacing = (this.height - 2 * paddingY) / Math.max(1, nodeCount - 1);
      for (let j = 0; j < nodeCount; j++) {
        const energy = 0.2 + Math.random() * 0.8;
        layerNodes.push({
          id: `${i}-${j}`, layer: i, index: j,
          x: paddingX + i * layerSpacing,
          y: paddingY + j * nodeSpacing + (this.height - 2 * paddingY - (nodeCount - 1) * nodeSpacing) / 2,
          baseX: paddingX + i * layerSpacing,
          baseY: paddingY + j * nodeSpacing + (this.height - 2 * paddingY - (nodeCount - 1) * nodeSpacing) / 2,
          r: 2 + energy * 4, energy, phase: Math.random() * Math.PI * 2, cluster: i,
        });
      }
      layers.push(layerNodes);
      this.nodes.push(...layerNodes);
    }
    for (let i = 0; i < numLayers - 1; i++) {
      const currentLayer = layers[i], nextLayer = layers[i + 1];
      for (const nodeA of currentLayer) {
        const connectionCount = Math.max(1, Math.ceil(Math.random() * nextLayer.length * 0.8));
        const targets = [...nextLayer].sort(() => 0.5 - Math.random()).slice(0, connectionCount);
        for (const nodeB of targets) {
          this.links.push({ source: nodeA, target: nodeB, weight: Math.random() * 0.8 + 0.2, activePhase: Math.random() * Math.PI * 2 });
        }
      }
    }
  }

  handleMouseMove(e) {
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    this.hoverIndex = -1;
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i];
      const dx = mx - n.x, dy = my - n.y;
      if (dx * dx + dy * dy <= (n.r + 6) * (n.r + 6)) { this.hoverIndex = i; break; }
    }
    if (!this.tooltip || this.hoverIndex < 0) { if (this.tooltip) this.tooltip.style.display = 'none'; return; }
    const n = this.nodes[this.hoverIndex];
    this.tooltip.style.display = 'block';
    this.tooltip.innerHTML = `<strong>Neural Node</strong><br><span style="color:#9bb">Layer ${n.layer + 1}</span><br><span style="color:#00e7ff">Activation ${(n.energy * 100).toFixed(1)}%</span>`;
    const tx = Math.min(this.width - 180, mx + 12), ty = Math.min(this.height - 80, my + 12);
    this.tooltip.style.left = `${Math.max(8, tx)}px`;
    this.tooltip.style.top = `${Math.max(8, ty)}px`;
  }

  animate() {
    this.raf = requestAnimationFrame(() => this.animate());
    this.tick += 0.015;
    this.ctx.clearRect(0, 0, this.width, this.height);
    this.updateNodes();
    this.updateSignals();
    this.drawLinks();
    this.drawNodes();
    this.drawSignals();
  }

  updateSignals() {
    const maxLayer = this.nodes.length > 0 ? Math.max(...this.nodes.map(n => n.layer)) : 3;
    this.signals = this.signals.filter(s => s.layer < maxLayer);
    for (const sig of this.signals) {
      if (!sig.targetNode) {
        const possibleLinks = this.links.filter(l => l.source === sig.sourceNode);
        if (possibleLinks.length > 0) {
          sig.targetNode = possibleLinks[Math.floor(Math.random() * possibleLinks.length)].target;
        } else { sig.layer = 999; continue; }
      }
      sig.progress += sig.speed;
      if (sig.progress >= 1) {
        sig.sourceNode = sig.targetNode;
        sig.targetNode = null;
        sig.progress = 0;
        sig.layer++;
        sig.sourceNode.energy = Math.min(1.0, sig.sourceNode.energy + 0.6);
      }
    }
  }

  updateNodes() {
    for (const n of this.nodes) {
      n.x = n.baseX + Math.cos(this.tick + n.phase) * 6;
      n.y = n.baseY + Math.sin(this.tick * 0.8 + n.phase) * 6;
    }
  }

  drawLinks() {
    for (const link of this.links) {
      const a = link.source, b = link.target;
      const activeSignal = Math.sin(this.tick * 3 + link.activePhase);
      const intensity = activeSignal > 0.7 ? 0.6 : 0.15;
      const alpha = link.weight * intensity;
      this.ctx.strokeStyle = activeSignal > 0.8 ? `rgba(0, 255, 160, ${alpha * 2})` : `rgba(90, 200, 255, ${alpha})`;
      this.ctx.lineWidth = link.weight * 1.5;
      this.ctx.beginPath();
      this.ctx.moveTo(a.x, a.y);
      const midX = (a.x + b.x) / 2;
      const cp1x = a.x + (midX - a.x) * 0.5, cp1y = a.y;
      const cp2x = b.x - (b.x - midX) * 0.5, cp2y = b.y;
      this.ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, b.x, b.y);
      this.ctx.stroke();
    }
  }

  drawNodes() {
    for (let i = 0; i < this.nodes.length; i++) {
      const n = this.nodes[i];
      const pulse = 0.55 + Math.sin(this.tick * 2 + n.phase) * 0.45;
      const rr = n.r * (0.8 + pulse * 0.3);
      const isHover = i === this.hoverIndex;
      const color = clusterColor(n.cluster, n.energy);
      this.ctx.beginPath();
      this.ctx.arc(n.x, n.y, rr + (isHover ? 3 : 0), 0, Math.PI * 2);
      this.ctx.fillStyle = color;
      this.ctx.shadowBlur = isHover ? 20 : (pulse * 10 + 4);
      this.ctx.shadowColor = color;
      this.ctx.fill();
      this.ctx.beginPath();
      this.ctx.arc(n.x, n.y, rr * 0.4, 0, Math.PI * 2);
      this.ctx.fillStyle = '#ffffff';
      this.ctx.fill();
      this.ctx.shadowBlur = 0;
    }
  }

  drawSignals() {
    for (const sig of this.signals) {
      if (!sig.targetNode) continue;
      const a = sig.sourceNode, b = sig.targetNode, p = sig.progress;
      const midX = (a.x + b.x) / 2;
      const cp1x = a.x + (midX - a.x) * 0.5, cp1y = a.y;
      const cp2x = b.x - (b.x - midX) * 0.5, cp2y = b.y;
      const mt = 1 - p, mt2 = mt * mt, mt3 = mt2 * mt;
      const p2 = p * p, p3 = p2 * p;
      const x = mt3 * a.x + 3 * mt2 * p * cp1x + 3 * mt * p2 * cp2x + p3 * b.x;
      const y = mt3 * a.y + 3 * mt2 * p * cp1y + 3 * mt * p2 * cp2y + p3 * b.y;
      this.ctx.beginPath();
      this.ctx.arc(x, y, 5, 0, Math.PI * 2);
      this.ctx.fillStyle = sig.color;
      this.ctx.shadowBlur = 18;
      this.ctx.shadowColor = sig.color;
      this.ctx.fill();
      this.ctx.shadowBlur = 0;
    }
  }
}

function fmtInt(v) { try { return Number(v || 0).toLocaleString(); } catch { return String(v || 0); } }

function clusterColor(cluster, energy) {
  const palette = [[0,220,255],[0,255,160],[180,140,255],[255,120,180],[255,200,90]];
  const base = palette[Math.abs(cluster) % palette.length];
  const a = 0.25 + Math.max(0.0, Math.min(1.0, energy)) * 0.7;
  return `rgba(${base[0]},${base[1]},${base[2]},${a})`;
}

/* ======================== Layout ======================== */

function buildLayout() {
  const style = `
    .rl-dash { display: flex; flex-direction: column; height: 100%; min-height: 0; gap: 15px; padding: 15px; }
    .rl-head { display: flex; justify-content: space-between; align-items: center; flex-shrink: 0; }
    .rl-title { margin:0; font-size: 1.4rem; font-weight: 700; color: #fff; letter-spacing: 0.5px; }

    .rl-mode-group { display: flex; background: rgba(0,0,0,0.4); border-radius: 8px; padding: 4px; border: 1px solid rgba(255,255,255,0.05); }
    .rl-mode-btn { border: none; background: transparent; color: #888; padding: 6px 14px; font-size: 0.8rem; font-weight: 600; cursor: pointer; border-radius: 6px; transition: 0.3s; }
    .rl-mode-btn.active { background: rgba(0, 255, 160, 0.15); color: #00ffa0; box-shadow: 0 0 0 1px rgba(0, 255, 160, 0.4) inset; }

    .rl-main-grid { display: grid; grid-template-columns: 320px 1fr 300px; gap: 15px; flex: 1; min-height: 0; }

    .rl-panel { background: rgba(10, 14, 18, 0.6); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 12px; backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); display: flex; flex-direction: column; overflow: hidden; }
    .rl-panel-header { padding: 12px 14px; font-size: 0.75rem; font-weight: 700; color: #9bb; text-transform: uppercase; letter-spacing: 1px; border-bottom: 1px solid rgba(255,255,255,0.03); background: rgba(0,0,0,0.2); flex-shrink: 0; display:flex; justify-content: space-between; align-items: center; }
    .rl-panel-body { padding: 12px; flex: 1; min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; }

    .rl-canvas-area { flex: 1; position: relative; background: radial-gradient(circle at center, rgba(0,255,160,0.03) 0%, transparent 60%); }

    .rl-metrics-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; padding: 12px 14px; background: rgba(0,0,0,0.2); border-bottom: 1px solid rgba(255,255,255,0.03); }
    .rl-metric { text-align: center; }
    .rl-metric-val { font-size: 1.3rem; font-weight: 800; font-family: 'Fira Code', monospace; line-height: 1; }
    .rl-metric-lbl { font-size: 0.6rem; color: #666; text-transform: uppercase; margin-top: 6px; letter-spacing: 0.5px; }

    .rl-graph-container { height: 100px; padding: 0 14px 14px; flex-shrink: 0; }

    .rl-tag { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); padding: 5px 8px; border-radius: 6px; font-size: 0.7rem; color: #ccc; white-space: nowrap; margin-bottom: 4px; }

    .rl-scrollable::-webkit-scrollbar { width: 4px; }
    .rl-scrollable::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 4px; }

    .rl-table { width: 100%; font-size: 0.75rem; text-align: left; border-collapse: collapse; }
    .rl-table th { padding: 6px; color: #666; font-weight: normal; border-bottom: 1px solid rgba(255,255,255,0.05); }
    .rl-table td { padding: 6px; border-bottom: 1px solid rgba(255,255,255,0.02); color: #ccc; }

    /* Manual mode overlay */
    .rl-manual-overlay { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 16px; background: rgba(5, 7, 9, 0.85); z-index: 5; }
    .rl-manual-icon { font-size: 3rem; opacity: 0.6; }
    .rl-manual-title { font-size: 1.4rem; font-weight: 800; color: #ffd166; letter-spacing: 2px; text-transform: uppercase; }
    .rl-manual-sub { font-size: 0.85rem; color: #888; text-align: center; max-width: 280px; line-height: 1.5; }
    .rl-manual-badge { padding: 6px 16px; border: 1px solid rgba(255,209,102,0.3); border-radius: 8px; background: rgba(255,209,102,0.08); color: #ffd166; font-size: 0.75rem; font-weight: 700; letter-spacing: 1px; animation: rl-pulse 2s ease-in-out infinite; }
    @keyframes rl-pulse { 0%,100% { opacity: 0.7; } 50% { opacity: 1; } }

    /* Center panel header mode badge */
    .rl-mode-badge { padding: 2px 10px; border-radius: 4px; font-size: 0.65rem; font-weight: 800; letter-spacing: 1px; }
    .rl-mode-badge.manual { background: rgba(255,209,102,0.15); color: #ffd166; border: 1px solid rgba(255,209,102,0.3); }
    .rl-mode-badge.auto { background: rgba(0,220,255,0.15); color: #00dcff; border: 1px solid rgba(0,220,255,0.3); }
    .rl-mode-badge.ai { background: rgba(0,255,160,0.15); color: #00ffa0; border: 1px solid rgba(0,255,160,0.3); }

    @media (max-width: 1200px) {
        .rl-main-grid { grid-template-columns: 1fr 1fr; }
        .rl-panel-center { grid-column: 1 / -1; height: 350px; order: -1; }
    }
    @media (max-width: 800px) {
        .rl-dash { padding: 10px; gap: 10px; overflow-y: auto; display: block; }
        .rl-head { margin-bottom: 10px; flex-wrap: wrap; gap: 10px; }
        .rl-main-grid { display: flex; flex-direction: column; overflow: visible; }
        .rl-panel { flex: none; height: 350px; }
        .rl-panel-center { height: 300px; }
    }
  `;

  return el('div', { class: 'rl-dash page-main' }, [
    el('style', {}, [style]),

    el('div', { class: 'rl-head' }, [
      el('h2', { class: 'rl-title' }, ['AI Dashboard']),
      el('div', { class: 'rl-mode-group' }, [
        el('button', { class: 'rl-mode-btn', id: 'mode-manual', onclick: () => setOperationMode('MANUAL') }, ['MANUAL']),
        el('button', { class: 'rl-mode-btn', id: 'mode-auto', onclick: () => setOperationMode('AUTO') }, ['AUTO']),
        el('button', { class: 'rl-mode-btn', id: 'mode-ai', onclick: () => setOperationMode('AI') }, ['AI']),
      ])
    ]),

    el('div', { class: 'rl-main-grid' }, [
      /* Left Panel: Stats & Graph */
      el('div', { class: 'rl-panel' }, [
        el('div', { class: 'rl-panel-header' }, ['Model Status']),
        el('div', { class: 'rl-metrics-grid' }, [
          el('div', { class: 'rl-metric' }, [el('div', { class: 'rl-metric-val', id: 'val-episodes' }, ['0']), el('div', { class: 'rl-metric-lbl' }, ['Episodes'])]),
          el('div', { class: 'rl-metric' }, [el('div', { class: 'rl-metric-val', id: 'val-epsilon', style: 'color:cyan' }, ['0.00']), el('div', { class: 'rl-metric-lbl' }, ['Epsilon'])]),
          el('div', { class: 'rl-metric' }, [el('div', { class: 'rl-metric-val', id: 'val-qsize' }, ['0']), el('div', { class: 'rl-metric-lbl' }, ['Q-Size'])]),
        ]),
        el('div', { class: 'rl-graph-container' }, [
          el('canvas', { id: 'metrics-canvas', style: 'width:100%; height:100%;' }),
        ]),
        el('div', { class: 'rl-panel-header', style: 'border-top: 1px solid rgba(255,255,255,0.03);' }, ['Model Manifest']),
        el('div', { class: 'rl-panel-body rl-scrollable' }, [
          el('div', { id: 'model-manifest', style: 'display:flex; flex-wrap:wrap; gap:6px;' })
        ])
      ]),

      /* Center: Canvas (mode-aware) */
      el('div', { class: 'rl-panel rl-panel-center' }, [
        el('div', { class: 'rl-panel-header' }, [
          el('span', { id: 'center-panel-title' }, ['Neural Network Architecture']),
          el('span', { id: 'center-mode-badge', class: 'rl-mode-badge auto' }, ['AUTO']),
        ]),
        el('div', { class: 'rl-canvas-area', id: 'brain-canvas-area' }, [
          el('canvas', { id: 'brain-canvas', style: 'width:100%; height:100%; position:absolute; top:0; left:0;' }),
          el('div', { id: 'brain-tooltip', style: 'position:absolute; top:0; left:0; background:rgba(0,0,0,0.85); border:1px solid var(--acid); color:#fff; padding:6px 10px; border-radius:6px; font-size:0.75rem; pointer-events:none; display:none; z-index:10; white-space:nowrap; box-shadow: 0 4px 12px rgba(0,0,0,0.5);' }),
          /* Manual mode overlay (hidden by default) */
          el('div', { class: 'rl-manual-overlay', id: 'manual-overlay', style: 'display:none;' }, [
            el('div', { class: 'rl-manual-icon' }, ['\uD83D\uDD79\uFE0F']),
            el('div', { class: 'rl-manual-title' }, ['Manual Mode']),
            el('div', { class: 'rl-manual-sub' }, ['AI and heuristic engines are paused. Actions are triggered manually by the operator.']),
            el('div', { class: 'rl-manual-badge' }, ['OPERATOR CONTROL']),
          ]),
        ])
      ]),

      /* Right Panel: Recent & Confidence */
      el('div', { class: 'rl-panel' }, [
        el('div', { class: 'rl-panel-header' }, ['Log Feed & Signals']),
        el('div', { class: 'rl-panel-body rl-scrollable' }, [
          el('div', { style: 'margin-bottom: 12px;' }, [
            el('div', { style: 'font-size:0.75rem; color:#888; margin-bottom:8px; display:flex; justify-content:space-between;' }, [
              el('span', {}, ['LATEST SIGNALS']),
              el('span', { style: 'color:var(--acid);' }, ['\u25CF LIVE'])
            ]),
            el('div', { id: 'confidence-bars', style: 'display:flex; flex-direction:column; gap:8px;' }),
          ]),
          el('div', { style: 'margin-bottom: 12px;' }, [
            el('div', { style: 'font-size:0.75rem; color:#888; margin-bottom:8px;' }, ['RECENT EXPERIENCES']),
            el('div', { id: 'experience-feed', style: 'display:flex; flex-direction:column; gap:6px;' }),
          ]),
          el('div', {}, [
            el('div', { style: 'font-size:0.75rem; color:#888; margin-bottom:8px;' }, ['DATA SYNC']),
            el('table', { class: 'rl-table' }, [
              el('thead', {}, [el('tr', {}, [el('th', {}, ['Time']), el('th', {}, ['Rec.']), el('th', {}, ['Status'])])]),
              el('tbody', { id: 'history-body' }),
            ]),
          ])
        ])
      ])
    ])
  ]);
}

/* ======================== Mode-aware canvas switching ======================== */

function switchCanvasMode(mode) {
  const m = String(mode).toUpperCase().trim();
  if (m === _currentMode) return;
  _currentMode = m;

  const overlay = $('#manual-overlay');
  const canvas = document.getElementById('brain-canvas');
  const title = $('#center-panel-title');
  const badge = $('#center-mode-badge');

  if (badge) {
    badge.textContent = m;
    badge.className = `rl-mode-badge ${m.toLowerCase()}`;
  }

  if (m === 'MANUAL') {
    /* Show manual overlay, hide canvas visualizations */
    if (overlay) overlay.style.display = 'flex';
    if (canvas) canvas.style.opacity = '0.1';
    if (title) title.textContent = 'Manual Mode';
    /* Destroy active visualizations */
    if (modelCloud) { modelCloud.destroy(); modelCloud = null; }
    if (heuristicGraph) { heuristicGraph.destroy(); heuristicGraph = null; }

  } else if (m === 'AUTO') {
    /* Show heuristic flow graph */
    if (overlay) overlay.style.display = 'none';
    if (canvas) canvas.style.opacity = '1';
    if (title) title.textContent = 'Heuristic Engine';
    /* Destroy neural cloud, create heuristic graph */
    if (modelCloud) { modelCloud.destroy(); modelCloud = null; }
    if (!heuristicGraph && canvas) {
      heuristicGraph = new HeuristicGraph('brain-canvas');
      if (tracker) tracker.trackResource(() => heuristicGraph && heuristicGraph.destroy());
    }

  } else {
    /* AI mode: show neural network */
    if (overlay) overlay.style.display = 'none';
    if (canvas) canvas.style.opacity = '1';
    if (title) title.textContent = 'Neural Network Architecture';
    /* Destroy heuristic, create neural cloud */
    if (heuristicGraph) { heuristicGraph.destroy(); heuristicGraph = null; }
    if (!modelCloud && canvas) {
      modelCloud = new ModelCloud('brain-canvas');
      if (tracker) tracker.trackResource(() => modelCloud && modelCloud.destroy());
    }
  }
}

/* ======================== Fetchers ======================== */

async function fetchStats() {
  try {
    const data = await api.get('/api/rl/stats');
    if (!data || !tracker) return;

    if (!metricsGraph && document.getElementById('metrics-canvas')) {
      metricsGraph = new MultiMetricGraph('metrics-canvas');
      if (tracker) tracker.trackResource(() => metricsGraph && metricsGraph.destroy());
    }
    if (metricsGraph) metricsGraph.update(data);

    const mode = data.mode || (data.ai_mode ? 'AI' : data.manual_mode ? 'MANUAL' : 'AUTO');
    updateModeUI(mode);
    switchCanvasMode(mode);

    /* Update stats (only for AI/Auto) */
    if (modelCloud) modelCloud.updateFromStats(data);

    setText($('#val-episodes'), data.episodes ?? 0);
    setText($('#val-epsilon'), Number(data.epsilon || 0).toFixed(4));
    setText($('#val-qsize'), data.q_table_size ?? 0);

    updateManifest(data);

    if (Array.isArray(data.recent_activity) && data.recent_activity.length) {
      renderConfidenceBars(data.recent_activity);
      if (modelCloud) modelCloud.triggerActivity(data.recent_activity);
      if (heuristicGraph) heuristicGraph.triggerActivity(data.recent_activity);
    }
  } catch (e) {
    console.error(e);
  }
}

function updateManifest(data) {
  const manifest = $('#model-manifest');
  if (!manifest) return;
  empty(manifest);
  const tags = [
    `MODE: ${_currentMode}`,
    `MODEL: ${data.model_loaded ? 'LOADED' : 'HEURISTIC'}`,
    `VERSION: ${data.model_version || 'N/A'}`,
    `PARAMS: ${fmtInt(data.model_param_count || 0)}`,
    `LAYERS: ${data.model_layer_count || 0}`,
    `FEATURES: ${data.model_feature_count || 0}`,
    `SAMPLES: ${fmtInt(data.training_samples || 0)}`,
  ];
  tags.forEach((txt) => manifest.appendChild(el('div', { class: 'rl-tag' }, [txt])));
}

function renderConfidenceBars(activity) {
  const container = $('#confidence-bars');
  if (!container) return;
  empty(container);
  activity.forEach((act) => {
    const reward = Number(act.reward || 0);
    const color = reward > 0 ? 'var(--acid)' : '#ff3333';
    const success = reward > 0;
    container.appendChild(el('div', { style: 'display:flex; flex-direction:column; gap:4px;' }, [
      el('div', { style: 'display:flex; justify-content:space-between; font-size:0.75rem; color:#ccc;' }, [
        el('span', {}, [act.action || '-']),
        el('span', { style: `color:${color}; font-weight:bold;` }, [success ? 'CONFIDENT' : 'UNCERTAIN']),
      ]),
      el('div', { style: 'height:3px; background:rgba(255,255,255,0.05); border-radius:2px; overflow:hidden' }, [
        el('div', { style: `height:100%; background:${color}; width:${Math.min(Math.abs(reward) * 5, 100)}%; transition:width 0.45s ease-out` }),
      ]),
    ]));
  });
}

async function fetchHistory() {
  try {
    const data = await api.get('/api/rl/history');
    if (!data || !tracker || !Array.isArray(data.history)) return;
    const tbody = $('#history-body');
    empty(tbody);
    data.history.forEach((row) => {
      const ts = String(row.timestamp || '');
      const parsed = new Date(ts.includes('Z') ? ts : `${ts}Z`);
      tbody.appendChild(el('tr', {}, [
        el('td', {}, [Number.isFinite(parsed.getTime()) ? parsed.toLocaleTimeString() : ts]),
        el('td', {}, [String(row.record_count || 0)]),
        el('td', { style: 'color:var(--acid)' }, ['COMPLETED']),
      ]));
    });
  } catch (e) { console.error(e); }
}

async function fetchExperiences() {
  try {
    const data = await api.get('/api/rl/experiences');
    if (!data || !tracker || !Array.isArray(data.experiences)) return;
    const container = $('#experience-feed');
    empty(container);
    data.experiences.forEach((exp) => {
      let color = '#ccc';
      if (exp.reward > 0) color = 'var(--acid)';
      if (exp.reward < 0) color = '#ff3333';
      container.appendChild(el('div', {
        style: `padding:6px 8px; background:rgba(255,255,255,0.02); border-radius:6px; border-left:2px solid ${color}; font-size:0.75rem;`,
      }, [
        el('div', { style: 'display:flex;justify-content:space-between; margin-bottom:3px;' }, [
          el('strong', { style: `color:${color};` }, [exp.action_name || '-']),
          el('span', { style: `font-weight:bold; color:${color};` }, [exp.reward > 0 ? `+${exp.reward}` : `${exp.reward}`]),
        ]),
        el('div', { style: 'color:#888;' }, [
          el('span', {}, [new Date(String(exp.timestamp || '').includes('Z') ? exp.timestamp : `${exp.timestamp}Z`).toLocaleTimeString()]),
          ' - ',
          el('span', {}, [exp.success ? 'SUCCESS' : 'FAIL']),
        ]),
      ]));
    });
  } catch (e) { console.error(e); }
}

function updateModeUI(mode) {
  if (!mode) return;
  const m = String(mode).toUpperCase().trim();
  ['MANUAL', 'AUTO', 'AI'].forEach((v) => {
    const btn = $(`#mode-${v.toLowerCase()}`);
    if (!btn) return;
    btn.classList.toggle('active', v === m);
  });
}

async function setOperationMode(mode) {
  try {
    const data = await api.post('/api/rl/config', { mode });
    if (data.status === 'ok') {
      updateModeUI(data.mode);
      switchCanvasMode(data.mode);
      if (window.toast) window.toast(`Operation Mode: ${data.mode}`);
      const bc = new BroadcastChannel('bjorn_mode_sync');
      bc.postMessage({ mode: data.mode });
      bc.close();
    } else if (window.toast) {
      window.toast(`Error: ${data.message}`, 'error');
    }
  } catch (err) {
    console.error(err);
    if (window.toast) window.toast('Communication Error', 'error');
  }
}
