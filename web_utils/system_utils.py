# web_utils/system_utils.py
"""
System utilities for management operations.
Handles system commands, service management, configuration.
"""
from __future__ import annotations
import json
import subprocess
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from logger import Logger


logger = Logger(name="system_utils.py", level=logging.DEBUG)

class SystemUtils:
    """Utilities for system-level operations."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def _send_json(self, handler, data, status=200):
        """Send a JSON response (helper to reduce boilerplate)."""
        handler.send_response(status)
        handler.send_header("Content-type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode('utf-8'))

    def reboot_system(self, handler):
        """Reboot the system."""
        try:
            subprocess.Popen(["sudo", "reboot"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._send_json(handler, {"status": "success", "message": "System is rebooting"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def shutdown_system(self, handler):
        """Shutdown the system."""
        try:
            subprocess.Popen(["sudo", "shutdown", "now"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._send_json(handler, {"status": "success", "message": "System is shutting down"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def restart_bjorn_service(self, handler):
        """Restart the Bjorn service."""
        if not hasattr(handler, 'send_response'):
            raise TypeError("Invalid handler passed. Expected an HTTP handler.")

        try:
            subprocess.Popen(["sudo", "systemctl", "restart", "bjorn.service"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self._send_json(handler, {"status": "success", "message": "Bjorn service restarted successfully"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def clear_logs(self, handler):
        """Clear logs directory contents."""
        try:
            logs_dir = os.path.join(self.shared_data.current_dir, "data", "logs")
            if os.path.isdir(logs_dir):
                for entry in os.scandir(logs_dir):
                    try:
                        if entry.is_file() or entry.is_symlink():
                            os.remove(entry.path)
                        elif entry.is_dir():
                            import shutil
                            shutil.rmtree(entry.path)
                    except OSError as e:
                        self.logger.warning(f"Failed to remove {entry.path}: {e}")
            self._send_json(handler, {"status": "success", "message": "Logs cleared successfully"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def initialize_db(self, handler):
        """Initialize or prepare database schema."""
        try:
            self.shared_data.sync_actions_to_database()
            self.shared_data.initialize_database()
            self.shared_data.initialize_statistics()
            self._send_json(handler, {"status": "success", "message": "Database initialized successfully"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def erase_bjorn_memories(self, handler):
        """Erase all Bjorn-related memories and restart service."""
        try:
            # Import file_utils for clear operations
            from web_utils.file_utils import FileUtils
            file_utils = FileUtils(self.logger, self.shared_data)
            
            # Clear various components
            file_utils.clear_output_folder(handler)
            self.clear_netkb(handler, restart=False)
            self.clear_livestatus(handler, restart=False)
            self.clear_actions_file(handler, restart=False)
            self.clear_shared_config_json(handler, restart=False)
            self.clear_logs(handler)

            # Restart service once at the end
            self.logger.debug("Restarting Bjorn service after clearing memories...")
            self.restart_bjorn_service(handler)

            self.logger.info("Bjorn memories erased and service restarted successfully.")
            handler.send_response(200)
            handler.send_header('Content-type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "success",
                "message": "Bjorn memories erased and service restarted successfully."
            }).encode('utf-8'))

        except Exception as e:
            self.logger.error(f"Error erasing Bjorn memories: {str(e)}")
            handler.send_response(500)
            handler.send_header('Content-type', 'application/json')
            handler.end_headers()
            handler.wfile.write(json.dumps({
                "status": "error",
                "message": f"Error erasing Bjorn memories: {str(e)}"
            }).encode('utf-8'))

    def clear_netkb(self, handler, restart=True):
        """Clear network knowledge base in database."""
        try:
            db = self.shared_data.db
            db.execute("DELETE FROM action_results;")
            db.execute("DELETE FROM hosts;")
            db.update_livestats(0, 0, 0, 0)
            if restart:
                self.restart_bjorn_service(handler)
            self._send_json(handler, {"status": "success", "message": "NetKB cleared in database"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def clear_livestatus(self, handler, restart=True):
        """Clear live status counters."""
        try:
            self.shared_data.db.update_livestats(0, 0, 0, 0)
            if restart:
                self.restart_bjorn_service(handler)
            self._send_json(handler, {"status": "success", "message": "Livestatus counters reset"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def clear_actions_file(self, handler, restart=True):
        """Clear actions table and resynchronize from modules."""
        try:
            self.shared_data.db.execute("DELETE FROM actions;")
            self.shared_data.generate_actions_json()
            if restart:
                self.restart_bjorn_service(handler)
            self._send_json(handler, {"status": "success", "message": "Actions table refreshed"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def clear_shared_config_json(self, handler, restart=True):
        """Reset configuration to defaults."""
        try:
            self.shared_data.config = self.shared_data.get_default_config()
            self.shared_data.save_config()
            if restart:
                self.restart_bjorn_service(handler)
            self._send_json(handler, {"status": "success", "message": "Configuration reset to defaults"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def save_configuration(self, data):
        """Save configuration to database."""
        try:
            if not isinstance(data, dict):
                return {"status": "error", "message": "Invalid data format: expected dictionary"}

            cfg = dict(self.shared_data.config)
            for k, v in data.items():
                if isinstance(v, bool):
                    cfg[k] = v
                elif isinstance(v, str) and v.lower() in ('true', 'false'):
                    cfg[k] = (v.lower() == 'true')
                elif isinstance(v, (int, float)):
                    cfg[k] = v
                elif isinstance(v, list) or v is None:
                    cfg[k] = [] if v is None else [x for x in v if x != ""]
                elif isinstance(v, str):
                    cfg[k] = float(v) if v.replace('.', '', 1).isdigit() and '.' in v else (int(v) if v.isdigit() else v)
                else:
                    cfg[k] = v

            self.shared_data.config = cfg
            self.shared_data.save_config()
            self.shared_data.load_config()
            return {"status": "success", "message": "Configuration saved"}
        except Exception as e:
            self.logger.error(f"Error saving configuration: {e}")
            return {"status": "error", "message": str(e)}

    def serve_current_config(self, handler):
        """Serve current configuration as JSON (Optimized via SharedData cache)."""
        handler.send_response(200)
        handler.send_header("Content-type", "application/json")
        handler.end_headers()
        handler.wfile.write(self.shared_data.config_json.encode('utf-8'))

    def restore_default_config(self, handler):
        """Restore default configuration."""
        handler.send_response(200)
        handler.send_header("Content-type", "application/json")
        handler.end_headers()
        self.shared_data.config = self.shared_data.default_config.copy()
        self.shared_data.save_config()
        handler.wfile.write(json.dumps(self.shared_data.config).encode('utf-8'))

    def serve_logs(self, handler):
        """Serve logs for web console."""
        try:
            log_file_path = self.shared_data.webconsolelog
            if not os.path.exists(log_file_path):
                # Create the log file if it doesn't exist; tail aggregation
                # is handled by the bjorn service, not by shell piping.
                Path(log_file_path).touch(exist_ok=True)

            with open(log_file_path, 'r') as log_file:
                log_lines = log_file.readlines()

            max_lines = 2000
            if len(log_lines) > max_lines:
                log_lines = log_lines[-max_lines:]
                with open(log_file_path, 'w') as log_file:
                    log_file.writelines(log_lines)

            log_data = ''.join(log_lines)

            handler.send_response(200)
            handler.send_header("Content-type", "text/plain")
            handler.end_headers()
            handler.wfile.write(log_data.encode('utf-8'))
        except BrokenPipeError:
            pass
        except Exception as e:
            handler.send_response(500)
            handler.send_header("Content-type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": str(e)}).encode('utf-8'))

    def sse_log_stream(self, handler):
        """Stream logs using Server-Sent Events (SSE)."""
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "keep-alive")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()

            log_file_path = self.shared_data.log_file

            handler.wfile.write(b"data: Connected\n\n")
            handler.wfile.flush()

            with open(log_file_path, 'r') as log_file:
                log_file.seek(0, os.SEEK_END)
                while True:
                    line = log_file.readline()
                    if line:
                        message = f"data: {line.strip()}\n\n"
                        handler.wfile.write(message.encode('utf-8'))
                        handler.wfile.flush()
                    else:
                        handler.wfile.write(b": heartbeat\n\n")
                        handler.wfile.flush()
                        time.sleep(1)

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as e:
            self.logger.info("Client disconnected from SSE stream")
        except Exception as e:
            self.logger.error(f"SSE Error: {e}")
        finally:
            self.logger.info("SSE stream closed")

    def _parse_progress(self):
        """Parse bjorn_progress ('42%', '', 0, '100%') → int 0-100."""
        raw = getattr(self.shared_data, "bjorn_progress", 0)
        if isinstance(raw, (int, float)):
            return max(0, min(int(raw), 100))
        if isinstance(raw, str):
            cleaned = raw.strip().rstrip('%').strip()
            if not cleaned:
                return 0
            try:
                return max(0, min(int(cleaned), 100))
            except (ValueError, TypeError):
                return 0
        return 0

    def serve_bjorn_status(self, handler):
        try:
            status_data = {
                "status": self.shared_data.bjorn_orch_status,
                "status2": self.shared_data.bjorn_status_text2,

                # 🟢 PROGRESS — parse "42%" / "" / 0 safely
                "progress": self._parse_progress(),

                "image_path": "/bjorn_status_image?t=" + str(int(time.time())),
                "battery": {
                    "present": bool(getattr(self.shared_data, "battery_present", False)),
                    "level_pct": int(getattr(self.shared_data, "battery_percent", 0)),
                    "charging": bool(getattr(self.shared_data, "battery_is_charging", False)),
                    "voltage": getattr(self.shared_data, "battery_voltage", None),
                    "source": getattr(self.shared_data, "battery_source", "unknown"),
                    "updated_at": float(getattr(self.shared_data, "battery_last_update", 0.0)),
                },
            }
            
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            handler.send_header("Pragma", "no-cache")
            handler.send_header("Expires", "0")
            handler.end_headers()
            handler.wfile.write(json.dumps(status_data).encode('utf-8'))
        except BrokenPipeError:
            pass
        except Exception as e:
            self.logger.error(f"Error in serve_bjorn_status: {str(e)}")

    def check_manual_mode(self, handler):
        """Check if manual mode is enabled."""
        try:
            handler.send_response(200)
            handler.send_header("Content-type", "text/plain")
            handler.end_headers()
            handler.wfile.write(str(self.shared_data.operation_mode).encode('utf-8'))
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Client closed the socket before response flush: normal with polling/XHR aborts.
            return
        except Exception as e:
            self.logger.error(f"check_manual_mode failed: {e}")

    def check_console_autostart(self, handler):
        """Check console autostart setting."""
        try:
            handler.send_response(200)
            handler.send_header("Content-type", "text/plain")
            handler.end_headers()
            handler.wfile.write(str(self.shared_data.consoleonwebstart).encode('utf-8'))
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            # Client closed the socket before response flush: normal with polling/XHR aborts.
            return
        except Exception as e:
            self.logger.error(f"check_console_autostart failed: {e}")

    # ----------------------------------------------------------------
    # EPD Layout API (EPD-01 / EPD-02)
    # ----------------------------------------------------------------

    def epd_get_layout(self, handler):
        """GET /api/epd/layout — return current layout JSON.

        Optional query param: ?epd_type=epd2in7
        If provided, returns the layout for that EPD type (custom or built-in)
        without changing the active device layout.
        """
        try:
            from urllib.parse import parse_qs, urlparse
            from display_layout import BUILTIN_LAYOUTS
            query = parse_qs(urlparse(handler.path).query)
            requested_type = query.get('epd_type', [''])[0]

            layout = getattr(self.shared_data, 'display_layout', None)
            if layout is None:
                self._send_json(handler, {"status": "error", "message": "Layout engine not initialised"}, 503)
                return

            if requested_type and requested_type != self.shared_data.config.get('epd_type', ''):
                # Return layout for the requested type without modifying active layout
                custom_path = os.path.join(layout._custom_dir, f'{requested_type}.json')
                if os.path.isfile(custom_path):
                    import json as _json
                    with open(custom_path, 'r') as f:
                        self._send_json(handler, _json.load(f))
                    return
                # Fallback to built-in
                base = requested_type.split('_')[0] if '_' in requested_type else requested_type
                builtin = BUILTIN_LAYOUTS.get(requested_type) or BUILTIN_LAYOUTS.get(base)
                if builtin:
                    self._send_json(handler, builtin)
                    return
                self._send_json(handler, {"status": "error", "message": f"Unknown EPD type: {requested_type}"}, 404)
                return

            self._send_json(handler, layout.to_dict())
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def epd_save_layout(self, handler, data):
        """POST /api/epd/layout — save a custom layout."""
        try:
            layout = getattr(self.shared_data, 'display_layout', None)
            if layout is None:
                self._send_json(handler, {"status": "error", "message": "Layout engine not initialised"}, 503)
                return
            if not isinstance(data, dict) or 'meta' not in data or 'elements' not in data:
                self._send_json(handler, {"status": "error", "message": "Invalid layout: must contain 'meta' and 'elements'"}, 400)
                return
            epd_type = data.get('meta', {}).get('epd_type') or None
            layout.save_custom(data, epd_type=epd_type)
            self._send_json(handler, {"status": "success", "message": "Layout saved"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def epd_reset_layout(self, handler, data):
        """POST /api/epd/layout/reset — reset to built-in default."""
        try:
            layout = getattr(self.shared_data, 'display_layout', None)
            if layout is None:
                self._send_json(handler, {"status": "error", "message": "Layout engine not initialised"}, 503)
                return
            epd_type = data.get('epd_type') if isinstance(data, dict) else None
            layout.reset_to_default(epd_type=epd_type)
            self._send_json(handler, {"status": "success", "message": "Layout reset to default"})
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)

    def epd_list_layouts(self, handler):
        """GET /api/epd/layouts — list available EPD types and their layouts."""
        try:
            from display_layout import BUILTIN_LAYOUTS
            result = {}
            for epd_type, layout_dict in BUILTIN_LAYOUTS.items():
                result[epd_type] = {
                    "meta": layout_dict.get("meta", {}),
                    "builtin": True,
                }
            # Check for custom overrides
            layout = getattr(self.shared_data, 'display_layout', None)
            if layout:
                custom_dir = layout._custom_dir
                if os.path.isdir(custom_dir):
                    for fname in os.listdir(custom_dir):
                        if fname.endswith('.json'):
                            epd_name = fname[:-5]
                            try:
                                with open(os.path.join(custom_dir, fname), 'r') as f:
                                    custom_data = json.load(f)
                                if epd_name in result:
                                    result[epd_name]["has_custom"] = True
                                    result[epd_name]["custom_meta"] = custom_data.get("meta", {})
                                else:
                                    result[epd_name] = {
                                        "meta": custom_data.get("meta", {}),
                                        "builtin": False,
                                        "has_custom": True,
                                    }
                            except Exception:
                                pass
            # Add current active type info
            current_type = self.shared_data.config.get('epd_type', 'epd2in13_V4')
            self._send_json(handler, {
                "current_epd_type": current_type,
                "layouts": result,
            })
        except Exception as e:
            self._send_json(handler, {"status": "error", "message": str(e)}, 500)
