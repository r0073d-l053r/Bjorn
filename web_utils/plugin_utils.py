"""plugin_utils.py - Plugin management web API endpoints."""

import json
import logging
from urllib.parse import parse_qs, urlparse

from logger import Logger

logger = Logger(name="plugin_utils", level=logging.DEBUG)


class PluginUtils:
    """Web API handlers for plugin management."""

    def __init__(self, shared_data):
        self.shared_data = shared_data

    @property
    def _mgr(self):
        return getattr(self.shared_data, 'plugin_manager', None)

    def _write_json(self, handler, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        try:
            handler.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── GET endpoints ────────────────────────────────────────────────

    def list_plugins(self, handler):
        """GET /api/plugins/list - All plugins with status."""
        try:
            mgr = self._mgr
            if not mgr:
                self._write_json(handler, {"status": "ok", "data": []})
                return

            plugins = mgr.get_all_status()
            self._write_json(handler, {"status": "ok", "data": plugins})
        except Exception as e:
            logger.error(f"list_plugins failed: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    def get_plugin_config(self, handler):
        """GET /api/plugins/config?id=<plugin_id> - Config schema + current values."""
        try:
            query = urlparse(handler.path).query
            params = parse_qs(query)
            plugin_id = params.get("id", [None])[0]

            if not plugin_id:
                self._write_json(handler, {"status": "error", "message": "Missing 'id' parameter"}, 400)
                return

            mgr = self._mgr
            if not mgr:
                self._write_json(handler, {"status": "error", "message": "Plugin manager not available"}, 503)
                return

            # Get metadata for schema
            meta = mgr._meta.get(plugin_id)
            if not meta:
                # Try to load from DB
                db_rec = self.shared_data.db.get_plugin_config(plugin_id)
                if db_rec:
                    meta = db_rec.get("meta", {})
                else:
                    self._write_json(handler, {"status": "error", "message": "Plugin not found"}, 404)
                    return

            schema = meta.get("config_schema", {})
            current_values = mgr.get_config(plugin_id)

            self._write_json(handler, {
                "status": "ok",
                "plugin_id": plugin_id,
                "schema": schema,
                "values": current_values,
            })
        except Exception as e:
            logger.error(f"get_plugin_config failed: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    def get_plugin_logs(self, handler):
        """GET /api/plugins/logs?id=<plugin_id> - Recent log lines (placeholder)."""
        try:
            query = urlparse(handler.path).query
            params = parse_qs(query)
            plugin_id = params.get("id", [None])[0]

            if not plugin_id:
                self._write_json(handler, {"status": "error", "message": "Missing 'id' parameter"}, 400)
                return

            # For now, return empty — full log filtering can be added later
            # by filtering the main log file for [plugin.<plugin_id>] entries
            self._write_json(handler, {
                "status": "ok",
                "plugin_id": plugin_id,
                "logs": [],
                "message": "Log filtering available via console SSE with [plugin.{id}] prefix"
            })
        except Exception as e:
            logger.error(f"get_plugin_logs failed: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    # ── POST endpoints (JSON body) ───────────────────────────────────

    def toggle_plugin(self, data: dict) -> dict:
        """POST /api/plugins/toggle - {id, enabled}"""
        try:
            plugin_id = data.get("id")
            enabled = data.get("enabled")

            if not plugin_id:
                return {"status": "error", "message": "Missing 'id' parameter"}
            if enabled is None:
                return {"status": "error", "message": "Missing 'enabled' parameter"}

            mgr = self._mgr
            if not mgr:
                return {"status": "error", "message": "Plugin manager not available"}

            mgr.toggle_plugin(plugin_id, bool(int(enabled)))

            return {
                "status": "ok",
                "plugin_id": plugin_id,
                "enabled": bool(int(enabled)),
            }
        except Exception as e:
            logger.error(f"toggle_plugin failed: {e}")
            return {"status": "error", "message": "Internal server error"}

    def save_config(self, data: dict) -> dict:
        """POST /api/plugins/config - {id, config: {...}}"""
        try:
            plugin_id = data.get("id")
            config = data.get("config")

            if not plugin_id:
                return {"status": "error", "message": "Missing 'id' parameter"}
            if config is None or not isinstance(config, dict):
                return {"status": "error", "message": "Missing or invalid 'config' parameter"}

            mgr = self._mgr
            if not mgr:
                return {"status": "error", "message": "Plugin manager not available"}

            mgr.save_config(plugin_id, config)

            return {"status": "ok", "plugin_id": plugin_id}
        except ValueError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            logger.error(f"save_config failed: {e}")
            return {"status": "error", "message": "Internal server error"}

    def uninstall_plugin(self, data: dict) -> dict:
        """POST /api/plugins/uninstall - {id}"""
        try:
            plugin_id = data.get("id")
            if not plugin_id:
                return {"status": "error", "message": "Missing 'id' parameter"}

            mgr = self._mgr
            if not mgr:
                return {"status": "error", "message": "Plugin manager not available"}

            return mgr.uninstall(plugin_id)
        except Exception as e:
            logger.error(f"uninstall_plugin failed: {e}")
            return {"status": "error", "message": "Internal server error"}

    # ── MULTIPART endpoints ──────────────────────────────────────────

    def install_plugin(self, handler):
        """POST /api/plugins/install - multipart upload of .zip"""
        try:
            mgr = self._mgr
            if not mgr:
                self._write_json(handler, {"status": "error", "message": "Plugin manager not available"}, 503)
                return

            content_type = handler.headers.get('Content-Type', '')
            content_length = int(handler.headers.get('Content-Length', 0))

            if content_length <= 0 or content_length > 10 * 1024 * 1024:  # 10MB max
                self._write_json(handler, {"status": "error", "message": "Invalid file size (max 10MB)"}, 400)
                return

            body = handler.rfile.read(content_length)

            # Extract zip bytes from multipart form data
            zip_bytes = None
            if 'multipart' in content_type:
                boundary = content_type.split('boundary=')[1].encode() if 'boundary=' in content_type else None
                if boundary:
                    parts = body.split(b'--' + boundary)
                    for part in parts:
                        if b'filename=' in part and b'.zip' in part.lower():
                            # Extract file data after double CRLF
                            if b'\r\n\r\n' in part:
                                zip_bytes = part.split(b'\r\n\r\n', 1)[1].rstrip(b'\r\n--')
                                break

            if not zip_bytes:
                # Maybe raw zip upload (no multipart)
                if body[:4] == b'PK\x03\x04':
                    zip_bytes = body
                else:
                    self._write_json(handler, {"status": "error", "message": "No .zip file found in upload"}, 400)
                    return

            result = mgr.install_from_zip(zip_bytes)
            status_code = 200 if result.get("status") == "ok" else 400
            self._write_json(handler, result, status_code)

        except Exception as e:
            logger.error(f"install_plugin failed: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)
