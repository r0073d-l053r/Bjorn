"""example_bjorn_action.py - Custom action template using the Bjorn action format."""

import time
import logging
from logger import Logger

logger = Logger(name="example_bjorn_action", level=logging.DEBUG)

# ---- Bjorn action metadata (required for Bjorn format detection) ----
b_class = "ExampleBjornAction"
b_module = "custom/example_bjorn_action"
b_name = "Example Bjorn Action"
b_description = "Demo custom action with shared_data access and DB queries."
b_author = "Bjorn Community"
b_version = "1.0.0"
b_action = "custom"
b_enabled = 1
b_priority = 50
b_port = None
b_service = None
b_trigger = None
b_parent = None
b_cooldown = 0
b_rate_limit = None
b_tags = '["custom", "example", "template"]'

# ---- Argument schema (drives the web UI controls) ----
b_args = {
    "target_ip": {
        "type": "text",
        "default": "192.168.1.1",
        "description": "Target IP address to probe"
    },
    "scan_count": {
        "type": "number",
        "default": 3,
        "min": 1,
        "max": 100,
        "description": "Number of probe iterations"
    },
    "verbose": {
        "type": "checkbox",
        "default": False,
        "description": "Enable verbose output"
    },
    "mode": {
        "type": "select",
        "choices": ["quick", "normal", "deep"],
        "default": "normal",
        "description": "Scan depth"
    }
}

b_examples = [
    {"name": "Quick local scan", "args": {"target_ip": "192.168.1.1", "scan_count": 1, "mode": "quick"}},
    {"name": "Deep scan", "args": {"target_ip": "10.0.0.1", "scan_count": 10, "mode": "deep", "verbose": True}},
]


class ExampleBjornAction:
    """Custom Bjorn action with full shared_data access."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        logger.info("ExampleBjornAction initialized")

    def execute(self, ip, port, row, status_key):
        """Main entry point called by action_runner / orchestrator.

        Args:
            ip:         Target IP address
            port:       Target port (may be empty)
            row:        Dict with MAC Address, IPs, Ports, Alive
            status_key: Action class name (for status tracking)

        Returns:
            'success' or 'failed'
        """
        verbose = getattr(self.shared_data, "verbose", False)
        scan_count = int(getattr(self.shared_data, "scan_count", 3))
        mode = getattr(self.shared_data, "mode", "normal")

        print(f"[*] Running ExampleBjornAction on {ip} (mode={mode}, count={scan_count})")

        # Example: query DB for known hosts
        try:
            host_count = self.shared_data.db.query_one(
                "SELECT COUNT(1) c FROM hosts"
            )
            print(f"[*] Known hosts in DB: {host_count['c'] if host_count else 0}")
        except Exception as e:
            print(f"[!] DB query failed: {e}")

        # Simulate work
        for i in range(scan_count):
            if getattr(self.shared_data, "orchestrator_should_exit", False):
                print("[!] Stop requested, aborting")
                return "failed"
            print(f"[*] Probe {i+1}/{scan_count} on {ip}...")
            if verbose:
                print(f"    MAC={row.get('MAC Address', 'unknown')} mode={mode}")
            time.sleep(1)

        print(f"[+] Done. {scan_count} probes completed on {ip}")
        return "success"
