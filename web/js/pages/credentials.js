/**
 * Credentials page module.
 * Displays credentials organized by service with tabs, search, and CSV export.
 * Endpoint: GET /list_credentials (returns HTML tables)
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api, Poller } from '../core/api.js';
import { el, $, $$, empty } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'credentials';
const REFRESH_INTERVAL = 30000;

/* ── state ── */
let tracker = null;
let poller = null;
let serviceData = []; // [{ service, category, credentials: { headers, rows } }]
let currentCategory = 'all';
let searchGlobal = '';
let searchTerms = {};
let collapsedCards = new Set();
let toastTimer = null;
let prevServiceFingerprint = ''; /* tracks service data for incremental updates */

/* ── localStorage ── */
const LS_CARD = 'cred:card:collapsed:';
const getCardPref = (svc) => { try { return localStorage.getItem(LS_CARD + svc); } catch { return null; } };
const setCardPref = (svc, collapsed) => { try { localStorage.setItem(LS_CARD + svc, collapsed ? '1' : '0'); } catch { } };

/* ── lifecycle ── */
export async function mount(container) {
    tracker = new ResourceTracker(PAGE);
    container.appendChild(buildShell());
    await fetchCredentials();
    poller = new Poller(fetchCredentials, REFRESH_INTERVAL);
    poller.start();
}

export function unmount() {
    if (poller) { poller.stop(); poller = null; }
    if (tracker) { tracker.cleanupAll(); tracker = null; }
    serviceData = [];
    currentCategory = 'all';
    searchGlobal = '';
    searchTerms = {};
    collapsedCards.clear();
    toastTimer = null;
    prevServiceFingerprint = '';
}

/* ── shell ── */
function buildShell() {
    return el('div', { class: 'credentials-container' }, [
        /* stats bar */
        el('div', { class: 'stats-bar' }, [
            statItem('🧩', 'stat-services', t('creds.services')),
            statItem('🔐', 'stat-creds', t('creds.totalCredentials')),
            statItem('🖥️', 'stat-hosts', t('creds.uniqueHosts')),
        ]),
        /* global search */
        el('div', { class: 'global-search-container' }, [
            el('input', {
                type: 'text', id: 'cred-global-search', class: 'global-search-input',
                placeholder: t('common.search'), oninput: onGlobalSearch
            }),
            el('button', { class: 'clear-global-button', id: 'cred-clear-global', onclick: clearGlobalSearch }, ['✖']),
        ]),
        /* tabs */
        el('div', { class: 'tabs-container', id: 'cred-tabs' }),
        /* services grid */
        el('div', { class: 'services-grid', id: 'credentials-grid' }),
        /* toast */
        el('div', { class: 'copied-feedback', id: 'cred-toast' }, [t('creds.copied')]),
    ]);
}

function statItem(icon, id, label) {
    return el('div', { class: 'stat-item' }, [
        el('span', { class: 'stat-icon' }, [icon]),
        el('span', { class: 'stat-value', id }, ['0']),
        el('span', { class: 'stat-label' }, [label]),
    ]);
}

/* ── fetch ── */
async function fetchCredentials() {
    try {
        const ac = tracker ? tracker.trackAbortController() : new AbortController();
        const text = await fetch('/list_credentials', { signal: ac.signal }).then(r => r.text());
        if (tracker) tracker.removeAbortController(ac);

        /* guard: page may have unmounted while awaiting */
        if (!tracker) return;

        const doc = new DOMParser().parseFromString(text, 'text/html');
        const tables = doc.querySelectorAll('table');

        serviceData = [];
        tables.forEach(table => {
            const titleEl = table.previousElementSibling;
            if (titleEl && titleEl.textContent) {
                const raw = titleEl.textContent.toLowerCase().replace('.csv', '').trim();
                const credentials = parseTable(table);
                serviceData.push({ service: raw, category: raw, credentials });
            }
        });

        // Sort by most credentials first
        serviceData.sort((a, b) => (b.credentials.rows?.length || 0) - (a.credentials.rows?.length || 0));

        /* Compute a fingerprint of the data to skip DOM rebuild when nothing changed */
        const fp = serviceData.map(s =>
          `${s.service}:${s.credentials.rows.length}:${s.credentials.rows.map(r => Object.values(r).join('|')).join(',')}`
        ).join(';');

        if (fp === prevServiceFingerprint) return; /* no changes — skip DOM rebuild */
        prevServiceFingerprint = fp;

        updateStats();
        renderTabs();
        renderServices();
        applyPersistedCollapse();
    } catch (err) {
        if (err.name === 'AbortError') return;
        console.error(`[${PAGE}] fetch error:`, err);
    }
}

function parseTable(table) {
    const headers = Array.from(table.querySelectorAll('th')).map(th => th.textContent.trim());
    const rows = Array.from(table.querySelectorAll('tr')).slice(1).map(row => {
        const cells = Array.from(row.querySelectorAll('td'));
        return Object.fromEntries(headers.map((h, i) => [h, (cells[i]?.textContent || '').trim()]));
    });
    return { headers, rows };
}

/* ── stats ── */
function updateStats() {
    const setVal = (id, v) => { const e = $(`#${id}`); if (e) e.textContent = v; };
    setVal('stat-services', serviceData.length);
    setVal('stat-creds', serviceData.reduce((a, s) => a + (s.credentials.rows?.length || 0), 0));

    // Count unique MACs
    const macSet = new Set();
    serviceData.forEach(s => {
        (s.credentials.rows || []).forEach(r => {
            for (const [k, v] of Object.entries(r)) {
                if (k.toLowerCase().includes('mac')) {
                    const norm = normalizeMac(v);
                    if (norm) macSet.add(norm);
                }
            }
        });
    });
    setVal('stat-hosts', macSet.size);
}

function normalizeMac(v) {
    if (!v) return null;
    const raw = String(v).toLowerCase().replace(/[^0-9a-f]/g, '');
    if (raw.length !== 12) return null;
    return raw.match(/.{2}/g).join(':');
}

/* ── tabs ── */
function getCategories() {
    return [...new Set(serviceData.map(s => s.category))];
}

function computeBadgeCounts() {
    const map = { all: 0 };
    getCategories().forEach(cat => map[cat] = 0);
    const needle = searchGlobal.toLowerCase();

    serviceData.forEach(svc => {
        const rows = svc.credentials.rows || [];
        let count;
        if (!needle) {
            count = rows.length;
        } else {
            count = rows.reduce((acc, row) => {
                const text = Object.values(row).join(' ').toLowerCase();
                return acc + (text.includes(needle) ? 1 : 0);
            }, 0);
        }
        map.all += count;
        map[svc.category] = (map[svc.category] || 0) + count;
    });
    return map;
}

function renderTabs() {
    const tabs = $('#cred-tabs');
    if (!tabs) return;
    const counts = computeBadgeCounts();
    const cats = ['all', ...getCategories()];
    empty(tabs);

    cats.forEach(cat => {
        const label = cat === 'all' ? t('common.all') : cat.toUpperCase();
        const count = counts[cat] || 0;
        const active = cat === currentCategory ? 'active' : '';
        const tab = el('div', { class: `tab ${active}`, 'data-cat': cat }, [
            label,
            el('span', { class: 'tab-badge' }, [String(count)]),
        ]);
        tab.onclick = () => {
            currentCategory = cat;
            tabs.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            renderServices();
            applyPersistedCollapse();
        };
        tabs.appendChild(tab);
    });
}

function updateBadges() {
    const counts = computeBadgeCounts();
    $$('#cred-tabs .tab').forEach(tab => {
        const cat = tab.dataset.cat;
        const badge = tab.querySelector('.tab-badge');
        if (badge) badge.textContent = counts[cat] || 0;
    });
}

/* ── services rendering ── */
function renderServices() {
    const grid = $('#credentials-grid');
    if (!grid) return;
    empty(grid);

    const needle = searchGlobal.toLowerCase();

    // Filter by global search
    let searched = serviceData.filter(svc => {
        if (!needle) return true;
        const titleMatch = svc.service.includes(needle);
        const rowMatch = svc.credentials.rows.some(r =>
            Object.values(r).join(' ').toLowerCase().includes(needle));
        return titleMatch || rowMatch;
    });

    // Filter by category
    if (currentCategory !== 'all') {
        searched = searched.filter(s => s.category === currentCategory);
    }

    if (searched.length === 0) {
        grid.appendChild(el('div', { style: 'text-align:center;color:var(--muted);padding:40px' }, [
            el('div', { style: 'font-size:3rem;margin-bottom:16px;opacity:.5' }, ['🔍']),
            t('creds.noCredentials'),
        ]));
        updateBadges();
        return;
    }

    searched.forEach(s => grid.appendChild(createServiceCard(s)));

    // If global search active, auto-expand and filter rows
    if (needle) {
        $$('.service-card', grid).forEach(card => {
            card.classList.remove('collapsed');
            card.querySelectorAll('.credential-item').forEach(item => {
                const text = item.textContent.toLowerCase();
                item.style.display = text.includes(needle) ? '' : 'none';
            });
        });
    }

    updateBadges();
}

function createServiceCard(svc) {
    const count = svc.credentials.rows.length;
    const isCollapsed = collapsedCards.has(svc.service);

    const card = el('div', {
        class: `service-card ${isCollapsed ? 'collapsed' : ''}`,
        'data-service': svc.service,
        'data-credentials': String(count),
    }, [
        /* header */
        el('div', { class: 'service-header', onclick: (e) => toggleCollapse(e, svc.service) }, [
            el('span', { class: 'service-title' }, [svc.service.toUpperCase()]),
            el('span', { class: 'service-count' }, [t('creds.credentialsCount', { count })]),
            el('div', { class: 'search-container', onclick: e => e.stopPropagation() }, [
                el('input', {
                    type: 'text', class: 'search-input', placeholder: t('creds.searchDots'),
                    'data-service': svc.service, oninput: (e) => filterServiceCreds(e, svc.service)
                }),
                el('button', { class: 'clear-button', onclick: (e) => clearServiceSearch(e, svc.service) }, ['✖']),
            ]),
            el('button', {
                class: 'download-button', title: t('creds.downloadCsv'),
                onclick: (e) => downloadCSV(e, svc.service, svc.credentials)
            }, ['💾']),
            el('span', { class: 'collapse-indicator' }, ['▼']),
        ]),
        /* content */
        el('div', { class: 'service-content' }, [
            ...svc.credentials.rows.map(row => createCredentialItem(row)),
        ]),
    ]);

    return card;
}

function createCredentialItem(row) {
    return el('div', { class: 'credential-item' }, [
        ...Object.entries(row).map(([key, value]) => {
            const val = String(value ?? '');
            const bubbleClass = getBubbleClass(key);
            return el('div', { class: 'credential-field' }, [
                el('span', { class: 'field-label' }, [key]),
                el('div', {
                    class: `field-value ${val.trim() ? bubbleClass : ''}`,
                    'data-value': val,
                    onclick: (e) => copyToClipboard(e.currentTarget),
                    title: t('creds.clickToCopy'),
                }, [val]),
            ]);
        }),
    ]);
}

function getBubbleClass(key) {
    const k = key.toLowerCase();
    if (k === 'port') return 'bubble-orange';
    if (['ip address', 'ip', 'hostname', 'mac address', 'mac'].includes(k)) return 'bubble-blue';
    return 'bubble-green';
}

/* ── collapse ── */
function toggleCollapse(e, service) {
    if (e.target.closest('.search-container') || e.target.closest('.download-button')) return;
    const card = $(`.service-card[data-service="${service}"]`);
    if (!card) return;
    const nowCollapsed = !card.classList.contains('collapsed');
    card.classList.toggle('collapsed');
    if (nowCollapsed) collapsedCards.add(service);
    else collapsedCards.delete(service);
    setCardPref(service, nowCollapsed);
}

function applyPersistedCollapse() {
    $$('.service-card').forEach(card => {
        const svc = card.dataset.service;
        const pref = getCardPref(svc);
        if (pref === '1') {
            card.classList.add('collapsed');
            collapsedCards.add(svc);
        } else if (pref === '0') {
            card.classList.remove('collapsed');
            collapsedCards.delete(svc);
        } else {
            // Default: collapsed
            card.classList.add('collapsed');
        }
    });
}

/* ── search ── */
function onGlobalSearch(e) {
    searchGlobal = e.target.value;
    const clearBtn = $('#cred-clear-global');
    if (clearBtn) clearBtn.classList.toggle('show', searchGlobal.length > 0);
    renderServices();
    applyPersistedCollapse();
}

function clearGlobalSearch() {
    const inp = $('#cred-global-search');
    if (inp) inp.value = '';
    searchGlobal = '';
    const clearBtn = $('#cred-clear-global');
    if (clearBtn) clearBtn.classList.remove('show');
    renderServices();
    applyPersistedCollapse();
    $$('.service-card').forEach(c => c.classList.add('collapsed'));
}

function filterServiceCreds(e, service) {
    const filter = e.target.value.toLowerCase();
    searchTerms[service] = filter;
    const card = $(`.service-card[data-service="${service}"]`);
    if (!card) return;

    if (filter.length > 0) card.classList.remove('collapsed');

    card.querySelectorAll('.credential-item').forEach(item => {
        const text = item.textContent.toLowerCase();
        item.style.display = text.includes(filter) ? '' : 'none';
    });

    // Toggle clear button
    const clearBtn = e.target.nextElementSibling;
    if (clearBtn) clearBtn.classList.toggle('show', filter.length > 0);
}

function clearServiceSearch(e, service) {
    e.stopPropagation();
    const card = $(`.service-card[data-service="${service}"]`);
    if (!card) return;
    const inp = card.querySelector('.search-input');
    if (inp) inp.value = '';
    searchTerms[service] = '';
    card.querySelectorAll('.credential-item').forEach(item => item.style.display = '');
    const clearBtn = card.querySelector('.clear-button');
    if (clearBtn) clearBtn.classList.remove('show');
}

/* ── copy ── */
function copyToClipboard(el) {
    const text = el.dataset.value || '';
    navigator.clipboard.writeText(text).then(() => {
        showToast();
        const bg = el.style.background;
        el.style.background = '#4CAF50';
        if (tracker) tracker.trackTimeout(() => { el.style.background = bg; }, 500);
        else setTimeout(() => { el.style.background = bg; }, 500);
    }).catch(() => {
        // Fallback
        const ta = document.createElement('textarea');
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showToast();
    });
}

function showToast() {
    const toast = $('#cred-toast');
    if (!toast) return;
    toast.classList.add('show');
    if (toastTimer != null) {
        if (tracker) tracker.clearTrackedTimeout(toastTimer);
        else clearTimeout(toastTimer);
    }
    toastTimer = tracker
        ? tracker.trackTimeout(() => { toast.classList.remove('show'); toastTimer = null; }, 1500)
        : setTimeout(() => { toast.classList.remove('show'); toastTimer = null; }, 1500);
}

/* ── CSV export ── */
function downloadCSV(e, service, credentials) {
    e.stopPropagation();
    if (!credentials.rows || credentials.rows.length === 0) return;
    const headers = Object.keys(credentials.rows[0]);
    let csv = headers.join(',') + '\n';
    credentials.rows.forEach(row => {
        const values = headers.map(h => {
            const v = String(row[h] ?? '');
            return v.includes(',') ? `"${v.replace(/"/g, '""')}"` : v;
        });
        csv += values.join(',') + '\n';
    });
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${service}_credentials.csv`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}
