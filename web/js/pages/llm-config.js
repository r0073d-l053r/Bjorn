/**
 * llm-config — LLM Bridge & MCP Server settings SPA page
 */
import { ResourceTracker } from '../core/resource-tracker.js';
import { api } from '../core/api.js';
import { el, $, empty, toast } from '../core/dom.js';
import { t } from '../core/i18n.js';

const PAGE = 'llm-config';

const ALL_TOOLS = [
  'get_hosts', 'get_vulnerabilities', 'get_credentials',
  'get_action_history', 'get_status', 'run_action', 'query_db',
];

/* ── State ─────────────────────────────────────────────── */

let tracker = null;
let root    = null;

/* ── Lifecycle ─────────────────────────────────────────── */

export async function mount(container) {
  tracker = new ResourceTracker(PAGE);
  root = buildShell();
  container.appendChild(root);
  bindEvents();
  await loadAll();
}

export function unmount() {
  if (tracker) { tracker.cleanupAll(); tracker = null; }
  root = null;
}

/* ── Shell ─────────────────────────────────────────────── */

function buildShell() {
  return el('div', { class: 'llmcfg-page' }, [

    /* Page header */
    el('div', { class: 'llmcfg-header' }, [
      el('span', { class: 'llmcfg-title' }, ['BJORN / LLM & MCP SETTINGS']),
      el('a', { class: 'llmcfg-nav-link', href: '#/llm-chat' }, ['→ ' + t('nav.llm_chat')]),
    ]),

    el('div', { class: 'llmcfg-container' }, [

      /* ── LLM Bridge section ──────────────────────────── */
      el('div', { class: 'llmcfg-section' }, [
        el('div', { class: 'llmcfg-section-title' }, [
          'LLM BRIDGE',
          el('span', { class: 'llmcfg-badge off', id: 'llm-badge' }, ['OFF']),
        ]),
        el('div', { class: 'llmcfg-body' }, [

          toggleRow('llm_enabled',          t('llm_cfg.enable_bridge')),
          toggleRow('llm_comments_enabled', t('llm_cfg.epd_comments')),
          toggleRow('llm_comments_log',    'Log comments to console'),
          toggleRow('llm_chat_enabled',    'Enable LLM chat'),
          toggleRow('llm_chat_tools_enabled', 'Enable tools in chat (function calling)'),
          toggleRow('epd_buttons_enabled', 'EPD physical buttons'),

          fieldEl(t('llm_cfg.backend'), el('select', { id: 'llm_backend', class: 'llmcfg-select' }, [
            el('option', { value: 'auto' },    ['Auto (LaRuche → Ollama → API)']),
            el('option', { value: 'laruche' }, ['LaRuche only']),
            el('option', { value: 'ollama' },  ['Ollama only']),
            el('option', { value: 'api' },     ['External API only']),
          ])),

          subsectionTitle('LARUCHE / LAND'),
          toggleRow('llm_laruche_discovery', t('llm_cfg.laruche_discovery')),
          el('div', { class: 'llmcfg-discovery-row', id: 'laruche-discovery-status' }),
          fieldEl(t('llm_cfg.laruche_url'),
            el('div', { class: 'llmcfg-url-row' }, [
              el('input', { type: 'text', id: 'llm_laruche_url', class: 'llmcfg-input',
                placeholder: 'Auto-detected via mDNS or enter manually' }),
              el('button', { class: 'llmcfg-btn compact', id: 'laruche-use-discovered',
                style: 'display:none' }, ['Use']),
            ])),
          fieldEl('LaRuche Model',
            el('div', { class: 'llmcfg-model-row' }, [
              el('select', { id: 'llm_laruche_model', class: 'llmcfg-select' }, [
                el('option', { value: '' }, ['Default (server decides)']),
              ]),
              el('button', { class: 'llmcfg-btn compact', id: 'laruche-refresh-models' }, ['⟳ Refresh']),
            ])),
          el('div', { class: 'llmcfg-laruche-default', id: 'laruche-default-model' }),

          subsectionTitle('OLLAMA (LOCAL)'),
          fieldEl(t('llm_cfg.ollama_url'),
            el('input', { type: 'text', id: 'llm_ollama_url', class: 'llmcfg-input',
              placeholder: 'http://127.0.0.1:11434' })),
          fieldEl(t('llm_cfg.ollama_model'),
            el('div', { class: 'llmcfg-model-row' }, [
              el('select', { id: 'llm_ollama_model', class: 'llmcfg-select' }, [
                el('option', { value: '' }, ['Default']),
              ]),
              el('button', { class: 'llmcfg-btn compact', id: 'ollama-refresh-models' }, ['⟳ Refresh']),
            ])),

          subsectionTitle('EXTERNAL API'),
          el('div', { class: 'llmcfg-row' }, [
            fieldEl(t('llm_cfg.provider'), el('select', { id: 'llm_api_provider', class: 'llmcfg-select' }, [
              el('option', { value: 'anthropic' },   ['Anthropic (Claude)']),
              el('option', { value: 'openai' },      ['OpenAI']),
              el('option', { value: 'openrouter' },  ['OpenRouter']),
            ])),
            fieldEl(t('llm_cfg.api_model'),
              el('input', { type: 'text', id: 'llm_api_model', class: 'llmcfg-input',
                placeholder: 'claude-haiku-4-5-20251001' })),
          ]),
          fieldEl(t('llm_cfg.api_key'),
            el('input', { type: 'password', id: 'llm_api_key', class: 'llmcfg-input',
              placeholder: t('llm_cfg.api_key_placeholder') })),
          fieldEl(t('llm_cfg.base_url'),
            el('input', { type: 'text', id: 'llm_api_base_url', class: 'llmcfg-input',
              placeholder: 'https://openrouter.ai/api' })),

          el('div', { class: 'llmcfg-row' }, [
            fieldEl(t('llm_cfg.timeout'),
              el('input', { type: 'number', id: 'llm_timeout_s', class: 'llmcfg-input',
                min: '5', max: '120', value: '30' })),
            fieldEl(t('llm_cfg.max_tokens_chat'),
              el('input', { type: 'number', id: 'llm_max_tokens', class: 'llmcfg-input',
                min: '50', max: '4096', value: '500' })),
            fieldEl(t('llm_cfg.max_tokens_epd'),
              el('input', { type: 'number', id: 'llm_comment_max_tokens', class: 'llmcfg-input',
                min: '20', max: '200', value: '80' })),
          ]),

          el('div', { class: 'llmcfg-row' }, [
            fieldEl('Chat history size',
              el('input', { type: 'number', id: 'llm_chat_history_size', class: 'llmcfg-input',
                min: '2', max: '100', value: '20' })),
          ]),

          el('div', { class: 'llmcfg-status-row', id: 'llm-status-row' }),

          el('div', { class: 'llmcfg-actions' }, [
            el('button', { class: 'llmcfg-btn primary', id: 'llm-save-btn' }, [t('llm_cfg.save_llm')]),
            el('button', { class: 'llmcfg-btn', id: 'llm-test-btn' }, [t('llm_cfg.test_connection')]),
          ]),
        ]),
      ]),

      /* ── LLM Orchestrator section ────────────────────── */
      el('div', { class: 'llmcfg-section' }, [
        el('div', { class: 'llmcfg-section-title' }, [
          'LLM ORCHESTRATOR',
          el('span', { class: 'llmcfg-badge off', id: 'orch-badge' }, ['OFF']),
        ]),
        el('div', { class: 'llmcfg-body' }, [

          fieldEl('Mode', el('select', { id: 'llm_orchestrator_mode', class: 'llmcfg-select' }, [
            el('option', { value: 'none' },       ['Disabled']),
            el('option', { value: 'advisor' },     ['Advisor (suggest 1 action per cycle)']),
            el('option', { value: 'autonomous' },  ['Autonomous (full agentic loop)']),
          ])),

          el('div', { class: 'llmcfg-row' }, [
            fieldEl('Cycle interval (s)',
              el('input', { type: 'number', id: 'llm_orchestrator_interval_s', class: 'llmcfg-input',
                min: '30', max: '600', value: '60' })),
            fieldEl('Max actions / cycle',
              el('input', { type: 'number', id: 'llm_orchestrator_max_actions', class: 'llmcfg-input',
                min: '1', max: '10', value: '3' })),
          ]),

          toggleRow('llm_orchestrator_log_reasoning',  'Log reasoning to chat history'),
          toggleRow('llm_orchestrator_skip_if_no_change', 'Skip cycle when nothing changed'),
          toggleRow('llm_orchestrator_skip_scheduler', 'Skip scheduler (LLM-only mode)'),

          el('div', { class: 'llmcfg-status-row', id: 'orch-status-row' }),

          el('div', { class: 'llmcfg-actions' }, [
            el('button', { class: 'llmcfg-btn primary', id: 'orch-save-btn' }, ['SAVE ORCHESTRATOR']),
          ]),
        ]),
      ]),

      /* ── Personality & Prompts section ───────────────── */
      el('div', { class: 'llmcfg-section' }, [
        el('div', { class: 'llmcfg-section-title' }, ['PERSONALITY & PROMPTS']),
        el('div', { class: 'llmcfg-body' }, [

          fieldEl('Operator Name',
            el('input', { type: 'text', id: 'llm_user_name', class: 'llmcfg-input',
              placeholder: 'Your name (Bjorn will address you)' })),
          fieldEl('About you',
            el('textarea', { id: 'llm_user_bio', class: 'llmcfg-textarea', rows: '2',
              placeholder: 'Brief description (e.g. security researcher, pentester, sysadmin...)' })),

          fieldEl('Chat System Prompt',
            el('div', {}, [
              el('textarea', { id: 'llm_system_prompt_chat', class: 'llmcfg-textarea', rows: '4',
                placeholder: 'Loading default prompt...' }),
              el('button', { class: 'llmcfg-btn compact llmcfg-reset-btn', id: 'reset-prompt-chat' },
                ['Reset to default']),
            ])),

          fieldEl('Comment System Prompt (EPD)',
            el('div', {}, [
              el('textarea', { id: 'llm_system_prompt_comment', class: 'llmcfg-textarea', rows: '3',
                placeholder: 'Loading default prompt...' }),
              el('button', { class: 'llmcfg-btn compact llmcfg-reset-btn', id: 'reset-prompt-comment' },
                ['Reset to default']),
            ])),

          el('div', { class: 'llmcfg-actions' }, [
            el('button', { class: 'llmcfg-btn primary', id: 'prompts-save-btn' }, ['SAVE PERSONALITY']),
          ]),
        ]),
      ]),

      /* ── MCP Server section ──────────────────────────── */
      el('div', { class: 'llmcfg-section' }, [
        el('div', { class: 'llmcfg-section-title' }, [
          'MCP SERVER',
          el('span', { class: 'llmcfg-badge off', id: 'mcp-badge' }, ['OFF']),
        ]),
        el('div', { class: 'llmcfg-body' }, [

          toggleRow('mcp_enabled', t('llm_cfg.enable_mcp')),

          el('div', { class: 'llmcfg-row' }, [
            fieldEl(t('llm_cfg.transport'), el('select', { id: 'mcp_transport', class: 'llmcfg-select' }, [
              el('option', { value: 'http' },  ['HTTP SSE (LAN accessible)']),
              el('option', { value: 'stdio' }, ['stdio (Claude Desktop)']),
            ])),
            fieldEl(t('llm_cfg.mcp_port'),
              el('input', { type: 'number', id: 'mcp_port', class: 'llmcfg-input',
                min: '1024', max: '65535', value: '8765' })),
          ]),

          fieldEl(t('llm_cfg.exposed_tools'),
            el('div', { class: 'llmcfg-tools-grid', id: 'tools-grid' })),

          el('div', { class: 'llmcfg-status-row', id: 'mcp-status-row' }),

          el('div', { class: 'llmcfg-actions' }, [
            el('button', { class: 'llmcfg-btn primary', id: 'mcp-save-btn' }, [t('llm_cfg.save_mcp')]),
          ]),
        ]),
      ]),

    ]),
  ]);
}

/* ── Builder helpers ───────────────────────────────────── */

function toggleRow(id, label) {
  return el('div', { class: 'llmcfg-toggle-row' }, [
    el('span', { class: 'llmcfg-toggle-label' }, [label]),
    el('label', { class: 'llmcfg-toggle' }, [
      el('input', { type: 'checkbox', id }),
      el('span', { class: 'llmcfg-slider' }),
    ]),
  ]);
}

function fieldEl(label, input) {
  return el('div', { class: 'llmcfg-field' }, [
    el('label', { class: 'llmcfg-label' }, [label]),
    input,
  ]);
}

function subsectionTitle(text) {
  return el('div', { class: 'llmcfg-subsection-title' }, [text]);
}

/* ── Events ────────────────────────────────────────────── */

function bindEvents() {
  const saveLlmBtn  = $('#llm-save-btn',  root);
  const testLlmBtn  = $('#llm-test-btn',  root);
  const saveMcpBtn  = $('#mcp-save-btn',  root);
  const mcpToggle   = $('#mcp_enabled',   root);

  const saveOrchBtn = $('#orch-save-btn', root);

  if (saveLlmBtn)  tracker.on(saveLlmBtn, 'click', saveLLM);
  if (testLlmBtn)  tracker.on(testLlmBtn, 'click', testLLM);
  if (saveMcpBtn)  tracker.on(saveMcpBtn, 'click', saveMCP);
  if (saveOrchBtn) tracker.on(saveOrchBtn, 'click', saveOrch);
  if (mcpToggle)   tracker.on(mcpToggle,  'change', () => toggleMCP(mcpToggle.checked));

  const savePromptsBtn = $('#prompts-save-btn', root);
  if (savePromptsBtn) tracker.on(savePromptsBtn, 'click', savePrompts);

  const resetChat = $('#reset-prompt-chat', root);
  if (resetChat) tracker.on(resetChat, 'click', () => {
    const ta = $('#llm_system_prompt_chat', root);
    if (ta) { ta.value = ''; toast('Prompt reset — save to apply'); }
  });
  const resetComment = $('#reset-prompt-comment', root);
  if (resetComment) tracker.on(resetComment, 'click', () => {
    const ta = $('#llm_system_prompt_comment', root);
    if (ta) { ta.value = ''; toast('Prompt reset — save to apply'); }
  });

  const larucheRefresh = $('#laruche-refresh-models', root);
  if (larucheRefresh) tracker.on(larucheRefresh, 'click', () => refreshModels('laruche'));

  const ollamaRefresh = $('#ollama-refresh-models', root);
  if (ollamaRefresh) tracker.on(ollamaRefresh, 'click', () => refreshModels('ollama'));
}

/* ── Data ──────────────────────────────────────────────── */

async function loadAll() {
  try {
    const [llmR, mcpR] = await Promise.all([
      api.get('/api/llm/config',  { timeout: 8000 }),
      api.get('/api/mcp/status', { timeout: 8000 }),
    ]);

    if (llmR) applyLLMConfig(llmR);
    if (mcpR) applyMCPConfig(mcpR);

  } catch (e) {
    toast('Load error: ' + e.message, 3000);
  }
}

function applyLLMConfig(cfg) {
  const boolKeys = [
    'llm_enabled', 'llm_comments_enabled', 'llm_comments_log',
    'llm_chat_enabled', 'llm_chat_tools_enabled',
    'llm_laruche_discovery', 'epd_buttons_enabled',
  ];
  const textKeys = ['llm_backend', 'llm_laruche_url', 'llm_ollama_url',
    'llm_api_provider', 'llm_api_model', 'llm_api_base_url',
    'llm_timeout_s', 'llm_max_tokens', 'llm_comment_max_tokens',
    'llm_chat_history_size',
    'llm_user_name', 'llm_user_bio',
    'llm_system_prompt_chat', 'llm_system_prompt_comment'];

  for (const k of boolKeys) {
    const el = $(('#' + k), root);
    if (el) el.checked = !!cfg[k];
  }
  for (const k of textKeys) {
    const el = $(('#' + k), root);
    if (el && cfg[k] !== undefined) el.value = cfg[k];
  }

  // Set default prompts as placeholders
  const chatPromptEl = $('#llm_system_prompt_chat', root);
  if (chatPromptEl && cfg.llm_default_prompt_chat) {
    chatPromptEl.placeholder = cfg.llm_default_prompt_chat;
  }
  const commentPromptEl = $('#llm_system_prompt_comment', root);
  if (commentPromptEl && cfg.llm_default_prompt_comment) {
    commentPromptEl.placeholder = cfg.llm_default_prompt_comment;
  }

  const badge = $('#llm-badge', root);
  if (badge) {
    badge.textContent = cfg.llm_enabled ? 'ON' : 'OFF';
    badge.className = 'llmcfg-badge ' + (cfg.llm_enabled ? 'on' : 'off');
  }

  const statusRow = $('#llm-status-row', root);
  if (statusRow) {
    statusRow.textContent = cfg.llm_api_key_set
      ? t('llm_cfg.api_key_set')
      : t('llm_cfg.api_key_not_set');
  }

  // LaRuche mDNS discovery status
  const discRow = $('#laruche-discovery-status', root);
  const useBtn  = $('#laruche-use-discovered', root);
  const urlEl   = $('#llm_laruche_url', root);
  const discovered = cfg.laruche_discovered_url || '';

  if (discRow) {
    if (discovered) {
      discRow.innerHTML = '';
      discRow.appendChild(el('span', { class: 'llmcfg-disc-found' },
        ['\u2705 LaRuche discovered: ' + discovered]));
    } else if (cfg.laruche_discovery_active === false && cfg.llm_laruche_discovery) {
      discRow.innerHTML = '';
      discRow.appendChild(el('span', { class: 'llmcfg-disc-searching' },
        ['\u23F3 mDNS scanning... no LaRuche node found yet']));
    } else if (!cfg.llm_laruche_discovery) {
      discRow.innerHTML = '';
      discRow.appendChild(el('span', { class: 'llmcfg-disc-off' },
        ['\u26A0 mDNS discovery disabled']));
    }
  }

  if (useBtn && urlEl) {
    if (discovered && urlEl.value !== discovered) {
      useBtn.style.display = '';
      useBtn.onclick = () => {
        urlEl.value = discovered;
        useBtn.style.display = 'none';
        toast('LaRuche URL applied — click Save to persist');
      };
    } else {
      useBtn.style.display = 'none';
    }
  }

  // Auto-populate empty URL field with discovered URL
  if (urlEl && discovered && !urlEl.value) {
    urlEl.value = discovered;
  }

  // ── Model selectors ──
  // Set saved model values on the selects (even before refresh populates full list)
  for (const k of ['llm_laruche_model', 'llm_ollama_model']) {
    const sel = $(('#' + k), root);
    if (sel && cfg[k]) {
      // Ensure the saved value exists as an option
      if (!sel.querySelector('option[value="' + CSS.escape(cfg[k]) + '"]')) {
        sel.appendChild(el('option', { value: cfg[k] }, [cfg[k] + ' (saved)']));
      }
      sel.value = cfg[k];
    }
  }

  // Auto-fetch LaRuche models if we have a URL
  const larucheUrl = urlEl?.value || discovered;
  if (larucheUrl) {
    refreshModels('laruche').catch(() => {});
  }

  // ── Orchestrator fields (included in same config response) ──
  const orchMode = $('#llm_orchestrator_mode', root);
  if (orchMode && cfg.llm_orchestrator_mode !== undefined) orchMode.value = cfg.llm_orchestrator_mode;

  const orchInterval = $('#llm_orchestrator_interval_s', root);
  if (orchInterval && cfg.llm_orchestrator_interval_s !== undefined) orchInterval.value = cfg.llm_orchestrator_interval_s;

  const orchMax = $('#llm_orchestrator_max_actions', root);
  if (orchMax && cfg.llm_orchestrator_max_actions !== undefined) orchMax.value = cfg.llm_orchestrator_max_actions;

  for (const k of ['llm_orchestrator_log_reasoning', 'llm_orchestrator_skip_if_no_change',
    'llm_orchestrator_skip_scheduler']) {
    const cb = $(('#' + k), root);
    if (cb) cb.checked = !!cfg[k];
  }

  const orchBadge = $('#orch-badge', root);
  if (orchBadge) {
    const mode = cfg.llm_orchestrator_mode || 'none';
    const label = mode === 'none' ? 'OFF' : mode.toUpperCase();
    orchBadge.textContent = label;
    orchBadge.className = 'llmcfg-badge ' + (mode === 'none' ? 'off' : 'on');
  }

  const orchStatus = $('#orch-status-row', root);
  if (orchStatus) {
    const mode = cfg.llm_orchestrator_mode || 'none';
    if (mode === 'none') {
      orchStatus.textContent = 'Orchestrator disabled — LLM has no role in scheduling';
    } else if (mode === 'advisor') {
      orchStatus.textContent = 'Advisor mode — LLM suggests 1 action per cycle';
    } else {
      orchStatus.textContent = 'Autonomous mode — LLM runs full agentic loop every '
        + (cfg.llm_orchestrator_interval_s || 60) + 's';
    }
  }
}

function applyMCPConfig(cfg) {
  const enabledEl = $('#mcp_enabled',   root);
  const portEl    = $('#mcp_port',      root);
  const transEl   = $('#mcp_transport', root);
  const badge     = $('#mcp-badge',     root);
  const statusRow = $('#mcp-status-row', root);

  if (enabledEl) enabledEl.checked = !!cfg.enabled;
  if (portEl)    portEl.value = cfg.port || 8765;
  if (transEl && cfg.transport) transEl.value = cfg.transport;

  buildToolsGrid(cfg.allowed_tools || ALL_TOOLS);

  const running = cfg.running;
  if (badge) {
    badge.textContent = running ? 'RUNNING' : (cfg.enabled ? 'ENABLED' : 'OFF');
    badge.className = 'llmcfg-badge ' + (running ? 'on' : 'off');
  }
  if (statusRow) {
    statusRow.textContent = running
      ? t('llm_cfg.mcp_running') + ' ' + (cfg.port || 8765) + ' (' + (cfg.transport || 'http') + ')'
      : t('llm_cfg.mcp_stopped');
  }
}

function buildToolsGrid(enabled) {
  const grid = $('#tools-grid', root);
  if (!grid) return;
  empty(grid);
  for (const name of ALL_TOOLS) {
    const label = el('label', { class: 'llmcfg-tool-item' }, [
      el('input', { type: 'checkbox', id: 'tool_' + name,
        checked: enabled.includes(name) ? 'checked' : undefined }),
      document.createTextNode(name),
    ]);
    grid.appendChild(label);
  }
}

function getSelectedTools() {
  return ALL_TOOLS.filter(n => $(('#tool_' + n), root)?.checked);
}

/* ── Model Selector ────────────────────────────────────── */

async function refreshModels(backend) {
  const selectId = backend === 'laruche' ? 'llm_laruche_model' : 'llm_ollama_model';
  const selectEl = $(('#' + selectId), root);
  if (!selectEl) return;

  toast('Fetching ' + backend + ' models…');
  try {
    const res = await api.get('/api/llm/models?backend=' + backend, { timeout: 15000 });
    if (res?.status === 'ok' && Array.isArray(res.models)) {
      populateModelSelect(selectEl, res.models, selectEl.value);
      toast(res.models.length + ' model(s) found');

      // Show LaRuche default model info
      if (backend === 'laruche') {
        const infoEl = $('#laruche-default-model', root);
        if (infoEl) {
          if (res.default_model) {
            infoEl.innerHTML = '';
            infoEl.appendChild(el('span', { class: 'llmcfg-laruche-default-label' },
              ['\u26A1 LaRuche default: ']));
            infoEl.appendChild(el('span', { class: 'llmcfg-laruche-default-value' },
              [res.default_model]));
          } else {
            infoEl.textContent = '';
          }
        }
      }
    } else {
      toast('No models returned: ' + (res?.message || 'unknown error'));
    }
  } catch (e) {
    toast('Error fetching models: ' + e.message);
  }
}

function populateModelSelect(selectEl, models, currentValue) {
  const prev = currentValue || selectEl.value || '';
  empty(selectEl);
  selectEl.appendChild(el('option', { value: '' }, ['Default (server decides)']));
  for (const m of models) {
    const name = m.name || '?';
    const sizeMB = m.size ? ' (' + (m.size / 1e9).toFixed(1) + 'G)' : '';
    selectEl.appendChild(el('option', { value: name }, [name + sizeMB]));
  }
  // Restore previous selection if it still exists
  if (prev) {
    selectEl.value = prev;
    // If the value didn't match any option, it resets to ''
    if (!selectEl.value && prev) {
      // Add it as a custom option so user doesn't lose their setting
      selectEl.appendChild(el('option', { value: prev }, [prev + ' (saved)']));
      selectEl.value = prev;
    }
  }
}

/* ── Actions ───────────────────────────────────────────── */

async function saveLLM() {
  const payload = {};

  for (const k of [
    'llm_enabled', 'llm_comments_enabled', 'llm_comments_log',
    'llm_chat_enabled', 'llm_chat_tools_enabled',
    'llm_laruche_discovery', 'epd_buttons_enabled',
  ]) {
    const el = $(('#' + k), root);
    payload[k] = el ? el.checked : false;
  }
  for (const k of ['llm_backend', 'llm_laruche_url', 'llm_laruche_model',
    'llm_ollama_url', 'llm_ollama_model',
    'llm_api_provider', 'llm_api_model', 'llm_api_base_url']) {
    const el = $(('#' + k), root);
    if (el) payload[k] = el.value;
  }
  for (const k of ['llm_timeout_s', 'llm_max_tokens', 'llm_comment_max_tokens',
    'llm_chat_history_size']) {
    const el = $(('#' + k), root);
    if (el) payload[k] = parseInt(el.value) || undefined;
  }
  const keyEl = $('#llm_api_key', root);
  if (keyEl?.value) payload.llm_api_key = keyEl.value;

  try {
    const res = await api.post('/api/llm/config', payload);
    if (res?.status === 'ok') {
      toast(t('llm_cfg.saved_llm'));
      await loadAll();
    } else {
      toast(t('llm_cfg.error') + ': ' + res?.message);
    }
  } catch (e) {
    toast(t('llm_cfg.save_error') + ': ' + e.message);
  }
}

async function savePrompts() {
  const payload = {};
  for (const k of ['llm_user_name', 'llm_user_bio', 'llm_system_prompt_chat', 'llm_system_prompt_comment']) {
    const el = $(('#' + k), root);
    if (el) payload[k] = el.value || '';
  }
  try {
    const res = await api.post('/api/llm/config', payload);
    if (res?.status === 'ok') {
      toast('Personality & prompts saved');
      await loadAll();
    } else {
      toast(t('llm_cfg.error') + ': ' + res?.message);
    }
  } catch (e) {
    toast(t('llm_cfg.save_error') + ': ' + e.message);
  }
}

async function testLLM() {
  toast(t('llm_cfg.testing'));
  try {
    const res = await api.post('/api/llm/chat', { message: 'ping', session_id: 'test' });
    if (res?.status === 'ok') {
      toast('OK — ' + (res.response || '').slice(0, 60));
    } else {
      toast(t('llm_cfg.test_failed') + ': ' + (res?.message || 'no response'));
    }
  } catch (e) {
    toast(t('llm_cfg.error') + ': ' + e.message);
  }
}

async function toggleMCP(enabled) {
  try {
    const res = await api.post('/api/mcp/toggle', { enabled });
    if (res?.status === 'ok') {
      toast(enabled ? t('llm_cfg.mcp_enabled') : t('llm_cfg.mcp_disabled'));
      await loadAll();
    } else {
      toast(t('llm_cfg.error') + ': ' + res?.message);
    }
  } catch (e) {
    toast(t('llm_cfg.error') + ': ' + e.message);
  }
}

async function saveOrch() {
  const payload = {};
  const modeEl = $('#llm_orchestrator_mode', root);
  if (modeEl) payload.llm_orchestrator_mode = modeEl.value;

  for (const k of ['llm_orchestrator_interval_s', 'llm_orchestrator_max_actions']) {
    const inp = $(('#' + k), root);
    if (inp) payload[k] = parseInt(inp.value) || undefined;
  }
  for (const k of ['llm_orchestrator_log_reasoning', 'llm_orchestrator_skip_if_no_change',
    'llm_orchestrator_skip_scheduler']) {
    const cb = $(('#' + k), root);
    if (cb) payload[k] = cb.checked;
  }

  try {
    const res = await api.post('/api/llm/config', payload);
    if (res?.status === 'ok') {
      toast('Orchestrator config saved');
      await loadAll();
    } else {
      toast(t('llm_cfg.error') + ': ' + res?.message);
    }
  } catch (e) {
    toast(t('llm_cfg.save_error') + ': ' + e.message);
  }
}

async function saveMCP() {
  const portEl  = $('#mcp_port',      root);
  const transEl = $('#mcp_transport', root);
  const payload = {
    allowed_tools: getSelectedTools(),
    port:          parseInt(portEl?.value) || 8765,
    transport:     transEl?.value || 'http',
  };
  try {
    const res = await api.post('/api/mcp/config', payload);
    if (res?.status === 'ok') {
      toast(t('llm_cfg.saved_mcp'));
      await loadAll();
    } else {
      toast(t('llm_cfg.error') + ': ' + res?.message);
    }
  } catch (e) {
    toast(t('llm_cfg.save_error') + ': ' + e.message);
  }
}
