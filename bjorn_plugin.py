"""bjorn_plugin.py - Base class and helpers for Bjorn plugins."""

import logging
from typing import Any, Dict, Optional

from logger import Logger


class PluginLogger:
    """Per-plugin logger that prefixes all messages with the plugin ID.
    Caches Logger instances by name to prevent handler accumulation on reload."""

    _cache: dict = {}  # class-level cache: name -> Logger instance

    def __init__(self, plugin_id: str):
        name = f"plugin.{plugin_id}"
        if name not in PluginLogger._cache:
            PluginLogger._cache[name] = Logger(name=name, level=logging.DEBUG)
        self._logger = PluginLogger._cache[name]

    def info(self, msg: str):
        self._logger.info(msg)

    def warning(self, msg: str):
        self._logger.warning(msg)

    def error(self, msg: str):
        self._logger.error(msg)

    def debug(self, msg: str):
        self._logger.debug(msg)

    def success(self, msg: str):
        self._logger.success(msg)


class BjornPlugin:
    """
    Base class every Bjorn plugin must extend.

    Provides:
        - Access to shared_data, database, and config
        - Convenience wrappers for status/progress/comment
        - Hook methods to override for event-driven behavior
        - Standard action interface (execute) for action-type plugins

    Usage:
        class MyPlugin(BjornPlugin):
            def setup(self):
                self.log.info("Ready!")

            def on_credential_found(self, cred):
                self.log.info(f"New cred: {cred}")
    """

    def __init__(self, shared_data, meta: dict, config: dict):
        """
        Args:
            shared_data: The global SharedData singleton.
            meta: Parsed plugin.json manifest.
            config: User-editable config values (from DB, merged with schema defaults).
        """
        self.shared_data = shared_data
        self.meta = meta
        self.config = config
        self.db = shared_data.db
        self.log = PluginLogger(meta.get("id", "unknown"))
        self.timeout = (meta.get("action") or {}).get("timeout", 300)
        self._plugin_id = meta.get("id", "unknown")

    # ── Convenience wrappers ─────────────────────────────────────────

    def set_progress(self, pct: str):
        """Update the global progress indicator (e.g., '42%')."""
        self.shared_data.bjorn_progress = pct

    def set_status(self, text: str):
        """Update the main status text shown on display and web UI."""
        self.shared_data.bjorn_status_text = text

    def set_comment(self, **params):
        """Update the EPD comment parameters."""
        self.shared_data.comment_params = params

    # ── Lifecycle ────────────────────────────────────────────────────

    def setup(self) -> None:
        """Called once when the plugin is loaded. Override to initialize resources."""
        pass

    def teardown(self) -> None:
        """Called when the plugin is unloaded or Bjorn shuts down. Override to cleanup."""
        pass

    # ── Action interface (type="action" plugins only) ────────────────

    def execute(self, ip: str, port: str, row: dict, status_key: str) -> str:
        """
        Called by the orchestrator for action-type plugins.

        Args:
            ip: Target IP address.
            port: Target port (may be empty string).
            row: Dict with keys: MAC Address, IPs, Ports, Alive.
            status_key: Action class name (for status tracking).

        Returns:
            'success' or 'failed' (string, case-sensitive).
        """
        raise NotImplementedError(
            f"Plugin {self._plugin_id} is type='action' but does not implement execute()"
        )

    # ── Hook methods (override selectively) ──────────────────────────

    def on_host_discovered(self, host: dict) -> None:
        """Hook: called when a new host is found by the scanner.

        Args:
            host: Dict with mac_address, ips, hostnames, vendor, etc.
        """
        pass

    def on_credential_found(self, cred: dict) -> None:
        """Hook: called when new credentials are discovered.

        Args:
            cred: Dict with service, mac, ip, user, password, port.
        """
        pass

    def on_vulnerability_found(self, vuln: dict) -> None:
        """Hook: called when a new vulnerability is found.

        Args:
            vuln: Dict with ip, port, cve_id, severity, description.
        """
        pass

    def on_action_complete(self, action_name: str, success: bool, target: dict) -> None:
        """Hook: called after any action finishes execution.

        Args:
            action_name: The b_class of the action that completed.
            success: True if action returned 'success'.
            target: Dict with mac, ip, port.
        """
        pass

    def on_scan_complete(self, results: dict) -> None:
        """Hook: called after a network scan cycle finishes.

        Args:
            results: Dict with hosts_found, new_hosts, scan_duration, etc.
        """
        pass
