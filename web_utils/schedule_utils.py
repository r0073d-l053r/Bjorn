"""schedule_utils.py - Schedule and trigger management endpoints."""
from __future__ import annotations
import json
import logging
from typing import Any, Dict

from logger import Logger

logger = Logger(name="schedule_utils.py", level=logging.DEBUG)


class ScheduleUtils:
    """Utilities for schedule and trigger CRUD operations."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    # =========================================================================
    # SCHEDULE ENDPOINTS
    # =========================================================================

    def list_schedules(self, data: Dict) -> Dict:
        """Return all schedules."""
        try:
            schedules = self.shared_data.db.list_schedules()
            return {"status": "success", "data": schedules}
        except Exception as e:
            self.logger.error(f"list_schedules error: {e}")
            return {"status": "error", "message": str(e)}

    def create_schedule(self, data: Dict) -> Dict:
        """Create a new schedule entry."""
        try:
            script_name = data.get("script_name")
            schedule_type = data.get("schedule_type")

            if not script_name:
                return {"status": "error", "message": "script_name is required"}
            if schedule_type not in ("recurring", "oneshot"):
                return {"status": "error", "message": "schedule_type must be 'recurring' or 'oneshot'"}

            interval_seconds = None
            run_at = None

            if schedule_type == "recurring":
                interval_seconds = data.get("interval_seconds")
                if interval_seconds is None:
                    return {"status": "error", "message": "interval_seconds is required for recurring schedules"}
                interval_seconds = int(interval_seconds)
                if interval_seconds < 30:
                    return {"status": "error", "message": "interval_seconds must be at least 30"}
            else:
                run_at = data.get("run_at")
                if not run_at:
                    return {"status": "error", "message": "run_at is required for oneshot schedules"}

            args = data.get("args", "")
            conditions = data.get("conditions")
            if conditions and isinstance(conditions, dict):
                conditions = json.dumps(conditions)

            new_id = self.shared_data.db.add_schedule(
                script_name=script_name,
                schedule_type=schedule_type,
                interval_seconds=interval_seconds,
                run_at=run_at,
                args=args,
                conditions=conditions,
            )
            return {"status": "success", "data": {"id": new_id}, "message": "Schedule created"}
        except Exception as e:
            self.logger.error(f"create_schedule error: {e}")
            return {"status": "error", "message": str(e)}

    def update_schedule(self, data: Dict) -> Dict:
        """Update an existing schedule."""
        try:
            schedule_id = data.get("id")
            if schedule_id is None:
                return {"status": "error", "message": "id is required"}

            kwargs = {k: v for k, v in data.items() if k != "id"}
            if "conditions" in kwargs and isinstance(kwargs["conditions"], dict):
                kwargs["conditions"] = json.dumps(kwargs["conditions"])

            self.shared_data.db.update_schedule(int(schedule_id), **kwargs)
            return {"status": "success", "message": "Schedule updated"}
        except Exception as e:
            self.logger.error(f"update_schedule error: {e}")
            return {"status": "error", "message": str(e)}

    def delete_schedule(self, data: Dict) -> Dict:
        """Delete a schedule by id."""
        try:
            schedule_id = data.get("id")
            if schedule_id is None:
                return {"status": "error", "message": "id is required"}

            self.shared_data.db.delete_schedule(int(schedule_id))
            return {"status": "success", "message": "Schedule deleted"}
        except Exception as e:
            self.logger.error(f"delete_schedule error: {e}")
            return {"status": "error", "message": str(e)}

    def toggle_schedule(self, data: Dict) -> Dict:
        """Enable or disable a schedule."""
        try:
            schedule_id = data.get("id")
            enabled = data.get("enabled")
            if schedule_id is None:
                return {"status": "error", "message": "id is required"}
            if enabled is None:
                return {"status": "error", "message": "enabled is required"}

            self.shared_data.db.toggle_schedule(int(schedule_id), bool(enabled))
            return {"status": "success", "message": f"Schedule {'enabled' if enabled else 'disabled'}"}
        except Exception as e:
            self.logger.error(f"toggle_schedule error: {e}")
            return {"status": "error", "message": str(e)}

    # =========================================================================
    # TRIGGER ENDPOINTS
    # =========================================================================

    def list_triggers(self, data: Dict) -> Dict:
        """Return all triggers."""
        try:
            triggers = self.shared_data.db.list_triggers()
            return {"status": "success", "data": triggers}
        except Exception as e:
            self.logger.error(f"list_triggers error: {e}")
            return {"status": "error", "message": str(e)}

    def create_trigger(self, data: Dict) -> Dict:
        """Create a new trigger entry."""
        try:
            script_name = data.get("script_name")
            trigger_name = data.get("trigger_name")
            conditions = data.get("conditions")

            if not script_name:
                return {"status": "error", "message": "script_name is required"}
            if not trigger_name:
                return {"status": "error", "message": "trigger_name is required"}
            if not conditions or not isinstance(conditions, dict):
                return {"status": "error", "message": "conditions must be a JSON object"}

            args = data.get("args", "")
            cooldown_seconds = int(data.get("cooldown_seconds", 60))

            new_id = self.shared_data.db.add_trigger(
                script_name=script_name,
                trigger_name=trigger_name,
                conditions=json.dumps(conditions),
                args=args,
                cooldown_seconds=cooldown_seconds,
            )
            return {"status": "success", "data": {"id": new_id}, "message": "Trigger created"}
        except Exception as e:
            self.logger.error(f"create_trigger error: {e}")
            return {"status": "error", "message": str(e)}

    def update_trigger(self, data: Dict) -> Dict:
        """Update an existing trigger."""
        try:
            trigger_id = data.get("id")
            if trigger_id is None:
                return {"status": "error", "message": "id is required"}

            kwargs = {k: v for k, v in data.items() if k != "id"}
            if "conditions" in kwargs and isinstance(kwargs["conditions"], dict):
                kwargs["conditions"] = json.dumps(kwargs["conditions"])

            self.shared_data.db.update_trigger(int(trigger_id), **kwargs)
            return {"status": "success", "message": "Trigger updated"}
        except Exception as e:
            self.logger.error(f"update_trigger error: {e}")
            return {"status": "error", "message": str(e)}

    def delete_trigger(self, data: Dict) -> Dict:
        """Delete a trigger by id."""
        try:
            trigger_id = data.get("id")
            if trigger_id is None:
                return {"status": "error", "message": "id is required"}

            self.shared_data.db.delete_trigger(int(trigger_id))
            return {"status": "success", "message": "Trigger deleted"}
        except Exception as e:
            self.logger.error(f"delete_trigger error: {e}")
            return {"status": "error", "message": str(e)}

    def toggle_trigger(self, data: Dict) -> Dict:
        """Enable or disable a trigger."""
        try:
            trigger_id = data.get("id")
            enabled = data.get("enabled")
            if trigger_id is None:
                return {"status": "error", "message": "id is required"}
            if enabled is None:
                return {"status": "error", "message": "enabled is required"}

            self.shared_data.db.update_trigger(int(trigger_id), enabled=1 if enabled else 0)
            return {"status": "success", "message": f"Trigger {'enabled' if enabled else 'disabled'}"}
        except Exception as e:
            self.logger.error(f"toggle_trigger error: {e}")
            return {"status": "error", "message": str(e)}

    def test_trigger(self, data: Dict) -> Dict:
        """Evaluate trigger conditions and return the result."""
        try:
            conditions = data.get("conditions")
            if not conditions or not isinstance(conditions, dict):
                return {"status": "error", "message": "conditions must be a JSON object"}

            from script_scheduler import evaluate_conditions
            result = evaluate_conditions(conditions, self.shared_data.db)
            return {"status": "success", "data": {"result": result}}
        except Exception as e:
            self.logger.error(f"test_trigger error: {e}")
            return {"status": "error", "message": str(e)}
