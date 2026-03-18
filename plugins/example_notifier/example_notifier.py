"""example_notifier.py - Example Bjorn plugin that logs events to the console.

This plugin demonstrates how to:
  - Extend BjornPlugin
  - Use config values from plugin.json config_schema
  - Subscribe to hooks (on_credential_found, on_vulnerability_found, etc.)
  - Use the PluginLogger for namespaced logging
  - Access the database via self.db

Copy this directory as a starting point for your own plugin!
"""

from bjorn_plugin import BjornPlugin


class ExampleNotifier(BjornPlugin):
    """Logs security events to the Bjorn console."""

    def setup(self):
        """Called once when the plugin is loaded."""
        self.prefix = self.config.get("custom_prefix", "ALERT")
        self.log.info(f"Example Notifier ready (prefix={self.prefix})")

    def teardown(self):
        """Called when the plugin is unloaded."""
        self.log.info("Example Notifier stopped")

    # ── Hook implementations ─────────────────────────────────────────

    def on_host_discovered(self, host):
        """Fired when a new host appears on the network."""
        mac = host.get("mac_address", "?")
        ips = host.get("ips", "?")
        self.log.info(f"[{self.prefix}] New host: {mac} ({ips})")

    def on_credential_found(self, cred):
        """Fired when a new credential is stored in the DB."""
        if not self.config.get("log_credentials", True):
            return

        service = cred.get("service", "?")
        user = cred.get("user", "?")
        ip = cred.get("ip", "?")
        self.log.success(
            f"[{self.prefix}] Credential found! {service}://{user}@{ip}"
        )

    def on_vulnerability_found(self, vuln):
        """Fired when a new vulnerability is recorded."""
        if not self.config.get("log_vulnerabilities", True):
            return

        cve = vuln.get("cve_id", "?")
        ip = vuln.get("ip", "?")
        severity = vuln.get("severity", "?")
        self.log.warning(
            f"[{self.prefix}] Vulnerability: {cve} on {ip} (severity={severity})"
        )

    def on_action_complete(self, action_name, success, target):
        """Fired after any orchestrated action finishes."""
        if not self.config.get("log_actions", False):
            return

        status = "SUCCESS" if success else "FAILED"
        ip = target.get("ip", "?")
        self.log.info(
            f"[{self.prefix}] Action {action_name} {status} on {ip}"
        )
