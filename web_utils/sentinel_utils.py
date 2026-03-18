"""sentinel_utils.py - Sentinel web API endpoints."""
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
        """GET /api/sentinel/status - overall sentinel state + unread count."""
        engine = self._engine
        if engine:
            data = engine.get_status()
        else:
            data = {"enabled": False, "running": False, "unread_alerts": 0}
        self._send_json(handler, data)

    def get_events(self, handler):
        """GET /api/sentinel/events - recent events with optional filters."""
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
        """GET /api/sentinel/rules - all rules."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM sentinel_rules ORDER BY id"
            ) or []
            self._send_json(handler, {"rules": rows})
        except Exception as e:
            logger.error("get_rules error: %s", e)
            self._send_json(handler, {"rules": []})

    def get_devices(self, handler):
        """GET /api/sentinel/devices - known device baselines."""
        try:
            rows = self.shared_data.db.query(
                "SELECT * FROM sentinel_devices ORDER BY last_seen DESC"
            ) or []
            self._send_json(handler, {"devices": rows})
        except Exception as e:
            logger.error("get_devices error: %s", e)
            self._send_json(handler, {"devices": []})

    def get_arp_table(self, handler):
        """GET /api/sentinel/arp - ARP cache for spoof analysis."""
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
        """POST /api/sentinel/toggle - enable/disable sentinel."""
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
        """POST /api/sentinel/ack - acknowledge single or all events."""
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
        """POST /api/sentinel/clear - clear all events."""
        try:
            self.shared_data.db.execute("DELETE FROM sentinel_events")
            return {"status": "ok", "message": "Events cleared"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def upsert_rule(self, data: Dict) -> Dict:
        """POST /api/sentinel/rule - create or update a rule."""
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
        """POST /api/sentinel/rule/delete - delete a rule."""
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
        """POST /api/sentinel/device - update device baseline."""
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

    # Mapping from frontend notifier keys to config keys
    _NOTIFIER_KEY_MAP = {
        "discord_webhook": "sentinel_discord_webhook",
        "webhook_url":     "sentinel_webhook_url",
        "email_smtp_host": "sentinel_email_smtp_host",
        "email_smtp_port": "sentinel_email_smtp_port",
        "email_username":  "sentinel_email_username",
        "email_password":  "sentinel_email_password",
        "email_from":      "sentinel_email_from",
        "email_to":        "sentinel_email_to",
    }

    def get_notifier_config(self, handler) -> None:
        """GET /api/sentinel/notifiers - return current notifier config."""
        cfg = self.shared_data.config
        notifiers = {}
        for frontend_key, cfg_key in self._NOTIFIER_KEY_MAP.items():
            val = cfg.get(cfg_key, "")
            if val:
                notifiers[frontend_key] = val
        self._send_json(handler, {"status": "ok", "notifiers": notifiers})

    def save_notifier_config(self, data: Dict) -> Dict:
        """POST /api/sentinel/notifiers - save notification channel config."""
        try:
            notifiers = data.get("notifiers", {})
            cfg = self.shared_data.config

            # Map frontend keys to config keys and persist
            for frontend_key, cfg_key in self._NOTIFIER_KEY_MAP.items():
                cfg[cfg_key] = notifiers.get(frontend_key, "")

            self.shared_data.config = cfg
            self.shared_data.save_config()

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

    # ── LLM-powered endpoints ────────────────────────────────────────────

    def analyze_events(self, data: Dict) -> Dict:
        """POST /api/sentinel/analyze - AI analysis of selected events."""
        try:
            event_ids = data.get("event_ids", [])
            if not event_ids:
                return {"status": "error", "message": "event_ids required"}

            # Fetch events
            placeholders = ",".join("?" for _ in event_ids)
            rows = self.shared_data.db.query(
                f"SELECT * FROM sentinel_events WHERE id IN ({placeholders})",
                [int(i) for i in event_ids],
            ) or []
            if not rows:
                return {"status": "error", "message": "No events found"}

            # Gather device info for context
            macs = set()
            ips = set()
            for ev in rows:
                meta = {}
                try:
                    meta = json.loads(ev.get("metadata", "{}") or "{}")
                except Exception:
                    pass
                if meta.get("mac"):
                    macs.add(meta["mac"])
                if meta.get("ip"):
                    ips.add(meta["ip"])

            devices = []
            if macs:
                mac_ph = ",".join("?" for _ in macs)
                devices = self.shared_data.db.query(
                    f"SELECT * FROM sentinel_devices WHERE mac_address IN ({mac_ph})",
                    list(macs),
                ) or []

            from llm_bridge import LLMBridge
            bridge = LLMBridge()

            system = (
                "You are a cybersecurity analyst reviewing sentinel alerts from Bjorn, "
                "a network security AI. Analyze the events below and provide: "
                "1) A severity assessment (critical/high/medium/low/info), "
                "2) A concise analysis of what happened, "
                "3) Concrete recommendations. "
                "Be technical and actionable. Respond in plain text, keep it under 300 words."
            )

            prompt = (
                f"Events:\n{json.dumps(rows, indent=2, default=str)}\n\n"
                f"Known devices:\n{json.dumps(devices, indent=2, default=str)}\n\n"
                "Analyze these security events."
            )

            response = bridge.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=600,
                system=system,
                timeout=30,
            )
            return {"status": "ok", "analysis": response or "(no response)"}

        except Exception as e:
            logger.error("analyze_events error: %s", e)
            return {"status": "error", "message": str(e)}

    def summarize_events(self, data: Dict) -> Dict:
        """POST /api/sentinel/summarize - AI summary of recent unread events."""
        try:
            limit = min(int(data.get("limit", 50)), 100)
            rows = self.shared_data.db.query(
                "SELECT * FROM sentinel_events WHERE acknowledged = 0 "
                "ORDER BY timestamp DESC LIMIT ?",
                [limit],
            ) or []

            if not rows:
                return {"status": "ok", "summary": "No unread events to summarize."}

            from llm_bridge import LLMBridge
            bridge = LLMBridge()

            system = (
                "You are a cybersecurity analyst. Summarize the security events below. "
                "Group by type, identify patterns, flag critical items. "
                "Be concise - max 200 words. Use bullet points."
            )

            prompt = (
                f"{len(rows)} unread sentinel events:\n"
                f"{json.dumps(rows, indent=2, default=str)}\n\n"
                "Summarize these events and identify patterns."
            )

            response = bridge.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=500,
                system=system,
                timeout=30,
            )
            return {"status": "ok", "summary": response or "(no response)"}

        except Exception as e:
            logger.error("summarize_events error: %s", e)
            return {"status": "error", "message": str(e)}

    def suggest_rule(self, data: Dict) -> Dict:
        """POST /api/sentinel/suggest-rule - AI generates a rule from description."""
        try:
            description = (data.get("description") or "").strip()
            if not description:
                return {"status": "error", "message": "description required"}

            from llm_bridge import LLMBridge
            bridge = LLMBridge()

            system = (
                "You are a security rule generator. Given a user description, generate a Bjorn sentinel rule "
                "as JSON. The rule schema is:\n"
                '{"name": "string", "trigger_type": "new_device|arp_spoof|port_change|service_change|'
                'dhcp_server|rogue_ap|high_traffic|vulnerability", "conditions": {"key": "value"}, '
                '"logic": "AND|OR", "actions": ["notify_web","notify_discord","notify_email","notify_webhook"], '
                '"cooldown_s": 60, "enabled": 1}\n'
                "Respond with ONLY the JSON object, no markdown fences, no explanation."
            )

            prompt = f"Generate a sentinel rule for: {description}"

            response = bridge.complete(
                [{"role": "user", "content": prompt}],
                max_tokens=400,
                system=system,
                timeout=20,
            )

            if not response:
                return {"status": "error", "message": "No LLM response"}

            # Try to parse the JSON
            try:
                # Strip markdown fences if present
                clean = response.strip()
                if clean.startswith("```"):
                    clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
                if clean.endswith("```"):
                    clean = clean[:-3]
                clean = clean.strip()
                if clean.startswith("json"):
                    clean = clean[4:].strip()

                rule = json.loads(clean)
                return {"status": "ok", "rule": rule}
            except json.JSONDecodeError:
                return {"status": "ok", "rule": None, "raw": response,
                        "message": "LLM response was not valid JSON"}

        except Exception as e:
            logger.error("suggest_rule error: %s", e)
            return {"status": "error", "message": str(e)}

    # ── Helpers ─────────────────────────────────────────────────────────

    def _send_json(self, handler, data, status=200):
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(json.dumps(data).encode("utf-8"))
