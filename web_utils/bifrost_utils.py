"""bifrost_utils.py - Bifrost web API endpoints."""
import json
import logging
from typing import Dict
from urllib.parse import urlparse, parse_qs

from logger import Logger

logger = Logger(name="bifrost_utils", level=logging.DEBUG)


class BifrostUtils:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    @property
    def _engine(self):
        return getattr(self.shared_data, 'bifrost_engine', None)

    # ── GET endpoints (handler signature) ─────────────────────

    def get_status(self, handler):
        """GET /api/bifrost/status - full engine state."""
        engine = self._engine
        if engine:
            data = engine.get_status()
        else:
            data = {
                'enabled': False, 'running': False,
                'mood': 'sleeping', 'face': '(-.-) zzZ', 'voice': '',
                'channel': 0, 'num_aps': 0, 'num_handshakes': 0,
                'uptime': 0, 'epoch': 0, 'mode': 'auto',
                'last_pwnd': '', 'reward': 0,
            }
        self._send_json(handler, data)

    def get_networks(self, handler):
        """GET /api/bifrost/networks - discovered WiFi networks."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM bifrost_networks ORDER BY rssi DESC LIMIT 200"
            ) or []
            self._send_json(handler, {'networks': rows})
        except Exception as e:
            logger.error("get_networks error: %s", e)
            self._send_json(handler, {'networks': []})

    def get_handshakes(self, handler):
        """GET /api/bifrost/handshakes - captured handshakes."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM bifrost_handshakes ORDER BY captured_at DESC LIMIT 200"
            ) or []
            self._send_json(handler, {'handshakes': rows})
        except Exception as e:
            logger.error("get_handshakes error: %s", e)
            self._send_json(handler, {'handshakes': []})

    def get_activity(self, handler):
        """GET /api/bifrost/activity - recent activity feed."""
        try:
            qs = parse_qs(urlparse(handler.path).query)
            limit = int(qs.get('limit', [50])[0])
            rows = self.shared_data.db.query(
                "SELECT * FROM bifrost_activity ORDER BY timestamp DESC LIMIT ?",
                (limit,)
            ) or []
            self._send_json(handler, {'activity': rows})
        except Exception as e:
            logger.error("get_activity error: %s", e)
            self._send_json(handler, {'activity': []})

    def get_epochs(self, handler):
        """GET /api/bifrost/epochs - epoch history."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM bifrost_epochs ORDER BY id DESC LIMIT 100"
            ) or []
            self._send_json(handler, {'epochs': rows})
        except Exception as e:
            logger.error("get_epochs error: %s", e)
            self._send_json(handler, {'epochs': []})

    def get_stats(self, handler):
        """GET /api/bifrost/stats - aggregate statistics."""
        try:
            db = self.shared_data.db
            nets = db.query_one("SELECT COUNT(*) AS c FROM bifrost_networks") or {}
            shakes = db.query_one("SELECT COUNT(*) AS c FROM bifrost_handshakes") or {}
            epochs = db.query_one("SELECT COUNT(*) AS c FROM bifrost_epochs") or {}
            deauths = db.query_one(
                "SELECT COALESCE(SUM(num_deauths),0) AS c FROM bifrost_epochs"
            ) or {}
            assocs = db.query_one(
                "SELECT COALESCE(SUM(num_assocs),0) AS c FROM bifrost_epochs"
            ) or {}
            peers = db.query_one("SELECT COUNT(*) AS c FROM bifrost_peers") or {}
            self._send_json(handler, {
                'total_networks': int(nets.get('c', 0)),
                'total_handshakes': int(shakes.get('c', 0)),
                'total_epochs': int(epochs.get('c', 0)),
                'total_deauths': int(deauths.get('c', 0)),
                'total_assocs': int(assocs.get('c', 0)),
                'total_peers': int(peers.get('c', 0)),
            })
        except Exception as e:
            logger.error("get_stats error: %s", e)
            self._send_json(handler, {
                'total_networks': 0, 'total_handshakes': 0,
                'total_epochs': 0, 'total_deauths': 0,
                'total_assocs': 0, 'total_peers': 0,
            })

    def get_plugins(self, handler):
        """GET /api/bifrost/plugins - loaded plugin list."""
        try:
            from bifrost.plugins import get_loaded_info
            self._send_json(handler, {'plugins': get_loaded_info()})
        except Exception as e:
            logger.error("get_plugins error: %s", e)
            self._send_json(handler, {'plugins': []})

    # ── POST endpoints (JSON data signature) ──────────────────

    def toggle_bifrost(self, data: Dict) -> Dict:
        """POST /api/bifrost/toggle - switch to/from BIFROST mode.

        BIFROST is a 4th exclusive operation mode. Enabling it stops the
        orchestrator (Manual/Auto/AI) because WiFi goes into monitor mode.
        Disabling it returns to the previous mode (defaults to AUTO).
        """
        enabled = bool(data.get('enabled', False))
        if enabled:
            # Switch to BIFROST mode (stops orchestrator, starts engine)
            self.shared_data.operation_mode = "BIFROST"
        else:
            # Leave BIFROST → return to AUTO (safest default)
            self.shared_data.operation_mode = "AUTO"
        return {'status': 'ok', 'enabled': enabled}

    def set_mode(self, data: Dict) -> Dict:
        """POST /api/bifrost/mode - set auto/manual."""
        mode = data.get('mode', 'auto')
        engine = self._engine
        if engine and engine.agent:
            engine.agent.mode = mode
        return {'status': 'ok', 'mode': mode}

    def toggle_plugin(self, data: Dict) -> Dict:
        """POST /api/bifrost/plugin/toggle - enable/disable a plugin."""
        try:
            from bifrost.plugins import toggle_plugin
            name = data.get('name', '')
            enable = bool(data.get('enabled', True))
            changed = toggle_plugin(name, enable)
            return {'status': 'ok', 'changed': changed}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def clear_activity(self, data: Dict) -> Dict:
        """POST /api/bifrost/activity/clear - clear activity log."""
        try:
            self.shared_data.db.execute("DELETE FROM bifrost_activity")
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    def update_whitelist(self, data: Dict) -> Dict:
        """POST /api/bifrost/whitelist - update AP whitelist."""
        try:
            whitelist = data.get('whitelist', '')
            self.shared_data.config['bifrost_whitelist'] = whitelist
            return {'status': 'ok'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}

    # ── Helpers ───────────────────────────────────────────────

    def _send_json(self, handler, data, status=200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode("utf-8"))
