import { ResourceTracker } from '../core/resource-tracker.js';
import { el, toast } from '../core/dom.js';
import { t as i18nT } from '../core/i18n.js';
import { initSharedSidebarLayout } from '../core/sidebar-layout.js';
import * as epdEditor from '../core/epd-editor.js';

const PAGE = 'attacks';
let tracker = null;
let root = null;
let currentAttack = null;
let selectedSection = null;
let selectedImageScope = null;
let selectedActionName = null;
let selectedImages = new Set();
let editMode = false;
let imageCache = [];
let imageResolver = null;
let sortKey = 'name';
let sortDir = 1;
const iconCache = new Map();
let disposeSidebarLayout = null;

function q(sel, base = root) { return base?.querySelector(sel) || null; }
function qa(sel, base = root) { return Array.from(base?.querySelectorAll(sel) || []); }
function note(msg, ms = 2200, type = 'info') { toast(String(msg ?? ''), ms, type); }
function L(key, vars) { return i18nT(key, vars); }
function Lx(key, fallback, vars) {
  const out = i18nT(key, vars);
  return out && out !== key ? out : fallback;
}

function markup() {
  return `
  <div class="attacks-sidebar">
    <div class="sidehead">
      <div class="sidetitle">${L('attacks.sidebar.management')}</div>
      <div class="spacer"></div>
      <button class="btn" id="hideSidebar" data-hide-sidebar="1" type="button">${Lx('common.hide', 'Hide')}</button>
    </div>
    <div class="tabs-container">
      <button class="tab-btn active" data-page="attacks">${L('attacks.tabs.attacks')}</button>
      <button class="tab-btn" data-page="comments">${L('attacks.tabs.comments')}</button>
      <button class="tab-btn" data-page="images">${L('attacks.tabs.images')}</button>
      <button class="tab-btn" data-page="epd-layout">${Lx('attacks.tabs.epdLayout', 'EPD Layout')}</button>
    </div>

    <div id="attacks-sidebar" class="sidebar-page" style="display:block">
      <ul class="unified-list" id="attacks-list"></ul>
      <div class="hero-btn">
        <button class="btn" id="add-attack-btn">${L('attacks.btn.addAttack')}</button>
        <button class="btn danger" id="remove-attack-btn">${L('attacks.btn.removeAttack')}</button>
        <button class="btn danger" id="delete-action-btn">${L('attacks.btn.deleteAction')}</button>
        <button class="btn" id="sync-missing-btn">${Lx('attacks.btn.syncMissing', 'Sync Missing')}</button>
        <button class="btn danger" id="restore-default-actions-btn">${L('attacks.btn.restoreDefaultsBundle')}</button>
      </div>
      <div id="empty-attacks-hint" style="display:none;opacity:.8;margin-top:8px">${L('attacks.empty.noAttacks')}</div>
    </div>

    <div id="comments-sidebar" class="sidebar-page" style="display:none">
      <ul class="unified-list" id="section-list"></ul>
      <div class="hero-btn">
        <button class="btn" id="add-section-btn">${L('attacks.btn.addSection')}</button>
        <button class="btn danger" id="delete-section-btn" disabled>${L('attacks.btn.deleteSection')}</button>
        <button class="btn danger" id="restore-default-btn">${L('attacks.btn.restoreDefault')}</button>
      </div>
      <div id="empty-comments-hint" style="display:none;opacity:.8;margin-top:8px">${L('attacks.empty.noComments')}</div>
    </div>

    <div id="images-sidebar" class="sidebar-page" style="display:none">
      <h3 style="margin:8px 0">${L('attacks.section.characters')}</h3>
      <ul class="unified-list" id="character-list"></ul>
      <div class="chips" style="margin:8px 0 16px 0">
        <button class="btn" id="create-character-btn">${L('attacks.btn.createCharacter')}</button>
        <button class="btn danger" id="delete-character-btn">${L('attacks.btn.deleteCharacter')}</button>
      </div>
      <h3 style="margin:8px 0">${L('attacks.section.statusImages')}</h3>
      <ul class="unified-list" id="action-list"></ul>
      <h3 style="margin:8px 0">${L('attacks.section.staticImages')}</h3>
      <ul class="unified-list" id="library-list"></ul>
      <h3 style="margin:8px 0">${L('attacks.section.webImages')}</h3>
      <ul class="unified-list" id="web-images-list"></ul>
      <h3 style="margin:8px 0">${L('attacks.section.actionIcons')}</h3>
      <ul class="unified-list" id="actions-icons-list"></ul>
    </div>

    <div id="epd-layout-sidebar" class="sidebar-page" style="display:none"></div>
  </div>

  <div class="attacks-main">
    <div id="attacks-page" class="page-content active">
      <div class="editor-textarea-container">
        <div class="editor-header">
          <h2 id="editor-title" style="margin:0">${L('attacks.editor.selectAttack')}</h2>
          <div class="editor-buttons">
            <button class="btn" id="save-attack-btn">${L('common.save')}</button>
            <button class="btn" id="restore-attack-btn">${L('attacks.btn.restoreDefault')}</button>
          </div>
        </div>
        <textarea id="editor-textarea" class="editor-textarea" disabled></textarea>
      </div>
    </div>

    <div id="comments-page" class="page-content">
      <div class="buttons-container">
        <h2 id="section-title" style="margin:0 0 10px 0">${L('attacks.tabs.comments')}</h2>
        <button class="btn" id="select-all-btn">${L('common.selectAll')}</button>
        <button class="btn" id="save-comments-btn">${L('common.save')}</button>
      </div>
      <div class="comments-container">
        <div class="comments-editor" id="comments-editor" contenteditable="true" data-placeholder="${L('attacks.comments.placeholder')}" role="textbox" aria-multiline="true"></div>
      </div>
    </div>

    <div id="images-page" class="page-content">
      <div class="actions-bar">
        <span class="chip" id="edit-mode-toggle-btn">${L('attacks.images.enterEditMode')}</span>
        <select id="sort-key" class="select">
          <option value="name">${L('attacks.images.sortName')}</option>
          <option value="dim">${L('attacks.images.sortDimensions')}</option>
        </select>
        <button id="sort-dir" class="sort-toggle">^</button>
        <div class="range-wrap" title="${Lx('attacks.images.gridDensity', 'Grid density')}">
          <span>${Lx('attacks.images.density', 'Density')}</span>
          <input id="density" type="range" min="120" max="260" value="160" class="range">
        </div>
        <div class="field"><span class="icon">S</span><input id="search-input" class="input" placeholder="${L('attacks.images.search')}"></div>
        <button id="rename-image-btn" class="edit-only">${L('attacks.images.rename')}</button>
        <button id="replace-image-btn" class="edit-only">${L('attacks.images.replace')}</button>
        <button id="resize-images-btn" class="edit-only">${L('attacks.images.resizeSelected')}</button>
        <button id="add-characters-btn" class="status-only">${L('attacks.images.addCharacters')}</button>
        <button id="delete-images-btn" class="edit-only danger">${L('attacks.images.deleteSelected')}</button>
        <button id="add-status-image-btn">${L('attacks.images.addStatus')}</button>
        <button id="add-static-image-btn">${L('attacks.images.addStatic')}</button>
        <button id="add-web-image-btn">${L('attacks.images.addWeb')}</button>
        <button id="add-icon-image-btn">${L('attacks.images.addIcon')}</button>
      </div>
      <div class="image-container" id="image-container"></div>
    </div>

    <div id="epd-layout-page" class="page-content"></div>
  </div>`;
}

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  const data = await r.json();
  if (!tracker) throw new Error('unmounted');
  return data;
}

async function postJSON(url, body = {}) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return r.json();
}

async function iconFor(name) {
  if (iconCache.has(name)) return iconCache.get(name);
  for (const url of [`/actions_icons/${encodeURIComponent(name)}.png`, `/get_status_icon?action=${encodeURIComponent(name)}`]) {
    try {
      const r = await fetch(url);
      if (!r.ok) continue;
      const b = await r.blob();
      const obj = URL.createObjectURL(b);
      iconCache.set(name, obj);
      return obj;
    } catch { }
  }
  return '/web/images/attack.png';
}

function iconCandidateURLs(actionName) {
  return [
    `/actions_icons/${encodeURIComponent(actionName)}.png`,
    `/actions_icons/${encodeURIComponent(actionName)}.bmp`,
    `/get_status_icon?action=${encodeURIComponent(actionName)}`,
  ];
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

async function makePlaceholderIconBlob(actionName) {
  const size = 128;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#0b0e13';
  ctx.fillRect(0, 0, size, size);
  ctx.lineWidth = 8;
  ctx.strokeStyle = '#59b6ff';
  ctx.beginPath();
  ctx.arc(size / 2, size / 2, size / 2 - 8, 0, Math.PI * 2);
  ctx.stroke();
  const initials = (actionName || 'A')
    .split(/[^A-Za-z0-9]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((x) => x[0])
    .join('')
    .toUpperCase() || 'A';
  ctx.fillStyle = '#59b6ff';
  ctx.font = 'bold 56px sans-serif';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(initials, size / 2, size / 2 + 4);
  return new Promise((resolve) => canvas.toBlob((b) => resolve(b || new Blob([], { type: 'image/png' })), 'image/png'));
}

async function fetchActionIconBlob(actionName) {
  for (const url of iconCandidateURLs(actionName)) {
    try {
      const r = await fetch(url, { cache: 'no-cache' });
      if (r.ok) return await r.blob();
    } catch { }
  }
  try {
    const r = await fetch('/web/images/attack.png', { cache: 'no-cache' });
    if (r.ok) return await r.blob();
  } catch { }
  return makePlaceholderIconBlob(actionName);
}

async function hasStatusImage(actionName) {
  const p = `/images/status/${encodeURIComponent(actionName)}/${encodeURIComponent(actionName)}.bmp`;
  try {
    const r = await fetch(p, { cache: 'no-cache' });
    return r.ok;
  } catch {
    return false;
  }
}

async function actionHasCharacterImages(actionName) {
  try {
    const data = await getJSON('/get_action_images?action=' + encodeURIComponent(actionName));
    const imgs = data?.images || [];
    if (!Array.isArray(imgs)) return false;
    const rx = new RegExp(`^${escapeRegExp(actionName)}\\d+\\.(bmp|png|jpe?g|gif|webp)$`, 'i');
    return imgs.some((im) => {
      const n = typeof im === 'string' ? im : (im.name || im.filename || '');
      return rx.test(String(n));
    });
  } catch {
    return false;
  }
}

async function ensureStatusImageFromIcon(actionName) {
  if (await hasStatusImage(actionName)) return false;
  const blob = await fetchActionIconBlob(actionName);
  const fd = new FormData();
  fd.append('type', 'action');
  fd.append('action_name', actionName);
  fd.append('status_image', new File([blob], `${actionName}.bmp`, { type: 'image/bmp' }));
  const r = await fetch('/upload_status_image', { method: 'POST', body: fd });
  const d = await r.json();
  if (d.status !== 'success') throw new Error(d.message || 'upload_status_image failed');
  return true;
}

async function ensureAtLeastOneCharacterImageFromIcon(actionName) {
  if (await actionHasCharacterImages(actionName)) return false;
  const blob = await fetchActionIconBlob(actionName);
  const fd = new FormData();
  fd.append('action_name', actionName);
  fd.append('character_images', new File([blob], `${actionName}1.png`, { type: blob.type || 'image/png' }));
  const r = await fetch('/upload_character_images', { method: 'POST', body: fd });
  const d = await r.json();
  if (d.status !== 'success') throw new Error(d.message || 'upload_character_images failed');
  return true;
}

async function ensureCommentsSection(sectionName, sectionsSet) {
  if (sectionsSet.has(sectionName)) return false;
  await postJSON('/save_comments', {
    section: sectionName,
    comments: [Lx('attacks.sync.defaultComment', 'Add comment for this action')],
  });
  sectionsSet.add(sectionName);
  return true;
}

async function syncMissing() {
  try {
    const attacksResp = await getJSON('/get_attacks');
    const attacks = Array.isArray(attacksResp) ? attacksResp : (Array.isArray(attacksResp?.attacks) ? attacksResp.attacks : []);
    const names = attacks.map((a) => a?.name || a?.id).filter(Boolean);
    if (!names.length) {
      note(Lx('attacks.sync.none', 'No attacks to sync.'), 2200, 'warning');
      return;
    }

    const sectionsResp = await getJSON('/get_sections');
    const sectionsSet = new Set((sectionsResp?.sections || []).map((x) => String(x)));
    let createdComments = 0;
    let createdStatus = 0;
    let createdChars = 0;

    for (const name of names) {
      if (await ensureCommentsSection(name, sectionsSet)) createdComments++;
      if (await ensureStatusImageFromIcon(name)) createdStatus++;
      if (await ensureAtLeastOneCharacterImageFromIcon(name)) createdChars++;
    }

    note(
      Lx(
        'attacks.sync.done',
        `Sync done. New comments: ${createdComments}, status images: ${createdStatus}, character images: ${createdChars}.`,
        { comments: createdComments, status: createdStatus, characters: createdChars },
      ),
      4200,
      'success',
    );
    await Promise.all([loadAttacks(), loadSections(), loadImageScopes(), loadCharacters()]);
    if (selectedImageScope) await refreshScope();
  } catch (e) {
    note(`${Lx('attacks.sync.failed', 'Sync Missing failed')}: ${e.message}`, 3200, 'error');
  }
}

async function loadAttacks() {
  const list = q('#attacks-list');
  const hint = q('#empty-attacks-hint');
  if (!list || !hint) return;
  list.innerHTML = '';

  try {
    const data = await getJSON('/get_attacks');
    const attacks = (Array.isArray(data) ? data : (data.attacks || []))
      .map((a) => ({ name: a.name || a.id || L('common.unknown'), enabled: Number(a.enabled ?? a.b_enabled ?? 0) }))
      .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' }));

    hint.style.display = attacks.length ? 'none' : 'block';
    for (const a of attacks) {
      const li = document.createElement('li');
      li.className = 'card';
      li.dataset.attackName = a.name;

      const img = document.createElement('img');
      iconFor(a.name).then((u) => { img.src = u; });

      const span = document.createElement('span');
      span.textContent = a.name;

      const dot = document.createElement('button');
      dot.className = 'enable-dot' + (a.enabled ? ' on' : '');
      dot.type = 'button';
      tracker.trackEventListener(dot, 'click', async (e) => {
        e.stopPropagation();
        const target = !dot.classList.contains('on');
        dot.classList.toggle('on', target);
        const d = await postJSON('/actions/set_enabled', { action_name: a.name, enabled: target ? 1 : 0 });
        if (d.status !== 'success') dot.classList.toggle('on', !target);
      });

      tracker.trackEventListener(li, 'click', () => selectAttack(a.name, li));
      li.append(img, span, dot);
      list.appendChild(li);
    }
  } catch {
    hint.style.display = 'block';
    hint.textContent = L('attacks.errors.loadAttacks');
  }
}

async function selectAttack(name, node) {
  qa('#attacks-list .card').forEach((n) => n.classList.remove('selected'));
  node?.classList.add('selected');
  currentAttack = name;
  q('#editor-title').textContent = name;
  const ta = q('#editor-textarea');
  ta.disabled = false;
  const d = await getJSON('/get_attack_content?name=' + encodeURIComponent(name));
  ta.value = d?.status === 'success' ? (d.content ?? '') : '';
}

function imageSort(list) {
  const cmpName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base', numeric: true }) * sortDir;
  const area = (x) => (x.width || 0) * (x.height || 0);
  return [...list].sort(sortKey === 'name' ? cmpName : ((a, b) => ((area(a) - area(b)) * sortDir || cmpName(a, b))));
}

function syncImageModeClasses() {
  if (!root) return;
  root.classList.toggle('edit-mode', !!editMode);
  root.classList.remove('status-mode', 'static-mode', 'web-mode', 'icons-mode');
  if (selectedImageScope === 'action') root.classList.add('status-mode');
  if (selectedImageScope === 'static') root.classList.add('static-mode');
  if (selectedImageScope === 'web') root.classList.add('web-mode');
  if (selectedImageScope === 'icons') root.classList.add('icons-mode');
}

function renderImages(items, resolver) {
  imageCache = items.map((im) => ({ name: typeof im === 'string' ? im : (im.name || im.filename || ''), width: im.width, height: im.height }));
  imageResolver = resolver;
  const grid = q('#image-container');
  const search = (q('#search-input')?.value || '').toLowerCase().trim();
  grid.innerHTML = '';

  imageSort(imageCache).filter((x) => !search || x.name.toLowerCase().includes(search)).forEach((im) => {
    const tile = document.createElement('div');
    tile.className = 'image-item';
    tile.classList.toggle('selectable', !!editMode);
    tile.dataset.imageName = im.name;

    const img = document.createElement('img');
    img.src = resolver(im.name);

    const info = document.createElement('div');
    info.className = 'image-info';
    info.textContent = im.width && im.height ? `${im.name} (${im.width}x${im.height})` : im.name;

    const ring = document.createElement('div');
    ring.className = 'select-ring';

    const tick = document.createElement('div');
    tick.className = 'tick-overlay';
    tick.textContent = 'OK';

    tracker.trackEventListener(tile, 'click', () => {
      if (!editMode) return;
      tile.classList.toggle('selected');
      if (tile.classList.contains('selected')) selectedImages.add(im.name);
      else selectedImages.delete(im.name);
    });

    tile.append(img, info, ring, tick);
    grid.appendChild(tile);
  });
}

async function loadSections() {
  const ul = q('#section-list');
  const hint = q('#empty-comments-hint');
  ul.innerHTML = '';

  try {
    const d = await getJSON('/get_sections');
    const sections = (d.sections || []).slice().sort((a, b) => String(a).localeCompare(String(b), undefined, { sensitivity: 'base', numeric: true }));
    hint.style.display = sections.length ? 'none' : 'block';

    for (const name of sections) {
      const li = document.createElement('li');
      li.className = 'card';
      li.dataset.section = name;

      const img = document.createElement('img');
      iconFor(name).then((u) => { img.src = u; });

      const span = document.createElement('span');
      span.textContent = name;

      tracker.trackEventListener(li, 'click', async () => {
        qa('#section-list .card').forEach((n) => n.classList.remove('selected'));
        li.classList.add('selected');
        selectedSection = name;
        q('#delete-section-btn').disabled = false;
        q('#section-title').textContent = `${L('attacks.tabs.comments')} - ${name}`;

        const c = await getJSON('/get_comments?section=' + encodeURIComponent(name));
        const ce = q('#comments-editor');
        ce.classList.remove('placeholder');
        ce.innerHTML = '';
        (c.comments || []).forEach((line) => {
          const div = document.createElement('div');
          div.className = 'comment-line';
          div.textContent = line || '\u200b';
          ce.appendChild(div);
        });
      });

      li.append(img, span);
      ul.appendChild(li);
    }
  } catch {
    hint.style.display = 'block';
  }
}

function addScopeCard(parent, type, name, imgSrc, onClick) {
  const li = document.createElement('li');
  li.className = 'card';
  li.dataset.type = type;
  li.dataset.name = name;
  const img = document.createElement('img'); img.src = imgSrc;
  const span = document.createElement('span'); span.textContent = name;
  tracker.trackEventListener(li, 'click', async () => { selectScope(type, name); await onClick(); });
  li.append(img, span);
  parent.appendChild(li);
}

async function loadImageScopes() {
  const actionList = q('#action-list'); actionList.innerHTML = '';
  const staticList = q('#library-list'); staticList.innerHTML = '';
  const webList = q('#web-images-list'); webList.innerHTML = '';
  const iconList = q('#actions-icons-list'); iconList.innerHTML = '';

  try {
    const actions = await getJSON('/get_actions');
    (actions.actions || []).forEach((a) => {
      const li = document.createElement('li'); li.className = 'card'; li.dataset.type = 'action'; li.dataset.name = a.name;
      const img = document.createElement('img'); iconFor(a.name).then((u) => { img.src = u; });
      const span = document.createElement('span'); span.textContent = a.name;
      tracker.trackEventListener(li, 'click', async () => {
        selectScope('action', a.name);
        const d = await getJSON('/get_action_images?action=' + encodeURIComponent(a.name));
        if (d.status === 'success') renderImages(d.images || [], (n) => `/images/status/${encodeURIComponent(a.name)}/${encodeURIComponent(n)}`);
      });
      li.append(img, span);
      actionList.appendChild(li);
    });

    addScopeCard(staticList, 'static', L('attacks.section.staticImages'), '/web/images/static_icon.png', async () => {
      const d = await getJSON('/list_static_images_with_dimensions');
      if (d.status === 'success') renderImages(d.images || [], (n) => '/static_images/' + encodeURIComponent(n));
    });

    addScopeCard(webList, 'web', L('attacks.section.webImages'), '/web/images/icon-192x192.png', async () => {
      const d = await getJSON('/list_web_images');
      if (d.status === 'success') renderImages(d.images || [], (n) => '/web/images/' + encodeURIComponent(n));
    });

    addScopeCard(iconList, 'icons', L('attacks.section.actionIcons'), '/web/images/attack.png', async () => {
      const d = await getJSON('/list_actions_icons');
      if (d.status === 'success') renderImages(d.images || [], (n) => '/actions_icons/' + encodeURIComponent(n));
    });
  } catch {
    note(L('attacks.errors.loadImages'), 2600, 'error');
  }
}

function selectScope(type, name) {
  qa('#action-list .card, #library-list .card, #web-images-list .card, #actions-icons-list .card').forEach((n) => n.classList.remove('selected'));
  qa(`[data-type="${type}"][data-name="${name}"]`).forEach((n) => n.classList.add('selected'));
  selectedImageScope = type;
  selectedActionName = type === 'action' ? name : null;
  selectedImages.clear();
  syncImageModeClasses();
}

async function refreshScope() {
  if (selectedImageScope === 'action' && selectedActionName) {
    const d = await getJSON('/get_action_images?action=' + encodeURIComponent(selectedActionName));
    if (d.status === 'success') renderImages(d.images || [], (n) => `/images/status/${encodeURIComponent(selectedActionName)}/${encodeURIComponent(n)}`);
  } else if (selectedImageScope === 'static') {
    const d = await getJSON('/list_static_images_with_dimensions');
    if (d.status === 'success') renderImages(d.images || [], (n) => '/static_images/' + encodeURIComponent(n));
  } else if (selectedImageScope === 'web') {
    const d = await getJSON('/list_web_images');
    if (d.status === 'success') renderImages(d.images || [], (n) => '/web/images/' + encodeURIComponent(n));
  } else if (selectedImageScope === 'icons') {
    const d = await getJSON('/list_actions_icons');
    if (d.status === 'success') renderImages(d.images || [], (n) => '/actions_icons/' + encodeURIComponent(n));
  }
}

async function loadCharacters() {
  const ul = q('#character-list');
  if (!ul) return;
  ul.innerHTML = '';
  const d = await getJSON('/list_characters');
  const current = d.current_character;
  (d.characters || []).forEach((c) => {
    const li = document.createElement('li'); li.className = 'card'; li.dataset.name = c.name;
    const img = document.createElement('img'); img.src = '/get_character_icon?character=' + encodeURIComponent(c.name) + '&t=' + Date.now();
    img.onerror = () => { img.src = '/web/images/default_character_icon.png'; };
    const span = document.createElement('span'); span.textContent = c.name;
    if (c.name === current) { const ck = document.createElement('span'); ck.textContent = L('common.yes'); li.appendChild(ck); }
    tracker.trackEventListener(li, 'click', async () => {
      if (!confirm(L('attacks.confirm.switchCharacter', { name: c.name }))) return;
      const r = await postJSON('/switch_character', { character_name: c.name });
      if (r.status === 'success') { note(L('attacks.toast.characterSwitched'), 1800, 'success'); loadCharacters(); }
    });
    li.append(img, span);
    ul.appendChild(li);
  });
}

function setPage(page) {
  qa('.tab-btn').forEach((b) => b.classList.toggle('active', b.dataset.page === page));
  qa('.sidebar-page').forEach((s) => { s.style.display = 'none'; });
  qa('.page-content').forEach((p) => p.classList.remove('active'));
  const sidebar = q(`#${page}-sidebar`);
  if (sidebar) sidebar.style.display = 'block';
  q(`#${page}-page`)?.classList.add('active');
}

function bindTabs() {
  qa('.tab-btn').forEach((btn) => tracker.trackEventListener(btn, 'click', async () => {
    const page = btn.dataset.page;
    setPage(page);
    if (page === 'attacks') await loadAttacks();
    if (page === 'comments') await loadSections();
    if (page === 'images') await Promise.all([loadImageScopes(), loadCharacters()]);
    if (page === 'epd-layout') await epdEditor.activate(q('#epd-layout-sidebar'), q('#epd-layout-page'));
  }));
}

function bindActions() {
  tracker.trackEventListener(q('#add-attack-btn'), 'click', async () => {
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.py';
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      const fd = new FormData(); fd.append('attack_file', f);
      const r = await fetch('/add_attack', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') {
        note(L('attacks.toast.attackImported'), 1800, 'success');
        await loadAttacks();
        await syncMissing();
      }
    };
    inp.click();
  });

  tracker.trackEventListener(q('#remove-attack-btn'), 'click', async () => {
    if (!currentAttack) return;
    if (!confirm(L('attacks.confirm.removeAttack', { name: currentAttack }))) return;
    const d = await postJSON('/remove_attack', { name: currentAttack });
    if (d.status === 'success') {
      currentAttack = null;
      q('#editor-textarea').value = '';
      q('#editor-textarea').disabled = true;
      q('#editor-title').textContent = L('attacks.editor.selectAttack');
      loadAttacks();
    }
  });

  tracker.trackEventListener(q('#delete-action-btn'), 'click', async () => {
    const actionName = currentAttack || selectedActionName;
    if (!actionName) return note(L('attacks.toast.selectAttackFirst'), 1800, 'warning');
    if (!confirm(L('attacks.confirm.deleteAction', { name: actionName }))) return;
    const d = await postJSON('/action/delete', { action_name: actionName });
    if (d.status === 'success') {
      if (currentAttack === actionName) {
        currentAttack = null;
        q('#editor-textarea').value = '';
        q('#editor-textarea').disabled = true;
        q('#editor-title').textContent = L('attacks.editor.selectAttack');
      }
      note(L('attacks.toast.actionDeleted'), 1800, 'success');
      await Promise.all([loadAttacks(), loadImageScopes()]);
    } else {
      note(d.message || L('common.error'), 2200, 'error');
    }
  });

  tracker.trackEventListener(q('#restore-default-actions-btn'), 'click', async () => {
    if (!confirm(L('attacks.confirm.restoreDefaultsBundle'))) return;
    const d = await postJSON('/actions/restore_defaults', {});
    if (d.status === 'success') {
      note(L('attacks.toast.defaultsRestored'), 2000, 'success');
      currentAttack = null;
      selectedImageScope = null;
      selectedActionName = null;
      selectedImages.clear();
      syncImageModeClasses();
      await Promise.all([loadAttacks(), loadSections(), loadImageScopes(), loadCharacters()]);
    } else {
      note(d.message || L('common.error'), 2200, 'error');
    }
  });

  tracker.trackEventListener(q('#sync-missing-btn'), 'click', async () => {
    await syncMissing();
  });

  tracker.trackEventListener(q('#save-attack-btn'), 'click', async () => {
    if (!currentAttack) return;
    const d = await postJSON('/save_attack', { name: currentAttack, content: q('#editor-textarea').value });
    if (d.status === 'success') note(L('common.saved'), 1500, 'success');
  });

  tracker.trackEventListener(q('#restore-attack-btn'), 'click', async () => {
    if (!currentAttack) return;
    if (!confirm(L('attacks.confirm.restoreAttack', { name: currentAttack }))) return;
    const d = await postJSON('/restore_attack', { name: currentAttack });
    if (d.status === 'success') selectAttack(currentAttack, q(`#attacks-list .card[data-attack-name="${currentAttack}"]`));
  });

  tracker.trackEventListener(q('#create-character-btn'), 'click', async () => {
    const name = prompt(L('attacks.prompt.newCharacterName'));
    if (!name) return;
    const d = await postJSON('/create_character', { character_name: name });
    if (d.status === 'success') { note(L('attacks.toast.characterCreated'), 1800, 'success'); loadCharacters(); }
  });

  tracker.trackEventListener(q('#delete-character-btn'), 'click', async () => {
    const d = await getJSON('/list_characters');
    const deletable = (d.characters || []).filter((x) => x.name !== 'BJORN').map((x) => x.name);
    if (!deletable.length) return note(L('attacks.toast.noDeletableCharacters'), 1800, 'warning');
    const name = prompt(L('attacks.prompt.characterToDelete') + '\n' + deletable.join('\n'));
    if (!name || !deletable.includes(name)) return;
    if (!confirm(L('attacks.confirm.deleteCharacter', { name }))) return;
    const r = await postJSON('/delete_character', { character_name: name });
    if (r.status === 'success') { note(L('attacks.toast.characterDeleted'), 1800, 'success'); loadCharacters(); }
  });

  tracker.trackEventListener(q('#add-section-btn'), 'click', async () => {
    const name = prompt(L('attacks.prompt.newSectionName'));
    if (!name) return;
    const d = await postJSON('/save_comments', { section: name, comments: [] });
    if (d.status === 'success') loadSections();
  });

  tracker.trackEventListener(q('#delete-section-btn'), 'click', async () => {
    if (!selectedSection) return;
    if (!confirm(L('attacks.confirm.deleteSection', { name: selectedSection }))) return;
    const d = await postJSON('/delete_comment_section', { section: selectedSection });
    if (d.status === 'success') {
      selectedSection = null;
      q('#comments-editor').innerHTML = '';
      q('#section-title').textContent = L('attacks.tabs.comments');
      loadSections();
    }
  });

  tracker.trackEventListener(q('#restore-default-btn'), 'click', async () => {
    if (!confirm(L('attacks.confirm.restoreDefaultComments'))) return;
    const r = await fetch('/restore_default_comments', { method: 'POST' });
    const d = await r.json();
    if (d.status === 'success') { note(L('attacks.toast.commentsRestored'), 1800, 'success'); loadSections(); }
  });

  tracker.trackEventListener(q('#save-comments-btn'), 'click', async () => {
    if (!selectedSection) return note(L('attacks.toast.selectSectionFirst'), 1800, 'warning');
    const lines = qa('.comment-line', q('#comments-editor')).map((x) => x.textContent?.trim()).filter(Boolean);
    const d = await postJSON('/save_comments', { section: selectedSection, comments: lines });
    if (d.status === 'success') note(L('attacks.toast.commentsSaved'), 1600, 'success');
  });

  tracker.trackEventListener(q('#select-all-btn'), 'click', () => {
    const ce = q('#comments-editor');
    if (!ce) return;
    ce.focus();
    const sel = window.getSelection();
    if (!sel) return;
    const range = document.createRange();
    range.selectNodeContents(ce);
    sel.removeAllRanges();
    sel.addRange(range);
  });

  tracker.trackEventListener(q('#search-input'), 'input', () => renderImages(imageCache, imageResolver || (() => '')));
  tracker.trackEventListener(q('#sort-key'), 'change', (e) => { sortKey = e.target.value; renderImages(imageCache, imageResolver || (() => '')); });
  tracker.trackEventListener(q('#sort-dir'), 'click', (e) => { sortDir *= -1; e.target.textContent = sortDir === 1 ? '^' : 'v'; renderImages(imageCache, imageResolver || (() => '')); });
  tracker.trackEventListener(q('#density'), 'input', (e) => {
    const px = Number(e.target.value) || 160;
    root?.style.setProperty('--tile-min', `${px}px`);
    try { localStorage.setItem('attacks.tileMin', String(px)); } catch { }
  });

  tracker.trackEventListener(q('#edit-mode-toggle-btn'), 'click', () => {
    editMode = !editMode;
    syncImageModeClasses();
    q('#edit-mode-toggle-btn').textContent = editMode ? L('attacks.images.exitEditMode') : L('attacks.images.enterEditMode');
    if (!editMode) {
      selectedImages.clear();
      qa('.image-item.selected').forEach((x) => x.classList.remove('selected'));
    }
    renderImages(imageCache, imageResolver || (() => ''));
  });

  tracker.trackEventListener(q('#rename-image-btn'), 'click', async () => {
    if (selectedImages.size !== 1) return note(L('attacks.toast.selectExactlyOneImage'), 1800, 'warning');
    const oldName = Array.from(selectedImages)[0];
    const newName = prompt(L('attacks.prompt.newImageName'), oldName);
    if (!newName || newName === oldName) return;
    const type = selectedImageScope === 'action' ? 'image' : selectedImageScope;
    const d = await postJSON('/rename_image', { type, action: selectedActionName, old_name: oldName, new_name: newName });
    if (d.status === 'success') { selectedImages.clear(); refreshScope(); }
  });

  tracker.trackEventListener(q('#replace-image-btn'), 'click', async () => {
    if (selectedImages.size !== 1) return note(L('attacks.toast.selectExactlyOneImage'), 1800, 'warning');
    const oldName = Array.from(selectedImages)[0];
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.bmp,.jpg,.jpeg,.png,.gif,.ico,.webp';
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      const fd = new FormData();
      fd.append('type', selectedImageScope);
      fd.append('image_name', oldName);
      if (selectedImageScope === 'action') fd.append('action', selectedActionName);
      fd.append('new_image', f);
      const r = await fetch('/replace_image', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') { selectedImages.clear(); refreshScope(); }
    };
    inp.click();
  });

  tracker.trackEventListener(q('#resize-images-btn'), 'click', async () => {
    if (!selectedImages.size) return note(L('attacks.toast.selectAtLeastOneImage'), 1800, 'warning');
    const w = Number(prompt(L('attacks.prompt.resizeWidth'), '100'));
    const h = Number(prompt(L('attacks.prompt.resizeHeight'), '100'));
    if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return;
    const payload = {
      type: selectedImageScope,
      action: selectedActionName,
      image_names: Array.from(selectedImages),
      width: Math.round(w),
      height: Math.round(h),
    };
    const d = await postJSON('/resize_images', payload);
    if (d.status === 'success') {
      note(L('attacks.toast.imagesResized'), 1800, 'success');
      selectedImages.clear();
      await refreshScope();
    } else {
      note(d.message || L('common.error'), 2200, 'error');
    }
  });

  tracker.trackEventListener(q('#add-characters-btn'), 'click', async () => {
    if (selectedImageScope !== 'action' || !selectedActionName) return note(L('attacks.toast.selectStatusActionFirst'), 1800, 'warning');
    const inp = document.createElement('input');
    inp.type = 'file';
    inp.multiple = true;
    inp.accept = '.bmp,.jpg,.jpeg,.png';
    inp.onchange = async () => {
      const files = Array.from(inp.files || []);
      if (!files.length) return;
      const fd = new FormData();
      fd.append('action_name', selectedActionName);
      files.forEach((f) => fd.append('character_images', f));
      const r = await fetch('/upload_character_images', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') {
        note(L('attacks.toast.characterImagesUploaded'), 1800, 'success');
        await refreshScope();
      } else {
        note(d.message || L('common.error'), 2200, 'error');
      }
    };
    inp.click();
  });

  tracker.trackEventListener(q('#delete-images-btn'), 'click', async () => {
    if (!selectedImages.size) return note(L('attacks.toast.selectAtLeastOneImage'), 1800, 'warning');
    if (!confirm(L('attacks.confirm.deleteSelectedImages'))) return;
    const d = await postJSON('/delete_images', { type: selectedImageScope, action: selectedActionName, image_names: Array.from(selectedImages) });
    if (d.status === 'success') { selectedImages.clear(); refreshScope(); }
  });

  tracker.trackEventListener(q('#add-status-image-btn'), 'click', async () => {
    if (selectedImageScope !== 'action' || !selectedActionName) return note(L('attacks.toast.selectStatusActionFirst'), 1800, 'warning');
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.bmp,.jpg,.jpeg,.png';
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      const fd = new FormData(); fd.append('type', 'action'); fd.append('action_name', selectedActionName); fd.append('status_image', f);
      const r = await fetch('/upload_status_image', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') refreshScope();
    };
    inp.click();
  });

  tracker.trackEventListener(q('#add-static-image-btn'), 'click', async () => {
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.bmp,.jpg,.jpeg,.png,.gif,.ico,.webp';
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      const fd = new FormData(); fd.append('static_image', f);
      const r = await fetch('/upload_static_image', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') refreshScope();
    };
    inp.click();
  });

  tracker.trackEventListener(q('#add-web-image-btn'), 'click', async () => {
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.bmp,.jpg,.jpeg,.png,.gif,.ico,.webp';
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      const fd = new FormData(); fd.append('web_image', f);
      const r = await fetch('/upload_web_image', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') refreshScope();
    };
    inp.click();
  });

  tracker.trackEventListener(q('#add-icon-image-btn'), 'click', async () => {
    const inp = document.createElement('input'); inp.type = 'file'; inp.accept = '.bmp,.jpg,.jpeg,.png,.gif,.ico,.webp';
    inp.onchange = async () => {
      const f = inp.files?.[0]; if (!f) return;
      const fd = new FormData(); fd.append('icon_image', f);
      const r = await fetch('/upload_actions_icon', { method: 'POST', body: fd });
      const d = await r.json();
      if (d.status === 'success') refreshScope();
    };
    inp.click();
  });
}

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = el('div', { class: 'attacks-container page-with-sidebar' });
  root.innerHTML = markup();
  container.appendChild(root);
  q('.attacks-sidebar')?.classList.add('page-sidebar');
  q('.attacks-main')?.classList.add('page-main');
  disposeSidebarLayout = initSharedSidebarLayout(root, {
    sidebarSelector: '.attacks-sidebar',
    mainSelector: '.attacks-main',
    storageKey: 'sidebar:attacks',
    mobileBreakpoint: 900,
    toggleLabel: Lx('common.menu', 'Menu'),
    mobileDefaultOpen: true,
  });
  bindTabs();
  bindActions();
  syncImageModeClasses();
  epdEditor.mount(tracker);

  const density = q('#density');
  if (density) {
    let tile = Number(density.value) || 160;
    try {
      const saved = Number(localStorage.getItem('attacks.tileMin'));
      if (Number.isFinite(saved) && saved >= 120 && saved <= 260) tile = saved;
    } catch { }
    density.value = String(tile);
    root.style.setProperty('--tile-min', `${tile}px`);
  }

  const ce = q('#comments-editor');
  if (ce && !ce.textContent.trim()) {
    ce.classList.add('placeholder');
    ce.textContent = ce.dataset.placeholder || L('attacks.comments.placeholder');
    tracker.trackEventListener(ce, 'focus', () => {
      if (ce.classList.contains('placeholder')) {
        ce.classList.remove('placeholder');
        ce.innerHTML = '<div class="comment-line"><br></div>';
      }
    });
  }

  await loadAttacks();
}

export function unmount() {
  epdEditor.unmount();
  for (const v of iconCache.values()) {
    if (typeof v === 'string' && v.startsWith('blob:')) URL.revokeObjectURL(v);
  }
  iconCache.clear();
  selectedImages.clear();
  currentAttack = null;
  selectedSection = null;
  selectedImageScope = null;
  selectedActionName = null;
  editMode = false;
  imageCache = [];
  imageResolver = null;
  if (disposeSidebarLayout) {
    disposeSidebarLayout();
    disposeSidebarLayout = null;
  }
  if (tracker) {
    tracker.cleanupAll();
    tracker = null;
  }
  root = null;
}
