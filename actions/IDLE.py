"""IDLE.py - No-op placeholder action for idle state."""

from shared import SharedData

b_class = "IDLE"
b_module = "idle"
b_status = "IDLE"
b_enabled = 0
b_action = "normal"
b_trigger = None
b_port = None
b_service = "[]"
b_priority = 0
b_timeout = 60
b_cooldown = 0
b_name = "IDLE"
b_description = "No-op placeholder action representing idle state."
b_author = "Bjorn Team"
b_version = "1.0.0"
b_max_retries = 0
b_stealth_level = 10
b_risk_level = "low"
b_tags = ["idle", "placeholder"]
b_category = "system"
b_icon = "IDLE.png"


class IDLE:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def execute(self, ip, port, row, status_key) -> str:
        """No-op action. Always returns success."""
        return "success"
