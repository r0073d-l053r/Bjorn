"""package_utils.py - Package installation, listing, and removal endpoints."""
from __future__ import annotations
import json
import logging
import os
import re
import subprocess
import time
from typing import Any, Dict

from logger import Logger

logger = Logger(name="package_utils.py", level=logging.DEBUG)

# Regex: alphanumeric, hyphens, underscores, dots, brackets (for extras like pkg[extra])
_VALID_PACKAGE_NAME = re.compile(r'^[a-zA-Z0-9_\-\.]+(\[[a-zA-Z0-9_\-\.,]+\])?$')


class PackageUtils:
    """Utilities for pip package management."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    # =========================================================================
    # JSON ENDPOINTS
    # =========================================================================

    def list_packages_json(self, data: Dict) -> Dict:
        """Return all tracked packages."""
        try:
            packages = self.shared_data.db.list_packages()
            return {"status": "success", "data": packages}
        except Exception as e:
            self.logger.error(f"list_packages error: {e}")
            return {"status": "error", "message": str(e)}

    def uninstall_package(self, data: Dict) -> Dict:
        """Uninstall a pip package and remove from DB."""
        try:
            name = data.get("name")
            if not name:
                return {"status": "error", "message": "name is required"}
            if not _VALID_PACKAGE_NAME.match(name):
                return {"status": "error", "message": "Invalid package name"}

            result = subprocess.run(
                ["pip", "uninstall", "-y", name],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                return {"status": "error", "message": result.stderr.strip() or "Uninstall failed"}

            self.shared_data.db.remove_package(name)
            return {"status": "success", "message": f"Package '{name}' uninstalled"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "Uninstall timed out"}
        except Exception as e:
            self.logger.error(f"uninstall_package error: {e}")
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # SSE ENDPOINT
    # =========================================================================

    def install_package(self, handler):
        """Stream pip install output as SSE events (GET endpoint)."""
        from urllib.parse import parse_qs, urlparse
        query = parse_qs(urlparse(handler.path).query)
        name = query.get("name", [""])[0].strip()

        # Validate
        if not name:
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": "name is required"}).encode("utf-8"))
            return
        if not _VALID_PACKAGE_NAME.match(name):
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status": "error", "message": "Invalid package name"}).encode("utf-8"))
            return

        max_lifetime = 300  # 5 minutes maximum
        start_time = time.time()
        process = None
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "keep-alive")
            handler.send_header("Access-Control-Allow-Origin", "*")
            handler.end_headers()

            process = subprocess.Popen(
                ["pip", "install", "--break-system-packages", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in process.stdout:
                if time.time() - start_time > max_lifetime:
                    self.logger.warning("install_package SSE stream reached max lifetime")
                    break

                payload = json.dumps({"line": line.rstrip(), "done": False})
                try:
                    handler.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    handler.wfile.flush()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                    self.logger.info("Client disconnected during package install")
                    break

            process.wait(timeout=30)
            success = process.returncode == 0

            # Get version on success
            version = ""
            if success:
                try:
                    show = subprocess.run(
                        ["pip", "show", name],
                        capture_output=True, text=True, timeout=15,
                    )
                    for show_line in show.stdout.splitlines():
                        if show_line.startswith("Version:"):
                            version = show_line.split(":", 1)[1].strip()
                            break
                except Exception:
                    pass

                # Record in DB
                self.shared_data.db.add_package(name, version)

            payload = json.dumps({"line": "", "done": True, "success": success, "version": version})
            try:
                handler.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                handler.wfile.flush()
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError):
                pass

        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            self.logger.info("Client disconnected from package install SSE stream")
        except Exception as e:
            self.logger.error(f"install_package SSE error: {e}")
            try:
                payload = json.dumps({"line": f"Error: {e}", "done": True, "success": False, "version": ""})
                handler.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                handler.wfile.flush()
            except Exception:
                pass
        finally:
            if process:
                try:
                    if process.stdout and not process.stdout.closed:
                        process.stdout.close()
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                except Exception:
                    pass
            self.logger.info("Package install SSE stream closed")
