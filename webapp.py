"""webapp.py - HTTP server with auth, gzip, and routing for the Bjorn web UI."""

import gzip
import hashlib
import hmac
import http.server
import io
import json
import logging
import os
import secrets
import signal
import socket
import socketserver
import sys
import threading
import time
import urllib.parse
from http import cookies
from urllib.parse import unquote

from init_shared import shared_data
from logger import Logger
from utils import WebUtils


# ============================================================================
# AUTH HARDENING — password hashing & signed session tokens
# ============================================================================

# Server-wide secret for HMAC-signed session cookies (regenerated each startup)
_SESSION_SECRET = secrets.token_bytes(32)
# Active session tokens (server-side set; cleared on logout)
_active_sessions: set = set()
_session_lock = threading.Lock()


def _hash_password(password: str, salt: str = None) -> dict:
    """Hash a password with SHA-256 + random salt. Returns {"hash": ..., "salt": ...}."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return {"hash": h, "salt": salt}


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """Verify a password against stored hash+salt."""
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(h, stored_hash)


def _make_session_token() -> str:
    """Create a cryptographically signed session token."""
    nonce = secrets.token_hex(16)
    sig = hmac.new(_SESSION_SECRET, nonce.encode(), hashlib.sha256).hexdigest()[:16]
    token = f"{nonce}:{sig}"
    with _session_lock:
        _active_sessions.add(token)
    return token


def _validate_session_token(token: str) -> bool:
    """Validate a session token is signed correctly AND still active."""
    if not token or ':' not in token:
        return False
    parts = token.split(':', 1)
    if len(parts) != 2:
        return False
    nonce, sig = parts
    expected_sig = hmac.new(_SESSION_SECRET, nonce.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(sig, expected_sig):
        return False
    with _session_lock:
        return token in _active_sessions


def _revoke_session_token(token: str):
    """Revoke a session token (server-side invalidation)."""
    with _session_lock:
        _active_sessions.discard(token)


def _ensure_password_hashed(webapp_json_path: str):
    """
    Auto-migrate webapp.json on first launch:
    if 'password_hash' is absent, hash the plaintext 'password' field,
    remove the plaintext, and rewrite the file.
    """
    if not os.path.exists(webapp_json_path):
        return
    try:
        with open(webapp_json_path, 'r', encoding='utf-8') as f:
            config = json.load(f)

        # Already migrated?
        if 'password_hash' in config:
            return

        plaintext = config.get('password')
        if not plaintext:
            return

        # Hash and replace
        hashed = _hash_password(plaintext)
        config['password_hash'] = hashed['hash']
        config['password_salt'] = hashed['salt']
        # Remove plaintext password
        config.pop('password', None)

        with open(webapp_json_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=4)

        Logger(name="webapp.py", level=logging.DEBUG).info(
            "webapp.json: plaintext password migrated to salted hash"
        )
    except Exception as e:
        Logger(name="webapp.py", level=logging.DEBUG).error(
            f"Failed to migrate webapp.json password: {e}"
        )

# ============================================================================
# INITIALIZATION
# ============================================================================

logger = Logger(name="webapp.py", level=logging.DEBUG)
favicon_path = os.path.join(shared_data.web_dir, '/images/favicon.ico')

# Security limit to prevent RAM saturation on Pi Zero 2
MAX_POST_SIZE = 5 * 1024 * 1024  # 5 MB max

# ============================================================================
# REQUEST HANDLER
# ============================================================================

# Global WebUtils instance to prevent re-initialization per request
web_utils_instance = WebUtils(shared_data)

# Auto-migrate plaintext passwords to hashed on first launch
_ensure_password_hashed(shared_data.webapp_json)

class CustomHandler(http.server.SimpleHTTPRequestHandler):
    """
    Custom HTTP request handler with authentication, compression, and routing.
    Refactored to use dynamic routing maps and Pi Zero optimizations.
    """

    # Routes built ONCE at class level (shared across all requests - saves RAM)
    _routes_initialized = False
    GET_ROUTES = {}
    POST_ROUTES_JSON = {}        # handlers that take (data) only
    POST_ROUTES_JSON_H = {}      # handlers that take (handler, data) - need the request handler
    POST_ROUTES_MULTIPART = {}

    def __init__(self, *args, **kwargs):
        self.shared_data = shared_data
        self.web_utils = web_utils_instance
        if not CustomHandler._routes_initialized:
            CustomHandler._register_routes_once()
        super().__init__(*args, **kwargs)

    @classmethod
    def _register_routes_once(cls):
        """Register all API routes ONCE at class level. Never per-request."""
        if cls._routes_initialized:
            return

        wu = web_utils_instance
        debug_enabled = bool(shared_data.config.get("bjorn_debug_enabled", False))

        # --- GET ROUTES ---
        # All GET handlers receive (handler) at call time via do_GET dispatch
        cls.GET_ROUTES = {
            # INDEX / DASHBOARD
            '/api/bjorn/stats': wu.index_utils.dashboard_stats,
            '/apple-touch-icon': wu.index_utils.serve_apple_touch_icon,
            '/favicon.ico': wu.index_utils.serve_favicon,
            '/manifest.json': wu.index_utils.serve_manifest,

            # C2
            '/c2/agents': wu.c2.c2_agents,
            '/c2/events': wu.c2.c2_events_sse,
            '/c2/list_clients': wu.c2.c2_list_clients,
            '/c2/status': wu.c2.c2_status,

            # WEBENUM (handled via startswith)

            # NETWORK
            '/get_known_wifi': wu.network_utils.get_known_wifi,
            '/scan_wifi': wu.network_utils.scan_wifi,
            '/get_web_delay': '_serve_web_delay',

            # FILE
            '/list_directories': wu.file_utils.list_directories,
            '/loot_directories': wu.file_utils.loot_directories,

            # BACKUP
            '/check_update': wu.backup_utils.check_update,

            # SYSTEM
            '/bjorn_status': wu.system_utils.serve_bjorn_status,
            '/load_config': wu.system_utils.serve_current_config,
            '/get_logs': wu.system_utils.serve_logs,
            '/stream_logs': wu.system_utils.sse_log_stream,
            '/check_console_autostart': wu.system_utils.check_console_autostart,
            '/check_manual_mode': wu.system_utils.check_manual_mode,
            '/restore_default_config': wu.system_utils.restore_default_config,

            # BLUETOOTH
            '/scan_bluetooth': wu.bluetooth_utils.scan_bluetooth,
            '/get_sections': wu.action_utils.get_sections,

            # SCRIPTS
            '/get_running_scripts': '_serve_running_scripts',
            '/list_scripts': '_serve_list_scripts',
            '/get_action_args_schema': '_serve_action_args_schema',

            # ACTION / IMAGES / STUDIO
            '/get_actions': wu.action_utils.get_actions,
            '/list_static_images': wu.action_utils.list_static_images_with_dimensions,
            '/list_web_images': wu.action_utils.list_web_images_with_dimensions,
            '/list_actions_icons': wu.action_utils.list_actions_icons_with_dimensions,
            '/list_characters': wu.action_utils.list_characters,
            '/bjorn_say': getattr(wu.action_utils, 'serve_bjorn_say', None),
            '/api/vulns/fix': wu.vuln_utils.fix_vulns_data,
            '/api/vulns/stats': wu.vuln_utils.serve_vulns_stats,
            '/api/feeds/status': wu.vuln_utils.serve_feed_status,
            '/api/studio/actions_db': wu.studio_utils.studio_get_actions_db,
            '/api/studio/actions_studio': wu.studio_utils.studio_get_actions_studio,
            '/api/studio/edges': wu.studio_utils.studio_get_edges,

            # DB & NETKB
            '/api/db/catalog': wu.db_utils.db_catalog_endpoint,
            '/api/db/export_all': wu.db_utils.db_export_all_endpoint,
            '/api/db/tables': wu.db_utils.db_list_tables_endpoint,
            '/netkb_data': wu.netkb_utils.serve_netkb_data,
            '/netkb_data_json': wu.netkb_utils.serve_netkb_data_json,
            '/network_data': wu.netkb_utils.serve_network_data,
            '/list_credentials': wu.orchestrator_utils.serve_credentials_data,

            # AI / RL
            '/api/rl/stats': wu.rl.get_stats,
            '/api/rl/history': wu.rl.get_training_history,
            '/api/rl/experiences': wu.rl.get_recent_experiences,

            # SENTINEL
            '/api/sentinel/status': wu.sentinel.get_status,
            '/api/sentinel/events': wu.sentinel.get_events,
            '/api/sentinel/rules': wu.sentinel.get_rules,
            '/api/sentinel/devices': wu.sentinel.get_devices,
            '/api/sentinel/arp': wu.sentinel.get_arp_table,
            '/api/sentinel/notifiers': wu.sentinel.get_notifier_config,

            # PLUGINS
            '/api/plugins/list': wu.plugin_utils.list_plugins,
            '/api/plugins/config': wu.plugin_utils.get_plugin_config,
            '/api/plugins/logs': wu.plugin_utils.get_plugin_logs,

            # BIFROST
            '/api/bifrost/status': wu.bifrost.get_status,
            '/api/bifrost/networks': wu.bifrost.get_networks,
            '/api/bifrost/handshakes': wu.bifrost.get_handshakes,
            '/api/bifrost/activity': wu.bifrost.get_activity,
            '/api/bifrost/epochs': wu.bifrost.get_epochs,
            '/api/bifrost/stats': wu.bifrost.get_stats,
            '/api/bifrost/plugins': wu.bifrost.get_plugins,

            # LOKI
            '/api/loki/status': wu.loki.get_status,
            '/api/loki/scripts': wu.loki.get_scripts,
            '/api/loki/script': wu.loki.get_script,
            '/api/loki/jobs': wu.loki.get_jobs,
            '/api/loki/payloads': wu.loki.get_payloads,
            '/api/loki/layouts': wu.loki.get_layouts,

            # EPD Layout
            '/api/epd/layout': wu.system_utils.epd_get_layout,
            '/api/epd/layouts': wu.system_utils.epd_list_layouts,

            # LLM Bridge
            '/api/llm/status': wu.llm_utils.get_llm_status,
            '/api/llm/config': wu.llm_utils.get_llm_config,
            '/api/llm/reasoning': wu.llm_utils.get_llm_reasoning,

            # MCP Server
            '/api/mcp/status': wu.llm_utils.get_mcp_status,
        }

        if debug_enabled:
            cls.GET_ROUTES.update({
                '/api/debug/snapshot': wu.debug_utils.get_snapshot,
                '/api/debug/history': wu.debug_utils.get_history,
                '/api/debug/gc': wu.debug_utils.get_gc_stats,
            })

        # --- POST ROUTES (MULTIPART) ---
        cls.POST_ROUTES_MULTIPART = {
            '/action/create': wu.action_utils.create_action,
            '/add_attack': wu.action_utils.add_attack,
            '/replace_image': wu.action_utils.replace_image,
            '/resize_images': wu.action_utils.resize_images,
            '/restore_default_images': wu.action_utils.restore_default_images,
            '/delete_images': wu.action_utils.delete_images,
            '/upload_static_image': wu.action_utils.upload_static_image,
            '/upload_status_icon': wu.action_utils.upload_status_image,
            '/upload_status_image': wu.action_utils.upload_status_image,
            '/upload_character_images': wu.action_utils.upload_character_images,
            '/upload_web_image': wu.action_utils.upload_web_image,
            '/upload_actions_icon': wu.action_utils.upload_actions_icon,
            '/upload_files': wu.file_utils.handle_file_upload,
            '/upload_project': wu.script_utils.upload_project,
            '/upload_script': wu.script_utils.upload_script,
            '/upload_custom_script': wu.script_utils.upload_custom_script,
            '/api/plugins/install': wu.plugin_utils.install_plugin,
            '/clear_actions_file': wu.system_utils.clear_actions_file,
            '/clear_livestatus': wu.system_utils.clear_livestatus,
            '/clear_logs': wu.system_utils.clear_logs,
            '/clear_netkb': wu.system_utils.clear_netkb,
            '/erase_bjorn_memories': wu.system_utils.erase_bjorn_memories,
            '/upload_potfile': wu.network_utils.upload_potfile,
            '/create_preconfigured_file': wu.network_utils.create_preconfigured_file,
            '/delete_preconfigured_file': wu.network_utils.delete_preconfigured_file,
            '/clear_shared_config_json': wu.index_utils.clear_shared_config_json,
            '/reload_generate_actions_json': wu.index_utils.reload_generate_actions_json,
        }

        # --- POST ROUTES (JSON) - data-only handlers: fn(data) ---
        cls.POST_ROUTES_JSON = {
            # WEBENUM
            # NETWORK
            '/connect_known_wifi': lambda d: (wu.network_utils.connect_known_wifi(d), setattr(shared_data, 'wifichanged', True))[0],
            '/connect_wifi': lambda d: (wu.network_utils.connect_wifi(d), setattr(shared_data, 'wifichanged', True))[0],
            '/delete_known_wifi': wu.network_utils.delete_known_wifi,
            '/update_wifi_priority': wu.network_utils.update_wifi_priority,
            '/import_potfiles': wu.network_utils.import_potfiles,
            # FILE
            '/create_folder': wu.file_utils.create_folder,
            '/delete_file': wu.file_utils.delete_file,
            '/duplicate_file': wu.file_utils.duplicate_file,
            '/move_file': wu.file_utils.move_file,
            '/rename_file': wu.file_utils.rename_file,
            '/clear_output_folder': wu.file_utils.clear_output_folder,
            # BACKUP
            '/create_backup': wu.backup_utils.create_backup,
            '/delete_backup': wu.backup_utils.delete_backup,
            '/list_backups': wu.backup_utils.list_backups,
            '/restore_backup': wu.backup_utils.restore_backup,
            '/set_default_backup': wu.backup_utils.set_default_backup,
            '/update_application': wu.backup_utils.update_application,
            # SYSTEM
            '/save_config': wu.system_utils.save_configuration,
            # BLUETOOTH
            '/connect_bluetooth': lambda d: (
                {"status": "error", "message": "Missing 'address' parameter"} if not d.get('address')
                else wu.bluetooth_utils.connect_bluetooth(d['address'])
            ),
            '/disconnect_bluetooth': lambda d: (
                {"status": "error", "message": "Missing 'address' parameter"} if not d.get('address')
                else wu.bluetooth_utils.disconnect_bluetooth(d['address'])
            ),
            '/forget_bluetooth': lambda d: (
                {"status": "error", "message": "Missing 'address' parameter"} if not d.get('address')
                else wu.bluetooth_utils.forget_bluetooth(d['address'])
            ),
            '/pair_bluetooth': lambda d: (
                {"status": "error", "message": "Missing 'address' parameter"} if not d.get('address')
                else wu.bluetooth_utils.pair_bluetooth(d['address'], d.get('pin'))
            ),
            '/trust_bluetooth': lambda d: (
                {"status": "error", "message": "Missing 'address' parameter"} if not d.get('address')
                else wu.bluetooth_utils.trust_bluetooth(d['address'])
            ),
            # SCRIPTS
            '/clear_script_output': wu.script_utils.clear_script_output,
            '/delete_script': wu.script_utils.delete_script,
            '/delete_custom_script': wu.script_utils.delete_custom_script,
            '/export_script_logs': wu.script_utils.export_script_logs,
            '/get_script_output': wu.script_utils.get_script_output,
            '/run_script': wu.script_utils.run_script,
            '/stop_script': wu.script_utils.stop_script,
            # CHARACTERS
            '/reload_fonts': getattr(wu.action_utils, 'reload_fonts', None),
            '/reload_images': getattr(wu.action_utils, 'reload_images', None),
            # COMMENTS
            '/delete_comment_section': wu.action_utils.delete_comment_section,
            '/restore_default_comments': wu.action_utils.restore_default_comments,
            '/save_comments': wu.action_utils.save_comments,
            # ATTACKS
            # STUDIO
            '/api/studio/action/replace': lambda d: wu.studio_utils.studio_replace_actions_with_db(),
            '/api/studio/action/update': wu.studio_utils.studio_update_action,
            '/api/studio/actions/sync': lambda d: wu.studio_utils.studio_sync_actions_studio(),
            '/api/studio/apply': lambda d: wu.studio_utils.studio_apply_to_runtime(),
            '/api/studio/edge/delete': wu.studio_utils.studio_delete_edge,
            '/api/studio/edge/upsert': wu.studio_utils.studio_upsert_edge,
            '/api/studio/host': wu.studio_utils.studio_upsert_host_flat,
            '/api/studio/host/delete': wu.studio_utils.studio_delete_host,
            '/api/studio/save': wu.studio_utils.studio_save_bundle,
            # ACTION
            # NETKB
            '/delete_all_actions': wu.netkb_utils.delete_all_actions,
            '/delete_netkb_action': wu.netkb_utils.delete_netkb_action,
            # ORCHESTRATOR
            '/manual_attack': wu.orchestrator_utils.execute_manual_attack,
            '/manual_scan': lambda d: wu.orchestrator_utils.execute_manual_scan(),
            '/start_orchestrator': lambda _: wu.orchestrator_utils.start_orchestrator(),
            '/stop_orchestrator': lambda _: wu.orchestrator_utils.stop_orchestrator(),
            # SENTINEL
            '/api/sentinel/toggle': wu.sentinel.toggle_sentinel,
            '/api/sentinel/ack': wu.sentinel.acknowledge_event,
            '/api/sentinel/clear': wu.sentinel.clear_events,
            '/api/sentinel/rule': wu.sentinel.upsert_rule,
            '/api/sentinel/rule/delete': wu.sentinel.delete_rule,
            '/api/sentinel/device': wu.sentinel.update_device,
            '/api/sentinel/notifiers': wu.sentinel.save_notifier_config,
            '/api/sentinel/analyze': wu.sentinel.analyze_events,
            '/api/sentinel/summarize': wu.sentinel.summarize_events,
            '/api/sentinel/suggest-rule': wu.sentinel.suggest_rule,
            # PLUGINS
            '/api/plugins/toggle': wu.plugin_utils.toggle_plugin,
            '/api/plugins/config': wu.plugin_utils.save_config,
            '/api/plugins/uninstall': wu.plugin_utils.uninstall_plugin,
            # BIFROST
            '/api/bifrost/toggle': wu.bifrost.toggle_bifrost,
            '/api/bifrost/mode': wu.bifrost.set_mode,
            '/api/bifrost/plugin/toggle': wu.bifrost.toggle_plugin,
            '/api/bifrost/activity/clear': wu.bifrost.clear_activity,
            '/api/bifrost/whitelist': wu.bifrost.update_whitelist,
            # LOKI
            '/api/loki/toggle': wu.loki.toggle_loki,
            '/api/loki/script/save': wu.loki.save_script,
            '/api/loki/script/delete': wu.loki.delete_script,
            '/api/loki/script/run': wu.loki.run_script,
            '/api/loki/job/cancel': wu.loki.cancel_job,
            '/api/loki/jobs/clear': wu.loki.clear_jobs,
            '/api/loki/quick': wu.loki.quick_type,
            '/api/loki/install': wu.loki.install_gadget,
            '/api/loki/reboot': wu.loki.reboot,
            # LLM Bridge
            '/api/llm/chat': wu.llm_utils.handle_chat,
            '/api/llm/clear_history': wu.llm_utils.clear_chat_history,
            '/api/llm/config': wu.llm_utils.save_llm_config,
            # MCP Server
            '/api/mcp/toggle': wu.llm_utils.toggle_mcp,
            '/api/mcp/config': wu.llm_utils.save_mcp_config,
            # Schedules & Triggers
            '/api/schedules/list': wu.schedule_utils.list_schedules,
            '/api/schedules/create': wu.schedule_utils.create_schedule,
            '/api/schedules/update': wu.schedule_utils.update_schedule,
            '/api/schedules/delete': wu.schedule_utils.delete_schedule,
            '/api/schedules/toggle': wu.schedule_utils.toggle_schedule,
            '/api/triggers/list': wu.schedule_utils.list_triggers,
            '/api/triggers/create': wu.schedule_utils.create_trigger,
            '/api/triggers/update': wu.schedule_utils.update_trigger,
            '/api/triggers/delete': wu.schedule_utils.delete_trigger,
            '/api/triggers/toggle': wu.schedule_utils.toggle_trigger,
            '/api/triggers/test': wu.schedule_utils.test_trigger,
            # Packages
            '/api/packages/uninstall': wu.package_utils.uninstall_package,
            '/api/packages/list': wu.package_utils.list_packages_json,
        }

        if debug_enabled:
            cls.POST_ROUTES_JSON.update({
                '/api/debug/tracemalloc': wu.debug_utils.toggle_tracemalloc,
                '/api/debug/gc/collect': wu.debug_utils.force_gc,
            })

        # --- POST ROUTES (JSON) - handler-aware: fn(handler, data) ---
        # These need the per-request handler instance (for send_response etc.)
        cls.POST_ROUTES_JSON_H = {
            '/api/bjorn/config': lambda h, d: wu.index_utils.set_config(h, d),
            '/api/bjorn/vulns/baseline': lambda h, _: wu.index_utils.mark_vuln_scan_baseline(h),
            '/api/rl/config': lambda h, d: wu.rl.set_mode(h, d),
            '/api/webenum/import': lambda h, d: wu.webenum_utils.import_webenum_results(h, d),
            # C2
            '/c2/broadcast': lambda h, d: wu.c2.c2_broadcast(h, d),
            '/c2/command': lambda h, d: wu.c2.c2_command(h, d),
            '/c2/deploy': lambda h, d: wu.c2.c2_deploy(h, d),
            '/c2/generate_client': lambda h, d: wu.c2.c2_generate_client(h, d),
            '/c2/purge_agents': lambda h, d: wu.c2.c2_purge_agents(h, d),
            '/c2/remove_client': lambda h, d: wu.c2.c2_remove_client(h, d),
            '/c2/start': lambda h, d: wu.c2.c2_start(h, d),
            '/c2/stop': lambda h, d: wu.c2.c2_stop(h, d),
            # SYSTEM (need handler for response)
            '/restart_bjorn_service': lambda h, _: wu.system_utils.restart_bjorn_service(h),
            '/reboot_system': lambda h, _: wu.system_utils.reboot_system(h),
            '/shutdown_system': lambda h, _: wu.system_utils.shutdown_system(h),
            '/initialize_csv': lambda h, _: wu.system_utils.initialize_db(h),
            '/restore_default_config': lambda h, _: wu.system_utils.restore_default_config(h),
            # VULN
            '/api/cve/bulk': lambda h, d: (wu.vuln_utils.serve_cve_bulk(h, d) or {"status": "ok"}),
            '/api/cve/bulk_exploits': lambda h, d: wu.vuln_utils.serve_cve_bulk(h, d),  # legacy alias
            '/api/feeds/sync': lambda h, _: wu.vuln_utils.serve_feed_sync(h),
            # DB (need handler for response)
            '/api/db/add_column': lambda h, d: wu.db_utils.db_add_column_endpoint(h, d),
            '/api/db/create_table': lambda h, d: wu.db_utils.db_create_table_endpoint(h, d),
            '/api/db/delete': lambda h, d: wu.db_utils.db_delete_rows_endpoint(h, d),
            '/api/db/insert': lambda h, d: wu.db_utils.db_insert_row_endpoint(h, d),
            '/api/db/rename_table': lambda h, d: wu.db_utils.db_rename_table_endpoint(h, d),
            '/api/db/update': lambda h, d: wu.db_utils.db_update_cells_endpoint(h, d),
            '/api/db/vacuum': lambda h, _: wu.db_utils.db_vacuum_endpoint(h),
            # ACTION
            '/create_character': lambda h, d: wu.action_utils.create_character(h, d),
            '/switch_character': lambda h, d: wu.action_utils.switch_character(h, d),
            '/delete_character': lambda h, d: wu.action_utils.delete_character(h, d),
            '/rename_image': lambda h, d: wu.action_utils.rename_image(h, d),
            '/remove_attack': lambda h, d: wu.action_utils.remove_attack(h, d),
            '/restore_attack': lambda h, d: wu.action_utils.restore_attack(h, d),
            '/save_attack': lambda h, d: wu.action_utils.save_attack(h, d),
            '/action/delete': lambda h, d: wu.action_utils.delete_action(h, d),
            '/actions/restore_defaults': lambda h, _: wu.action_utils.restore_defaults(h),
            '/actions/set_enabled': lambda h, d: wu.action_utils.set_action_enabled(h, d),
            # EPD Layout
            '/api/epd/layout': lambda h, d: wu.system_utils.epd_save_layout(h, d),
            '/api/epd/layout/reset': lambda h, d: wu.system_utils.epd_reset_layout(h, d),

            # Legacy aliases
            'reboot': lambda h, _: wu.system_utils.reboot_system(h),
            'shutdown': lambda h, _: wu.system_utils.shutdown_system(h),
        }

        cls._routes_initialized = True
        if debug_enabled:
            logger.info("Routes registered (once). Bjorn Debug API enabled.")
        else:
            logger.info("Routes registered (once). Bjorn Debug API disabled.")

    # ------------------------------------------------------------------------
    # HELPER HANDLERS
    # ------------------------------------------------------------------------
    
    def _serve_web_delay(self, handler):
        self._send_json({"web_delay": self.shared_data.web_delay})

    def _serve_running_scripts(self, handler):
        self._send_json(self.web_utils.script_utils.get_running_scripts())

    def _serve_list_scripts(self, handler):
        self._send_json(self.web_utils.script_utils.list_scripts())

    def _serve_action_args_schema(self, handler):
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(self.path).query)
        action_name = query.get('action_name', [''])[0]
        self._send_json(self.web_utils.script_utils.get_action_args_schema({"action_name": action_name}))

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

    # ... [Authentication helpers] ...
    def delete_cookie(self, key, path='/'):
        self.set_cookie(key, '', path=path, max_age=0)

    def get_cookie(self, key):
        if "Cookie" in self.headers:
            cookie = cookies.SimpleCookie(self.headers["Cookie"])
            if key in cookie:
                return cookie[key].value
        return None

    def is_authenticated(self):
        if not self.shared_data.webauth:
            return True
        token = self.get_cookie('bjorn_session')
        return _validate_session_token(token) if token else False

    def set_cookie(self, key, value, path='/', max_age=None):
        cookie = cookies.SimpleCookie()
        cookie[key] = value
        cookie[key]['path'] = path
        cookie[key]['httponly'] = True
        cookie[key]['samesite'] = 'Strict'
        if max_age is not None:
            cookie[key]['max-age'] = max_age
        self.send_header('Set-Cookie', cookie.output(header='', sep=''))

    # ... [Compression helpers] ...
    def gzip_encode(self, content):
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="w") as f:
            f.write(content)
        return out.getvalue()

    def send_gzipped_response(self, content, content_type):
        gzipped_content = self.gzip_encode(content)
        self.send_response(200)
        self.send_header("Content-type", content_type)
        self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(gzipped_content)))
        self.end_headers()
        self.wfile.write(gzipped_content)

    def serve_file_gzipped(self, file_path, content_type):
        if os.path.exists(file_path):
            with open(file_path, 'rb') as file:
                content = file.read()
            self.send_gzipped_response(content, content_type)
        else:
            self.send_error(404)

    # ... [Login/Logout handlers] ...
    def handle_login(self):
        if not self.shared_data.webauth:
            self.send_response(302); self.send_header('Location', '/'); self.end_headers(); return

        content_length = int(self.headers.get('Content-Length', 0))
        # Protect against large POST payloads on login
        if content_length > MAX_POST_SIZE:
            self.send_error(413)
            return

        post_data = self.rfile.read(content_length).decode('utf-8')
        params = urllib.parse.parse_qs(post_data)

        username = params.get('username', [None])[0]
        password = params.get('password', [None])[0]

        try:
            with open(self.shared_data.webapp_json, 'r', encoding='utf-8') as f:
                auth_config = json.load(f)
                expected_user = auth_config.get('username', '')
        except Exception as e:
            logger.error(f"Error loading webapp.json: {e}")
            self.send_error(500)
            return

        # Verify password: support both hashed (new) and plaintext (legacy fallback)
        password_ok = False
        if 'password_hash' in auth_config and 'password_salt' in auth_config:
            # Hashed password (migrated)
            password_ok = _verify_password(
                password or '',
                auth_config['password_hash'],
                auth_config['password_salt']
            )
        elif 'password' in auth_config:
            # Legacy plaintext (auto-migrate now)
            password_ok = (password == auth_config['password'])
            if password_ok:
                # Auto-migrate to hashed on successful login
                _ensure_password_hashed(self.shared_data.webapp_json)

        if username == expected_user and password_ok:
            always_auth = params.get('alwaysAuth', [None])[0] == 'on'
            try:
                with open(self.shared_data.webapp_json, 'r+', encoding='utf-8') as f:
                    config = json.load(f)
                    config['always_require_auth'] = always_auth
                    f.seek(0)
                    json.dump(config, f, indent=4)
                    f.truncate()
            except Exception as e:
                logger.error(f"Error saving auth preference: {e}")

            # Create HMAC-signed session token (server-validated)
            token = _make_session_token()
            if not always_auth:
                self.set_cookie('bjorn_session', token, max_age=30*24*60*60)
            else:
                self.set_cookie('bjorn_session', token)

            self.send_response(302)
            self.send_header('Location', '/')
            self.end_headers()
        else:
            self.send_error(401, "Unauthorized")

    def handle_logout(self):
        if not self.shared_data.webauth:
            self.send_response(302); self.send_header('Location', '/'); self.end_headers(); return
        # Server-side session invalidation
        token = self.get_cookie('bjorn_session')
        if token:
            _revoke_session_token(token)
        self.send_response(302)
        self.delete_cookie('bjorn_session')
        self.send_header('Location', '/login.html')
        self.end_headers()

    def serve_login_page(self):
        login_page_path = os.path.join(self.shared_data.web_dir, 'login.html')
        self.serve_file_gzipped(login_page_path, 'text/html')
        
    def log_message(self, format, *args):
            """
            Filter noisy web server logs. Suppresses repetitive polling requests.
            """
            if not self.shared_data.config.get("web_logging_enabled", False):
                return

            msg = format % args

            # High-frequency polling routes to suppress from logs
            silent_routes = [
                "/api/bjorn/stats",
                "/bjorn_status",
                "/bjorn_status_image",
                "/bjorn_character",
                "/bjorn_say",
                "/netkb_data",
                "/web/screen.png",
                "/action_queue",
                "/api/rl/stats",
                "/api/rl/config",
                "/api/rl/experiences",
                "/api/rl/history",
            ]

            # If any silent route matches, skip logging
            if any(route in msg for route in silent_routes):
                return

            # Log everything else (errors, connections, config changes)
            logger.info("%s - [%s] %s" % (
                self.client_address[0],
                self.log_date_time_string(),
                msg
            ))
        
    # ------------------------------------------------------------------------
    # DELETE REQUEST HANDLER
    # ------------------------------------------------------------------------
    @staticmethod
    def _is_valid_mac(mac):
        """Validate MAC address format (XX:XX:XX:XX:XX:XX or XX-XX-XX-XX-XX-XX)."""
        import re
        if not mac or not isinstance(mac, str):
            return False
        return bool(re.fullmatch(r'([0-9a-fA-F]{2}[:\-]){5}[0-9a-fA-F]{2}', mac))

    def do_DELETE(self):
        if self.shared_data.webauth and not self.is_authenticated():
            self._send_json({"status": "error", "message": "Unauthorized"}, 401)
            return

        try:
            if self.path.startswith('/api/studio/host/'):
                mac = unquote(self.path.split('/api/studio/host/')[-1])
            elif self.path.startswith('/studio/host/'):
                mac = unquote(self.path.split('/studio/host/')[-1])
            else:
                super().do_GET()
                return

            if not self._is_valid_mac(mac):
                self._send_json({"status": "error", "message": "Invalid MAC address format"}, 400)
                return

            resp = self.web_utils.studio_utils.studio_delete_host({"mac_address": mac})
            status_code = 400 if resp.get("status") == "error" else 200
            self._send_json(resp, status_code)
        except Exception as e:
            logger.error(f"DELETE error: {e}")
            self._send_json({"status": "error", "message": str(e)}, 500)

    # ------------------------------------------------------------------------
    # GET REQUEST HANDLER
    # ------------------------------------------------------------------------
    def do_GET(self):
        # Clean path for routing (strip query string)
        path_clean = self.path.split('?')[0]
        
        legacy_page_redirects = {
            '/index.html': '/#/dashboard',
            '/bjorn.html': '/#/bjorn',
            '/netkb.html': '/#/netkb',
            '/network.html': '/#/network',
            '/credentials.html': '/#/credentials',
            '/vulnerabilities.html': '/#/vulnerabilities',
            '/attacks.html': '/#/attacks',
            '/scheduler.html': '/#/scheduler',
            '/database.html': '/#/database',
            '/files_explorer.html': '/#/files',
            '/loot.html': '/#/loot',
            '/actions_launcher.html': '/#/actions',
            '/actions_studio.html': '/#/actions-studio',
            '/backup_update.html': '/#/backup',
            '/web_enum.html': '/#/web-enum',
            '/zombieland.html': '/#/zombieland',
        }

        if path_clean in legacy_page_redirects:
            self.send_response(302)
            self.send_header('Location', legacy_page_redirects[path_clean])
            self.end_headers()
            return

        # Public assets
        public_paths = [
            '/apple-touch-icon', '/favicon.ico', '/manifest.json',
            '/static/', '/web/css/', '/web/images/', '/web/js/',
            '/web/i18n/', 
            '/web_old/', 
        ]
        if self.shared_data.webauth:
            public_paths.extend(['/login', '/login.html', '/logout'])
        
        # Bypass auth for public paths
        if any(path_clean.startswith(p) for p in public_paths):
            if self.shared_data.webauth:
                if path_clean in ['/login', '/login.html']:
                    self.serve_login_page()
                    return
                elif path_clean == '/logout':
                    self.handle_logout()
                    return

            # Serve legacy files from an absolute path (independent of process CWD)
            if path_clean.startswith('/web_old/'):
                rel = path_clean.lstrip('/')
                file_path = os.path.join(self.shared_data.current_dir, rel)
                if os.path.isfile(file_path):
                    content_type = self.guess_type(file_path) or 'application/octet-stream'
                    self.serve_file_gzipped(file_path, content_type)
                    return
                self.send_error(404, "File not found.")
                return

            super().do_GET()
            return
                
        # Enforce auth
        if self.shared_data.webauth and not self.is_authenticated():
            self.send_response(302)
            self.send_header('Location', '/login.html')
            self.end_headers()
            return

        # Serve web/index.html for /
        if path_clean == '/':
            index_path = os.path.join(self.shared_data.web_dir, 'index.html')
            self.serve_file_gzipped(index_path, 'text/html')
            return

        # --- DYNAMIC ROUTING MATCHING ---
        
        # 1. Exact match
        if path_clean in self.GET_ROUTES:
            handler_or_name = self.GET_ROUTES[path_clean]
            # String = instance method name (resolved per-request, avoids lambda)
            if isinstance(handler_or_name, str):
                getattr(self, handler_or_name)(self)
            else:
                handler_or_name(self)
            return

        # 2. Prefix match (for routes with params in path)
        if self.path.startswith('/c2/download_client/'):
            filename = unquote(self.path.split('/c2/download_client/')[-1])
            self.web_utils.c2.c2_download_client(self, filename)
            return
        elif self.path.startswith('/api/llm/models'):
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            params = {k: v[0] for k, v in query.items()}
            self.web_utils.llm_utils.get_llm_models(self, params)
            return
        elif self.path.startswith('/c2/stale_agents'):
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            threshold = int(query.get("threshold", [300])[0])
            self.web_utils.c2.c2_stale_agents(self, threshold)
            return
        elif self.path.startswith('/api/webenum/results'):
            self.web_utils.webenum_utils.serve_webenum_data(self)
            return
        elif self.path.startswith('/download_file'):
            self.web_utils.file_utils.download_file(self)
            return
        elif self.path.startswith('/list_files'):
            self.web_utils.file_utils.list_files_endpoint(self)
            return
        elif self.path.startswith('/loot_download'):
            self.web_utils.file_utils.loot_download(self)
            return
        elif self.path.startswith('/download_backup'):
            self.web_utils.backup_utils.download_backup(self)
            return
        elif self.path.startswith('/get_script_output/'):
            script_name = unquote(self.path.split('/')[-1])
            response = self.web_utils.script_utils.get_script_output({"script_name": script_name})
            self._send_json(response)
            return
        elif self.path.startswith('/get_action_images?'):
            self.web_utils.action_utils.get_action_images(self)
            return
        elif self.path.startswith('/get_status_icon?'):
            self.web_utils.action_utils.get_status_icon(self)
            return
        elif self.path.startswith('/images/status/'):
            self.web_utils.action_utils.serve_status_image(self)
            return
        elif self.path.startswith('/list_static_images_with_dimensions'):
            self.web_utils.action_utils.list_static_images_with_dimensions(self)
            return
        elif self.path.startswith('/screen.png'):
            self.web_utils.action_utils.serve_image(self)
            return
        elif self.path.startswith('/static_images/'):
            self.web_utils.action_utils.serve_static_image(self)
            return
        elif self.path.startswith('/bjorn_status_image'):
            self.web_utils.action_utils.serve_bjorn_status_image(self)
            return
        elif self.path.startswith('/get_character_icon'):
            self.web_utils.action_utils.get_character_icon(self)
            return
        elif self.path.startswith('/get_character_image?'):
            self.web_utils.action_utils.get_character_image(self)
            return
        elif self.path.startswith('/bjorn_character'):
            fn = getattr(self.web_utils.action_utils, 'serve_bjorn_character', self.web_utils.action_utils.serve_bjorn_status_image)
            fn(self)
            return
        elif self.path.startswith('/get_comments?'):
            self.web_utils.action_utils.get_comments(self)
            return
        elif self.path.startswith('/get_attack_content'):
            self.web_utils.action_utils.get_attack_content(self)
            return
        elif self.path.startswith('/get_attacks'):
            self.web_utils.action_utils.get_attacks(self)
            return
        elif self.path.startswith('/actions_icons'):
            self.web_utils.action_utils.serve_actions_icons(self)
            return
        elif self.path.startswith('/list_vulnerabilities'):
            if '?' in self.path and 'page=' in self.path:
                self.web_utils.vuln_utils.serve_vulns_data_optimized(self)
            else:
                self.web_utils.vuln_utils.serve_vulns_data(self)
            return
        elif self.path.startswith('/vulnerabilities/history'):
            self.web_utils.vuln_utils.serve_vuln_history(self)
            return
        elif self.path.startswith('/api/cve/'):
            cve_id = self.path.split('/api/cve/')[-1].split('?')[0]
            self.web_utils.vuln_utils.serve_cve_details(self, cve_id)
            return
        elif self.path.startswith('/api/exploitdb/'):
            cve_id = self.path.split('/api/exploitdb/')[-1].split('?')[0]
            self.web_utils.vuln_utils.serve_exploitdb_by_cve(self, cve_id)
            return
        elif self.path.startswith('/api/studio/hosts'):
            self.web_utils.studio_utils.studio_get_hosts(self)
            return
        elif self.path.startswith('/api/studio/layout'):
            self.web_utils.studio_utils.studio_load_layout(self)
            return
        elif self.path.startswith('/api/db/export/'):
            table_name = unquote(self.path.split('/api/db/export/', 1)[1].split('?', 1)[0])
            self.web_utils.db_utils.db_export_table_endpoint(self, table_name)
            return
        elif self.path.startswith('/api/db/schema/'):
            name = unquote(self.path.split('/api/db/schema/', 1)[1])
            self.web_utils.db_utils.db_schema_endpoint(self, name)
            return
        elif self.path.startswith('/api/db/table/'):
            table_name = unquote(self.path.split('/api/db/table/', 1)[1].split('?', 1)[0])
            self.web_utils.db_utils.db_get_table_endpoint(self, table_name)
            return
        elif self.path.startswith('/attempt_history'):
            self.web_utils.netkb_utils.serve_attempt_history(self)
            return
        elif self.path.startswith('/action_queue'):
            self.web_utils.netkb_utils.serve_action_queue(self)
            return
        elif self.path.startswith('/api/packages/install'):
            self.web_utils.package_utils.install_package(self)
            return

        super().do_GET()

    # ------------------------------------------------------------------------
    # POST REQUEST HANDLER
    # ------------------------------------------------------------------------
    def do_POST(self):
        # Handle Auth
        if self.path == '/login' and self.shared_data.webauth:
            self.handle_login()
            return
        elif self.path == '/logout' and self.shared_data.webauth:
            self.handle_logout()
            return
        
        if self.shared_data.webauth and not self.is_authenticated():
            self.send_error(401)
            return

        # Special Route
        if self.path == '/queue_cmd':
            self.web_utils.netkb_utils.handle_queue_cmd(self)
            return

        try:
            # 1. MULTIPART ROUTES
            if self.path in self.POST_ROUTES_MULTIPART:
                self.POST_ROUTES_MULTIPART[self.path](self)
                return

            # 2. JSON ROUTES
            content_length = int(self.headers.get('Content-Length', 0))
            
            # GUARD: Max size check for JSON payloads too
            if content_length > MAX_POST_SIZE:
                self.send_error(413)
                return
            
            body = self.rfile.read(content_length) if content_length > 0 else b'{}'
            
            # Guard against multipart mistakenly sent as generic post
            content_type = self.headers.get('Content-Type', '')
            if content_type.startswith('multipart/form-data'):
                self._send_json({"status": "error", "message": "Unexpected multipart/form-data"}, 400)
                return

            data = json.loads(body)

            # Special case for livestatus
            if self.path == '/clear_livestatus':
                restart = data.get("restart", True)
                self.web_utils.system_utils.clear_livestatus(self, restart=restart)
                return

            # Dynamic Dispatch for JSON - data-only handlers
            if self.path in self.POST_ROUTES_JSON:
                handler = self.POST_ROUTES_JSON[self.path]
                if callable(handler):
                    response = handler(data)
                    if response is not None:
                        status_code = 400 if isinstance(response, dict) and response.get("status") == "error" else 200
                        self._send_json(response, status_code)
                    return

            # Dynamic Dispatch for JSON - handler-aware: fn(handler, data)
            if self.path in self.POST_ROUTES_JSON_H:
                handler_fn = self.POST_ROUTES_JSON_H[self.path]
                if callable(handler_fn):
                    response = handler_fn(self, data)
                    if response is not None:
                        status_code = 400 if isinstance(response, dict) and response.get("status") == "error" else 200
                        self._send_json(response, status_code)
                    return

            # Path params routes (DB)
            if self.path.startswith('/api/db/drop/'):
                table_name = unquote(self.path.split('/api/db/drop/', 1)[1])
                self.web_utils.db_utils.db_drop_table_endpoint(self, table_name)
                return
            elif self.path.startswith('/api/db/drop_view/'):
                view_name = unquote(self.path.split('/api/db/drop_view/', 1)[1])
                self.web_utils.db_utils.db_drop_view_endpoint(self, view_name)
                return
            elif self.path.startswith('/api/db/truncate/'):
                table_name = unquote(self.path.split('/api/db/truncate/', 1)[1])
                self.web_utils.db_utils.db_truncate_table_endpoint(self, table_name)
                return

            # 404
            self._send_json({"status": "error", "message": "Route not found"}, 404)

        except json.JSONDecodeError:
            self._send_json({"status": "error", "message": "Invalid JSON format"}, 400)
        except Exception as e:
            logger.error(f"Error handling POST request: {e}")
            self._send_json({"status": "error", "message": str(e)}, 500)


# ============================================================================
# WEB SERVER THREAD
# ============================================================================

class WebThread(threading.Thread):
    """
    Threaded web server with automatic port conflict resolution and timeouts.
    Handles graceful shutdown and server lifecycle.
    """
    
    def __init__(self, port=8000):
        super().__init__(name="WebThread", daemon=True)
        self.shared_data = shared_data
        self.initial_port = port
        self.current_port = port
        self.httpd = None

    def setup_server(self):
            max_retries = 10
            retry_count = 0
            
            while retry_count < max_retries:
                try:
                    # Define server class with timeout logic
                    class ThreadedTCPServer(socketserver.ThreadingTCPServer):
                        allow_reuse_address = True
                        daemon_threads = True # Prevents zombie processes
                        request_queue_size = 16  # Limit pending connections backlog

                        # Limit concurrent handler threads to prevent RAM exhaustion on Pi Zero 2
                        _max_threads = 20
                        _thread_semaphore = threading.BoundedSemaphore(_max_threads)

                        def process_request(self, request, client_address):
                            if not self._thread_semaphore.acquire(blocking=True, timeout=5.0):
                                # All slots busy - reject to protect RAM
                                try:
                                    request.close()
                                except Exception:
                                    pass
                                return
                            super().process_request(request, client_address)

                        def process_request_thread(self, request, client_address):
                            try:
                                super().process_request_thread(request, client_address)
                            finally:
                                self._thread_semaphore.release()

                        # Timeout logic to kill hanging connections (critical for Pi Zero)
                        def finish_request(self, request, client_address):
                            request.settimeout(10.0)
                            super().finish_request(request, client_address)

                    # Instantiate server
                    server = ThreadedTCPServer(("", self.current_port), CustomHandler)
                    
                    # Apply socket options
                    server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if hasattr(socket, "SO_REUSEPORT"):
                        try:
                            server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                        except: pass
                    
                    return server
                    
                except OSError as e:
                    if e.errno == 98:  # Address already in use
                        retry_count += 1
                        logger.warning(f"Port {self.current_port} busy, trying next...")
                        time.sleep(0.5)
                        self.current_port += 1
                    else:
                        raise

            raise RuntimeError(f"Unable to start server after {max_retries} attempts")

    def run(self):
        # Start the script scheduler daemon
        try:
            from script_scheduler import ScriptSchedulerDaemon
            daemon = ScriptSchedulerDaemon(self.shared_data)
            self.shared_data.script_scheduler = daemon
            daemon.start()
            logger.info("ScriptSchedulerDaemon started")
        except Exception as e:
            logger.warning(f"Failed to start ScriptSchedulerDaemon: {e}")

        while not self.shared_data.webapp_should_exit:
            try:
                self.httpd = self.setup_server()
                logger.info(f"Server started on port {self.current_port}")
                self.httpd.serve_forever()
            except Exception as e:
                logger.error(f"Server error: {e}")
                if self.httpd:
                    self.httpd.server_close()
                time.sleep(2)

    def shutdown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            logger.info("Web server stopped.")


def handle_exit_web(signum, frame):
    shared_data.webapp_should_exit = True
    if web_thread.is_alive():
        web_thread.shutdown()
    sys.exit(0)


web_thread = WebThread(port=8000)

if __name__ == "__main__":
    try:
        signal.signal(signal.SIGINT, handle_exit_web)
        signal.signal(signal.SIGTERM, handle_exit_web)
        web_thread.start()
        logger.info("Web server thread started.")
        while True:
            time.sleep(1)
    except Exception as e:
        logger.error(f"An exception occurred during web server start: {e}")
        handle_exit_web(signal.SIGINT, None)
        sys.exit(1)
