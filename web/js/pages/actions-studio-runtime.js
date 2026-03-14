/**
 * Actions Studio runtime for SPA mode.
 * Keeps graph behavior from original studio while running inside route mount/unmount lifecycle.
 */
import { t } from '../core/i18n.js';

export function mountStudioRuntime(__root) {
  const tracked = [];
  const nativeAdd = EventTarget.prototype.addEventListener;
  const nativeRemove = EventTarget.prototype.removeEventListener;
  EventTarget.prototype.addEventListener = function(type, listener, options) {
    tracked.push([this, type, listener, options]);
    return nativeAdd.call(this, type, listener, options);
  };
  try {
/* ===================== Config & State ===================== */
const API_BASE = window.BJORN_API_BASE || '/api';

const state = {
  actions: new Map(),        // b_class -> action
  hosts: new Map(),          // mac -> host
  nodes: new Map(),          // nodeId -> {type, data, x, y, slots:{in:[],out:[]}}
  links: [],                 // [{id,from,to,type,mode,label?}]
  selected: null,
  pan: { x: 0, y: 0, scale: 1 },
  placedActions: new Set(),
  placedHosts: new Set(),
  minGapH: 220,
  minGapV: 150,
  testMode:true
};

const PREFS_KEY = 'bjorn_studio_prefs_v2';
const defaultPrefs = {
  pan: { x: 0, y: 0, scale: 1 },
  activeTab: 'actions',
  mobileLeftOpen: false,
  mobileRightOpen: false,
  hideCanvasHint: false,
};
let prefsSaveTimer = null;

const root = __root || document;
const $  = s => root.querySelector(s);
const $$ = s => Array.from(root.querySelectorAll(s));
const uid = (p='id') => p + '_' + Math.random().toString(36).slice(2) + Date.now().toString(36);
const clamp = (v,a,b)=>Math.max(a,Math.min(b,v));
const tryJSON = (s, fb) => { try { return typeof s === 'string' ? JSON.parse(s) : (s ?? fb); } catch { return fb; } };
const toCSV   = a => (a || []).join(', ');
const fromCSV = s => (s || '').split(',').map(x => x.trim()).filter(Boolean);

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return { ...defaultPrefs };
    const parsed = JSON.parse(raw);
    return {
      ...defaultPrefs,
      ...parsed,
      pan: {
        ...defaultPrefs.pan,
        ...(parsed?.pan || {}),
      },
    };
  } catch {
    return { ...defaultPrefs };
  }
}

function savePrefsNow(next) {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(next));
  } catch { /* no-op */ }
}

function queuePrefsSave(partial = {}) {
  const current = loadPrefs();
  const merged = {
    ...current,
    ...partial,
    pan: {
      ...current.pan,
      ...(partial.pan || {}),
    },
  };
  if (prefsSaveTimer) clearTimeout(prefsSaveTimer);
  prefsSaveTimer = setTimeout(() => savePrefsNow(merged), 90);
}

function updateSearchClear(idInput, idClear) {
  const input = $(idInput);
  const clear = $(idClear);
  if (!input || !clear) return;
  clear.classList.toggle('show', !!input.value);
}

const initialPrefs = loadPrefs();
state.pan = {
  ...state.pan,
  ...(initialPrefs.pan || {}),
};
state.pan.scale = clamp(Number(state.pan.scale) || 1, 0.25, 2.5);

/* ===================== API (mock fallback) ===================== */
async function fetchActions(){
  try{
    const r = await fetch(`${API_BASE}/studio/actions_studio`);
    if(!r.ok) throw 0; const j = await r.json(); return Array.isArray(j)?j:(j.data||[]);
  }catch{
    // Fallback de démo
    return [
      { b_class:'NetworkScanner', b_module:'network_scanner', b_action:'global', b_trigger:'on_interval:600', b_priority:10, b_enabled:1, b_icon:'NetworkScanner.png' },
      { b_class:'SSHbruteforce', b_module:'ssh_bruteforce',  b_trigger:'on_new_port:22', b_priority:70, b_enabled:1, b_port:22, b_service:'["ssh"]', b_icon:'SSHbruteforce.png' },
      { b_class:'StealFilesSSH',  b_module:'steal_files_ssh', b_trigger:'on_success:SSHbruteforce', b_priority:70, b_enabled:1, b_requires:'{"all":[{"has_port":22},{"service_is_open":"ssh"}]}', b_icon:'StealFilesSSH.png' },
      { b_class:'ScanSSH',        b_module:'ssh_scan',        b_trigger:'on_service:ssh',      b_priority:60, b_enabled:1, b_icon:'ScanSSH.png' },
      { b_class:'NmapVuln',       b_module:'nmap_vuln',       b_trigger:'on_new_port:445',     b_priority:11, b_enabled:1, b_icon:'NmapVulnScanner.png' }
    ];
  }
}
async function fetchHosts(){
  try{
    const r = await fetch(`${API_BASE}/studio/hosts`);
    if(!r.ok) throw 0; const j = await r.json(); return Array.isArray(j)?j:(j.data||[]);
  }catch{
    return [
      { mac_address:'AA:BB:CC:DD:EE:FF', hostname:'server-01', ips:'192.168.1.100', ports:'22;80;443', services:'[{"port":22,"service":"ssh"},{"port":80,"service":"http"}]', vulns:'CVE-2023-0001', creds:'[]', alive:1, is_simulated:0 },
      { mac_address:'11:22:33:44:55:66', hostname:'db-01',     ips:'192.168.1.101', ports:'3306;22',  services:'[{"port":3306,"service":"mysql"},{"port":22,"service":"ssh"}]', vulns:'', creds:'[]', alive:1, is_simulated:0 },
      { mac_address:'22:33:44:55:66:77', hostname:'cam-01',    ips:'192.168.1.120', ports:'554;80',   services:'[{"port":80,"service":"http"}]', vulns:'', creds:'[]', alive:0, is_simulated:0 }
    ];
  }
}
async function saveToStudio(){
  const data = { edges: state.links, nodes: [], transform: state.pan };
  state.nodes.forEach((n,id)=> data.nodes.push({id,...n}));
  try{
    const r = await fetch(`${API_BASE}/studio/save`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
    if(!r.ok) throw 0; toast(t('studio.saved'),'success');
  }catch{
    localStorage.setItem('bjorn_studio_backup', JSON.stringify(data));
    toast(t('studio.localBackup'),'warn');
  }
}
async function applyToRuntime(){
  try{ const r = await fetch(`${API_BASE}/studio/apply`,{method:'POST'}); if(!r.ok) throw 0; toast(t('studio.applied'),'success'); }
  catch{ toast(t('studio.applyFailed'),'error'); }
}

/* ===================== Helpers UI ===================== */
function toast(msg,type='info'){
  const t=document.createElement('div');
  t.className=`studio-toast ${type}`; t.textContent=msg;
  document.body.appendChild(t);
  requestAnimationFrame(()=>{ t.style.opacity='1'; });
  setTimeout(()=>{ t.style.opacity='0'; setTimeout(()=>t.remove(),260); }, 2100);
}
function updateStats(){
  const nodes = state.nodes.size;
  const links = state.links.length;
  if ($('#nodeCount')) $('#nodeCount').textContent = String(nodes);
  if ($('#linkCount')) $('#linkCount').textContent = String(links);
  if ($('#nodeCountMini')) $('#nodeCountMini').textContent = String(nodes);
  if ($('#linkCountMini')) $('#linkCountMini').textContent = String(links);
}
const byHostnameIpMac = (a,b)=>{
  const aHost=(a.hostname||'').toLowerCase(), bHost=(b.hostname||'').toLowerCase();
  if(aHost||bHost){ const c=aHost.localeCompare(bHost); if(c) return c; }
  const aIp=(a.ips||'').split(/[,; ]/)[0]||'', bIp=(b.ips||'').split(/[,; ]/)[0]||'';
  if(aIp||bIp){ const c=aIp.localeCompare(bIp,undefined,{numeric:true,sensitivity:'base'}); if(c) return c; }
  return (a.mac_address||'').localeCompare(b.mac_address||'');
};
function resolveIcon(action){
  const raw=(action?.b_icon||'').toString().trim();
  const name=raw || `${action.b_class}.png`;
  if(/^https?:\/\//.test(name) || name.startsWith('/')) return name;
  return `/actions/actions_icons/${encodeURIComponent(name)}`;
}

/* ===================== Palette ===================== */
function buildPalette(){
  const list=$('#plist'); list.innerHTML='';
  const q=($('#filterActions').value||'').toLowerCase();
  const arr=[...state.actions.values()].sort((a,b)=>a.b_class.localeCompare(b.b_class));
  let visibleCount = 0;
  for(const a of arr){
    if(q && !a.b_class.toLowerCase().includes(q) && !(a.b_module||'').toLowerCase().includes(q)) continue;
    visibleCount++;
    const placed=state.placedActions.has(a.b_class);
    const el=document.createElement('div'); el.className=`pitem ${placed?'placed':''}`; el.draggable=true;
    const icon=resolveIcon(a);
    el.innerHTML=`
      <div style="display:flex;align-items:center;flex:1">
        ${icon?`<img src="${icon}" class="action-icon" onerror="this.style.display='none'">`:''}
        <div><div class="nname">${a.b_class}${a.b_action==='global'?' <span style="color:#cbb8ff">[GLOBAL]</span>':''}</div>
        <div class="pmeta">${a.b_module||''} · prio:${a.b_priority??'-'}</div></div>
      </div>
      <button class="padd">➕</button>`;
    el.addEventListener('dragstart',e=>e.dataTransfer.setData('action',JSON.stringify(a)));
    el.querySelector('.padd').addEventListener('click',()=>dropActionCenter(a));
    list.appendChild(el);
  }
  if (!visibleCount) {
    const empty = document.createElement('div');
    empty.className = 'small';
    empty.textContent = t('studio.noActionsMatch');
    list.appendChild(empty);
  }
  const total = arr.length;
  const placed = state.placedActions.size;
  if ($('#actionsTotalCount')) $('#actionsTotalCount').textContent = String(total);
  if ($('#actionsPlacedCount')) $('#actionsPlacedCount').textContent = String(placed);
  updateSearchClear('#filterActions', '#clearFilterActions');
}
function buildHostPalette(){
  const real=$('#realHosts'), test=$('#testHosts'); real.innerHTML=''; test.innerHTML='';
  const q=($('#filterHosts').value||'').toLowerCase();
  const all=[...state.hosts.values()]
    .filter(h=>!q || (h.mac_address||'').toLowerCase().includes(q) || (h.hostname||'').toLowerCase().includes(q) || (h.ips||'').toLowerCase().includes(q))
    .sort(byHostnameIpMac);
  let visibleReal = 0;
  let visibleTest = 0;
  for(const h of all){
    const placed=state.placedHosts.has(h.mac_address);
    const el=document.createElement('div'); el.className=`host-card ${h.is_simulated?'simulated':''}`;
    el.innerHTML=`
      <div><b style="color:#9effc5">${h.hostname || h.ips || h.mac_address}</b></div>
      <div class="row"><div>IP: ${h.ips||'—'}</div><div>Ports: ${h.ports||'—'}</div><div>Alive: ${h.alive?'🟢':'🔴'}</div></div>
      <div class="row">
        <button class="btn" onclick="addHostToCanvas('${h.mac_address}')">${placed?'Focus':'Place'}</button>
        ${h.is_simulated?`<button class="btn" onclick="deleteTestHost('${h.mac_address}')">🗑</button>`:''}
      </div>`;
    if (h.is_simulated) {
      visibleTest++;
      test.appendChild(el);
    } else {
      visibleReal++;
      real.appendChild(el);
    }
  }
  if (!visibleReal) {
    const empty = document.createElement('div');
    empty.className = 'small';
    empty.textContent = t('studio.noRealHostsMatch');
    real.appendChild(empty);
  }
  if (!visibleTest) {
    const empty = document.createElement('div');
    empty.className = 'small';
    empty.textContent = t('studio.noTestHostsYet');
    test.appendChild(empty);
  }
  const allHosts = [...state.hosts.values()];
  const aliveCount = allHosts.filter(h => parseInt(h.alive, 10) === 1).length;
  if ($('#hostsTotalCount')) $('#hostsTotalCount').textContent = String(allHosts.length);
  if ($('#hostsAliveCount')) $('#hostsAliveCount').textContent = String(aliveCount);
  if ($('#hostsPlacedCount')) $('#hostsPlacedCount').textContent = String(state.placedHosts.size);
  updateSearchClear('#filterHosts', '#clearFilterHosts');
}

/* ===================== Nodes ===================== */
function addActionNode(action,x=100,y=100){
  const id=uid('action'); const node={type:'action',data:action,x,y}; state.nodes.set(id,node); state.placedActions.add(action.b_class);
  const isGlobal=action.b_action==='global';
  const el=document.createElement('div'); el.className=`node ${isGlobal?'global':''}`; el.dataset.id=id; el.dataset.type='action';
  el.style.left=x+'px'; el.style.top=y+'px';
  const icon=resolveIcon(action);
  el.innerHTML=`
    <div class="nhdr"><div class="nname">${action.b_class}</div><span class="badge">${action.b_action||'normal'}</span><button class="nclose">×</button></div>
    <div class="nbody">
      ${icon?`<img src="${icon}" class="node-icon" style="width:70px;height:70px;display:block;margin:0 auto 8px" onerror="this.style.display='none'">`:''}
      <div class="row"><span class="k">module:</span><span class="v">${action.b_module||'—'}</span></div>
      <div class="row"><span class="k">trigger:</span><span class="v trigger">${summTrig(action.b_trigger||'')}</span></div>
      <div class="row"><span class="k">priority:</span><span class="v">${action.b_priority??50}</span></div>
      <div class="row"><span class="k">requires:</span><span class="v requires">${requireSummary(action)}</span></div>
    </div>
    <div class="rail left"  data-side="in"></div><div class="rail right" data-side="out"></div>`;
  $('#nodes').appendChild(el);
  el.querySelector('.nclose').addEventListener('click',e=>{e.stopPropagation(); deleteNode(id);});
  setupNodeEvents(el,id);
  buildPalette(); updateStats(); LinkEngine.rebuildRails(); LinkEngine.render();
  return id;
}
function addHostNode(host,x=100,y=100){
  const id=uid('host'); const node={type:'host',data:host,x,y}; state.nodes.set(id,node); state.placedHosts.add(host.mac_address);
  const el=document.createElement('div'); el.className='node host'; el.dataset.id=id; el.dataset.type='host';
  el.style.left=x+'px'; el.style.top=y+'px';
  el.innerHTML=`
    <div class="nhdr"><div class="nname">${host.hostname || host.ips || host.mac_address}</div><span class="badge">HOST</span><button class="nclose">×</button></div>
    <div class="nbody">
      <div class="row"><span class="k">IP:</span><span class="v">${host.ips||'—'}</span></div>
      <div class="row"><span class="k">Ports:</span><span class="v">${host.ports||'—'}</span></div>
      <div class="row"><span class="k">Alive:</span><span class="v">${host.alive?'🟢':'🔴'}</span></div>
      ${host.is_simulated?'<div class="row"><span class="badge" style="background:var(--neon2)">TEST HOST</span></div>':''}
    </div>
    <div class="rail left" data-side="in"></div><div class="rail right" data-side="out"></div>`;
  $('#nodes').appendChild(el);
  el.querySelector('.nclose').addEventListener('click',e=>{e.stopPropagation(); deleteNode(id);});
  setupNodeEvents(el,id);
  buildHostPalette(); updateStats(); LinkEngine.rebuildRails(); LinkEngine.render();
  return id;
}
function deleteNode(id){
  const n=state.nodes.get(id); if(!n) return;
  if(n.type==='action'){ state.placedActions.delete(n.data.b_class); buildPalette(); }
  if(n.type==='host'){ state.placedHosts.delete(n.data.mac_address); buildHostPalette(); }
  state.links = state.links.filter(l=>l.from!==id && l.to!==id);
  const el=$(`[data-id="${id}"]`); if(el) el.remove();
  state.nodes.delete(id);
  state.selected=null; $('#edit').style.display='none'; $('#noSel').style.display='block'; $('#hostInspector').style.display='none';
  LinkEngine.rebuildRails(); LinkEngine.render(); updateStats();
}
function dropActionCenter(a){
  const rect=$('#center').getBoundingClientRect();
  const x=(rect.width/2 - state.pan.x)/state.pan.scale - 120;
  const y=(rect.height/2 - state.pan.y)/state.pan.scale - 80;
  addActionNode(a,x,y);
}

/* ===================== Node Events / Drag ===================== */
function setupNodeEvents(el,id){
  el.addEventListener('click',()=>selectNode(id));
  const beginDrag=(clientX,clientY)=>{
    const n=state.nodes.get(id); if(!n) return;
    const drag={x:clientX,y:clientY,nx:n.x,ny:n.y};
    return drag;
  };
  const applyDrag=(drag,clientX,clientY)=>{
    const n=state.nodes.get(id); if(!n) return;
    const dx=(clientX-drag.x)/state.pan.scale, dy=(clientY-drag.y)/state.pan.scale;
    n.x=drag.nx+dx; n.y=drag.ny+dy; el.style.left=n.x+'px'; el.style.top=n.y+'px';
    LinkEngine.render();
  };

  let drag=null;
  el.addEventListener('mousedown',e=>{
    if(e.target.closest('.rail') || e.target.classList.contains('nclose')) return;
    e.preventDefault();
    drag=beginDrag(e.clientX,e.clientY); if(!drag) return;
    const move=ev=>applyDrag(drag,ev.clientX,ev.clientY);
    const up=()=>{ document.removeEventListener('mousemove',move); document.removeEventListener('mouseup',up); drag=null; repelLayout(); };
    document.addEventListener('mousemove',move); document.addEventListener('mouseup',up);
  });

  el.addEventListener('touchstart',e=>{
    if(e.touches.length!==1) return;
    if(e.target.closest('.rail') || e.target.classList.contains('nclose')) return;
    const t=e.touches[0];
    drag=beginDrag(t.clientX,t.clientY); if(!drag) return;
    const move=ev=>{
      if(!drag || ev.touches.length!==1) return;
      const tt=ev.touches[0];
      applyDrag(drag,tt.clientX,tt.clientY);
      ev.preventDefault();
    };
    const up=()=>{
      document.removeEventListener('touchmove',move);
      document.removeEventListener('touchend',up);
      document.removeEventListener('touchcancel',up);
      drag=null;
      repelLayout();
    };
    document.addEventListener('touchmove',move,{passive:false});
    document.addEventListener('touchend',up);
    document.addEventListener('touchcancel',up);
  },{passive:true});
}
function selectNode(id){
  $$('.node.sel').forEach(n=>n.classList.remove('sel'));
  const el=$(`[data-id="${id}"]`); if(el) el.classList.add('sel');
  state.selected=id;
  const n=state.nodes.get(id); if(!n) return;
  if(n.type==='action') showActionInspector(n.data);
  else if(n.type==='host') showHostInspector(n.data);
}

/* ===================== Rails & Links Engine ===================== */
const LinkEngine = {
  slotGap: 18, railPad: 12,
  rebuildRails(){
    state.nodes.forEach(n=>{ n.slots={in:[],out:[]}; });
    for(const l of state.links){
      const a=state.nodes.get(l.from), b=state.nodes.get(l.to);
      if(!a||!b) continue; a.slots.out.push(l); b.slots.in.push(l);
    }
    const yOf=id=>{ const el=$(`[data-id="${id}"]`), n=state.nodes.get(id); return (n && el)? n.y + el.offsetHeight/2 : 0; };
    state.nodes.forEach((n,id)=>{
      n.slots.in.sort((l1,l2)=>yOf(l1.from)-yOf(l2.from));
      n.slots.out.sort((l1,l2)=>yOf(l1.to)-yOf(l2.to));
      const el=$(`[data-id="${id}"]`); if(!el) return;
      const railL=el.querySelector('.rail.left'), railR=el.querySelector('.rail.right');
      const needIn=Math.max(1,n.slots.in.length), needOut=Math.max(1,n.slots.out.length);
      const needMax=Math.max(needIn,needOut);
      const body=el.querySelector('.nbody'); const base=Math.max(140, body?body.scrollHeight+24:140);
      const need=this.railPad*2 + needMax*this.slotGap;
      el.style.minHeight=Math.max(base,need)+'px';
      const build=(rail,count,side)=>{
        rail.innerHTML=''; for(let i=0;i<count;i++){ const d=document.createElement('div'); d.className='port'; d.dataset.side=side; d.dataset.index=String(i); rail.appendChild(d);
          d.addEventListener('mousedown',e=>startConnect(e,id,side,i));
          d.addEventListener('touchstart',e=>startConnect(e,id,side,i),{passive:false}); }
        const add=document.createElement('div'); add.className='port add'; add.dataset.side=side; add.dataset.index=String(count); rail.appendChild(add);
        add.addEventListener('mousedown',e=>startConnect(e,id,side,count));
        add.addEventListener('touchstart',e=>startConnect(e,id,side,count),{passive:false});
      };
      build(railL,needIn,'in'); build(railR,needOut,'out');
      n.slots.in.forEach((l,i)=> l._inSlot=i);
      n.slots.out.forEach((l,i)=> l._outSlot=i);
    });
  },
  anchor(id,side,slot=0){
    const n=state.nodes.get(id), el=$(`[data-id="${id}"]`); if(!n||!el) return {x:0,y:0};
    const rail=el.querySelector(side==='in'?'.rail.left':'.rail.right');
    const r=rail.getBoundingClientRect(), c=$('#center').getBoundingClientRect();
    const x=(r.left-c.left-state.pan.x)/state.pan.scale + r.width/2 + (side==='in'?-9:9);
    const y=(r.top-c.top-state.pan.y)/state.pan.scale + this.railPad + slot*this.slotGap + 6;
    return {x,y};
  },
  evaluate(link){
    const A=state.nodes.get(link.from), B=state.nodes.get(link.to);
    if(!A||!B) return {cls:'req',lbl:link.label||'requires'};
    if(A.type==='host' && B.type==='action'){
      const ev = evaluateHostToAction(link); return {cls: ev.ok?'ok':'bad', lbl: ev.label};
    }
    if(link.type==='success') return {cls:'ok', lbl:'on_success'};
    if(link.type==='failure') return {cls:'bad', lbl:'on_failure'};
    return {cls:'req', lbl: link.label||'requires'};
  },
  route(link){
    const a=this.anchor(link.from,'out',link._outSlot||0);
    const b=this.anchor(link.to,'in', link._inSlot||0);
    const lane=((hash(link.from+':'+link.to)%7)-3)*8;
    const midX=((a.x+b.x)/2)+lane;
    return `M ${a.x} ${a.y} L ${midX} ${a.y} L ${midX} ${b.y} L ${b.x} ${b.y}`;
  },
  render(){
    const svg=$('#links'); svg.innerHTML='';
    this.rebuildRails();
    for(const l of state.links){
      const style=this.evaluate(l);
      const path=document.createElementNS('http://www.w3.org/2000/svg','path');
      path.classList.add('path',style.cls); if(style.cls==='ok') path.classList.add('flow');
      path.setAttribute('d',this.route(l));
      path.dataset.linkId=l.id;
      path.addEventListener('click',e=>openEdgeMenu(l,e.clientX,e.clientY));
      svg.appendChild(path);

      const a=this.anchor(l.from,'out',l._outSlot||0), b=this.anchor(l.to,'in',l._inSlot||0);
      const midX=(a.x+b.x)/2, midY=(a.y+b.y)/2;
      const txt=document.createElementNS('http://www.w3.org/2000/svg','text');
      txt.classList.add('edgelabel',style.cls); txt.setAttribute('x',midX); txt.setAttribute('y',midY-8); txt.textContent=style.lbl;
      txt.dataset.linkId=l.id; txt.addEventListener('click',e=>openEdgeMenu(l,e.clientX,e.clientY));
      svg.appendChild(txt);
    }
    updateStats();
  }
};
function hash(s){let h=0;for(let i=0;i<s.length;i++){h=((h<<5)-h)+s.charCodeAt(i);h|=0;}return Math.abs(h);}

/* ===================== Connect Drag ===================== */
let tempPath=null, connectFrom=null;
function startConnect(e,fromId,side,slotIndex){
  e.preventDefault();
  const isTouch = !!e.touches;
  const getPoint = ev => {
    if (ev.touches && ev.touches[0]) return { x: ev.touches[0].clientX, y: ev.touches[0].clientY };
    if (ev.changedTouches && ev.changedTouches[0]) return { x: ev.changedTouches[0].clientX, y: ev.changedTouches[0].clientY };
    return { x: ev.clientX, y: ev.clientY };
  };
  connectFrom={id:fromId,side,slot:slotIndex};
  const svg=$('#links');
  tempPath=document.createElementNS('http://www.w3.org/2000/svg','path');
  tempPath.classList.add('path','req'); tempPath.style.strokeDasharray='6 8';
  svg.appendChild(tempPath);

  const move=ev=>{
    const p = getPoint(ev);
    const rect=$('#center').getBoundingClientRect();
    const cx=(p.x-rect.left-state.pan.x)/state.pan.scale;
    const cy=(p.y-rect.top -state.pan.y)/state.pan.scale;
    const a=LinkEngine.anchor(fromId,side,slotIndex);
    const midX=(a.x+cx)/2;
    tempPath.setAttribute('d',`M ${a.x} ${a.y} L ${midX} ${a.y} L ${midX} ${cy} L ${cx} ${cy}`);
  };
  const up=ev=>{
    if (isTouch) {
      document.removeEventListener('touchmove',move);
      document.removeEventListener('touchend',up);
      document.removeEventListener('touchcancel',up);
    } else {
      document.removeEventListener('mousemove',move);
      document.removeEventListener('mouseup',up);
    }
    if(tempPath){tempPath.remove(); tempPath=null;}
    const p = getPoint(ev);
    const t=document.elementFromPoint(p.x,p.y);
    const rail=t?.closest?.('.rail'); if(!rail) return;
    const toEl=rail.closest('.node'); if(!toEl) return;
    const toId=toEl.dataset.id; const toSide=rail.classList.contains('left')?'in':'out';
    if(toId===fromId || side===toSide) return;

    const actualFrom = side==='out'? fromId : toId;
    const actualTo   = side==='out'? toId   : fromId;

    openLinkWizard(actualFrom,actualTo,null);
  };
  if (isTouch) {
    document.addEventListener('touchmove',move,{passive:false});
    document.addEventListener('touchend',up);
    document.addEventListener('touchcancel',up);
  } else {
    document.addEventListener('mousemove',move);
    document.addEventListener('mouseup',up);
  }
}

/* ===================== Edge Menu & Wizard ===================== */
let edgeMenuLink=null;
function openEdgeMenu(link, x, y){
  edgeMenuLink=link;
  const m=$('#edgeMenu'); m.style.left=x+'px'; m.style.top=y+'px'; m.classList.add('show');
}
window.addEventListener('click',e=>{ const m=$('#edgeMenu'); if(!m) return; if(!e.target.closest('.edge-menu')) m.classList.remove('show'); });
$('#edgeMenu').addEventListener('click',e=>{
  const act=e.target.closest('.edge-menu-item')?.dataset.action; if(!act) return;
  if(act==='delete'){ state.links=state.links.filter(l=>l.id!==edgeMenuLink.id); LinkEngine.render(); }
  if(act==='toggle-success'){ edgeMenuLink.type='success'; edgeMenuLink.mode='trigger'; syncActionForEdge(edgeMenuLink); LinkEngine.render(); }
  if(act==='toggle-failure'){ edgeMenuLink.type='failure'; edgeMenuLink.mode='trigger'; syncActionForEdge(edgeMenuLink); LinkEngine.render(); }
  if(act==='toggle-req'){ edgeMenuLink.type='requires'; edgeMenuLink.mode='requires'; LinkEngine.render(); }
  if(act==='edit'){ openLinkWizard(edgeMenuLink.from, edgeMenuLink.to, edgeMenuLink); }
  $('#edgeMenu').classList.remove('show');
});

const linkWizard = { from:null,to:null,editing:null, presets:[] };
function prettyNodeName(n){ if(n.type==='action') return n.data?.b_class||'Action'; if(n.type==='host') return n.data?.hostname||n.data?.ips||n.data?.mac_address||'Host'; return 'Node'; }
function splitTriggerSafe(s){ s=(s||'').trim(); const i=s.indexOf(':'); return i===-1?{name:s,param:''}:{name:s.slice(0,i),param:s.slice(i+1)}; }
function summTrig(t){ if(!t) return '—'; const {name,param}=splitTriggerSafe(t); if(name==='on_any'||name==='on_all'){ try{const a=JSON.parse(param); return `${name}(${Array.isArray(a)?a.length:0})`;}catch{} } return t; }
function requireSummary(action){ const r=tryJSON(action.b_requires,null); if(!r) return '—'; if(r.all) return 'ALL '+r.all.length; if(r.any) return 'ANY '+r.any.length; if(r.action) return `${r.action}:${r.status||'success'}`; return Object.keys(r).join(', '); }

function computePresets(fromNode,toNode,mode){
  const list=[], add=(id,label)=>list.push({id,label});
  const ctx=`${fromNode.type}->${toNode.type}`;
  if(mode==='trigger'){
    if(ctx==='action->action'){ add('on_success','on_success (from action)'); add('on_failure','on_failure (from action)'); }
    else if(ctx==='host->action'){ add('on_service','on_service:<service>'); add('on_web_service','on_web_service'); add('on_new_port','on_new_port:<port>'); add('on_port_change','on_port_change:<port>'); add('on_has_cve','on_has_cve:<CVE>'); add('on_mac_is','on_mac_is:<MAC>'); add('on_ip_is','on_ip_is:<IP>'); add('on_essid_is','on_essid_is:<ESSID>'); add('on_host_alive','on_host_alive'); }
  }else{
    if(ctx==='action->action'){ add('req_action','requires action:status'); }
    else if(ctx==='host->action'){ add('has_port','requires has_port:<port>'); add('service_is_open','requires service_is_open:<service>'); add('has_cve','requires has_cve:<CVE>'); add('has_cpe','requires has_cpe:<CPE>'); add('has_cred','requires has_cred:<service>'); add('mac_is','requires mac_is:<MAC>'); add('essid_is','requires essid_is:<ESSID>'); }
  }
  return list;
}
function guessParams(fromNode,toNode,preset){
  const host = fromNode.type==='host' ? fromNode.data : (toNode.type==='host'?toNode.data:null);
  let def1='',def2='',ph1='Param 1',ph2='Param 2'; const p=preset?.id;
  if(host){
    const sv=tryJSON(host.services,[]); const s1=(sv[0]?.service)||'ssh'; const po=(sv[0]?.port)||parseInt((host.ports||'').split(/[,; ]+/)[0]||'22',10);
    const cve=(host.vulns||'').split(/[,; ]+/).filter(Boolean)[0]||'CVE-2023-0001';
    if(p==='on_service'||p==='service_is_open'){def1=s1; ph1='service';}
    if(p==='on_new_port'||p==='on_port_change'||p==='has_port'){def1=po||22; ph1='port';}
    if(p==='on_has_cve'||p==='has_cve'){def1=cve; ph1='CVE-YYYY-NNNN';}
    if(p==='mac_is'||p==='on_mac_is'){def1=host.mac_address||''; ph1='AA:BB:…';}
    if(p==='on_ip_is'){def1=(host.ips||'').split(/[,; ]+/)[0]||''; ph1='192.168.x.x';}
    if(p==='on_essid_is'||p==='essid_is'){def1=host.essid||''; ph1='ESSID';}
  }
  if(p==='req_action'){ def1= fromNode.type==='action' ? (fromNode.data?.b_class||'') : ''; def2='success'; ph1='ActionName'; ph2='status'; }
  if(p==='on_all'||p==='on_any'){ def1='["ActionA","ActionB"]'; ph1='JSON array'; }
  return {def1,def2,ph1,ph2};
}
function normalizeAnyAllParam(raw){
  const t=(raw||'').trim(); if(!t) return '[]';
  try{ const a=JSON.parse(t); if(Array.isArray(a)) return JSON.stringify(a.map(String)); }catch{}
  return JSON.stringify(t.split(',').map(s=>s.trim()).filter(Boolean));
}
function updateLWPreview(){
  const mode=$('#lwMode').value, pid=$('#lwPreset').value, p1=($('#lwParam1').value||'').trim(), p2=($('#lwParam2').value||'').trim();
  let txt='—';
  if(mode==='trigger'){
    if(pid==='on_success'||pid==='on_failure'){ txt=`${pid}:${$('#lwFromName').textContent}`; }
    else if(pid==='on_all'||pid==='on_any'){ txt=`${pid}:${normalizeAnyAllParam(p1)}`; }
    else if(pid==='on_web_service'){ txt='on_web_service'; }
    else{ const val=(pid==='on_new_port'||pid==='on_port_change') && p1 ? String(parseInt(p1,10)) : p1; txt=`${pid}${val?':'+val:''}`; }
  }else{
    if(pid==='req_action'){ txt=`requires:{action:"${p1||'Action'}",status:"${p2||'success'}"}`; }
    else{ const v=(pid==='has_port'&&p1)?Number(p1):(p1||'?'); const value=typeof v==='number'&&!isNaN(v)?String(v):`"${v}"`; txt=`requires:{"${pid}":${value}}`; }
  }
  $('#lwPreview').textContent=txt;
}
function openLinkWizard(from,to,editing){
  linkWizard.from=from; linkWizard.to=to; linkWizard.editing=editing||null;
  const nf=state.nodes.get(from), nt=state.nodes.get(to);
  $('#lwFromName').textContent=prettyNodeName(nf); $('#lwToName').textContent=prettyNodeName(nt);
  const modeSel=$('#lwMode'); modeSel.value=(editing?.mode)||'trigger';
  const presetSel=$('#lwPreset'); presetSel.innerHTML='';
  linkWizard.presets=computePresets(nf,nt,modeSel.value);
  linkWizard.presets.forEach((p,i)=>{ const o=document.createElement('option'); o.value=p.id; o.textContent=p.label; if(i===0) o.selected=true; presetSel.appendChild(o); });
  const {def1,def2,ph1,ph2}=guessParams(nf,nt,linkWizard.presets[0]);
  $('#lwParam1').value=editing?.label||def1||''; $('#lwParam2').value=def2||''; $('#lwParam1').placeholder=ph1||''; $('#lwParam2').placeholder=ph2||''; updateLWPreview();
  $('#linkWizard').classList.add('show');
}
$('#lwClose').addEventListener('click',()=>$('#linkWizard').classList.remove('show'));
$('#lwCancel').addEventListener('click',()=>$('#linkWizard').classList.remove('show'));
$('#lwMode').addEventListener('change',()=>{
  const nf=state.nodes.get(linkWizard.from), nt=state.nodes.get(linkWizard.to);
  const presetSel=$('#lwPreset'); presetSel.innerHTML=''; linkWizard.presets=computePresets(nf,nt,$('#lwMode').value);
  linkWizard.presets.forEach((p,i)=>{ const o=document.createElement('option'); o.value=p.id; o.textContent=p.label; if(i===0) o.selected=true; presetSel.appendChild(o); });
  const g=guessParams(nf,nt,linkWizard.presets[0]); $('#lwParam1').value=g.def1||''; $('#lwParam2').value=g.def2||''; $('#lwParam1').placeholder=g.ph1||''; $('#lwParam2').placeholder=g.ph2||''; updateLWPreview();
});
$('#lwPreset').addEventListener('change',()=>{
  const nf=state.nodes.get(linkWizard.from), nt=state.nodes.get(linkWizard.to);
  const sel=linkWizard.presets.find(p=>p.id===$('#lwPreset').value); const g=guessParams(nf,nt,sel);
  $('#lwParam1').value=g.def1||''; $('#lwParam2').value=g.def2||''; $('#lwParam1').placeholder=g.ph1||''; $('#lwParam2').placeholder=g.ph2||''; updateLWPreview();
});
$('#lwParam1').addEventListener('input',updateLWPreview);
$('#lwParam2').addEventListener('input',updateLWPreview);

$('#lwCreate').addEventListener('click',()=>{
  const from=linkWizard.from, to=linkWizard.to; if(!from||!to) return;
  const nf=state.nodes.get(from), nt=state.nodes.get(to);
  const mode=$('#lwMode').value, pid=$('#lwPreset').value, p1=$('#lwParam1').value.trim(), p2=$('#lwParam2').value.trim();

  if(linkWizard.editing){ state.links = state.links.filter(l=>l.id!==linkWizard.editing.id); }
  createContextualLink(nf,nt,mode,pid,p1,p2,from,to);
  $('#linkWizard').classList.remove('show');
  LinkEngine.render();
  repelLayout();
});
function ensureLink(obj){ const ex=state.links.find(l=>l.from===obj.from&&l.to===obj.to&&l.type===obj.type&&(l.mode||'')===(obj.mode||'')&&(l.label||'')===(obj.label||'')); if(!ex){ obj.id=uid('edge'); state.links.push(obj); } return obj; }
function createContextualLink(fromNode,toNode,mode,presetId,p1,p2,fromId,toId){
  const push=(type,label,mode)=>ensureLink({from:fromId,to:toId,type,mode:mode||null,label:label||null});
  const refreshCard=(id,a)=>{ const el=$(`[data-id="${id}"]`); if(!el) return; const t=el.querySelector('.v.trigger'); const r=el.querySelector('.v.requires'); if(t) t.textContent=summTrig(a.b_trigger||''); if(r) r.textContent=requireSummary(a); };

  if(mode==='trigger' && toNode.type==='action'){
    let trig='';
    if(presetId==='on_success'||presetId==='on_failure'){
      const fromName=(fromNode.type==='action')?(fromNode.data?.b_class||'Action'):''; trig=`${presetId}:${fromName}`; push(presetId==='on_failure'?'failure':'success',presetId,'trigger');
    }else if(presetId==='on_web_service'){ trig='on_web_service'; push('requires','on_web_service','trigger'); }
    else if(presetId==='on_all'||presetId==='on_any'){ trig=`${presetId}:${normalizeAnyAllParam(p1)}`; push('requires',trig,'trigger'); }
    else{ const v=(presetId==='on_new_port'||presetId==='on_port_change')&&p1?String(parseInt(p1,10)):p1; trig=`${presetId}${v?':'+v:''}`; push('requires',trig,'trigger'); }
    toNode.data.b_trigger=trig; refreshCard(toId,toNode.data);
  }else if(toNode.type==='action'){ // requires
    let obj=null;
    if(presetId==='req_action'){ obj={action:p1|| (fromNode.type==='action'?fromNode.data?.b_class||'Action':'Action'), status:p2||'success'}; }
    else if(presetId==='has_port'){ obj={has_port:Number(p1)}; }
    else if(presetId==='service_is_open'){ obj={service_is_open:p1||'ssh'}; }
    else if(presetId==='has_cve'){ obj={has_cve:p1||'CVE-2023-0001'}; }
    else if(presetId==='has_cpe'){ obj={has_cpe:p1||'cpe:/a:vendor:product:version'}; }
    else if(presetId==='has_cred'){ obj={has_cred:p1||'ssh'}; }
    else if(presetId==='mac_is'){ obj={mac_is:p1||''}; }
    else if(presetId==='essid_is'){ obj={essid_is:p1||''}; }
    if(obj){
      toNode.data.b_requires = addRequirementClause(toNode.data.b_requires, obj);
      refreshCard(toId,toNode.data);
      push('requires',presetId,'requires');
    }
  }
}
function syncActionForEdge(edge){
  const A=state.nodes.get(edge.from), B=state.nodes.get(edge.to);
  if(!A||!B) return;
  if(edge.mode==='trigger' && edge.type!=='requires' && A.type==='action' && B.type==='action'){
    B.data.b_trigger = `on_${edge.type}:${A.data.b_class}`;
    const el=$(`[data-id="${edge.to}"] .v.trigger`); if(el) el.textContent=summTrig(B.data.b_trigger);
  }
}
function addRequirementClause(current, clause){
  const r=tryJSON(current,null);
  if(!r) return JSON.stringify(clause);
  if(r.all){ r.all.push(clause); return JSON.stringify(r); }
  if(r.any){ r.any.push(clause); return JSON.stringify(r); }
  return JSON.stringify({ all:[ r, clause ] });
}

/* ===================== Host evaluation ===================== */
function parseHostServices(host){
  let arr=tryJSON(host.services,[]); if(!Array.isArray(arr)) arr=[];
  const names=new Set(arr.map(s=>String(s.service||s.name||'').toLowerCase()).filter(Boolean));
  const ports=new Set(arr.map(s=>Number(s.port)).filter(p=>Number.isFinite(p)));
  (host.ports||'').split(/[,; ]+/).map(x=>parseInt(x,10)).filter(n=>!isNaN(n)).forEach(p=>ports.add(p));
  return {names,ports};
}
function splitTriggerList(raw){
  if(!raw) return {mode:'any',list:[]};
  if(Array.isArray(raw)) return {mode:'any',list:raw.map(String)};
  const t=String(raw).trim();
  if(t.startsWith('[')){ try{const a=JSON.parse(t); if(Array.isArray(a)) return {mode:'any',list:a.map(String)};}catch{} }
  const {name,param}=splitTriggerSafe(t);
  if(name==='on_all'||name==='on_any'){ try{const a=JSON.parse(param); return {mode:name==='on_all'?'all':'any',list:Array.isArray(a)?a.map(String):[]};}catch{} }
  return {mode:'any',list:[t]};
}
function hostMatchesSingleTriggerString(trigStr, host){
  const {name,param}=splitTriggerSafe(trigStr);
  const {names,ports}=parseHostServices(host);
  const vulns=(host.vulns||'').split(/[,; ]+/).map(s=>s.trim()).filter(Boolean);
  if(name==='on_service')     return names.has(String(param).toLowerCase());
  if(name==='on_web_service') return names.has('http')||names.has('https');
  if(name==='on_new_port'||name==='on_port_change') return ports.has(parseInt(param,10));
  if(name==='on_has_cve')     return vulns.includes(String(param));
  if(name==='on_mac_is')      return (host.mac_address||'').toLowerCase()===String(param).toLowerCase();
  if(name==='on_ip_is')       return (host.ips||'').split(/[,; ]+/).includes(String(param));
  if(name==='on_essid_is')    return (host.essid||'')===String(param);
  if(name==='on_host_alive')  return parseInt(host.alive)==1;
  if(name==='on_host_dead')   return parseInt(host.alive)==0;
  if(name==='on_new_host')    return true;
  return false;
}
function hostMatchesActionByTriggers(action, host){
  const {mode,list}=splitTriggerList(action.b_trigger||'');
  if(list.length===0) return false;
  const res=list.map(t=>hostMatchesSingleTriggerString(t,host));
  return mode==='all'? res.every(Boolean): res.some(Boolean);
}
function checkHostRequires(reqRaw,host){
  const req=tryJSON(reqRaw,null); if(!req) return true;
  const {names,ports}=parseHostServices(host);
  const vulns=(host.vulns||'').split(/[,; ]+/).map(s=>s.trim()).filter(Boolean);
  const check=r=>{
    if(r.has_port!=null) return ports.has(parseInt(r.has_port,10));
    if(r.service_is_open) return names.has(String(r.service_is_open).toLowerCase());
    if(r.has_cve) return vulns.includes(String(r.has_cve));
    if(r.has_cpe) return (host.cpe||'').includes(String(r.has_cpe));
    if(r.has_cred) return (host.creds||'[]').includes(String(r.has_cred));
    if(r.mac_is) return (host.mac_address||'').toLowerCase()===String(r.mac_is).toLowerCase();
    if(r.essid_is) return (host.essid||'')===String(r.essid_is);
    if(r.action) return false;
    return false;
  };
  if(req.all) return req.all.every(check);
  if(req.any) return req.any.some(check);
  return check(req);
}
function evaluateHostToAction(link){
  const A=state.nodes.get(link.from), B=state.nodes.get(link.to);
  if(!A||!B||A.type!=='host'||B.type!=='action') return {ok:false,label:link.label||'requires'};
  const h=A.data, a=B.data;
  let ok=false;
  if(link.mode==='trigger') ok = hostMatchesActionByTriggers(a,h);
  else if(link.mode==='requires') ok = checkHostRequires(a.b_requires,h);
  else ok = hostMatchesActionByTriggers(a,h) || checkHostRequires(a.b_requires,h);
  return {ok,label:link.label|| (link.mode==='trigger'?'trigger':'requires')};
}

// remplace complètement la fonction existante
function repelLayout(iter = 16, str = 0.6) {
  const HOST_X  = 80;   // X fixe pour la colonne des hosts (même valeur que l’autolayout)
  const TOP_Y   = 60;   // Y de départ de la colonne
  const V_GAP   = 160;  // espacement vertical entre hosts

  const ids = [...state.nodes.keys()];
  const boxes = ids.map(id => {
    const n  = state.nodes.get(id);
    const el = document.querySelector(`[data-id="${id}"]`);
    if (!n || !el) return null;
    const w = el.offsetWidth, h = el.offsetHeight;
    return {
      id, type: n.type,
      x: n.x, y: n.y, w, h,
      cx: n.x + w / 2, cy: n.y + h / 2
    };
  }).filter(Boolean);

  if (boxes.length < 2) { LinkEngine.render(); return; }

  // répulsion douce en évitant de bouger les hosts en X
  for (let it = 0; it < iter; it++) {
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const a = boxes[i], b = boxes[j];
        const dx = b.cx - a.cx, dy = b.cy - a.cy;
        const ox = (a.w/2 + b.w/2 + state.minGapH) - Math.abs(dx);
        const oy = (a.h/2 + b.h/2 + state.minGapV) - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          const pushX = (ox/2) * str * Math.sign(dx || (Math.random() - .5));
          const pushY = (oy/2) * str * Math.sign(dy || (Math.random() - .5));

          // Sur l’axe X, on NE BOUGE PAS les hosts
          const aCanX = a.type !== 'host';
          const bCanX = b.type !== 'host';

          if (ox > oy) { // pousser surtout en X
            if (aCanX && bCanX) { a.x -= pushX; a.cx -= pushX; b.x += pushX; b.cx += pushX; }
            else if (aCanX)      { a.x -= 2*pushX; a.cx -= 2*pushX; }
            else if (bCanX)      { b.x += 2*pushX; b.cx += 2*pushX; }
            // sinon (deux hosts) : on ne touche pas l’axe X
          } else {        // pousser surtout en Y (hosts OK en Y)
            a.y -= pushY; a.cy -= pushY;
            b.y += pushY; b.cy += pushY;
          }
        }
      }
    }
  }

  // Snap final : hosts parfaitement en colonne et espacés régulièrement
  const hosts = boxes.filter(b => b.type === 'host').sort((u, v) => u.y - v.y);
  hosts.forEach((b, i) => { b.x = HOST_X; b.cx = b.x + b.w/2; b.y = TOP_Y + i * V_GAP; b.cy = b.y + b.h/2; });

  // appliquer positions au DOM + state
  boxes.forEach(b => {
    const n  = state.nodes.get(b.id);
    const el = document.querySelector(`[data-id="${b.id}"]`);
    n.x = b.x; n.y = b.y;
    el.style.left = n.x + 'px';
    el.style.top  = n.y + 'px';
  });

  LinkEngine.render();
}
/* ===== Auto-layout: hosts en colonne verticale (X constant), actions à droite ===== */
function autoLayout(){
  const col = new Map(); // id -> column
  const set=(id,c)=>col.set(id, Math.max(c, col.get(id)??-Infinity));

  // Colonne 0 = HOSTS
  state.nodes.forEach((n,id)=>{ if(n.type==='host') set(id,0); });

  // Colonnes suivantes = actions (en fonction des dépendances action->action)
  const edges=[];
  state.links.forEach(l=>{
    const A=state.nodes.get(l.from), B=state.nodes.get(l.to);
    if(A&&B && A.type==='action' && B.type==='action') edges.push([l.from,l.to]);
  });
  const g=new Map(); edges.forEach(([u,v])=>{ if(!g.has(u)) g.set(u,[]); g.get(u).push(v); });
  const memo=new Map();
  const depth=(id)=>{ if(memo.has(id)) return memo.get(id); const nxt=(g.get(id)||[]); let d=0; for(const v of nxt) d=Math.max(d,1+depth(v)); memo.set(id,d); return d; };
  state.nodes.forEach((n,id)=>{ if(n.type==='action'){ set(id, 1 + depth(id)); } });

  const cols=new Map(); col.forEach((c,id)=>{ if(!cols.has(c)) cols.set(c,[]); cols.get(c).push(id); });
  const xPad=80, colW=320, gapH=Math.max(200,state.minGapH);

  cols.forEach((ids,c)=>{
    const prev=cols.get(c-1)||[];
    const avgY=id=>{
      const up=prev.filter(p=> state.links.find(l=>l.from===p && l.to===id));
      if(up.length===0) return 0;
      return up.reduce((s,p)=> s + (state.nodes.get(p).y||0),0)/up.length;
    };
    // tri : hosts triés par hostname/IP/MAC pour une colonne bien lisible
    ids.sort((a,b)=>{
      if(c===0){
        const na=state.nodes.get(a), nb=state.nodes.get(b);
        if(na?.type==='host' && nb?.type==='host') return byHostnameIpMac(na.data, nb.data);
      }
      return avgY(a)-avgY(b);
    });
    ids.forEach((id,i)=>{
      const n=state.nodes.get(id), el=$(`[data-id="${id}"]`);
      const vGap = c===0 ? 160 : Math.max(140,state.minGapV);
      n.x = xPad + c*(colW+gapH*0.25);
      n.y = 60 + i*vGap;
      el.style.left=n.x+'px'; el.style.top=n.y+'px';
    });
  });
  // à la fin d'autoLayout():
  repelLayout(6, 0.4); // applique aussi le snap vertical des hosts

  toast(t('studio.autoLayoutApplied'),'success');
}

/* ===================== Inspectors ===================== */
function showActionInspector(a){
  $('#actionInspector').style.display='block'; $('#hostInspector').style.display='none';
  $('#edit').style.display='block'; $('#noSel').style.display='none';
  $('#e_class').value=a.b_class||''; $('#e_module').value=a.b_module||''; $('#e_status').value=a.b_status||a.b_class||'';
  $('#e_type').value=a.b_action||'normal'; $('#e_enabled').value=String(a.b_enabled??1);
  $('#e_prio').value=a.b_priority??50; $('#e_timeout').value=a.b_timeout??300; $('#e_retry').value=a.b_max_retries??3;
  $('#e_cool').value=a.b_cooldown??0; $('#e_rate').value=a.b_rate_limit||''; $('#e_port').value=a.b_port??'';
  $('#e_services').value=toCSV(tryJSON(a.b_service,[])); $('#e_tags').value=a.b_tags||'[]';
  const {name,param}=splitTriggerSafe(a.b_trigger||''); $('#t_type').value=name||'on_host_alive'; $('#t_param').value=param||'';
  buildReqUI(a);
}
function buildReqUI(a){
  const root=$('#r_list'); root.innerHTML='';
  const req=tryJSON(a.b_requires,null); let mode='all',items=[];
  if(req){ if(req.all){mode='all'; items=req.all.slice();} else if(req.any){mode='any'; items=req.any.slice();} else {mode='all'; items=[req];} }
  $('#r_mode').value=mode;
  items.forEach(it=>root.appendChild(reqRow(it)));
  $('#r_add').onclick=()=>{ root.appendChild(reqRow({action:'SomeAction',status:'success'})); sync(); };
  $('#r_mode').onchange=sync;
  function reqRow(it){
    const row=document.createElement('div'); row.className='row'; row.style.cssText='align-items:flex-end;margin:4px 0';
    row.innerHTML=`<label style="flex:1"><span>Type</span>
        <select class="rt">
          <option value="action">action:status</option>
          <option value="has_port">has_port</option>
          <option value="has_cred">has_cred</option>
          <option value="has_cve">has_cve</option>
          <option value="has_cpe">has_cpe</option>
          <option value="mac_is">mac_is</option>
          <option value="essid_is">essid_is</option>
          <option value="service_is_open">service_is_open</option>
        </select></label>
      <label style="flex:1"><span>Param 1</span><input class="rp1"></label>
      <label style="flex:1"><span>Param 2</span><input class="rp2" placeholder="status si action"></label>
      <button class="btn" title="Delete">🗑</button>`;
    const rt=row.querySelector('.rt'), rp1=row.querySelector('.rp1'), rp2=row.querySelector('.rp2'), del=row.querySelector('button');
    if(it.action){ rt.value='action'; rp1.value=it.action; rp2.value=it.status||'success'; } else { const k=Object.keys(it)[0]; if(k){ rt.value=k; rp1.value=it[k]; } }
    rt.onchange=()=>{ rp1.value=''; rp2.value=''; sync(); }; rp1.oninput=rp2.oninput=sync; del.onclick=()=>{ row.remove(); sync(); };
    return row;
  }
  function sync(){
    const md=$('#r_mode').value; const rows=[...root.children].map(r=>{
      const t=r.querySelector('.rt').value, p1=r.querySelector('.rp1').value.trim(), p2=r.querySelector('.rp2').value.trim();
      if(t==='action') return {action:p1, status:p2||'success'};
      if(p1) return {[t]:t==='has_port'?Number(p1):p1};
      return null;
    }).filter(Boolean);
    let obj=null; if(rows.length===1) obj=rows[0]; else if(rows.length>1) obj={[md]:rows};
    a.b_requires = obj?JSON.stringify(obj):'';
    const sel=state.selected&&state.nodes.get(state.selected); if(sel&&sel.type==='action'){ const el=$(`[data-id="${state.selected}"] .v.requires`); if(el) el.textContent=requireSummary(a); }
    LinkEngine.render();
  }
}
function showHostInspector(h){
  $('#actionInspector').style.display='none'; $('#hostInspector').style.display='block';
  $('#h_mac').value=h.mac_address||''; $('#h_hostname').value=h.hostname||''; $('#h_ips').value=h.ips||''; $('#h_ports').value=h.ports||''; $('#h_alive').value=h.alive?'1':'0';
  $('#h_essid').value=h.essid||''; $('#h_services').value=h.services||'[]';
  $('#h_vulns').value=h.vulns||''; $('#h_creds').value=h.creds||'[]';
}

/* ===================== Pan / Zoom ===================== */
function applyPanZoom(){
  $('#canvas').style.transform=`translate(${state.pan.x}px,${state.pan.y}px) scale(${state.pan.scale})`;
  queuePrefsSave({ pan: state.pan });
}
$('#center').addEventListener('wheel',e=>{
  e.preventDefault();
  const delta=e.deltaY>0?0.9:1.1; const rect=$('#center').getBoundingClientRect();
  const x=e.clientX-rect.left, y=e.clientY-rect.top;
  const before={x:(x-state.pan.x)/state.pan.scale, y:(y-state.pan.y)/state.pan.scale};
  state.pan.scale=clamp(state.pan.scale*delta,0.25,2.5);
  state.pan.x=x-before.x*state.pan.scale; state.pan.y=y-before.y*state.pan.scale; applyPanZoom();
},{passive:false});
$('#zIn').addEventListener('click',()=>{ state.pan.scale=clamp(state.pan.scale*1.2,0.25,2.5); applyPanZoom(); });
$('#zOut').addEventListener('click',()=>{ state.pan.scale=clamp(state.pan.scale/1.2,0.25,2.5); applyPanZoom(); });
$('#zFit').addEventListener('click',()=>fitToScreen());
function fitToScreen(){
  if(state.nodes.size===0) return;
  let minX=Infinity,minY=Infinity,maxX=-Infinity,maxY=-Infinity;
  state.nodes.forEach((n,id)=>{ const el=$(`[data-id="${id}"]`); if(!el) return; minX=Math.min(minX,n.x); minY=Math.min(minY,n.y); maxX=Math.max(maxX,n.x+el.offsetWidth); maxY=Math.max(maxY,n.y+el.offsetHeight); });
  const rect=$('#center').getBoundingClientRect(), pad=50;
  const scaleX=(rect.width - pad*2)/Math.max(1,(maxX-minX));
  const scaleY=(rect.height- pad*2)/Math.max(1,(maxY-minY));
  const s=Math.min(scaleX,scaleY,1);
  state.pan={x:rect.width/2 - ((minX+maxX)/2)*s, y:rect.height/2 - ((minY+maxY)/2)*s, scale:s};
  applyPanZoom();
}

/* ===== background drag = pan (mouse & touch) ===== */
let panning=null;
$('#center').addEventListener('mousedown',e=>{
  const isInteractive = e.target.closest('.node') || e.target.closest('.rail') || e.target.closest('.edge-menu') || e.target.closest('.modal');
  if(e.button===1 || (e.button===0 && !isInteractive)){
    panning={x:e.clientX,y:e.clientY,px:state.pan.x,py:state.pan.y};
    $('#center').style.cursor='grabbing';
  }
});
document.addEventListener('mousemove',e=>{
  if(!panning) return;
  state.pan.x = panning.px + (e.clientX - panning.x);
  state.pan.y = panning.py + (e.clientY - panning.y);
  applyPanZoom();
});
document.addEventListener('mouseup',()=>{ if(panning){ panning=null; $('#center').style.cursor='grab'; } });

let touchPan=null;
let touchPinch=null;
const touchDist=(a,b)=>Math.hypot(a.clientX-b.clientX,a.clientY-b.clientY);
const touchMid=(a,b)=>({x:(a.clientX+b.clientX)/2,y:(a.clientY+b.clientY)/2});
function zoomAt(clientX,clientY,factor){
  const rect=$('#center').getBoundingClientRect();
  const x=clientX-rect.left, y=clientY-rect.top;
  const before={x:(x-state.pan.x)/state.pan.scale, y:(y-state.pan.y)/state.pan.scale};
  state.pan.scale=clamp(state.pan.scale*factor,0.25,2.5);
  state.pan.x=x-before.x*state.pan.scale;
  state.pan.y=y-before.y*state.pan.scale;
  applyPanZoom();
}
$('#center').addEventListener('touchstart',e=>{
  if(e.touches.length===2){
    const a=e.touches[0], b=e.touches[1];
    touchPinch={ dist:touchDist(a,b), scale:state.pan.scale, panX:state.pan.x, panY:state.pan.y, mid:touchMid(a,b) };
    touchPan=null;
    return;
  }
  if(e.touches.length===1 && !e.target.closest('.node') && !e.target.closest('.rail')){
    const t=e.touches[0];
    touchPan={x:t.clientX,y:t.clientY,px:state.pan.x,py:state.pan.y};
  }
},{passive:false});
$('#center').addEventListener('touchmove',e=>{
  if(touchPinch && e.touches.length===2){
    const a=e.touches[0], b=e.touches[1];
    const d=touchDist(a,b);
    if(touchPinch.dist>0){
      const ratio=d/touchPinch.dist;
      const targetScale=clamp(touchPinch.scale*ratio,0.25,2.5);
      const factor=targetScale/state.pan.scale;
      const m=touchMid(a,b);
      zoomAt(m.x,m.y,factor);
    }
    e.preventDefault();
    return;
  }
  if(!touchPan || e.touches.length!==1) return;
  const t=e.touches[0];
  state.pan.x = touchPan.px + (t.clientX - touchPan.x);
  state.pan.y = touchPan.py + (t.clientY - touchPan.y);
  applyPanZoom();
  e.preventDefault();
},{passive:false});
$('#center').addEventListener('touchend',e=>{ touchPan=null; if(!e.touches || e.touches.length<2) touchPinch=null; });
$('#center').addEventListener('touchcancel',()=>{ touchPan=null; touchPinch=null; });

function setupTouchSidebarGestures(){
  const center = $('#center');
  if(!center) return;
  const EDGE_ZONE = 22;
  const OPEN_THRESHOLD = 56;
  const CLOSE_THRESHOLD = 56;
  let edgeSwipe = null;
  let panelSwipe = null;

  center.addEventListener('touchstart',e=>{
    if(!isMobileStudio() || e.touches.length !== 1) return;
    const target = e.target;
    if(
      target.closest('.node') ||
      target.closest('.rail') ||
      target.closest('#controls') ||
      target.closest('.ctrl') ||
      target.closest('.studio-mobile-dock')
    ) return;
    const t = e.touches[0];
    const rect = center.getBoundingClientRect();
    const localX = t.clientX - rect.left;
    const side = localX <= EDGE_ZONE ? 'left' : (localX >= rect.width - EDGE_ZONE ? 'right' : null);
    if(!side) return;
    edgeSwipe = { side, sx: t.clientX, sy: t.clientY };
  },{ passive: true });

  center.addEventListener('touchmove',e=>{
    if(!edgeSwipe || !isMobileStudio() || e.touches.length !== 1) return;
    const t = e.touches[0];
    const dx = t.clientX - edgeSwipe.sx;
    const dy = t.clientY - edgeSwipe.sy;
    if(Math.abs(dy) > Math.abs(dx)) return;
    if(edgeSwipe.side === 'left' && dx > OPEN_THRESHOLD){
      openLeftPanel();
      edgeSwipe = null;
      e.preventDefault();
      return;
    }
    if(edgeSwipe.side === 'right' && dx < -OPEN_THRESHOLD){
      openRightPanel();
      edgeSwipe = null;
      e.preventDefault();
    }
  },{ passive: false });
  center.addEventListener('touchend',()=>{ edgeSwipe = null; },{ passive: true });
  center.addEventListener('touchcancel',()=>{ edgeSwipe = null; },{ passive: true });

  const wirePanelSwipeClose = (selector, side) => {
    const panel = $(selector);
    if(!panel) return;
    panel.addEventListener('touchstart',e=>{
      if(!isMobileStudio() || e.touches.length !== 1 || !panel.classList.contains('open')) return;
      const t = e.touches[0];
      panelSwipe = { side, sx: t.clientX, sy: t.clientY };
    },{ passive: true });
    panel.addEventListener('touchmove',e=>{
      if(!panelSwipe || panelSwipe.side !== side || !isMobileStudio() || e.touches.length !== 1) return;
      const t = e.touches[0];
      const dx = t.clientX - panelSwipe.sx;
      const dy = t.clientY - panelSwipe.sy;
      if(Math.abs(dy) > Math.abs(dx)) return;
      const shouldClose = (side === 'left' && dx < -CLOSE_THRESHOLD) || (side === 'right' && dx > CLOSE_THRESHOLD);
      if(!shouldClose) return;
      closeSidePanels();
      panelSwipe = null;
      e.preventDefault();
    },{ passive: false });
    panel.addEventListener('touchend',()=>{ panelSwipe = null; },{ passive: true });
    panel.addEventListener('touchcancel',()=>{ panelSwipe = null; },{ passive: true });
  };

  wirePanelSwipeClose('#left','left');
  wirePanelSwipeClose('#right','right');
}

/* ===================== Toolbar & Misc ===================== */
function initSidebarFabs(){
  const btnPal = $('#btnPal');
  const btnIns = $('#btnIns');
  if (btnPal) {
    btnPal.classList.add('sidebar-fab-unified');
    btnPal.setAttribute('aria-label', 'Toggle left sidebar');
  }
  if (btnIns) {
    btnIns.classList.add('sidebar-fab-unified');
    btnIns.setAttribute('aria-label', 'Toggle right sidebar');
  }
}
function switchTab(target){
  if(!target) return;
  $$('.tab').forEach(x=>x.classList.toggle('active', x.dataset.tab===target));
  $$('.tab-content').forEach(c=>c.classList.toggle('active', c.id===`tab-${target}`));
  queuePrefsSave({ activeTab: target });
}
function setHelpModalOpen(open){
  const modal=$('#helpModal');
  if(!modal) return;
  modal.classList.toggle('show', !!open);
}
function syncCanvasHint(){
  const hint=$('#canvasHint');
  if(!hint) return;
  const prefs=loadPrefs();
  hint.classList.toggle('hidden', !!prefs.hideCanvasHint);
}
function isMobileStudio(){
  return window.matchMedia('(max-width: 1100px)').matches;
}
function closeLeftPanel(){
  $('#left')?.classList.remove('open');
  syncSidebarA11y();
}
function closeRightPanel(){
  $('#right')?.classList.remove('open');
  syncSidebarA11y();
}
function closeSidePanels(){
  closeLeftPanel();
  closeRightPanel();
}
function openLeftPanel(){
  const left = $('#left');
  const right = $('#right');
  if(!left) return;
  if(isMobileStudio()){
    if(left.classList.contains('open')) left.classList.remove('open');
    else{
      right?.classList.remove('open');
      left.classList.add('open');
    }
  }else{
    left.classList.toggle('open');
  }
  syncSidebarA11y();
}
function openRightPanel(){
  const left = $('#left');
  const right = $('#right');
  if(!right) return;
  if(isMobileStudio()){
    if(right.classList.contains('open')) right.classList.remove('open');
    else{
      left?.classList.remove('open');
      right.classList.add('open');
    }
  }else{
    right.classList.toggle('open');
  }
  syncSidebarA11y();
}
function syncDockState(mobile, leftOpen, rightOpen){
  const dock = $('#studioMobileDock');
  if(!dock) return;
  dock.classList.toggle('panel-open', !!(mobile && (leftOpen || rightOpen)));
  $('#btnPalDock')?.classList.toggle('is-active', !!(mobile && leftOpen));
  $('#btnInsDock')?.classList.toggle('is-active', !!(mobile && rightOpen));
}
function syncSidebarA11y(){
  const left = $('#left');
  const right = $('#right');
  const btnPal = $('#btnPal');
  const btnIns = $('#btnIns');
  const btnPalDock = $('#btnPalDock');
  const btnInsDock = $('#btnInsDock');
  const backdrop = $('#sideBackdrop');
  const mobile = isMobileStudio();
  const leftOpen = mobile ? !!left?.classList.contains('open') : true;
  const rightOpen = mobile ? !!right?.classList.contains('open') : true;
  if (left && btnPal) {
    left.setAttribute('aria-hidden', leftOpen ? 'false' : 'true');
    btnPal.setAttribute('aria-expanded', leftOpen ? 'true' : 'false');
  }
  if (btnPalDock) btnPalDock.setAttribute('aria-expanded', leftOpen ? 'true' : 'false');
  if (right && btnIns) {
    right.setAttribute('aria-hidden', rightOpen ? 'false' : 'true');
    btnIns.setAttribute('aria-expanded', rightOpen ? 'true' : 'false');
  }
  if (btnInsDock) btnInsDock.setAttribute('aria-expanded', rightOpen ? 'true' : 'false');
  if (backdrop) {
    const showBackdrop = mobile && (leftOpen || rightOpen);
    backdrop.classList.toggle('show', showBackdrop);
    backdrop.setAttribute('aria-hidden', showBackdrop ? 'false' : 'true');
  }
  syncDockState(mobile, leftOpen, rightOpen);
  if (mobile) {
    queuePrefsSave({
      mobileLeftOpen: !!left?.classList.contains('open'),
      mobileRightOpen: !!right?.classList.contains('open'),
    });
  }
}
$('#btnPal').addEventListener('click',openLeftPanel);
$('#btnIns').addEventListener('click',openRightPanel);
$('#btnPalDock')?.addEventListener('click',openLeftPanel);
$('#btnInsDock')?.addEventListener('click',openRightPanel);
$('#btnFitDock')?.addEventListener('click',fitToScreen);
$('#btnApplyDock')?.addEventListener('click',async()=>{ await saveToStudio(); await applyToRuntime(); });
$('#btnCloseLeft')?.addEventListener('click',closeLeftPanel);
$('#btnCloseRight')?.addEventListener('click',closeRightPanel);
$('#sideBackdrop')?.addEventListener('click',closeSidePanels);
window.addEventListener('resize',()=>{
  const mm = $('#mainMenu');
  if(mm) mm.style.display = 'none';
  syncSidebarA11y();
});
window.addEventListener('orientationchange',()=>{
  const mm = $('#mainMenu');
  if(mm) mm.style.display = 'none';
  syncSidebarA11y();
});
initSidebarFabs();
syncSidebarA11y();
const startupPrefs = loadPrefs();
switchTab(startupPrefs.activeTab || 'actions');
if (isMobileStudio()) {
  if (startupPrefs.mobileRightOpen) $('#right')?.classList.add('open');
  else if (startupPrefs.mobileLeftOpen) $('#left')?.classList.add('open');
  syncSidebarA11y();
}
syncCanvasHint();
$$('.tab').forEach(t=>t.addEventListener('click',()=>switchTab(t.dataset.tab)));
$('#btnAutoLayout').addEventListener('click',autoLayout);
$('#btnRepel').addEventListener('click',()=>repelLayout());
$('#btnApply').addEventListener('click',async()=>{ await saveToStudio(); await applyToRuntime(); });
$('#btnHelp')?.addEventListener('click',()=>setHelpModalOpen(true));
$('#helpClose')?.addEventListener('click',()=>setHelpModalOpen(false));
$('#helpModal')?.addEventListener('click',e=>{ if(e.target?.id==='helpModal') setHelpModalOpen(false); });
$('#btnMenu').addEventListener('click',()=>$('#mainMenu').style.display=$('#mainMenu').style.display==='block'?'none':'block');
$('#mSave').addEventListener('click',async()=>{ $('#mainMenu').style.display='none'; await saveToStudio(); });
$('#mAutoLayout')?.addEventListener('click',()=>{ $('#mainMenu').style.display='none'; autoLayout(); });
$('#mRepel')?.addEventListener('click',()=>{ $('#mainMenu').style.display='none'; repelLayout(); });
$('#mFit')?.addEventListener('click',()=>{ $('#mainMenu').style.display='none'; fitToScreen(); });
$('#mHelp')?.addEventListener('click',()=>{ $('#mainMenu').style.display='none'; setHelpModalOpen(true); });
$('#mImportdbActions').addEventListener('click',()=>{ $('#mainMenu').style.display='none'; toast(t('studio.importActionsDb') + ' - TODO','warn'); });
$('#mImportdbActionsStudio').addEventListener('click',()=>{ $('#mainMenu').style.display='none'; toast(t('studio.importStudioDb') + ' - TODO','warn'); });
$('#btnHideCanvasHint')?.addEventListener('click',()=>{
  const p = loadPrefs();
  savePrefsNow({ ...p, hideCanvasHint: true });
  syncCanvasHint();
});

/* close kebab on outside click */
window.addEventListener('click',e=>{ const mm=$('#mainMenu'); if(!mm) return; if(!e.target.closest('#btnMenu') && !e.target.closest('#mainMenu')){ mm.style.display='none'; } });
window.addEventListener('keydown',e=>{
  const tag=(document.activeElement?.tagName||'').toLowerCase();
  const typing=['input','textarea','select'].includes(tag);
  if((e.ctrlKey || e.metaKey) && e.key.toLowerCase()==='s'){
    e.preventDefault();
    saveToStudio();
    return;
  }
  if(!typing && (e.key==='f' || e.key==='F')){
    e.preventDefault();
    fitToScreen();
    return;
  }
  if(!typing && (e.key==='?' || e.key.toLowerCase()==='h')){
    e.preventDefault();
    setHelpModalOpen(true);
    return;
  }
  if(e.key!=='Escape') return;
  const mm=$('#mainMenu');
  if(mm) mm.style.display='none';
  setHelpModalOpen(false);
  closeSidePanels();
});

$('#filterActions').addEventListener('input',buildPalette);
$('#filterHosts').addEventListener('input',buildHostPalette);
$('#clearFilterActions')?.addEventListener('click',()=>{ $('#filterActions').value=''; buildPalette(); $('#filterActions').focus(); });
$('#clearFilterHosts')?.addEventListener('click',()=>{ $('#filterHosts').value=''; buildHostPalette(); $('#filterHosts').focus(); });

$('#canvas').addEventListener('dragover',e=>{ e.preventDefault(); e.dataTransfer.dropEffect='copy'; });
$('#canvas').addEventListener('drop',e=>{
  e.preventDefault();
  const data=e.dataTransfer.getData('action'); if(!data) return;
  const a=JSON.parse(data); const rect=$('#center').getBoundingClientRect();
  const x=(e.clientX-rect.left-state.pan.x)/state.pan.scale, y=(e.clientY-rect.top-state.pan.y)/state.pan.scale;
  addActionNode(a,x,y);
});

$('#btnUpdateAction').addEventListener('click',()=>{
  const n=state.nodes.get(state.selected); if(!n||n.type!=='action') return;
  const a=n.data;
  a.b_module=$('#e_module').value; a.b_status=$('#e_status').value; a.b_action=$('#e_type').value; a.b_enabled=parseInt($('#e_enabled').value);
  a.b_priority=parseInt($('#e_prio').value); a.b_timeout=parseInt($('#e_timeout').value); a.b_max_retries=parseInt($('#e_retry').value);
  a.b_cooldown=parseInt($('#e_cool').value); a.b_rate_limit=$('#e_rate').value; a.b_port=parseInt($('#e_port').value)||null;
  a.b_service=JSON.stringify(fromCSV($('#e_services').value)); a.b_tags=$('#e_tags').value;
  const tt=$('#t_type').value, tp=$('#t_param').value.trim(); a.b_trigger=tp?`${tt}:${tp}`:tt;

  const el=$(`[data-id="${state.selected}"]`); if(el){ el.className=`node ${a.b_action==='global'?'global':''}`; el.querySelector('.badge').textContent=a.b_action||'normal'; el.querySelector('.v.trigger').textContent=summTrig(a.b_trigger||''); el.querySelector('.v.requires').textContent=requireSummary(a); }
  LinkEngine.render(); toast(t('studio.actionUpdated'),'success');
});
$('#btnDeleteNode').addEventListener('click',()=>{ if(state.selected) deleteNode(state.selected); });

$('#btnUpdateHost').addEventListener('click',()=>{
  const n=state.nodes.get(state.selected); if(!n||n.type!=='host') return; const h=n.data;
  h.hostname=$('#h_hostname').value.trim(); h.ips=$('#h_ips').value.trim(); h.ports=$('#h_ports').value.trim(); h.alive=parseInt($('#h_alive').value);
  h.essid=$('#h_essid').value.trim(); h.services=$('#h_services').value.trim(); h.vulns=$('#h_vulns').value.trim(); h.creds=$('#h_creds').value.trim();
  const el=$(`[data-id="${state.selected}"]`); if(el){ el.querySelector('.nname').textContent=h.hostname||h.ips||h.mac_address; const rows=el.querySelectorAll('.nbody .row .v'); if(rows[0]) rows[0].textContent=h.ips||'—'; if(rows[1]) rows[1].textContent=h.ports||'—'; if(rows[2]) rows[2].textContent=h.alive?'🟢':'🔴'; }
  LinkEngine.render(); toast(t('studio.hostUpdated'),'success');
});
$('#btnDeleteHost').addEventListener('click',()=>{ if(state.selected) deleteNode(state.selected); });

/* Palette hosts helpers (global) */
window.addHostToCanvas=function(mac){
  const h=state.hosts.get(mac); if(!h) return;
  let existing=null; state.nodes.forEach((n,id)=>{ if(n.type==='host'&&n.data.mac_address===mac) existing=id; });
  if(existing){ const el=$(`[data-id="${existing}"]`); el.classList.add('sel'); selectNode(existing); }
  else{ const rect=$('#center').getBoundingClientRect(); const x=80; const y=(rect.height/2 - state.pan.y)/state.pan.scale - 60; addHostNode(h,x,y); LinkEngine.render(); }
};
window.deleteTestHost=function(mac){
  if(!confirm(t('studio.deleteTestHost'))) return;
  state.hosts.delete(mac); const ids=[]; state.nodes.forEach((n,id)=>{ if(n.type==='host'&&n.data.mac_address===mac) ids.push(id); }); ids.forEach(id=>deleteNode(id)); buildHostPalette(); toast(t('studio.testHostDeleted'),'success');
};
window.openHostModal=function(){ $('#hostModal').classList.add('show'); };
window.closeHostModal=function(){ $('#hostModal').classList.remove('show'); };
window.createTestHost=function(){
  const mac=$('#new_mac').value.trim() || `AA:BB:CC:${Math.random().toString(16).slice(2,8).toUpperCase()}`;
  if(state.hosts.has(mac)){ toast(t('studio.macExists'),'error'); return; }
  const host={ mac_address:mac, hostname:$('#new_hostname').value.trim()||'test-host', ips:$('#new_ips').value.trim()||'', ports:$('#new_ports').value.trim()||'', services:$('#new_services').value.trim()||'[]', vulns:$('#new_vulns').value.trim()||'', creds:$('#new_creds').value.trim()||'[]', alive:parseInt($('#new_alive').value)||1, is_simulated:1 };
  state.hosts.set(mac,host); buildHostPalette(); closeHostModal(); toast(t('studio.testHostCreated'),'success'); addHostToCanvas(mac);
};
$('#btnCreateHost').addEventListener('click',openHostModal);
$('#mAddHost').addEventListener('click',openHostModal);

/* ===== keyboard helpers ===== */
document.addEventListener('keydown',e=>{
  if(e.key==='Escape'){
    $('#edgeMenu').classList.remove('show');
    $('#linkWizard').classList.remove('show');
    $('#hostModal').classList.remove('show');
    setHelpModalOpen(false);
  }
  const tag=(document.activeElement?.tagName||'').toLowerCase();
  if((e.key==='Delete' || e.key==='Backspace') && !['input','textarea','select'].includes(tag)){
    if(state.selected){ e.preventDefault(); deleteNode(state.selected); }
  }
});

/* ===================== Auto-import & Auto-link ===================== */
function ensureActionNodeByClass(bClass){
  let existing=null; state.nodes.forEach((n,id)=>{ if(n.type==='action'&&n.data?.b_class===bClass) existing=id; });
  if(existing) return existing; const a=state.actions.get(bClass); if(!a) return null;
  const rect=$('#center').getBoundingClientRect();
  const x=(rect.width/2 - state.pan.x)/state.pan.scale - 110, y=(rect.height/2 - state.pan.y)/state.pan.scale - 60;
  return addActionNode(a,x,y);
}
function findHostNodeIdByMac(mac){
  let id=null; state.nodes.forEach((n,nd)=>{ if(n.type==='host' && n.data.mac_address===mac) id=nd; }); return id;
}
/* hosts vertical placement (initial) */
function placeAllAliveHosts(){
  const alive = [...state.hosts.values()].filter(h=>parseInt(h.alive)==1).sort(byHostnameIpMac);
  let i=0; const x=80, startY=80, dy=170;
  for(const h of alive){
    if(state.placedHosts.has(h.mac_address)){ i++; continue; }
    const y=startY + i*dy;
    addHostNode(h,x,y); i++;
  }
}
function isHostRuleInRequires(req){
  const r=tryJSON(req,null); if(!r) return false;
  const hasHostKey = obj => ['has_port','service_is_open','has_cve','has_cpe','has_cred','mac_is','essid_is'].some(k=>k in obj);
  if(r.all) return r.all.some(hasHostKey);
  if(r.any) return r.any.some(hasHostKey);
  return hasHostKey(r);
}
function importActionsForHostsAndDeps(){
  const aliveHosts=[...state.hosts.values()].filter(h=>parseInt(h.alive)==1);

  // 1) actions liées aux hôtes (triggers/requires) => placer + lier
  for(const a of state.actions.values()){
    const matches = aliveHosts.filter(h=> hostMatchesActionByTriggers(a,h) || (isHostRuleInRequires(a.b_requires) && checkHostRequires(a.b_requires,h)) );
    if(matches.length===0) continue;
    const actionId = ensureActionNodeByClass(a.b_class); if(!actionId) continue;
    for(const h of matches){
      const hostId = findHostNodeIdByMac(h.mac_address); if(!hostId) continue;
      if(hostMatchesActionByTriggers(a,h)) ensureLink({from:hostId,to:actionId,type:'requires',mode:'trigger',label:'trigger'});
      if(isHostRuleInRequires(a.b_requires) && checkHostRequires(a.b_requires,h)) ensureLink({from:hostId,to:actionId,type:'requires',mode:'requires',label:'requires'});
    }
  }

  // 2) dépendances entre actions (on_success/on_failure + requires action)
  state.nodes.forEach((nA,idA)=>{
    if(nA.type!=='action') return;
    const a=nA.data;
    const {name,param}=splitTriggerSafe(a.b_trigger||'');
    if(name==='on_success' || name==='on_failure'){
      const srcId = ensureActionNodeByClass(param);
      if(srcId) ensureLink({from:srcId,to:idA,type:(name==='on_success'?'success':'failure'),mode:'trigger'});
    }
    const req=tryJSON(a.b_requires,null);
    const addReq=r=>{ if(r&&r.action){ const srcId=ensureActionNodeByClass(r.action); if(srcId) ensureLink({from:srcId,to:idA,type:'requires',mode:'requires',label:'req_action'}); } };
    if(req){ if(req.all) req.all.forEach(addReq); else if(req.any) req.any.forEach(addReq); else addReq(req); }
  });
}

/* ===================== Boot ===================== */
async function init(){
  const actions=await fetchActions(); const hosts=await fetchHosts();
  actions.forEach(a=>state.actions.set(a.b_class,a)); hosts.forEach(h=>state.hosts.set(h.mac_address,h));

  // >>> plus de BJORN ni NetworkScanner auto-placés

  // 1) Tous les hosts ALIVE sont importés (vertical)
  placeAllAliveHosts();

  buildPalette(); buildHostPalette();

  // 2) Auto-import des actions dont trigger/require matchent les hôtes + liens
  importActionsForHostsAndDeps();

  // 3) Layout + rendu
  autoLayout();
  applyPanZoom();
  LinkEngine.render();
  updateStats();
  toast(t('studio.saved'),'success');
}

init();
  } finally {
    EventTarget.prototype.addEventListener = nativeAdd;
  }
  return function unmountStudioRuntime() {
    if (prefsSaveTimer) {
      clearTimeout(prefsSaveTimer);
      prefsSaveTimer = null;
    }
    for (const [target, type, listener, options] of tracked) {
      try { nativeRemove.call(target, type, listener, options); } catch {}
    }
    try {
      if (__root) {
        __root.innerHTML = '';
      }
    } catch {}
    try { delete window.addHostToCanvas; } catch {}
    try { delete window.deleteTestHost; } catch {}
    try { delete window.openHostModal; } catch {}
    try { delete window.closeHostModal; } catch {}
    try { delete window.createTestHost; } catch {}
  };
}
