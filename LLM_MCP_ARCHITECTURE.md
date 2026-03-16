# BJORN — LLM Bridge, MCP Server & LLM Orchestrator
## Complete architecture, operation, commands, fallbacks

---

## Table of contents

1. [Overview](#1-overview)
2. [Created / modified files](#2-created--modified-files)
3. [LLM Bridge (`llm_bridge.py`)](#3-llm-bridge-llm_bridgepy)
4. [MCP Server (`mcp_server.py`)](#4-mcp-server-mcp_serverpy)
5. [LLM Orchestrator (`llm_orchestrator.py`)](#5-llm-orchestrator-llm_orchestratorpy)
6. [Orchestrator & Scheduler integration](#6-orchestrator--scheduler-integration)
7. [Web Utils LLM (`web_utils/llm_utils.py`)](#7-web-utils-llm-web_utilsllm_utilspy)
8. [EPD comment integration (`comment.py`)](#8-epd-comment-integration-commentpy)
9. [Configuration (`shared.py`)](#9-configuration-sharedpy)
10. [HTTP Routes (`webapp.py`)](#10-http-routes-webapppy)
11. [Web interfaces](#11-web-interfaces)
12. [Startup (`Bjorn.py`)](#12-startup-bjornpy)
13. [LaRuche / LAND Protocol compatibility](#13-laruche--land-protocol-compatibility)
14. [Optional dependencies](#14-optional-dependencies)
15. [Quick activation & configuration](#15-quick-activation--configuration)
16. [Complete API endpoint reference](#16-complete-api-endpoint-reference)
17. [Queue priority system](#17-queue-priority-system)
18. [Fallbacks & graceful degradation](#18-fallbacks--graceful-degradation)
19. [Call sequences](#19-call-sequences)

---

## 1. Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           BJORN (RPi)                               │
│                                                                     │
│  ┌─────────────┐  ┌──────────────────┐  ┌─────────────────────┐   │
│  │ Core BJORN  │  │   MCP Server     │  │ Web UI              │   │
│  │ (unchanged) │  │ (mcp_server.py)  │  │ /chat.html          │   │
│  │             │  │ 7 exposed tools  │  │ /mcp-config.html    │   │
│  │ comment.py  │  │ HTTP SSE / stdio │  │  ↳ Orch Log button  │   │
│  │  ↕ LLM hook │  │                  │  │                     │   │
│  └──────┬──────┘  └────────┬─────────┘  └──────────┬──────────┘   │
│         └─────────────────────────────────────────────┘            │
│                             │                                       │
│  ┌──────────────────────────▼─────────────────────────────────┐   │
│  │                 LLM Bridge (llm_bridge.py)                  │   │
│  │                   Singleton · Thread-safe                   │   │
│  │                                                             │   │
│  │  Automatic cascade:                                         │   │
│  │  1. LaRuche node  (LAND/mDNS → HTTP POST /infer)           │   │
│  │  2. Local Ollama  (HTTP POST /api/chat)                     │   │
│  │  3. External API  (Anthropic / OpenAI / OpenRouter)         │   │
│  │  4. None          (→ fallback templates in comment.py)      │   │
│  │                                                             │   │
│  │  Agentic tool-calling loop (stop_reason=tool_use, ≤6 turns) │   │
│  │  _BJORN_TOOLS: 7 tools in Anthropic format                 │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌──────────────────────────▼─────────────────────────────────┐   │
│  │              LLM Orchestrator (llm_orchestrator.py)         │   │
│  │                                                             │   │
│  │  mode = none      → LLM has no role in scheduling           │   │
│  │  mode = advisor   → LLM suggests 1 action/cycle (prio 85)  │   │
│  │  mode = autonomous→ own thread, loop + tools (prio 82)     │   │
│  │                                                             │   │
│  │  Fingerprint (hosts↑, vulns↑, creds↑, queue_id↑)          │   │
│  │  → skip LLM if nothing new (token savings)                 │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                             │                                       │
│  ┌──────────────────────────▼─────────────────────────────────┐   │
│  │                Action Queue (SQLite)                        │   │
│  │  scheduler=40  normal=50  MCP=80  autonomous=82  advisor=85│   │
│  └─────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
          ↕ mDNS  _ai-inference._tcp.local.  (zeroconf)
┌──────────────────────────────────────────┐
│         LaRuche Swarm (LAN)              │
│  Node A → Mistral 7B   :8419             │
│  Node B → DeepSeek Coder :8419           │
│  Node C → Phi-3 Mini   :8419             │
└──────────────────────────────────────────┘
```

**Design principles:**
- Everything is **disabled by default** — zero impact if not configured
- All dependencies are **optional** — silent import if missing
- **Systematic fallback** at every level — Bjorn never crashes because of the LLM
- The bridge is a **singleton** — one instance per process, thread-safe
- EPD comments preserve their **exact original behaviour** if LLM is disabled
- The LLM is the **brain** (decides what to do), the orchestrator is the **arms** (executes)

---

## 2. Created / modified files

### Created files

| File | Approx. size | Role |
|------|-------------|------|
| `llm_bridge.py` | ~450 lines | LLM Singleton — backend cascade + agentic tool-calling loop |
| `mcp_server.py` | ~280 lines | FastMCP MCP Server — 7 Bjorn tools |
| `web_utils/llm_utils.py` | ~220 lines | LLM/MCP HTTP endpoints (web_utils pattern) |
| `llm_orchestrator.py` | ~410 lines | LLM Orchestrator — advisor & autonomous modes |
| `web/chat.html` | ~300 lines | Chat interface + Orch Log button |
| `web/mcp-config.html` | ~400 lines | LLM & MCP configuration page |

### Modified files

| File | What changed |
|------|-------------|
| `shared.py` | +45 config keys (LLM bridge, MCP, orchestrator) |
| `comment.py` | LLM hook in `get_comment()` — 12 lines added |
| `utils.py` | +1 entry in lazy WebUtils registry: `"llm_utils"` |
| `webapp.py` | +9 GET/POST routes in `_register_routes_once()` |
| `Bjorn.py` | LLM Bridge warm-up + conditional MCP server start |
| `orchestrator.py` | +`LLMOrchestrator` lifecycle + advisor call in background tasks |
| `action_scheduler.py` | +skip scheduler if LLM autonomous only (`llm_orchestrator_skip_scheduler`) |
| `requirements.txt` | +3 comment lines (optional dependencies documented) |

---

## 3. LLM Bridge (`llm_bridge.py`)

### Internal architecture

```
LLMBridge (Singleton)
├── __init__()              Initialises singleton, launches LaRuche discovery
├── complete()              Main API — cascades all backends
│     └── tools=None/[...]  Optional param to enable tool-calling
├── generate_comment()      Generates a short EPD comment (≤80 tokens)
├── chat()                  Stateful chat with per-session history
│     └── tools=_BJORN_TOOLS if llm_chat_tools_enabled=True
├── clear_history()         Clears a session's history
├── status()                Returns bridge state (for the UI)
│
├── _start_laruche_discovery()   Starts mDNS thread in background
├── _discover_laruche_mdns()     Listens to _ai-inference._tcp.local. continuously
│
├── _call_laruche()         Backend 1 — POST http://[node]:8419/infer
├── _call_ollama()          Backend 2 — POST http://localhost:11434/api/chat
├── _call_anthropic()       Backend 3a — POST api.anthropic.com + AGENTIC LOOP
│     └── loop ≤6 turns: send → tool_use → execute → feed result → repeat
├── _call_openai_compat()   Backend 3b — POST [base_url]/v1/chat/completions
│
├── _execute_tool(name, inputs)  Dispatches to mcp_server._impl_*
│     └── gate: checks mcp_allowed_tools before executing
│
└── _build_system_prompt()  Builds system prompt with live Bjorn context

_BJORN_TOOLS : List[Dict]   Anthropic-format definitions for the 7 MCP tools
```

### _BJORN_TOOLS — full list

```python
_BJORN_TOOLS = [
    {"name": "get_hosts",           "description": "...", "input_schema": {...}},
    {"name": "get_vulnerabilities", ...},
    {"name": "get_credentials",     ...},
    {"name": "get_action_history",  ...},
    {"name": "get_status",          ...},
    {"name": "run_action",          ...},  # gated by mcp_allowed_tools
    {"name": "query_db",            ...},  # SELECT only
]
```

### Backend cascade

```
llm_backend = "auto"    →  LaRuche → Ollama → API → None
llm_backend = "laruche" →  LaRuche only
llm_backend = "ollama"  →  Ollama only
llm_backend = "api"     →  External API only
```

At each step, if a backend fails (timeout, network error, missing model), the next one is tried **silently**. If all fail, `complete()` returns `None`.

### Agentic tool-calling loop (`_call_anthropic`)

When `tools` is passed to `complete()`, the Anthropic backend enters agentic mode:

```
_call_anthropic(messages, system, tools, max_tokens, timeout)
  │
  ├─ POST /v1/messages {tools: [...]}
  │
  ├─ [stop_reason = "tool_use"]
  │     for each tool_use block:
  │       result = _execute_tool(name, inputs)
  │       append {role: "tool", tool_use_id: ..., content: result}
  │     POST /v1/messages [messages + tool results]  ← next turn
  │
  └─ [stop_reason = "end_turn"]  → returns final text
     [≥6 turns]                  → returns partial text + warning
```

`_execute_tool()` dispatches directly to `mcp_server._impl_*` (no network), checking `mcp_allowed_tools` for `run_action`.

### Tool-calling in chat (`chat()`)

If `llm_chat_tools_enabled = True`, the chat passes `tools=_BJORN_TOOLS` to the backend, letting the LLM answer with real-time data (hosts, vulns, creds…) rather than relying only on its training knowledge.

### Chat history

- Each session has its own history (key = `session_id`)
- Special session `"llm_orchestrator"`: contains the autonomous orchestrator's reasoning
- Max size configurable: `llm_chat_history_size` (default: 20 messages)
- History is **in-memory only** — not persisted across restarts
- Thread-safe via `_hist_lock`

---

## 4. MCP Server (`mcp_server.py`)

### What is MCP?

The **Model Context Protocol** (Anthropic) is an open-source protocol that lets AI agents (Claude Desktop, custom agents, etc.) use external tools via a standardised interface.

By enabling Bjorn's MCP server, **any MCP client can query and control Bjorn** — without knowing the internal DB structure.

### Exposed tools

| Tool | Arguments | Description |
|------|-----------|-------------|
| `get_hosts` | `alive_only: bool = True` | Returns discovered hosts (IP, MAC, hostname, OS, ports) |
| `get_vulnerabilities` | `host_ip: str = ""`, `limit: int = 100` | Returns discovered CVE vulnerabilities |
| `get_credentials` | `service: str = ""`, `limit: int = 100` | Returns captured credentials (SSH, FTP, SMB…) |
| `get_action_history` | `limit: int = 50`, `action_name: str = ""` | History of executed actions |
| `get_status` | *(none)* | Real-time state: mode, active action, counters |
| `run_action` | `action_name: str`, `target_ip: str`, `target_mac: str = ""` | Queues a Bjorn action (MCP priority = 80) |
| `query_db` | `sql: str`, `params: str = "[]"` | Free SELECT against the SQLite DB (read-only) |

**Security:** each tool checks `mcp_allowed_tools` — unlisted tools return a clean error. `query_db` rejects anything that is not a `SELECT`.

### `_impl_run_action` — priority detail

```python
_MCP_PRIORITY = 80  # > scheduler(40) > normal(50)

sd.db.queue_action(
    action_name=action_name,
    mac=mac,          # resolved from hosts WHERE ip=? if not supplied
    ip=target_ip,
    priority=_MCP_PRIORITY,
    trigger="mcp",
    metadata={"decision_method": "mcp", "decision_origin": "mcp"},
)
sd.queue_event.set()  # wakes the orchestrator immediately
```

### Available transports

| Transport | Config | Usage |
|-----------|--------|-------|
| `http` (default) | `mcp_transport: "http"`, `mcp_port: 8765` | Accessible from any MCP client on LAN via SSE |
| `stdio` | `mcp_transport: "stdio"` | Claude Desktop, CLI agents |

---

## 5. LLM Orchestrator (`llm_orchestrator.py`)

The LLM Orchestrator transforms Bjorn from a scriptable tool into an autonomous agent. It is **completely optional and disableable** via `llm_orchestrator_mode = "none"`.

### Operating modes

| Mode | Config value | Operation |
|------|-------------|-----------|
| Disabled | `"none"` (default) | LLM plays no role in planning |
| Advisor | `"advisor"` | LLM consulted periodically, suggests 1 action |
| Autonomous | `"autonomous"` | Own thread, LLM observes + plans with tools |

### Internal architecture

```
LLMOrchestrator
├── start()                    Starts autonomous thread if mode=autonomous
├── stop()                     Stops thread (join 15s max)
├── restart_if_mode_changed()  Called from orchestrator.run() each iteration
├── is_active()                True if autonomous thread is alive
│
├── [ADVISOR MODE]
│   advise()                   → called from orchestrator._process_background_tasks()
│     ├── _build_snapshot()    → compact dict (hosts, vulns, creds, queue)
│     ├── LLMBridge().complete(prompt, system)
│     └── _apply_advisor_response(raw, allowed)
│           ├── parse JSON {"action": str, "target_ip": str, "reason": str}
│           ├── validate action ∈ allowed
│           └── db.queue_action(priority=85, trigger="llm_advisor")
│
└── [AUTONOMOUS MODE]
    _autonomous_loop()         Thread "LLMOrchestrator" (daemon)
      └── loop:
            _compute_fingerprint()   → (hosts, vulns, creds, max_queue_id)
            _has_actionable_change() → skip if nothing increased
            _run_autonomous_cycle()
              ├── filter tools: read-only always + run_action if in allowed
              ├── LLMBridge().complete(prompt, system, tools=[...])
              │     └── _call_anthropic() agentic loop
              │           → LLM calls run_action via tools
              │                → _execute_tool → _impl_run_action → queue
              └── if llm_orchestrator_log_reasoning=True:
                    logger.info("[LLM_ORCH_REASONING]...")
                    _push_to_chat()  → "llm_orchestrator" session in LLMBridge
            sleep(llm_orchestrator_interval_s)
```

### Fingerprint and smart skip

```python
def _compute_fingerprint(self) -> tuple:
    # (host_count, vuln_count, cred_count, max_completed_queue_id)
    return (hosts, vulns, creds, last_id)

def _has_actionable_change(self, fp: tuple) -> bool:
    if self._last_fingerprint is None:
        return True  # first cycle always runs
    # Triggers ONLY if something INCREASED
    # hosts going offline → not actionable
    return any(fp[i] > self._last_fingerprint[i] for i in range(len(fp)))
```

**Token savings:** if `llm_orchestrator_skip_if_no_change = True` (default), the LLM cycle is skipped if no new hosts/vulns/creds and no action completed since the last cycle.

### LLM priorities vs queue

```python
_ADVISOR_PRIORITY    = 85  # advisor > MCP(80) > normal(50) > scheduler(40)
_AUTONOMOUS_PRIORITY = 82  # autonomous slightly below advisor
```

### Autonomous system prompt — example

```
"You are Bjorn's autonomous orchestrator, running on a Raspberry Pi network security tool.
Current state: 12 hosts discovered, 3 vulnerabilities, 1 credentials.
Operation mode: ATTACK. Hard limit: at most 3 run_action calls per cycle.
Only these action names may be queued: NmapScan, SSHBruteforce, SMBScan.
Strategy: prioritise unexplored services, hosts with high port counts, and hosts with no recent scans.
Do not queue duplicate actions already pending or recently successful.
Use Norse references occasionally. Be terse and tactical."
```

### Advisor response format

```json
// Action recommended:
{"action": "NmapScan", "target_ip": "192.168.1.42", "reason": "unexplored host, 0 open ports known"}

// Nothing to do:
{"action": null}
```

### Reasoning log

When `llm_orchestrator_log_reasoning = True`:
- Full reasoning is logged via `logger.info("[LLM_ORCH_REASONING]...")`
- It is also injected into the `"llm_orchestrator"` session in `LLMBridge._chat_histories`
- Viewable in real time in `chat.html` via the **Orch Log** button

---

## 6. Orchestrator & Scheduler integration

### `orchestrator.py`

```python
# __init__
self.llm_orchestrator = None
self._init_llm_orchestrator()

# _init_llm_orchestrator()
if shared_data.config.get("llm_enabled") and shared_data.config.get("llm_orchestrator_mode") != "none":
    from llm_orchestrator import LLMOrchestrator
    self.llm_orchestrator = LLMOrchestrator(shared_data)
    self.llm_orchestrator.start()

# run() — each iteration
self._sync_llm_orchestrator()   # starts/stops thread according to runtime config

# _process_background_tasks()
if self.llm_orchestrator and mode == "advisor":
    self.llm_orchestrator.advise()
```

### `action_scheduler.py` — skip option

```python
# In run(), each iteration:
_llm_skip = bool(
    shared_data.config.get("llm_orchestrator_skip_scheduler", False)
    and shared_data.config.get("llm_orchestrator_mode") == "autonomous"
    and shared_data.config.get("llm_enabled", False)
)

if not _llm_skip:
    self._publish_all_upcoming()    # step 2: publish due actions
    self._evaluate_global_actions() # step 3: global evaluation
    self.evaluate_all_triggers()    # step 4: per-host triggers
# Steps 1 (promote due) and 5 (cleanup/priorities) always run
```

When `llm_orchestrator_skip_scheduler = True` + `mode = autonomous` + `llm_enabled = True`:
- The scheduler no longer publishes automatic actions (no more `B_require`, `B_trigger`, etc.)
- The autonomous LLM becomes **sole master of the queue**
- Queue hygiene (promotions, cleanup) remains active

---

## 7. Web Utils LLM (`web_utils/llm_utils.py`)

Follows the exact **same pattern** as all other `web_utils` (constructor `__init__(self, shared_data)`, methods called by `webapp.py`).

### Methods

| Method | Type | Description |
|--------|------|-------------|
| `get_llm_status(handler)` | GET | LLM bridge state (active backend, LaRuche URL…) |
| `get_llm_config(handler)` | GET | Current LLM config (api_key masked) |
| `get_llm_reasoning(handler)` | GET | `llm_orchestrator` session history (reasoning log) |
| `handle_chat(data)` | POST | Sends a message, returns LLM response |
| `clear_chat_history(data)` | POST | Clears a session's history |
| `get_mcp_status(handler)` | GET | MCP server state (running, port, transport) |
| `toggle_mcp(data)` | POST | Enables/disables MCP server + saves config |
| `save_mcp_config(data)` | POST | Saves MCP config (tools, port, transport) |
| `save_llm_config(data)` | POST | Saves LLM config (all parameters) |

---

## 8. EPD comment integration (`comment.py`)

### Behaviour before modification

```
get_comment(status, lang, params)
  └── if delay elapsed OR status changed
        └── _pick_text(status, lang, params)  ← SQLite DB
              └── returns weighted text
```

### Behaviour after modification

```
get_comment(status, lang, params)
  └── if delay elapsed OR status changed
        │
        ├── [if llm_comments_enabled = True]
        │     └── LLMBridge().generate_comment(status, params)
        │           ├── success → LLM text (≤12 words, ~8s max)
        │           └── failure/timeout → text = None
        │
        └── [if text = None]  ← SYSTEMATIC FALLBACK
              └── _pick_text(status, lang, params)  ← original behaviour
                    └── returns weighted DB text
```

**Original behaviour preserved 100% if LLM disabled or failing.**

---

## 9. Configuration (`shared.py`)

### LLM Bridge section (`__title_llm__`)

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `llm_enabled` | `False` | bool | **Master toggle** — activates the entire bridge |
| `llm_comments_enabled` | `False` | bool | Use LLM for EPD comments |
| `llm_chat_enabled` | `True` | bool | Enable /chat.html interface |
| `llm_chat_tools_enabled` | `False` | bool | Enable tool-calling in web chat |
| `llm_backend` | `"auto"` | str | `auto` \| `laruche` \| `ollama` \| `api` |
| `llm_laruche_discovery` | `True` | bool | Auto-discover LaRuche nodes via mDNS |
| `llm_laruche_url` | `""` | str | Manual LaRuche URL (overrides discovery) |
| `llm_ollama_url` | `"http://127.0.0.1:11434"` | str | Local Ollama URL |
| `llm_ollama_model` | `"phi3:mini"` | str | Ollama model to use |
| `llm_api_provider` | `"anthropic"` | str | `anthropic` \| `openai` \| `openrouter` |
| `llm_api_key` | `""` | str | API key (masked in UI) |
| `llm_api_model` | `"claude-haiku-4-5-20251001"` | str | External API model |
| `llm_api_base_url` | `""` | str | Custom base URL (OpenRouter, proxy…) |
| `llm_timeout_s` | `30` | int | Global LLM call timeout (seconds) |
| `llm_max_tokens` | `500` | int | Max tokens for chat |
| `llm_comment_max_tokens` | `80` | int | Max tokens for EPD comments |
| `llm_chat_history_size` | `20` | int | Max messages per chat session |

### MCP Server section (`__title_mcp__`)

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `mcp_enabled` | `False` | bool | Enable MCP server |
| `mcp_transport` | `"http"` | str | `http` (SSE) \| `stdio` |
| `mcp_port` | `8765` | int | HTTP SSE port |
| `mcp_allowed_tools` | `[all]` | list | List of authorised MCP tools |

### LLM Orchestrator section (`__title_llm_orch__`)

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `llm_orchestrator_mode` | `"none"` | str | `none` \| `advisor` \| `autonomous` |
| `llm_orchestrator_interval_s` | `60` | int | Delay between autonomous cycles (min 30s) |
| `llm_orchestrator_max_actions` | `3` | int | Max actions per autonomous cycle |
| `llm_orchestrator_allowed_actions` | `[]` | list | Actions the LLM may queue (empty = mcp_allowed_tools) |
| `llm_orchestrator_skip_scheduler` | `False` | bool | Disable scheduler when autonomous is active |
| `llm_orchestrator_skip_if_no_change` | `True` | bool | Skip cycle if fingerprint unchanged |
| `llm_orchestrator_log_reasoning` | `False` | bool | Log full LLM reasoning |

---

## 10. HTTP Routes (`webapp.py`)

### GET routes

| Route | Handler | Description |
|-------|---------|-------------|
| `GET /api/llm/status` | `llm_utils.get_llm_status` | LLM bridge state |
| `GET /api/llm/config` | `llm_utils.get_llm_config` | LLM config (api_key masked) |
| `GET /api/llm/reasoning` | `llm_utils.get_llm_reasoning` | Orchestrator reasoning log |
| `GET /api/mcp/status` | `llm_utils.get_mcp_status` | MCP server state |

### POST routes (JSON data-only)

| Route | Handler | Description |
|-------|---------|-------------|
| `POST /api/llm/chat` | `llm_utils.handle_chat` | Send a message to the LLM |
| `POST /api/llm/clear_history` | `llm_utils.clear_chat_history` | Clear a session's history |
| `POST /api/llm/config` | `llm_utils.save_llm_config` | Save LLM config |
| `POST /api/mcp/toggle` | `llm_utils.toggle_mcp` | Enable/disable MCP |
| `POST /api/mcp/config` | `llm_utils.save_mcp_config` | Save MCP config |

All routes respect Bjorn's existing authentication (`webauth`).

---

## 11. Web interfaces

### `/chat.html`

Terminal-style chat interface (black/red, consistent with Bjorn).

**Features:**
- Auto-detects LLM state on load (`GET /api/llm/status`)
- Displays active backend (LaRuche URL, or mode)
- "Bjorn is thinking..." indicator during response
- Unique session ID per browser tab
- `Enter` = send, `Shift+Enter` = new line
- Textarea auto-resize
- **"Clear history"** button — clears server-side session
- **"Orch Log"** button — loads the autonomous orchestrator's reasoning
  - Calls `GET /api/llm/reasoning`
  - Renders each message (cycle prompt + LLM response) as chat bubbles
  - "← Back to chat" to return to normal chat
  - Helper message if log is empty (hint: enable `llm_orchestrator_log_reasoning`)

**Access:** `http://[bjorn-ip]:8000/chat.html`

### `/mcp-config.html`

Full LLM & MCP configuration page.

**LLM Bridge section:**
- Master enable/disable toggle
- EPD comments, chat, chat tool-calling toggles
- Backend selector (auto / laruche / ollama / api)
- LaRuche mDNS discovery toggle + manual URL
- Ollama configuration (URL + model)
- External API configuration (provider, key, model, custom URL)
- Timeout and token parameters
- "TEST CONNECTION" button

**MCP Server section:**
- Enable toggle with live start/stop
- Transport selector (HTTP SSE / stdio)
- HTTP port
- Per-tool checkboxes
- "RUNNING" / "OFF" indicator

**Access:** `http://[bjorn-ip]:8000/mcp-config.html`

---

## 12. Startup (`Bjorn.py`)

```python
# LLM Bridge — warm up singleton
try:
    from llm_bridge import LLMBridge
    LLMBridge()  # Starts mDNS discovery if llm_laruche_discovery=True
    logger.info("LLM Bridge initialised")
except Exception as e:
    logger.warning("LLM Bridge init skipped: %s", e)

# MCP Server
try:
    import mcp_server
    if shared_data.config.get("mcp_enabled", False):
        mcp_server.start()      # Daemon thread "MCPServer"
        logger.info("MCP server started")
    else:
        logger.info("MCP server loaded (disabled)")
except Exception as e:
    logger.warning("MCP server init skipped: %s", e)
```

The LLM Orchestrator is initialised inside `orchestrator.py` (not `Bjorn.py`), since it depends on the orchestrator loop cycle.

---

## 13. LaRuche / LAND Protocol compatibility

### LAND Protocol

LAND (Local AI Network Discovery) is the LaRuche protocol:
- **Discovery:** mDNS service type `_ai-inference._tcp.local.`
- **Inference:** `POST http://[node]:8419/infer`

### What Bjorn implements on the Python side

```python
# mDNS listening (zeroconf)
from zeroconf import Zeroconf, ServiceBrowser
ServiceBrowser(zc, "_ai-inference._tcp.local.", listener)
# → Auto-detects LaRuche nodes

# Inference call (urllib stdlib, zero dependency)
payload = {"prompt": "...", "capability": "llm", "max_tokens": 500}
urllib.request.urlopen(f"{url}/infer", data=json.dumps(payload))
```

### Scenarios

| Scenario | Behaviour |
|----------|-----------|
| LaRuche node detected on LAN | Used automatically as priority backend |
| Multiple LaRuche nodes | First discovered is used |
| Manual URL configured | Used directly, discovery ignored |
| LaRuche node absent | Cascades to Ollama or external API |
| `zeroconf` not installed | Discovery silently disabled, DEBUG log |

---

## 14. Optional dependencies

| Package | Min version | Feature unlocked | Install command |
|---------|------------|------------------|----------------|
| `mcp[cli]` | ≥ 1.0.0 | Full MCP server | `pip install "mcp[cli]"` |
| `zeroconf` | ≥ 0.131.0 | LaRuche mDNS discovery | `pip install zeroconf` |

**No new dependencies** added for LLM backends:
- **LaRuche / Ollama**: uses `urllib.request` (Python stdlib)
- **Anthropic / OpenAI**: REST API via `urllib` — no SDK needed

---

## 15. Quick activation & configuration

### Basic LLM chat

```bash
curl -X POST http://[bjorn-ip]:8000/api/llm/config \
  -H "Content-Type: application/json" \
  -d '{"llm_enabled": true, "llm_backend": "ollama", "llm_ollama_model": "phi3:mini"}'
# → http://[bjorn-ip]:8000/chat.html
```

### Chat with tool-calling (LLM accesses live network data)

```bash
curl -X POST http://[bjorn-ip]:8000/api/llm/config \
  -d '{"llm_enabled": true, "llm_chat_tools_enabled": true}'
```

### LLM Orchestrator — advisor mode

```bash
curl -X POST http://[bjorn-ip]:8000/api/llm/config \
  -d '{
    "llm_enabled": true,
    "llm_orchestrator_mode": "advisor",
    "llm_orchestrator_allowed_actions": ["NmapScan", "SSHBruteforce"]
  }'
```

### LLM Orchestrator — autonomous mode (LLM as sole planner)

```bash
curl -X POST http://[bjorn-ip]:8000/api/llm/config \
  -d '{
    "llm_enabled": true,
    "llm_orchestrator_mode": "autonomous",
    "llm_orchestrator_skip_scheduler": true,
    "llm_orchestrator_max_actions": 5,
    "llm_orchestrator_interval_s": 120,
    "llm_orchestrator_allowed_actions": ["NmapScan", "SSHBruteforce", "SMBScan"],
    "llm_orchestrator_log_reasoning": true
  }'
# → View reasoning: http://[bjorn-ip]:8000/chat.html  → Orch Log button
```

### With Anthropic API

```bash
curl -X POST http://[bjorn-ip]:8000/api/llm/config \
  -d '{
    "llm_enabled": true,
    "llm_backend": "api",
    "llm_api_provider": "anthropic",
    "llm_api_key": "sk-ant-...",
    "llm_api_model": "claude-haiku-4-5-20251001"
  }'
```

### With OpenRouter (access to all models)

```bash
curl -X POST http://[bjorn-ip]:8000/api/llm/config \
  -d '{
    "llm_enabled": true,
    "llm_backend": "api",
    "llm_api_provider": "openrouter",
    "llm_api_key": "sk-or-...",
    "llm_api_model": "meta-llama/llama-3.2-3b-instruct",
    "llm_api_base_url": "https://openrouter.ai/api"
  }'
```

### Model recommendations by scenario

| Scenario | Backend | Recommended model | Pi RAM |
|----------|---------|-------------------|--------|
| Autonomous orchestrator + LaRuche on LAN | laruche | Mistral/Phi on the node | 0 (remote inference) |
| Autonomous orchestrator offline | ollama | `qwen2.5:3b` | ~3 GB |
| Autonomous orchestrator cloud | api | `claude-haiku-4-5-20251001` | 0 |
| Chat + tools | ollama | `phi3:mini` | ~2 GB |
| EPD comments only | ollama | `smollm2:360m` | ~400 MB |

---

## 16. Complete API endpoint reference

### GET

```
GET /api/llm/status
→ {"enabled": bool, "backend": str, "laruche_url": str|null,
   "laruche_discovery": bool, "ollama_url": str, "ollama_model": str,
   "api_provider": str, "api_model": str, "api_key_set": bool}

GET /api/llm/config
→ {all llm_* keys except api_key, + "llm_api_key_set": bool}

GET /api/llm/reasoning
→ {"status": "ok", "messages": [{"role": str, "content": str}, ...], "count": int}
→ {"status": "error", "message": str, "messages": [], "count": 0}

GET /api/mcp/status
→ {"enabled": bool, "running": bool, "transport": str,
   "port": int, "allowed_tools": [str]}
```

### POST

```
POST /api/llm/chat
Body: {"message": str, "session_id": str?}
→ {"status": "ok", "response": str, "session_id": str}
→ {"status": "error", "message": str}

POST /api/llm/clear_history
Body: {"session_id": str?}
→ {"status": "ok"}

POST /api/llm/config
Body: {any subset of llm_* and llm_orchestrator_* keys}
→ {"status": "ok"}
→ {"status": "error", "message": str}

POST /api/mcp/toggle
Body: {"enabled": bool}
→ {"status": "ok", "enabled": bool, "started": bool?}

POST /api/mcp/config
Body: {"allowed_tools": [str]?, "port": int?, "transport": str?}
→ {"status": "ok", "config": {...}}
```

---

## 17. Queue priority system

```
Priority  Source              Trigger
──────────────────────────────────────────────────────────────
   85     LLM Advisor         llm_orchestrator.advise()
   82     LLM Autonomous      _run_autonomous_cycle() via run_action tool
   80     External MCP        _impl_run_action() via MCP client or chat
   50     Normal / manual     queue_action() without explicit priority
   40     Scheduler           action_scheduler evaluates triggers
```

The scheduler always processes the highest-priority pending item first. LLM and MCP actions therefore preempt scheduler actions.

---

## 18. Fallbacks & graceful degradation

| Condition | Behaviour |
|-----------|-----------|
| `llm_enabled = False` | `complete()` returns `None` immediately — zero overhead |
| `llm_orchestrator_mode = "none"` | LLMOrchestrator not instantiated |
| `mcp` not installed | `_build_mcp_server()` returns `None`, WARNING log |
| `zeroconf` not installed | LaRuche discovery silently disabled, DEBUG log |
| LaRuche node timeout | Exception caught, cascade to next backend |
| Ollama not running | `URLError` caught, cascade to API |
| API key missing | `_call_api()` returns `None`, cascade |
| All backends fail | `complete()` returns `None` |
| LLM returns `None` for EPD | `comment.py` uses `_pick_text()` (original behaviour) |
| LLM advisor: invalid JSON | DEBUG log, returns `None`, next cycle |
| LLM advisor: disallowed action | WARNING log, ignored |
| LLM autonomous: no change | cycle skipped, zero API call |
| LLM autonomous: ≥6 tool turns | returns partial text + warning |
| Exception in LLM Bridge | `try/except` at every level, DEBUG log |

### Timeouts

```
Chat / complete()     → llm_timeout_s (default: 30s)
EPD comments          → 8s (hardcoded, short to avoid blocking render)
Autonomous cycle      → 90s (long: may chain multiple tool calls)
Advisor               → 20s (short prompt + JSON response)
```

---

## 19. Call sequences

### Web chat with tool-calling

```
Browser → POST /api/llm/chat {"message": "which hosts are vulnerable?"}
  └── LLMUtils.handle_chat(data)
        └── LLMBridge().chat(message, session_id)
              └── complete(messages, system, tools=_BJORN_TOOLS)
                    └── _call_anthropic(messages, tools=[...])
                          ├── POST /v1/messages → stop_reason=tool_use
                          │     └── tool: get_hosts(alive_only=true)
                          │           → _execute_tool → _impl_get_hosts()
                          │                 → JSON of hosts
                          ├── POST /v1/messages [+ tool result] → end_turn
                          └── returns "3 exposed SSH hosts: 192.168.1.10, ..."
← {"status": "ok", "response": "3 exposed SSH hosts..."}
```

### LLM autonomous cycle

```
Thread "LLMOrchestrator" (daemon, interval=60s)
  └── _run_autonomous_cycle()
        ├── fp = _compute_fingerprint()  → (12, 3, 1, 47)
        ├── _has_actionable_change(fp)   → True (vuln_count 2→3)
        ├── self._last_fingerprint = fp
        │
        └── LLMBridge().complete(prompt, system, tools=[read-only + run_action])
              └── _call_anthropic(tools=[...])
                    ├── POST → tool_use: get_hosts()
                    │     → [{ip: "192.168.1.20", ports: "22,80,443"}]
                    ├── POST → tool_use: get_action_history()
                    │     → [...]
                    ├── POST → tool_use: run_action("SSHBruteforce", "192.168.1.20")
                    │     → _execute_tool → _impl_run_action()
                    │           → db.queue_action(priority=82, trigger="llm_autonomous")
                    │           → queue_event.set()
                    └── POST → end_turn
                          → "Queued SSHBruteforce on 192.168.1.20 (Mjolnir strikes the unguarded gate)"
              → [if log_reasoning=True] logger.info("[LLM_ORCH_REASONING]...")
              → [if log_reasoning=True] _push_to_chat(bridge, prompt, response)
```

### Reading reasoning from chat.html

```
User clicks "Orch Log"
  └── fetch GET /api/llm/reasoning
        └── LLMUtils.get_llm_reasoning(handler)
              └── LLMBridge()._chat_histories["llm_orchestrator"]
                    → [{"role": "user",      "content": "[Autonomous cycle]..."},
                       {"role": "assistant", "content": "Queued SSHBruteforce..."}]
← {"status": "ok", "messages": [...], "count": 2}
→ Rendered as chat bubbles in #messages
```

### MCP from external client (Claude Desktop)

```
Claude Desktop → tool_call: run_action("NmapScan", "192.168.1.0/24")
  └── FastMCP dispatch
        └── mcp_server.run_action(action_name, target_ip)
              └── _impl_run_action()
                    ├── db.queue_action(priority=80, trigger="mcp")
                    └── queue_event.set()
← {"status": "queued", "action": "NmapScan", "target": "192.168.1.0/24", "priority": 80}
```

### EPD comment with LLM

```
display.py → CommentAI.get_comment("SSHBruteforce", params={...})
  └── delay elapsed OR status changed → proceed
        ├── llm_comments_enabled = True ?
        │     └── LLMBridge().generate_comment("SSHBruteforce", params)
        │           └── complete([{role:user, content:"Status: SSHBruteforce..."}],
        │                        max_tokens=80, timeout=8)
        │                 ├── LaRuche → "Norse gods smell SSH credentials..."  ✓
        │                 └── [or timeout 8s] → None
        └── text = None → _pick_text("SSHBruteforce", lang, params)
              └── SELECT FROM comments WHERE status='SSHBruteforce'
                    → "Processing authentication attempts..."
```
