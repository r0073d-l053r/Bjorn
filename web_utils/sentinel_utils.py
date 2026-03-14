"""
Sentinel web API endpoints.
"""
import json
import logging
from typing import Dict

from logger import Logger

logger = Logger(name="sentinel_utils", level=logging.DEBUG)


class SentinelUtils:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    @property
    def _engine(self):
        return getattr(self.shared_data, 'sentinel_engine', None)

    # ── GET endpoints (handler signature) ───────────────────────────────

    def get_status(self, handler):
        """GET /api/sentinel/status — overall sentinel state + unread count."""
        engine = self._engine
        if engine:
            data = engine.get_status()
        else:
            data = {"enabled": False, "running": False, "unread_alerts": 0}
        self._send_json(handler, data)

    def get_events(self, handler):
        """GET /api/sentinel/events — recent events with optional filters."""
        try:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(handler.path).query)
            limit = int(qs.get("limit", [50])[0])
            offset = int(qs.get("offset", [0])[0])
            event_type = qs.get("type", [""])[0]
            unread = qs.get("unread", [""])[0] == "1"

            rows = self.shared_data.db.query(
                *self._build_events_query(limit, offset, event_type, unread)
            )
            count_row = self.shared_data.db.query_one(
                "SELECT COUNT(*) AS c FROM sentinel_events WHERE acknowledged = 0"
            )
            unread_count = int(count_row.get("c", 0)) if count_row else 0

            self._send_json(handler, {
                "events": rows or [],
                "unread_count": unread_count,
            })
        except Exception as e:
            logger.error("get_events error: %s", e)
            self._send_json(handler, {"events": [], "unread_count": 0})

    def _build_events_query(self, limit, offset, event_type, unread_only):
        sql = "SELECT * FROM sentinel_events WHERE 1=1"
        params = []
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if unread_only:
            sql += " AND acknowledged = 0"
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return sql, params

    def get_rules(self, handler):
        """GET /api/sentinel/rules — all rules."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM sentinel_rules ORDER BY id"
            ) or []
            self._send_json(handler, {"rules": rows})
        except Exception as e:
            logger.error("get_rules error: %s", e)
            self._send_json(handler, {"rules": []})

    def get_devices(self, handler):
        """GET /api/sentinel/devices — known device baselines."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM sentinel_devices ORDER BY last_seen DESC"
            ) or []
            self._send_json(handler, {"devices": rows})
        except Exception as e:
            logger.error("get_devices error: %s", e)
            self._send_json(handler, {"devices": []})

    def get_arp_table(self, handler):
        """GET /api/sentinel/arp — ARP cache for spoof analysis."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM sentinel_arp_cache ORDER BY last_seen DESC LIMIT 200"
            ) or []
            self._send_json(handler, {"arp": rows})
        except Exception as e:
            logger.error("get_arp error: %s", e)
            self._send_json(handler, {"arp": []})

    # ── POST endpoints (JSON data signature) ────────────────────────────

    def toggle_sentinel(self, data: Dict) -> Dict:
        """POST /api/sentinel/toggle — enable/disable sentinel."""
        enabled = bool(data.get("enabled", False))
        self.shared_data.sentinel_enabled = enabled
        engine = self._engine
        if engine:
            if enabled:
                engine.start()
            else:
                engine.stop()
        return {"status": "ok", "enabled": enabled}

    def acknowledge_event(self, data: Dict) -> Dict:
        """POST /api/sentinel/ack — acknowledge single or all events."""
        try:
            event_id = data.get("id")
            if data.get("all"):
                self.shared_data.db.execute(
                    "UPDATE sentinel_events SET acknowledged = 1"
                )
                return {"status": "ok", "message": "All events acknowledged"}
            elif event_id:
                self.shared_data.db.execute(
                    "UPDATE sentinel_events SET acknowledged = 1 WHERE id = ?",
                    (int(event_id),)
                )
                return {"status": "ok", "id": event_id}
            return {"status": "error", "message": "No id or all flag provided"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def clear_events(self, data: Dict) -> Dict:
        """POST /api/sentinel/clear — clear all events."""
        try:
            self.shared_data.db.execute("DELETE FROM sentinel_events")
            return {"status": "ok", "message": "Events cleared"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def upsert_rule(self, data: Dict) -> Dict:
        """POST /api/sentinel/rule — create or update a rule."""
        try:
            rule = data.get("rule", data)
            if not rule.get("name") or not rule.get("trigger_type"):
                return {"status": "error", "message": "name and trigger_type required"}

            conditions = rule.get("conditions", {})
            if isinstance(conditions, dict):
                conditions = json.dumps(conditions)
            actions = rule.get("actions", ["notify_web"])
            if isinstance(actions, list):
                actions = json.dumps(actions)

            rule_id = rule.get("id")
            if rule_id:
                self.shared_data.db.execute(
                    """UPDATE sentinel_rules SET
                       name=?, enabled=?, trigger_type=?, conditions=?,
                       logic=?, actions=?, cooldown_s=?
                       WHERE id=?""",
                    (rule["name"], int(rule.get("enabled", 1)),
                     rule["trigger_type"], conditions,
                     rule.get("logic", "AND"), actions,
                     int(rule.get("cooldown_s", 60)), rule_id)
                )
            else:
                self.shared_data.db.execute(
                    """INSERT INTO sentinel_rules
                       (name, enabled, trigger_type, conditions, logic, actions, cooldown_s)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (rule["name"], int(rule.get("enabled", 1)),
                     rule["trigger_type"], conditions,
                     rule.get("logic", "AND"), actions,
                     int(rule.get("cooldown_s", 60)))
                )
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def delete_rule(self, data: Dict) -> Dict:
        """POST /api/sentinel/rule/delete — delete a rule."""
        try:
            rule_id = data.get("id")
            if not rule_id:
                return {"status": "error", "message": "id required"}
            self.shared_data.db.execute(
                "DELETE FROM sentinel_rules WHERE id = ?", (int(rule_id),)
            )
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def update_device(self, data: Dict) -> Dict:
        """POST /api/sentinel/device — update device baseline."""
        try:
            mac = data.get("mac_address", "").lower()
            if not mac:
                return {"status": "error", "message": "mac_address required"}
            self.shared_data.db.execute(
                """INSERT INTO sentinel_devices
                   (mac_address, alias, trusted, watch, expected_ips, expected_ports, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(mac_address)
                   DO UPDATE SET alias=?, trusted=?, watch=?,
                   expected_ips=?, expected_ports=?, notes=?,
                   last_seen=CURRENT_TIMESTAMP""",
                (mac, data.get("alias", ""), int(data.get("trusted", 0)),
                 int(data.get("watch", 1)), data.get("expected_ips", ""),
                 data.get("expected_ports", ""), data.get("notes", ""),
                 data.get("alias", ""), int(data.get("trusted", 0)),
                 int(data.get("watch", 1)), data.get("expected_ips", ""),
                 data.get("expected_ports", ""), data.get("notes", ""))
            )
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def save_notifier_config(self, data: Dict) -> Dict:
        """POST /api/sentinel/notifiers — save notification channel config."""
        try:
            # Store notifier configs in shared_data for persistence
            notifiers = data.get("notifiers", {})
            self.shared_data.sentinel_notifiers = notifiers

            # Re-register notifiers on the engine
            engine = self._engine
            if engine:
                self._setup_notifiers(engine, notifiers)

            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _setup_notifiers(self, engine, config: Dict):
        """Register notifier instances on the engine from config dict."""
        from sentinel import DiscordNotifier, WebhookNotifier, EmailNotifier

        if config.get("discord_webhook"):
            engine.register_notifier("notify_discord",
                                     DiscordNotifier(config["discord_webhook"]))
        if config.get("webhook_url"):
            engine.register_notifier("notify_webhook",
                                     WebhookNotifier(config["webhook_url"],
                                                     config.get("webhook_headers", {})))
        if config.get("email_smtp_host"):
            engine.register_notifier("notify_email", EmailNotifier(
                smtp_host=config["email_smtp_host"],
                smtp_port=int(config.get("email_smtp_port", 587)),
                username=config.get("email_username", ""),
                password=config.get("email_password", ""),
                from_addr=config.get("email_from", ""),
                to_addrs=config.get("email_to", []),
                use_tls=config.get("email_tls", True),
            ))

    # ── Helpers ─────────────────────────────────────────────────────────

    def _send_json(self, handler, data, status=200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode("utf-8"))
