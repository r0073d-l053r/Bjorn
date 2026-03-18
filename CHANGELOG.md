# BJORN — Changelog

> **From Viking Raider to Cyber Warlord.**
> This release represents a complete transformation of Bjorn — from a \~8,200-line Python prototype into a **\~58,000-line Python + \~42,000-line frontend** autonomous cybersecurity platform with AI orchestration, WiFi recon, HID attacks, network watchdog, C2 infrastructure, and a full Single-Page Application dashboard.

---

## [2.1.0] — 2026-03-19

### Codebase Cleanup
- All Python file headers standardized to `"""filename.py - Description."""` format (~120 files)
- All French comments, docstrings, log/print strings, and error messages translated to English
- Removed redundant/obvious comments, verbose 10-20 line header essays trimmed to 1-3 lines
- Fixed encoding artifacts (garbled UTF-8 box-drawing chars in CSS)
- Fixed `# webutils/` path typos in 3 web_utils files
- Replaced LLM-style em dashes with plain hyphens across all .py files

### Custom Scripts System
- **Custom scripts directory** (`actions/custom/`) for user-uploaded scripts, ignored by orchestrator
- **Two script formats supported**: Bjorn-format (class + `execute()` + `shared_data`) and free Python scripts (plain `argparse`)
- **Auto-detection** via AST parsing: scripts with `b_class` var use action_runner, others run as raw subprocess
- **`b_args` support** for both formats: drives web UI controls (text, number, select, checkbox, slider)
- **Upload/delete** via web UI with metadata extraction (no code exec during upload)
- **Auto-registration**: scripts dropped in `actions/custom/` via SSH are detected on next API call
- Two example templates: `example_bjorn_action.py` and `example_free_script.py`
- Custom scripts appear in console-sse manual mode dropdown under `<optgroup>`

### Action Runner
- **`action_runner.py`** - Generic subprocess wrapper that bootstraps `shared_data` for manual action execution
- Supports `--ip`, `--port`, `--mac` + arbitrary `--key value` args injected as `shared_data` attributes
- SIGTERM handler for graceful stop from the web UI
- MAC auto-resolution from DB if not provided
- Handles both `execute()` and `scan()` (global actions like NetworkScanner)

### Script Scheduler & Conditional Triggers
- **`script_scheduler.py`** - Lightweight 30s-tick background daemon for automated script execution
- **Recurring schedules**: run every N seconds (min 30s), persistent across reboots
- **One-shot schedules**: fire at a specific datetime, auto-disable after
- **Conditional triggers**: fire scripts when DB conditions are met (AND/OR block logic)
- **8 condition types**: `action_result`, `hosts_with_port`, `hosts_alive`, `cred_found`, `has_vuln`, `db_count`, `time_after`, `time_before`
- **Orchestrator hook**: triggers evaluated immediately when actions complete (not just on 30s tick)
- **Concurrency limited** to 4 simultaneous scheduled scripts (Pi Zero friendly)
- **Condition builder** (`web/js/core/condition-builder.js`) - Visual nested AND/OR block editor
- Scheduler page extended with 3 tabs: Queue (existing kanban), Schedules, Triggers
- Full CRUD UI for schedules and triggers with inline edit, toggle, delete, auto-refresh
- "Test" button for dry-run condition evaluation

### Package Manager
- **pip package management** for custom script dependencies
- **SSE streaming** install progress (`pip install --break-system-packages`)
- Packages tracked in DB (`custom_packages` table) - only recorded after successful install
- Uninstall with DB cleanup
- Package name validation (regex whitelist, no shell injection)
- New "Packages" tab in Actions page sidebar

### New Database Modules
- `db_utils/schedules.py` - Schedule and trigger persistence (CRUD, due queries, cooldown checks)
- `db_utils/packages.py` - Custom package tracking

### New Web Endpoints
- `/api/schedules/*` (list, create, update, delete, toggle) - 5 endpoints
- `/api/triggers/*` (list, create, update, delete, toggle, test) - 6 endpoints
- `/api/packages/*` (list, install SSE, uninstall) - 3 endpoints
- `/upload_custom_script`, `/delete_custom_script` - Custom script management

### Resource & Memory Fixes
- Script output buffer capped at 2000 lines (was unbounded)
- Finished scripts dict auto-pruned (max 20 historical entries)
- AST parse results cached by file mtime (no re-parsing on every API call)
- Module imports replaced with AST extraction in `list_scripts()` (no more `sys.modules` pollution)
- Custom scripts filesystem scan throttled to once per 30s
- Scheduler daemon: event queue capped at 100, subprocess cleanup with `wait()` + `stdout.close()`
- Package install: graceful terminate -> wait -> kill cascade with FD cleanup

### Multilingual Comments Import
- `comment.py` `_ensure_comments_loaded()` now imports all `comments.*.json` files on every startup
- Drop `comments.fr.json`, `comments.de.json`, etc. next to `comments.en.json` for automatic multi-language support
- Existing comments untouched via `INSERT OR IGNORE` (unique index dedup)

---

## [2.0.0] — 2025/2026 Major Release

### TL;DR — What's New

| Area | v1 (alpha 2) | v2 (this release) |
|------|-------------|-------------------|
| Python codebase | ~8,200 lines | **~58,000 lines** (7x) |
| Web frontend | ~2,100 lines (6 static HTML pages) | **~42,000 lines** (25-page SPA) |
| Action modules | 17 | **32** |
| Database | Monolithic SQLite helper | **Modular facade** (18 specialized modules) |
| AI/ML | Basic heuristic scoring | **Full RL engine** + LLM orchestrator + MCP server |
| Web UI | Static multi-page HTML | **Hash-routed SPA** with lazy-loading, theming, i18n |
| Languages | English only | **7 languages** (EN, FR, ES, DE, IT, RU, ZH) |
| WiFi recon | None | **Bifrost engine** (Pwnagotchi-compatible) |
| HID attacks | None | **Loki module** (USB Rubber Ducky-style) |
| Network watchdog | None | **Sentinel engine** (9 detection modules) |
| C2 server | None | **ZombieLand** (encrypted C2 with agent management) |
| LLM integration | None | **LLM Bridge** + MCP Server + Autonomous Orchestrator |
| Display | Basic 2.13" e-paper | **Multi-size EPD** + web-based layout editor |

---

### New Major Features

#### AI & LLM Integration — Bjorn Gets a Brain

- **LLM Bridge** (`llm_bridge.py`) — Singleton, thread-safe LLM backend with automatic cascade:
  1. LaRuche swarm node (LAND protocol / mDNS auto-discovery)
  2. Local Ollama instance
  3. External API (Anthropic / OpenAI / OpenRouter)
  4. Graceful fallback to templates
- **Agentic tool-calling loop** — Up to 6-turn tool-use cycles with Anthropic API, enabling the LLM to query live network data and queue actions autonomously
- **MCP Server** (`mcp_server.py`) — Model Context Protocol server exposing 7 Bjorn tools (`get_hosts`, `get_vulnerabilities`, `get_credentials`, `get_action_history`, `get_status`, `run_action`, `query_db`), compatible with Claude Desktop and any MCP client
- **LLM Orchestrator** (`llm_orchestrator.py`) — Three operating modes:
  - `none` — LLM disabled (default, zero overhead)
  - `advisor` — LLM suggests one action per cycle (priority 85)
  - `autonomous` — Own daemon thread, full tool-calling loop, LLM becomes sole master of the action queue
- **Smart fingerprint skip** — Autonomous mode only calls the LLM when network state actually changes (new hosts, vulns, or credentials), saving API tokens
- **LAND Protocol** (`land_protocol.py`) — Native Python client for Local AI Network Discovery, auto-detects LaRuche inference nodes on LAN via mDNS
- **LLM-powered EPD comments** — E-paper display comments optionally generated by LLM with Norse personality, seamless fallback to database templates
- **Web chat interface** — Terminal-style chat with the LLM, tool-calling support, orchestrator reasoning log viewer
- **LLM configuration page** — Full web UI for all LLM/MCP settings, connection testing, per-tool access control
- **45+ new configuration parameters** for LLM bridge, MCP server, and orchestrator

#### Bifrost — WiFi Reconnaissance Engine

- **Pwnagotchi-compatible** WiFi recon daemon running alongside all Bjorn modes
- **BettercapClient** — Full HTTP API client for bettercap (session control, WiFi module management, handshake capture)
- **BifrostAgent** — Drives channel hopping, AP tracking, client deauth, handshake collection
- **BifrostAutomata** — State machine (MANUAL, AUTOMATIC, BORED, SAD, EXCITED, LONELY) controlling recon aggressiveness
- **BifrostEpoch** — Tracks WiFi recon epochs with reward calculation
- **BifrostVoice** — Personality/mood system for EPD display messages
- **Plugin system** — Extensible event-driven plugin architecture
- **Dedicated web page** (`bifrost.js`) for real-time WiFi recon monitoring
- **Database module** (`db_utils/bifrost.py`) for persistent handshake and AP storage
- **Monitor mode management** — Automatic WiFi interface setup/teardown scripts

#### Loki — USB HID Attack Framework

- **USB Rubber Ducky-style HID injection** via Raspberry Pi USB gadget mode
- **HID Controller** (`loki/hid_controller.py`) — Low-level USB HID keyboard/mouse report writer to `/dev/hidg0`/`/dev/hidg1`
- **HIDScript engine** (`loki/hidscript.py`) — JavaScript-based payload scripting language
- **Multi-language keyboard layouts** — US, FR, DE, ES, IT, RU, UK, ZH with JSON layout definitions and auto-generation tool
- **Pre-built payloads** — Hello World, Reverse Shell (Linux), Rickroll, WiFi credential exfiltration (Windows)
- **Job queue** (`loki/jobs.py`) — Managed execution of HID payloads with status tracking
- **Loki Deceiver action** (`actions/loki_deceiver.py`) — Rogue access point creation for WiFi authentication capture and MITM
- **Dedicated web page** (`loki.js`) for payload management and execution
- **Database module** (`db_utils/loki.py`) for job persistence

#### Sentinel — Network Watchdog Engine

- **9 detection modules** running as a lightweight background daemon:
  - `new_device` — Never-seen MAC appears on the network
  - `device_join` — Known device comes back online
  - `device_leave` — Known device goes offline
  - `arp_spoof` — Same IP claimed by multiple MACs (ARP cache conflict)
  - `port_change` — Host ports changed since last snapshot
  - `service_change` — New service detected on known host
  - `rogue_dhcp` — Multiple DHCP servers detected
  - `dns_anomaly` — DNS response pointing to unexpected IP
  - `mac_flood` — Sudden burst of new MACs (possible MAC flooding attack)
- **Zero extra network traffic** — All checks read from existing Bjorn DB
- **Configurable severity levels** (info, warning, critical)
- **Dedicated web page** (`sentinel.js`) for alert browsing and rule management
- **Database module** (`db_utils/sentinel.py`) for alert persistence

#### ZombieLand — Command & Control Infrastructure

- **C2 Manager** (`c2_manager.py`) — Professional C2 server with:
  - Encrypted agent communication (Fernet)
  - SSH-based agent registration via Paramiko
  - Agent heartbeat monitoring and health tracking
  - Job dispatch and result collection
  - UUID-based agent identification
- **Dedicated web page** (`zombieland.js`) with SSE-powered real-time agent monitoring
- **Database module** (`db_utils/agents.py`) for agent and job persistence
- **Marked as experimental** with appropriate UI warnings

---

### New Action Modules (15 New Actions)

| Action | Module | Description |
|--------|--------|-------------|
| **ARP Spoofer** | `arp_spoofer.py` | Bidirectional ARP cache poisoning for MITM positioning with automatic gateway detection and clean ARP table restoration |
| **Berserker Force** | `berserker_force.py` | Service resilience stress-testing — baseline measurement, controlled TCP/SYN/HTTP load testing, performance degradation quantification |
| **DNS Pillager** | `dns_pillager.py` | Comprehensive DNS reconnaissance — reverse DNS, record enumeration (A/AAAA/MX/NS/TXT/CNAME/SOA/SRV/PTR), zone transfer attempts |
| **Freya Harvest** | `freya_harvest.py` | Network-wide data harvesting and consolidation action |
| **Heimdall Guard** | `heimdall_guard.py` | Advanced stealth module for traffic manipulation and IDS/IPS evasion |
| **Loki Deceiver** | `loki_deceiver.py` | Rogue access point creation for WiFi authentication capture and MITM attacks |
| **Odin Eye** | `odin_eye.py` | Passive network analyzer for credential and data pattern hunting |
| **Rune Cracker** | `rune_cracker.py` | Advanced hash/credential cracking module |
| **Thor Hammer** | `thor_hammer.py` | Lightweight service fingerprinting via TCP connect + banner grab (Pi Zero friendly, no nmap dependency) |
| **Valkyrie Scout** | `valkyrie_scout.py` | Web surface reconnaissance — probes common paths, extracts auth types, login forms, missing security headers, error/debug strings |
| **Yggdrasil Mapper** | `yggdrasil_mapper.py` | Network topology mapper via traceroute with service enrichment from DB and merged JSON topology graph |
| **Web Enumeration** | `web_enum.py` | Web service enumeration and directory discovery |
| **Web Login Profiler** | `web_login_profiler.py` | Web login form detection and profiling |
| **Web Surface Mapper** | `web_surface_mapper.py` | Web application surface mapping and endpoint discovery |
| **WPAsec Potfiles** | `wpasec_potfiles.py` | WPA-sec.stanev.org potfile integration for WiFi password recovery |
| **Presence Join** | `presence_join.py` | Event-triggered action when a host joins the network (priority 90) |
| **Presence Leave** | `presence_left.py` | Event-triggered action when a host leaves the network (priority 90) |
| **Demo Action** | `demo_action.py` | Template/demonstration action for community developers |

### Improved Action Modules

- All bruteforce actions (SSH, FTP, SMB, SQL, Telnet) **rewritten** with shared `bruteforce_common.py` module providing:
  - `ProgressTracker` class for unified EPD progress reporting
  - Standardized credential iteration and result handling
  - Configurable rate limiting and timeout management
- **Scanning action** (`scanning.py`) improved with better network discovery and host tracking
- **Nmap Vulnerability Scanner** refined with better CVE parsing and result persistence
- All steal/exfiltrate modules updated for new database schema compatibility

### Removed Actions

| Action | Reason |
|--------|--------|
| `rdp_connector.py` / `steal_files_rdp.py` | Replaced by more capable modules |
| `log_standalone.py` / `log_standalone2.py` | Consolidated into proper logging system |
| `ftp_connector.py`, `smb_connector.py`, etc. | Connector pattern replaced by dedicated bruteforce modules |

---

### Web Interface — Complete Rewrite

#### Architecture Revolution

- **Static multi-page HTML** (6 pages) replaced by a **hash-routed Single Page Application** with 25 lazy-loaded page modules
- **SPA Router** (`web/js/core/router.js`) — Hash-based routing with guaranteed `unmount()` cleanup before page transitions
- **ResourceTracker** (`web/js/core/resource-tracker.js`) — Automatic tracking and cleanup of intervals, timeouts, event listeners, and AbortControllers per page — **zero memory leaks**
- **Single `index.html`** entry point replaces 6 separate HTML files
- **Modular CSS** — Global stylesheet + per-page CSS files (`web/css/pages/*.css`)

#### New Web Pages (19 New Pages)

| Page | Module | Description |
|------|--------|-------------|
| **Dashboard** | `dashboard.js` | Real-time system stats, resource monitoring, uptime tracking |
| **Actions** | `actions.js` | Action browser with enable/disable toggles and configuration |
| **Actions Studio** | `actions-studio.js` | Visual action pipeline editor with drag-and-drop canvas |
| **Attacks** | `attacks.js` | Attack configuration with image upload and EPD layout editor tab |
| **Backup** | `backup.js` | Database backup/restore management |
| **Bifrost** | `bifrost.js` | WiFi recon monitoring dashboard |
| **Database** | `database.js` | Direct database browser and query tool |
| **Files** | `files.js` | File manager with upload, drag-drop, rename, delete |
| **LLM Chat** | `llm-chat.js` | Terminal-style LLM chat with tool-calling and orch log viewer |
| **LLM Config** | `llm-config.js` | Full LLM/MCP configuration panel |
| **Loki** | `loki.js` | HID attack payload management and execution |
| **RL Dashboard** | `rl-dashboard.js` | Reinforcement Learning metrics and model performance visualization |
| **Scheduler** | `scheduler.js` | Action scheduler configuration and monitoring |
| **Sentinel** | `sentinel.js` | Network watchdog alerts and rule management |
| **Vulnerabilities** | `vulnerabilities.js` | CVE browser with modal details and feed sync |
| **Web Enum** | `web-enum.js` | Web enumeration results browser with status filters |
| **ZombieLand** | `zombieland.js` | C2 agent management dashboard (experimental) |
| **Bjorn Debug** | `bjorn-debug.js` | System debug information and diagnostics |
| **Scripts** | (via scheduler) | Custom script upload and execution |

#### Improved Existing Pages

- **Network** (`network.js`) — D3 force-directed graph completely rewritten with proper cleanup on unmount, lazy D3 loading, search debounce, simulation stop
- **Credentials** (`credentials.js`) — AbortController tracking, toast timer cleanup, proper state reset
- **Loot** (`loot.js`) — Search timer cleanup, ResourceTracker integration
- **NetKB** (`netkb.js`) — View mode persistence, filter tracking, pagination integration
- **Bjorn/EPD** (`bjorn.js`) — Image refresh tracking, zoom controls, null EPD state handling

#### Internationalization (i18n)

- **7 supported languages**: English, French, Spanish, German, Italian, Russian, Chinese
- **i18n module** (`web/js/core/i18n.js`) with JSON translation files, `t()` helper function, and `data-i18n` attribute auto-translation
- **Fallback chain**: Current language -> English -> developer warning
- **Language selector** in UI with `localStorage` persistence

#### Theming Engine

- **Theme module** (`web/js/core/theme.js`) — CSS variable-based theming system
- **Preset themes** including default "Nordic Acid" (dark green/cyan)
- **User custom themes** with color picker + raw CSS editing
- **Icon pack switching** via icon registry
- **Theme import/export** as JSON
- **Live preview** — changes applied instantly without page reload
- **`localStorage` persistence** across sessions

#### Other Frontend Features

- **Console SSE** (`web/js/core/console-sse.js`) — Server-Sent Events for real-time log streaming with reconnect logic
- **Quick Panel** (`web/js/core/quickpanel.js`) — Fast-access control panel
- **Sidebar Layout** (`web/js/core/sidebar-layout.js`) — Collapsible sidebar navigation
- **Settings Config** (`web/js/core/settings-config.js`) — Dynamic form generation from config schema with chip editor
- **EPD Layout Editor** (`web/js/core/epd-editor.js`) — SVG drag-and-drop editor for e-paper display layouts with grid/snap, zoom (50-600%), undo stack, element properties panel
- **D3.js v7** bundled for network topology visualization
- **PWA Manifest** updated for installable web app experience

---

### Core Engine Improvements

#### Database — Modular Facade Architecture

- **Complete database rewrite** — Monolithic SQLite helper replaced by `BjornDatabase` facade delegating to **18 specialized modules** in `db_utils/`:
  - `base.py` — Connection management, thread-safe connection pool
  - `config.py` — Configuration CRUD operations
  - `hosts.py` — Host discovery and tracking
  - `actions.py` — Action metadata and history
  - `queue.py` — Action queue with priority system and circuit breaker
  - `vulnerabilities.py` — CVE vulnerability storage
  - `software.py` — Software inventory
  - `credentials.py` — Credential storage
  - `services.py` — Service/port tracking
  - `scripts.py` — Custom script management
  - `stats.py` — Statistics and metrics
  - `backups.py` — Database backup/restore
  - `comments.py` — EPD comment templates
  - `agents.py` — C2 agent management
  - `studio.py` — Actions Studio pipeline data
  - `webenum.py` — Web enumeration results
  - `sentinel.py` — Sentinel alert storage
  - `bifrost.py` — WiFi recon data
  - `loki.py` — HID attack job storage
- **Full backward compatibility** maintained via `__getattr__` delegation

#### Orchestrator — Smarter, More Resilient

- **Action Scheduler** (`action_scheduler.py`) — Complete rewrite with:
  - Trigger evaluation system (`on_host_alive`, `on_port_change`, `on_web_service`, `on_join`, `on_leave`, `on_start`, `on_success:*`)
  - Requirements checking with dependency resolution
  - Cooldown and rate limiting per action
  - Priority queue processing
  - Circuit breaker integration
  - LLM autonomous mode skip option
- **Per-action circuit breaker** — 3-state machine (closed -> open -> half-open) with exponential backoff, prevents repeated failures from wasting resources
- **Global concurrency limiter** — DB-backed running action count check, configurable `semaphore_slots`
- **Manual mode with active scanning** — Background scan timer keeps network discovery running even in manual mode
- **Runtime State Updater** (`runtime_state_updater.py`) — Dedicated background thread keeping display-facing data fresh, decoupled from render loop

#### AI/ML Engine — From Heuristic to Reinforcement Learning

- **AI Engine** (`ai_engine.py`) — Full reinforcement learning decision engine:
  - Feature-based action scoring
  - Model versioning with up to 3 versions on disk
  - Auto-rollback if average reward drops after 50 decisions
  - Cold-start bootstrap with persistent per-(action, port_profile) running averages
  - Blended heuristic/bootstrap scoring during warm-up phase
- **Feature Logger** (`feature_logger.py`) — Structured feature logging for ML training with variance-based feature selection
- **Data Consolidator** (`data_consolidator.py`) — Aggregates logged features into training-ready datasets exportable for TensorFlow/PyTorch
- **Continuous reward shaping** — Novelty bonus, repeat penalty, diminishing returns, partial credit for long-running failed actions
- **AI utility modules** (`ai_utils.py`) for shared ML helper functions

#### Display — Multi-Size EPD Support

- **Display Layout Engine** (`display_layout.py`) — JSON-based element positioning system:
  - Built-in layouts for 2.13" and 2.7" Waveshare e-paper displays
  - 20+ positionable UI elements (icons, text, bars, status indicators)
  - Custom layout override via `resources/layouts/{epd_type}.json`
  - `px()`/`py()` scaling preserved for resolution independence
- **EPD Manager** (`epd_manager.py`) — Abstraction layer over Waveshare EPD hardware
- **Web-based EPD Layout Editor** — SVG drag-and-drop canvas with:
  - Corner resize handles
  - Color/NB/BN display mode preview
  - Grid/snap, zoom (50-600%), toggleable element labels
  - Add/delete elements, import/export layout JSON
  - 50-deep undo stack (Ctrl+Z)
  - Color-coded elements by type
  - Arrow key nudge, keyboard shortcuts
- **Display module** (`display.py`) grew from 390 to **1,130 lines** with multi-layout rendering pipeline

#### Web Server — Massive Expansion

- **webapp.py** grew from 222 to **1,037 lines**
- **18 web utility modules** in `web_utils/` (was: 0):
  - `action_utils.py`, `attack_utils.py`, `backup_utils.py`, `bifrost_utils.py`
  - `bluetooth_utils.py`, `c2_utils.py`, `character_utils.py`, `comment_utils.py`
  - `db_utils.py`, `debug_utils.py`, `file_utils.py`, `image_utils.py`
  - `index_utils.py`, `llm_utils.py`, `loki_utils.py`, `netkb_utils.py`
  - `network_utils.py`, `orchestrator_utils.py`, `rl_utils.py`, `script_utils.py`
  - `sentinel_utils.py`, `studio_utils.py`, `system_utils.py`, `vuln_utils.py`
  - `webenum_utils.py`
- **Paginated API endpoints** for heavy data (`?page=N&per_page=M`)
- **RESTful API** covering all new features (LLM, MCP, Sentinel, Bifrost, Loki, C2, EPD editor, backups, etc.)

#### Configuration — Greatly Expanded

- **shared.py** grew from 685 to **1,502 lines** — more than doubled
- **New configuration sections**:
  - LLM Bridge (14 parameters)
  - MCP Server (4 parameters)
  - LLM Orchestrator (7 parameters)
  - AI/ML Engine (feature selection, model versioning, cold-start bootstrap)
  - Circuit breaker (threshold, cooldown)
  - Manual mode scanning (interval, auto-scan toggle)
  - Sentinel watchdog settings
  - Bifrost WiFi recon settings
  - Loki HID attack settings
  - Runtime state updater timings
- **Default config system** — `resources/default_config/` with bundled default action modules and comment templates

---

### Security Fixes

- **[SEC-01]** Eliminated all `shell=True` subprocess calls — replaced with safe argument lists
- **[SEC-02]** Added MAC address validation (regex) in DELETE route handler to prevent path traversal
- **[SEC-03]** Strengthened path validation using `os.path.realpath()` + dedicated validation helper to prevent symlink-based path traversal
- **[SEC-04]** Cortex config secrets replaced with placeholder values, properly `.gitignore`d
- **[SEC-05]** Added JWT authentication to Cortex WebSocket `/ws/logs` endpoint
- **[SEC-06]** Cortex device API authentication now required by default, CORS configurable via environment variable
- **MCP security** — Per-tool access control via `mcp_allowed_tools`, `query_db` restricted to SELECT only
- **File operations** — All file upload/download/delete operations use canonicalized path validation

### Bug Fixes

- **[BT-01]** Replaced bare `except:` clauses with specific exception handling + logging in Bluetooth utils
- **[BT-02]** Added null address validation in Bluetooth route entry points
- **[BT-03]** Added `threading.Lock` for `bt.json` read/write (race condition fix)
- **[BT-04]** Changed `auto_bt_connect` service restart to non-fatal (`check=False`)
- **[WEB-01]** Fixed SSE reconnect counter — only resets after 5+ consecutive healthy messages (was: reset on every single message, enabling infinite reconnect loops)
- **[WEB-02]** Removed empty string from `silent_routes` that was suppressing ALL log messages
- **[STAB-03]** Cleaned up dead GPS UI references, wired rl-dashboard mount
- **[ORCH-BUG]** Fixed Auto->Manual mode switch not resetting status to IDLE (4-location fix across `orchestrator.py`, `Bjorn.py`, and `orchestrator_utils.py`)
- Fixed D3 network graph memory leaks on page navigation
- Fixed multiple zombie timer and event listener leaks across all SPA pages
- Fixed search debounce timers not being cleaned up on unmount

### Quality & Stability

- **Standardized error handling** across all `web_utils` modules with consistent JSON response format
- **Magic numbers extracted** to named constants throughout the codebase
- **All 18 SPA pages** reviewed and hardened:
  - 11 pages fully rewritten with ResourceTracker, safe DOM (no innerHTML), visibility-aware pollers
  - 7 pages with targeted fixes for memory leaks, zombie timers, state reset issues
- **Uniform action metadata format** — All actions use AST-friendly `b_*` module-level constants for class, module, status, port, service, trigger, priority, cooldown, rate_limit, etc.

---

### Infrastructure & DevOps

- **Mode Switcher** (`mode-switcher.sh`) — Shell script for switching between operation modes
- **Bluetooth setup** (`bjorn_bluetooth.sh`) — Automated Bluetooth service configuration
- **USB Gadget setup** (`bjorn_usb_gadget.sh`) — USB HID gadget mode configuration for Loki
- **WiFi setup** (`bjorn_wifi.sh`) — WiFi interface and monitor mode management
- **MAC prefix database** (`data/input/prefixes/nmap-mac-prefixes.txt`) — Vendor identification for discovered devices
- **Common wordlists** (`data/input/wordlists/common.txt`) — Built-in wordlist for web enumeration

### Dependencies

**Added:**
- `zeroconf>=0.131.0` — LaRuche/LAND mDNS auto-discovery
- `paramiko` — SSH operations for C2 agent communication (moved from optional to core)
- `cryptography` (via Fernet) — C2 communication encryption

**Removed:**
- `Pillow==9.4.0` — No longer pinned (use system version)
- `rich==13.9.4` — Removed (was used for standalone logging)
- `pandas==2.2.3` — Removed (lightweight alternatives used instead)

**Optional (documented):**
- `mcp[cli]>=1.0.0` — MCP server support

---

### Breaking Changes

- **Web UI URLs changed** — Individual page URLs (`/bjorn.html`, `/config.html`, etc.) replaced by SPA hash routes (`/#/bjorn`, `/#/settings`, etc.)
- **Database schema expanded** — New tables for actions queue, circuit breaker, sentinel alerts, bifrost data, loki jobs, C2 agents, web enumeration, studio pipelines. Migration is automatic.
- **Configuration keys expanded** — `shared_config.json` now contains 45+ additional keys. Unknown keys are safely ignored; new defaults are applied automatically.
- **Action module format updated** — Actions now use `b_*` metadata constants instead of class-level attributes. Old-format actions will need migration.
- **RDP actions removed** — `rdp_connector.py` and `steal_files_rdp.py` dropped in favor of more capable modules.

---

### Stats

```
 Component           | v1        | v2          | Change
─────────────────────┼───────────┼─────────────┼──────────
 Python files        | 37        | 130+        | +250%
 Python LoC          | ~8,200    | ~58,000     | +607%
 JS/CSS/HTML LoC     | ~2,100    | ~42,000     | +1,900%
 Action modules      | 17        | 32          | +88%
 Web pages           | 6         | 25          | +317%
 DB modules          | 1         | 18          | +1,700%
 Web API modules     | 0         | 18+         | New
 Config parameters   | ~80       | ~180+       | +125%
 Supported languages | 1         | 7           | +600%
 Shell scripts       | 3         | 5           | +67%
```

---

*Skol! The Cyberviking has evolved.*
