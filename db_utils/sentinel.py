"""sentinel.py - Events, rules, and known devices baseline."""
import json
import logging
from typing import Any, Dict, List, Optional

from logger import Logger
from db_utils.base import _validate_identifier

logger = Logger(name="db_utils.sentinel", level=logging.DEBUG)


class SentinelOps:
    def __init__(self, base):
        self.base = base

    def create_tables(self):
        """Create all Sentinel tables."""

        # Known device baselines - MAC → expected behavior
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_devices (
                mac_address   TEXT PRIMARY KEY,
                alias         TEXT,
                trusted       INTEGER DEFAULT 0,
                watch         INTEGER DEFAULT 1,
                first_seen    TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen     TEXT DEFAULT CURRENT_TIMESTAMP,
                expected_ips  TEXT DEFAULT '',
                expected_ports TEXT DEFAULT '',
                notes         TEXT DEFAULT ''
            )
        """)

        # Events / alerts log
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp     TEXT DEFAULT CURRENT_TIMESTAMP,
                event_type    TEXT NOT NULL,
                severity      TEXT DEFAULT 'info',
                title         TEXT NOT NULL,
                details       TEXT DEFAULT '',
                mac_address   TEXT,
                ip_address    TEXT,
                acknowledged  INTEGER DEFAULT 0,
                notified      INTEGER DEFAULT 0,
                meta_json     TEXT DEFAULT '{}'
            )
        """)
        self.base.execute(
            "CREATE INDEX IF NOT EXISTS idx_sentinel_events_ts "
            "ON sentinel_events(timestamp DESC)"
        )
        self.base.execute(
            "CREATE INDEX IF NOT EXISTS idx_sentinel_events_type "
            "ON sentinel_events(event_type)"
        )
        self.base.execute(
            "CREATE INDEX IF NOT EXISTS idx_sentinel_events_ack "
            "ON sentinel_events(acknowledged)"
        )

        # Configurable rules (AND/OR composable)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_rules (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                enabled       INTEGER DEFAULT 1,
                trigger_type  TEXT NOT NULL,
                conditions    TEXT DEFAULT '{}',
                logic         TEXT DEFAULT 'AND',
                actions       TEXT DEFAULT '["notify_web"]',
                cooldown_s    INTEGER DEFAULT 60,
                last_fired    TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # ARP cache snapshots for spoof detection
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS sentinel_arp_cache (
                mac_address   TEXT NOT NULL,
                ip_address    TEXT NOT NULL,
                first_seen    TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen     TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (mac_address, ip_address)
            )
        """)

        # Insert default rules if empty
        existing = self.base.query("SELECT COUNT(*) AS c FROM sentinel_rules")
        if existing and existing[0].get('c', 0) == 0:
            self._insert_default_rules()

    def _insert_default_rules(self):
        """Seed default Sentinel rules."""
        defaults = [
            {
                "name": "New Device Detected",
                "trigger_type": "new_device",
                "conditions": "{}",
                "logic": "AND",
                "actions": '["notify_web"]',
                "cooldown_s": 0,
            },
            {
                "name": "Device Joined Network",
                "trigger_type": "device_join",
                "conditions": "{}",
                "logic": "AND",
                "actions": '["notify_web"]',
                "cooldown_s": 30,
            },
            {
                "name": "Device Left Network",
                "trigger_type": "device_leave",
                "conditions": "{}",
                "logic": "AND",
                "actions": '["notify_web"]',
                "cooldown_s": 30,
            },
            {
                "name": "ARP Spoofing Detected",
                "trigger_type": "arp_spoof",
                "conditions": "{}",
                "logic": "AND",
                "actions": '["notify_web", "notify_discord"]',
                "cooldown_s": 10,
            },
            {
                "name": "Port Change on Host",
                "trigger_type": "port_change",
                "conditions": "{}",
                "logic": "AND",
                "actions": '["notify_web"]',
                "cooldown_s": 120,
            },
            {
                "name": "Rogue DHCP Server",
                "trigger_type": "rogue_dhcp",
                "conditions": "{}",
                "logic": "AND",
                "actions": '["notify_web", "notify_discord"]',
                "cooldown_s": 60,
            },
        ]
        for rule in defaults:
            self.base.execute(
                """INSERT INTO sentinel_rules
                   (name, trigger_type, conditions, logic, actions, cooldown_s)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (rule["name"], rule["trigger_type"], rule["conditions"],
                 rule["logic"], rule["actions"], rule["cooldown_s"])
            )

    # ── Events ──────────────────────────────────────────────────────────

    def insert_event(self, event_type: str, severity: str, title: str,
                     details: str = "", mac: str = "", ip: str = "",
                     meta: Optional[Dict] = None) -> int:
        return self.base.execute(
            """INSERT INTO sentinel_events
               (event_type, severity, title, details, mac_address, ip_address, meta_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_type, severity, title, details, mac, ip,
             json.dumps(meta or {}))
        )

    def get_events(self, limit: int = 100, offset: int = 0,
                   event_type: str = "", unread_only: bool = False) -> List[Dict]:
        sql = "SELECT * FROM sentinel_events WHERE 1=1"
        params: list = []
        if event_type:
            sql += " AND event_type = ?"
            params.append(event_type)
        if unread_only:
            sql += " AND acknowledged = 0"
        sql += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        return self.base.query(sql, params)

    def count_unread(self) -> int:
        row = self.base.query_one(
            "SELECT COUNT(*) AS c FROM sentinel_events WHERE acknowledged = 0"
        )
        return int(row.get("c", 0)) if row else 0

    def acknowledge_event(self, event_id: int):
        self.base.execute(
            "UPDATE sentinel_events SET acknowledged = 1 WHERE id = ?",
            (event_id,)
        )

    def acknowledge_all(self):
        self.base.execute("UPDATE sentinel_events SET acknowledged = 1")

    def clear_events(self):
        self.base.execute("DELETE FROM sentinel_events")

    # ── Rules ───────────────────────────────────────────────────────────

    def get_rules(self) -> List[Dict]:
        return self.base.query("SELECT * FROM sentinel_rules ORDER BY id")

    def get_enabled_rules(self, trigger_type: str = "") -> List[Dict]:
        if trigger_type:
            return self.base.query(
                "SELECT * FROM sentinel_rules WHERE enabled = 1 AND trigger_type = ?",
                (trigger_type,)
            )
        return self.base.query(
            "SELECT * FROM sentinel_rules WHERE enabled = 1"
        )

    def upsert_rule(self, data: Dict) -> Dict:
        rule_id = data.get("id")
        if rule_id:
            self.base.execute(
                """UPDATE sentinel_rules SET
                   name=?, enabled=?, trigger_type=?, conditions=?,
                   logic=?, actions=?, cooldown_s=?
                   WHERE id=?""",
                (data["name"], int(data.get("enabled", 1)),
                 data["trigger_type"], json.dumps(data.get("conditions", {})),
                 data.get("logic", "AND"),
                 json.dumps(data.get("actions", ["notify_web"])),
                 int(data.get("cooldown_s", 60)), rule_id)
            )
        else:
            self.base.execute(
                """INSERT INTO sentinel_rules
                   (name, enabled, trigger_type, conditions, logic, actions, cooldown_s)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (data["name"], int(data.get("enabled", 1)),
                 data["trigger_type"], json.dumps(data.get("conditions", {})),
                 data.get("logic", "AND"),
                 json.dumps(data.get("actions", ["notify_web"])),
                 int(data.get("cooldown_s", 60)))
            )
        return {"status": "ok"}

    def delete_rule(self, rule_id: int):
        self.base.execute("DELETE FROM sentinel_rules WHERE id = ?", (rule_id,))

    def update_rule_fired(self, rule_id: int):
        self.base.execute(
            "UPDATE sentinel_rules SET last_fired = CURRENT_TIMESTAMP WHERE id = ?",
            (rule_id,)
        )

    # ── Devices baseline ────────────────────────────────────────────────

    def get_known_device(self, mac: str) -> Optional[Dict]:
        return self.base.query_one(
            "SELECT * FROM sentinel_devices WHERE mac_address = ?", (mac,)
        )

    def upsert_device(self, mac: str, **kwargs):
        existing = self.get_known_device(mac)
        if existing:
            sets = []
            params = []
            _ALLOWED_DEVICE_COLS = {"alias", "trusted", "watch", "expected_ips",
                                     "expected_ports", "notes"}
            for k, v in kwargs.items():
                if k in _ALLOWED_DEVICE_COLS:
                    _validate_identifier(k, "column name")
                    sets.append(f"{k} = ?")
                    params.append(v)
            sets.append("last_seen = CURRENT_TIMESTAMP")
            if sets:
                params.append(mac)
                self.base.execute(
                    f"UPDATE sentinel_devices SET {', '.join(sets)} WHERE mac_address = ?",
                    params
                )
        else:
            self.base.execute(
                """INSERT INTO sentinel_devices
                   (mac_address, alias, trusted, watch, expected_ips, expected_ports, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (mac, kwargs.get("alias", ""),
                 int(kwargs.get("trusted", 0)),
                 int(kwargs.get("watch", 1)),
                 kwargs.get("expected_ips", ""),
                 kwargs.get("expected_ports", ""),
                 kwargs.get("notes", ""))
            )

    def get_all_known_devices(self) -> List[Dict]:
        return self.base.query("SELECT * FROM sentinel_devices ORDER BY last_seen DESC")

    # ── ARP cache ───────────────────────────────────────────────────────

    def update_arp_entry(self, mac: str, ip: str):
        self.base.execute(
            """INSERT INTO sentinel_arp_cache (mac_address, ip_address)
               VALUES (?, ?)
               ON CONFLICT(mac_address, ip_address)
               DO UPDATE SET last_seen = CURRENT_TIMESTAMP""",
            (mac, ip)
        )

    def get_arp_for_ip(self, ip: str) -> List[Dict]:
        return self.base.query(
            "SELECT * FROM sentinel_arp_cache WHERE ip_address = ?", (ip,)
        )

    def get_arp_for_mac(self, mac: str) -> List[Dict]:
        return self.base.query(
            "SELECT * FROM sentinel_arp_cache WHERE mac_address = ?", (mac,)
        )

    def get_full_arp_cache(self) -> List[Dict]:
        return self.base.query("SELECT * FROM sentinel_arp_cache ORDER BY last_seen DESC")
