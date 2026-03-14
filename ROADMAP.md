# BJORN Cyberviking — Roadmap & Changelog

> Comprehensive audit-driven roadmap for the v2 release.
> Each section tracks scope, status, and implementation notes.

---

## Legend

| Tag | Meaning |
|-----|---------|
| `[DONE]` | Implemented and verified |
| `[WIP]` | Work in progress |
| `[TODO]` | Not yet started |
| `[DROPPED]` | Descoped / won't fix |

---

## P0 — Security & Blockers (Must-fix before release)

### SEC-01: Shell injection in system_utils.py `[DONE]`
- **File:** `web_utils/system_utils.py`
- **Issue:** `subprocess.Popen(command, shell=True)` on reboot, shutdown, restart, clear_logs
- **Fix:** Replace all `shell=True` calls with argument lists (`["sudo", "reboot"]`)
- **Risk:** Command injection if any parameter is ever user-controlled

### SEC-02: Path traversal in DELETE route `[DONE]`
- **File:** `webapp.py:497-498`
- **Issue:** MAC address extracted from URL path with no validation — `self.path.split(...)[-1]`
- **Fix:** URL-decode and validate MAC format with regex before passing to handler

### SEC-03: Path traversal in file operations `[DONE]`
- **File:** `web_utils/file_utils.py`
- **Issue:** `move_file`, `rename_file`, `delete_file` accept paths from POST body.
  Path validation uses `startswith()` which can be bypassed (symlinks, encoding).
- **Fix:** Use `os.path.realpath()` instead of `os.path.abspath()` for canonicalization.
  Add explicit path validation helper used by all file ops.

### SEC-04: Cortex secrets committed to repo `[DONE]`
- **Files:** `bjorn-cortex/Cortex/security_config.json`, `server_config.json`
- **Issue:** JWT secret, TOTP secret, admin password hash, device API key in git
- **Fix:** Replaced with clearly-marked placeholder values + WARNING field, already in `.gitignore`

### SEC-05: Cortex WebSocket without auth `[DONE]`
- **File:** `bjorn-cortex/Cortex/server.py`
- **Issue:** `/ws/logs` endpoint has no authentication — anyone can see training logs
- **Fix:** Added `_verify_ws_token()` — JWT via query param or first message, close 4401 on failure

### SEC-06: Cortex device API auth disabled by default `[DONE]`
- **File:** `bjorn-cortex/Cortex/server_config.json`
- **Issue:** `allow_device_api_without_auth: true` + empty `device_api_key`
- **Fix:** Default to `false`, placeholder API key, CORS origins via `CORS_ORIGINS` env var

---

## P0 — Bluetooth Fixes

### BT-01: Bare except clauses `[DONE]`
- **File:** `web_utils/bluetooth_utils.py:225,258`
- **Issue:** `except:` swallows all exceptions including SystemExit, KeyboardInterrupt
- **Fix:** Replace with `except (dbus.exceptions.DBusException, Exception) as e:` with logging

### BT-02: Null address passed to BT functions `[DONE]`
- **File:** `webapp.py:210-214`
- **Issue:** `d.get('address')` can return None, passed directly to BT methods
- **Fix:** Add null check + early return with error in each lambda/BT method entry point

### BT-03: Race condition on bt.json `[DONE]`
- **File:** `web_utils/bluetooth_utils.py:200-216`
- **Issue:** Read-modify-write on shared file without locking
- **Fix:** Add `threading.Lock` for bt.json access, use atomic write pattern

### BT-04: auto_bt_connect service crash `[DONE]`
- **File:** `web_utils/bluetooth_utils.py:219`
- **Issue:** `subprocess.run(..., check=True)` raises CalledProcessError if service missing
- **Fix:** Use `check=False` and log warning instead of crashing

---

## P0 — Web Server Fixes

### WEB-01: SSE reconnect counter reset bug `[DONE]`
- **File:** `web/js/core/console-sse.js:367`
- **Issue:** `reconnectCount = 0` on every message — a single flaky message resets counter,
  enabling infinite reconnect loops
- **Fix:** Only reset counter after sustained healthy connection (e.g., 5+ messages)

### WEB-02: Silent routes list has trailing empty string `[DONE]`
- **File:** `webapp.py:474`
- **Issue:** Empty string `""` in `silent_routes` matches ALL log messages
- **Fix:** Remove empty string from list

---

## P1 — Stability & Consistency

### STAB-01: Uniform error handling pattern `[DONE]`
- **Files:** All `web_utils/*.py`
- **Issue:** Mix of bare `except:`, `except Exception`, inconsistent error response format
- **Fix:** Establish `_json_response(handler, data, status)` helper; catch specific exceptions

### STAB-02: Add pagination to heavy API endpoints `[DONE]`
- **Files:** `web_utils/netkb_utils.py`, `web_utils/orchestrator_utils.py`
- **Endpoints:** `/netkb_data`, `/list_credentials`, `/network_data`
- **Fix:** Accept `?page=N&per_page=M` query params, return `{data, total, page, pages}`

### STAB-03: Dead routes & unmounted pages `[DONE]`
- **Files:** `web/js/app.js`, various
- **Issue:** GPS UI elements with no backend, rl-dashboard not mounted, zombieland incomplete
- **Fix:** Remove GPS placeholder, wire rl-dashboard mount, mark zombieland as beta

### STAB-04: Missing constants for magic numbers `[DONE]`
- **Files:** `web_utils/bluetooth_utils.py`, `webapp.py`
- **Fix:** Extract timeout values, pool sizes, size limits to named constants

---

## P2 — Web SPA Quality

### SPA-01: Review & fix dashboard.js `[DONE]`
- Check stat polling, null safety, error display

### SPA-02: Review & fix network.js `[DONE]`
- D3 graph cleanup on unmount, memory leak check

### SPA-03: Review & fix credentials.js `[DONE]`
- Search/filter robustness, export edge cases

### SPA-04: Review & fix vulnerabilities.js `[DONE]`
- CVE modal error handling, feed sync status

### SPA-05: Review & fix files.js `[DONE]`
- Upload progress, drag-drop edge cases, path validation

### SPA-06: Review & fix netkb.js `[DONE]`
- View mode transitions, filter persistence, pagination integration

### SPA-07: Review & fix web-enum.js `[DONE]`
- Status code filter, date range, export completeness

### SPA-08: Review & fix rl-dashboard.js `[DONE]`
- Canvas cleanup, mount lifecycle, null data handling

### SPA-09: Review & fix zombieland.js (C2) `[DONE]`
- SSE lifecycle, agent list refresh, mark as experimental

### SPA-10: Review & fix scripts.js `[DONE]`
- Output polling cleanup, project upload validation

### SPA-11: Review & fix attacks.js `[DONE]`
- Tab switching, image upload validation

### SPA-12: Review & fix bjorn.js (EPD viewer) `[DONE]`
- Image refresh, zoom controls, null EPD state

### SPA-13: Review & fix settings-config.js `[DONE]`
- Form generation edge cases, chip editor validation

### SPA-14: Review & fix actions-studio.js `[DONE]`
- Canvas lifecycle, node dragging, edge persistence

---

## P2 — AI/Cortex Improvements

### AI-01: Feature selection / importance analysis `[DONE]`
- Variance-based feature filtering in data consolidator (drops near-zero variance features)
- Feature manifest exported alongside training data
- `get_feature_importance()` method on FeatureLogger for introspection
- Config: `ai_feature_selection_min_variance` (default 0.001)

### AI-02: Continuous reward shaping `[DONE]`
- Extended reward function with 4 new components: novelty bonus, repeat penalty,
  diminishing returns, partial credit for long-running failed actions
- Helper methods to query attempt counts and consecutive failures from ml_features

### AI-03: Model versioning & rollback `[DONE]`
- Keep up to 3 model versions on disk (configurable)
- Model history tracking: version, loaded_at, accuracy, avg_reward
- `rollback_model()` method to load previous version
- Auto-rollback if average reward drops below previous model after 50 decisions

### AI-04: Low-data cold-start bootstrap `[DONE]`
- Bootstrap scores dict accumulating per (action_name, port_profile) running averages
- Blended heuristic/bootstrap scoring (40-80% weight based on sample count)
- Persistent `ai_bootstrap_scores.json` across restarts
- Config: `ai_cold_start_bootstrap_weight` (default 0.6)

---

## P3 — Future Features

### EPD-01: Multi-size EPD layout engine `[DONE]`
- New `display_layout.py` module with `DisplayLayout` class
- JSON layout definitions per EPD type (2.13", 2.7")
- Element-based positioning: each UI component has named anchor `{x, y, w, h}`
- Custom layouts stored in `resources/layouts/{epd_type}.json`
- `px()`/`py()` scaling preserved, layout provides reference coordinates
- Integrated into `display.py` rendering pipeline

### EPD-02: Web-based EPD layout editor `[DONE]`
- Backend API: `GET/POST /api/epd/layout`, `POST /api/epd/layout/reset`
- `GET /api/epd/layouts` lists all supported EPD types and their layouts
- `GET /api/epd/layout?epd_type=X` to fetch layout for a specific EPD type
- Frontend editor: `web/js/core/epd-editor.js` — 4th tab in attacks page
- SVG canvas with drag-and-drop element positioning and corner resize handles
- Display mode preview: Color, NB (black-on-white), BN (white-on-black)
- Grid/snap, zoom (50-600%), toggleable element labels
- Add/delete elements, import/export layout JSON
- Properties panel with x/y/w/h editors, font size editors
- Undo system (50-deep snapshot stack, Ctrl+Z)
- Color-coded elements by type (icons=blue, text=green, bars=orange, etc.)
- Transparency-aware checkerboard canvas background
- Arrow key nudge, keyboard shortcuts

### ORCH-01: Per-action circuit breaker `[DONE]`
- New `action_circuit_breaker` DB table: failure_streak, circuit_status, cooldown_until
- Three states: closed → open (after N fails) → half_open (after cooldown)
- Exponential backoff: `min(2^streak * 60, 3600)` seconds
- Integrated into `_should_queue_action()` check
- Success on half-open resets circuit, failure re-opens with longer cooldown
- Config: `circuit_breaker_threshold` (default 3)

### ORCH-02: Global concurrency limiter `[DONE]`
- DB-backed running action count check before scheduling
- `count_running_actions()` method in queue.py
- Per-action `max_concurrent` support in requirements evaluator
- Respects `semaphore_slots` config (default 5)

### ORCH-03: Manual mode with active scanning `[DONE]`
- Background scan timer thread in MANUAL mode
- NetworkScanner runs at `manual_mode_scan_interval` (default 180s)
- Config: `manual_mode_auto_scan` (default True)
- Scan timer auto-stops when switching back to AUTO/AI

---

## Changelog

### 2026-03-12 — Security & Stability Audit

#### Security
- **[SEC-01]** Replaced all `shell=True` subprocess calls with safe argument lists
- **[SEC-02]** Added MAC address validation (regex) in DELETE route handler
- **[SEC-03]** Strengthened path validation using `os.path.realpath()` + dedicated helper
- **[BT-01]** Replaced bare `except:` with specific exception handling + logging
- **[BT-02]** Added null address validation in Bluetooth route lambdas and method entry points
- **[BT-03]** Added file lock for bt.json read/write operations
- **[BT-04]** Changed auto_bt_connect restart to non-fatal (check=False)
- **[SEC-04]** Cortex config files: placeholder secrets + WARNING field, already gitignored
- **[SEC-05]** Added JWT auth to Cortex WebSocket `/ws/logs` endpoint
- **[SEC-06]** Cortex device API auth now required by default, CORS configurable via env var

#### Bug Fixes
- **[WEB-01]** Fixed SSE reconnect counter: only resets after 5+ consecutive healthy messages
- **[WEB-02]** Removed empty string from silent_routes that was suppressing all log messages
- **[STAB-03]** Cleaned up dead GPS UI references, wired rl-dashboard mount
- **[ORCH-BUG]** Fixed Auto→Manual mode switch not resetting status to IDLE (4-location fix):
  - `orchestrator.py`: Reset all status fields after main loop exit AND after action completes with exit flag
  - `Bjorn.py`: Reset status even when `thread.join(10)` times out
  - `orchestrator_utils.py`: Explicit IDLE reset in web API stop handler

#### Quality
- **[STAB-01]** Standardized error handling across web_utils modules
- **[STAB-04]** Extracted magic numbers to named constants

#### SPA Page Review (SPA-01..14)
All 18 SPA page modules reviewed and fixed:

**Pages fully rewritten (11 pages):**
- **dashboard.js** — New layout with ResourceTracker, safe DOM (no innerHTML), visibility-aware pollers, proper uptime ticker cleanup
- **network.js** — D3 force graph cleanup on unmount, lazy d3 loading, search debounce tracked, simulation stop
- **credentials.js** — AbortController tracked, toast timer tracked, proper state reset in unmount
- **vulnerabilities.js** — ResourceTracker integration, abort controllers, null safety throughout
- **files.js** — Upload progress, drag-drop safety, ResourceTracker lifecycle
- **netkb.js** — View mode persistence, filter tracked, pagination integration
- **web-enum.js** — Status filter, date range, tracked pollers and timeouts
- **rl-dashboard.js** — Canvas cleanup, chart lifecycle, null data guards
- **zombieland.js** — SSE lifecycle tracked, agent list cleanup, experimental flag
- **attacks.js** — Tab switching, ResourceTracker integration, proper cleanup
- **bjorn.js** — Image refresh tracked, zoom controls, null EPD state handling

**Pages with targeted fixes (7 pages):**
- **bjorn-debug.js** — Fixed 3 button event listeners using raw `addEventListener` → `tracker.trackEventListener` (memory leak)
- **scheduler.js** — Added `searchDeb` timeout cleanup + state reset in unmount (zombie timer)
- **actions.js** — Added resize debounce cleanup in unmount + tracked `highlightPane` timeout (zombie timer)
- **backup.js** — Already clean: ResourceTracker, sidebar layout cleanup, state reset (no changes needed)
- **database.js** — Already clean: search debounce cleanup, sidebar layout, Poller lifecycle (no changes needed)
- **loot.js** — Already clean: search timer cleanup, ResourceTracker, state reset (no changes needed)
- **actions-studio.js** — Already clean: runtime cleanup function, ResourceTracker (no changes needed)

#### AI Pipeline (AI-01..04)
- **[AI-01]** Feature selection: variance-based filtering in `data_consolidator.py`, feature manifest export, `get_feature_importance()` in `feature_logger.py`
- **[AI-02]** Continuous reward shaping in `orchestrator.py`: novelty bonus, diminishing returns penalty, partial credit for long-running failures, attempt/streak DB queries
- **[AI-03]** Model versioning in `ai_engine.py`: 3-model history, `rollback_model()`, auto-rollback after 50 decisions if avg reward drops
- **[AI-04]** Cold-start bootstrap in `ai_engine.py`: persistent `ai_bootstrap_scores.json`, blended heuristic/bootstrap scoring with adaptive weighting

#### Orchestrator (ORCH-01..03)
- **[ORCH-01]** Circuit breaker: new `action_circuit_breaker` DB table in `db_utils/queue.py`, 3-state machine (closed→open→half-open), exponential backoff `min(2^N*60, 3600)s`, integrated into `action_scheduler.py` scheduling decisions and `orchestrator.py` post-execution
- **[ORCH-02]** Global concurrency limiter: `count_running_actions()` in `db_utils/queue.py`, pre-schedule check in `action_scheduler.py` against `semaphore_slots` config
- **[ORCH-03]** Manual mode scanning: background `_scan_loop` thread in `orchestrator_utils.py`, runs at `manual_mode_scan_interval` (180s default), auto-stops on mode switch

#### EPD Multi-Size (EPD-01..02)
- **[EPD-01]** New `display_layout.py` module: `DisplayLayout` class with JSON-based element positioning, built-in layouts for 2.13" and 2.7" displays, custom layout override via `resources/layouts/`, 20+ elements integrated into `display.py` rendering pipeline
- **[EPD-02]** Backend API: `GET/POST /api/epd/layout`, `POST /api/epd/layout/reset`, `GET /api/epd/layouts` — endpoints in `web_utils/system_utils.py`, routes in `webapp.py`
- **[EPD-02]** Frontend editor: `web/js/core/epd-editor.js` as 4th tab in attacks page — SVG drag-and-drop canvas, resize handles, Color/NB/BN display modes, grid/snap/zoom, add/delete elements, import/export JSON, undo stack, font size editing, arrow key nudge

#### New Configuration Parameters
- `ai_feature_selection_min_variance` (0.001) — minimum variance for feature inclusion
- `ai_model_history_max` (3) — max model versions kept on disk
- `ai_auto_rollback_window` (50) — decisions before auto-rollback evaluation
- `ai_cold_start_bootstrap_weight` (0.6) — bootstrap vs static heuristic weight
- `circuit_breaker_threshold` (3) — consecutive failures to open circuit
- `manual_mode_auto_scan` (true) — auto-scan in MANUAL mode
- `manual_mode_scan_interval` (180) — seconds between manual mode scans
