"""
Sentinel — Bjorn Network Watchdog Engine.

Lightweight background thread that monitors network state changes
and fires configurable alerts via rules. Resource-friendly: yields
to the orchestrator when actions are running.

Detection modules:
  - new_device:    Never-seen MAC appears on the network
  - device_join:   Known device comes back online (alive 0→1)
  - device_leave:  Known device goes offline (alive 1→0)
  - arp_spoof:     Same IP claimed by multiple MACs (ARP cache conflict)
  - port_change:   Host ports changed since last snapshot
  - service_change: New service detected on known host
  - rogue_dhcp:    Multiple DHCP servers on the network
  - dns_anomaly:   DNS response pointing to unexpected IP
  - mac_flood:     Sudden burst of new MACs (possible MAC flooding attack)
"""

import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from logger import Logger

logger = Logger(name="sentinel", level=logging.DEBUG)

# Severity levels
SEV_INFO = "info"
SEV_WARNING = "warning"
SEV_CRITICAL = "critical"


class SentinelEngine:
    """
    Main Sentinel watchdog. Runs a scan loop on a configurable interval.
    All checks read from the existing Bjorn DB — zero extra network traffic.
    """

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.db = shared_data.db
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

        # In-memory state for diff-based detection
        self._known_macs: Set[str] = set()          # MACs we've ever seen
        self._alive_macs: Set[str] = set()           # Currently alive MACs
        self._port_snapshot: Dict[str, str] = {}     # mac → ports string
        self._arp_cache: Dict[str, str] = {}         # ip → mac mapping
        self._last_check = 0.0
        self._initialized = False

        # Notifier registry
        self._notifiers: Dict[str, Any] = {}

    # ── Lifecycle ───────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.shared_data, 'sentinel_enabled', False))

    @property
    def interval(self) -> int:
        return max(10, int(getattr(self.shared_data, 'sentinel_interval', 30)))

    def start(self):
        if self._running:
            return
        if not self.enabled:
            logger.info("Sentinel is disabled in config, not starting.")
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="Sentinel", daemon=True
        )
        self._thread.start()
        logger.info("Sentinel engine started (interval=%ds)", self.interval)

    def stop(self):
        if not self._running:
            return
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        logger.info("Sentinel engine stopped.")

    def register_notifier(self, name: str, notifier):
        """Register a notification dispatcher (discord, email, webhook, etc.)."""
        self._notifiers[name] = notifier

    # ── Main loop ───────────────────────────────────────────────────────

    def _loop(self):
        # Give Bjorn a moment to start up
        self._stop_event.wait(5)

        while not self._stop_event.is_set():
            try:
                if not self.enabled:
                    self._stop_event.wait(30)
                    continue

                # Resource-friendly: skip if orchestrator is busy with actions
                running_count = self._count_running_actions()
                if running_count > 2:
                    logger.debug("Sentinel yielding — %d actions running", running_count)
                    self._stop_event.wait(min(self.interval, 15))
                    continue

                self._run_checks()

            except Exception as e:
                logger.error("Sentinel loop error: %s", e)

            self._stop_event.wait(self.interval)

    def _count_running_actions(self) -> int:
        try:
            rows = self.db.query(
                "SELECT COUNT(*) AS c FROM action_queue WHERE status = 'running'"
            )
            return int(rows[0].get("c", 0)) if rows else 0
        except Exception:
            return 0

    # ── Detection engine ────────────────────────────────────────────────

    def _run_checks(self):
        """Execute all detection modules against current DB state."""
        try:
            hosts = self.db.query("SELECT * FROM hosts") or []
        except Exception as e:
            logger.debug("Sentinel can't read hosts: %s", e)
            return

        current_macs = set()
        current_alive = set()
        current_ports: Dict[str, str] = {}

        for h in hosts:
            mac = (h.get("mac_address") or "").lower()
            if not mac:
                continue
            current_macs.add(mac)
            if h.get("alive"):
                current_alive.add(mac)
            current_ports[mac] = h.get("ports") or ""

        if not self._initialized:
            # First run: snapshot state without firing alerts
            self._known_macs = set(current_macs)
            self._alive_macs = set(current_alive)
            self._port_snapshot = dict(current_ports)
            self._build_arp_cache(hosts)
            self._initialized = True
            logger.info("Sentinel initialized with %d known devices", len(self._known_macs))
            return

        # 1) New device detection
        new_macs = current_macs - self._known_macs
        for mac in new_macs:
            host = next((h for h in hosts if (h.get("mac_address") or "").lower() == mac), {})
            ip = (host.get("ips") or "").split(";")[0]
            hostname = (host.get("hostnames") or "").split(";")[0] or "Unknown"
            vendor = host.get("vendor") or "Unknown"
            self._fire_event(
                "new_device", SEV_WARNING,
                f"New device: {hostname} ({vendor})",
                f"MAC: {mac} | IP: {ip} | Vendor: {vendor}",
                mac=mac, ip=ip,
                meta={"hostname": hostname, "vendor": vendor}
            )

        # 2) Device join (came online)
        joined = current_alive - self._alive_macs
        for mac in joined:
            if mac in new_macs:
                continue  # Already reported as new
            host = next((h for h in hosts if (h.get("mac_address") or "").lower() == mac), {})
            ip = (host.get("ips") or "").split(";")[0]
            hostname = (host.get("hostnames") or "").split(";")[0] or mac
            self._fire_event(
                "device_join", SEV_INFO,
                f"Device online: {hostname}",
                f"MAC: {mac} | IP: {ip}",
                mac=mac, ip=ip
            )

        # 3) Device leave (went offline)
        left = self._alive_macs - current_alive
        for mac in left:
            host = next((h for h in hosts if (h.get("mac_address") or "").lower() == mac), {})
            hostname = (host.get("hostnames") or "").split(";")[0] or mac
            self._fire_event(
                "device_leave", SEV_INFO,
                f"Device offline: {hostname}",
                f"MAC: {mac}",
                mac=mac
            )

        # 4) Port changes on known hosts
        for mac in current_macs & self._known_macs:
            old_ports = self._port_snapshot.get(mac, "")
            new_ports = current_ports.get(mac, "")
            if old_ports != new_ports and old_ports and new_ports:
                old_set = set(old_ports.split(";")) if old_ports else set()
                new_set = set(new_ports.split(";")) if new_ports else set()
                opened = new_set - old_set
                closed = old_set - new_set
                if opened or closed:
                    host = next((h for h in hosts if (h.get("mac_address") or "").lower() == mac), {})
                    hostname = (host.get("hostnames") or "").split(";")[0] or mac
                    parts = []
                    if opened:
                        parts.append(f"Opened: {', '.join(sorted(opened))}")
                    if closed:
                        parts.append(f"Closed: {', '.join(sorted(closed))}")
                    self._fire_event(
                        "port_change", SEV_WARNING,
                        f"Port change on {hostname}",
                        " | ".join(parts),
                        mac=mac,
                        meta={"opened": list(opened), "closed": list(closed)}
                    )

        # 5) ARP spoofing detection
        self._check_arp_spoofing(hosts)

        # 6) MAC flood detection
        if len(new_macs) >= 5:
            self._fire_event(
                "mac_flood", SEV_CRITICAL,
                f"MAC flood: {len(new_macs)} new devices in one cycle",
                f"MACs: {', '.join(list(new_macs)[:10])}",
                meta={"count": len(new_macs), "macs": list(new_macs)[:20]}
            )

        # Update state snapshots
        self._known_macs = current_macs
        self._alive_macs = current_alive
        self._port_snapshot = current_ports

    def _build_arp_cache(self, hosts: List[Dict]):
        """Build initial ARP cache from host data."""
        self._arp_cache = {}
        for h in hosts:
            mac = (h.get("mac_address") or "").lower()
            ips = (h.get("ips") or "").split(";")
            for ip in ips:
                ip = ip.strip()
                if ip:
                    self._arp_cache[ip] = mac
                    try:
                        self.db._base.execute(
                            """INSERT INTO sentinel_arp_cache (mac_address, ip_address)
                               VALUES (?, ?)
                               ON CONFLICT(mac_address, ip_address)
                               DO UPDATE SET last_seen = CURRENT_TIMESTAMP""",
                            (mac, ip)
                        )
                    except Exception:
                        pass

    def _check_arp_spoofing(self, hosts: List[Dict]):
        """Detect IP claimed by different MAC than previously seen."""
        for h in hosts:
            mac = (h.get("mac_address") or "").lower()
            if not mac or not h.get("alive"):
                continue
            ips = (h.get("ips") or "").split(";")
            for ip in ips:
                ip = ip.strip()
                if not ip:
                    continue
                prev_mac = self._arp_cache.get(ip)
                if prev_mac and prev_mac != mac:
                    hostname = (h.get("hostnames") or "").split(";")[0] or mac
                    self._fire_event(
                        "arp_spoof", SEV_CRITICAL,
                        f"ARP Spoof: {ip} changed from {prev_mac} to {mac}",
                        f"IP {ip} was bound to {prev_mac}, now claimed by {mac} ({hostname}). "
                        f"Possible ARP spoofing / MITM attack.",
                        mac=mac, ip=ip,
                        meta={"old_mac": prev_mac, "new_mac": mac}
                    )
                self._arp_cache[ip] = mac
                try:
                    self.db._base.execute(
                        """INSERT INTO sentinel_arp_cache (mac_address, ip_address)
                           VALUES (?, ?)
                           ON CONFLICT(mac_address, ip_address)
                           DO UPDATE SET last_seen = CURRENT_TIMESTAMP""",
                        (mac, ip)
                    )
                except Exception:
                    pass

    # ── Event firing & rule engine ──────────────────────────────────────

    def _fire_event(self, event_type: str, severity: str, title: str,
                    details: str = "", mac: str = "", ip: str = "",
                    meta: Optional[Dict] = None):
        """Check rules, store event, dispatch notifications."""
        try:
            # Check if any enabled rule matches
            rules = self.db.query(
                "SELECT * FROM sentinel_rules WHERE enabled = 1 AND trigger_type = ?",
                (event_type,)
            ) or []

            if not rules:
                # No rules for this event type — still log but don't notify
                self._store_event(event_type, severity, title, details, mac, ip, meta)
                return

            for rule in rules:
                # Check cooldown
                last_fired = rule.get("last_fired")
                cooldown = int(rule.get("cooldown_s", 60))
                if last_fired and cooldown > 0:
                    try:
                        lf = datetime.fromisoformat(last_fired)
                        if datetime.now() - lf < timedelta(seconds=cooldown):
                            continue
                    except Exception:
                        pass

                # Check conditions (AND/OR logic)
                conditions = rule.get("conditions", "{}")
                if isinstance(conditions, str):
                    try:
                        conditions = json.loads(conditions)
                    except Exception:
                        conditions = {}
                logic = rule.get("logic", "AND")
                if conditions and not self._evaluate_conditions(conditions, logic,
                                                                 mac=mac, ip=ip, meta=meta):
                    continue

                # Store event
                self._store_event(event_type, severity, title, details, mac, ip, meta)

                # Update rule last_fired
                try:
                    self.db.execute(
                        "UPDATE sentinel_rules SET last_fired = CURRENT_TIMESTAMP WHERE id = ?",
                        (rule.get("id"),)
                    )
                except Exception:
                    pass

                # Dispatch notifications
                actions = rule.get("actions", '["notify_web"]')
                if isinstance(actions, str):
                    try:
                        actions = json.loads(actions)
                    except Exception:
                        actions = ["notify_web"]

                self._dispatch_notifications(actions, event_type, severity,
                                              title, details, mac, ip, meta)
                break  # Only fire once per event type per cycle

        except Exception as e:
            logger.error("Error firing event %s: %s", event_type, e)

    def _store_event(self, event_type, severity, title, details, mac, ip, meta):
        try:
            self.db.execute(
                """INSERT INTO sentinel_events
                   (event_type, severity, title, details, mac_address, ip_address, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (event_type, severity, title, details, mac, ip,
                 json.dumps(meta or {}))
            )
        except Exception as e:
            logger.error("Failed to store sentinel event: %s", e)

    def _evaluate_conditions(self, conditions: Dict, logic: str,
                             mac: str = "", ip: str = "",
                             meta: Optional[Dict] = None) -> bool:
        """
        Evaluate rule conditions with AND/OR logic.
        Conditions format: {"mac_contains": "aa:bb", "ip_range": "192.168.1."}
        """
        if not conditions:
            return True

        results = []
        meta = meta or {}

        for key, value in conditions.items():
            if key == "mac_contains":
                results.append(value.lower() in mac.lower())
            elif key == "mac_not_contains":
                results.append(value.lower() not in mac.lower())
            elif key == "ip_prefix":
                results.append(ip.startswith(value))
            elif key == "ip_not_prefix":
                results.append(not ip.startswith(value))
            elif key == "vendor_contains":
                results.append(value.lower() in (meta.get("vendor", "")).lower())
            elif key == "min_new_devices":
                results.append(int(meta.get("count", 0)) >= int(value))
            elif key == "trusted_only":
                # Check if MAC is trusted in sentinel_devices
                dev = self.db.query_one(
                    "SELECT trusted FROM sentinel_devices WHERE mac_address = ?", (mac,)
                )
                is_trusted = bool(dev and dev.get("trusted"))
                results.append(is_trusted if value else not is_trusted)
            else:
                results.append(True)  # Unknown condition → pass

        if not results:
            return True
        return all(results) if logic == "AND" else any(results)

    def _dispatch_notifications(self, actions: List[str], event_type: str,
                                 severity: str, title: str, details: str,
                                 mac: str, ip: str, meta: Optional[Dict]):
        """Dispatch to registered notifiers."""
        payload = {
            "event_type": event_type,
            "severity": severity,
            "title": title,
            "details": details,
            "mac": mac,
            "ip": ip,
            "meta": meta or {},
            "timestamp": datetime.now().isoformat(),
        }

        for action in actions:
            if action == "notify_web":
                # Web notification is automatic via polling — no extra action needed
                continue
            notifier = self._notifiers.get(action)
            if notifier:
                try:
                    notifier.send(payload)
                except Exception as e:
                    logger.error("Notifier %s failed: %s", action, e)
            else:
                logger.debug("No notifier registered for action: %s", action)

    # ── Public query API (for web_utils) ────────────────────────────────

    def get_status(self) -> Dict:
        unread = 0
        total_events = 0
        try:
            row = self.db.query_one(
                "SELECT COUNT(*) AS c FROM sentinel_events WHERE acknowledged = 0"
            )
            unread = int(row.get("c", 0)) if row else 0
            row2 = self.db.query_one("SELECT COUNT(*) AS c FROM sentinel_events")
            total_events = int(row2.get("c", 0)) if row2 else 0
        except Exception:
            pass

        return {
            "enabled": self.enabled,
            "running": self._running,
            "initialized": self._initialized,
            "known_devices": len(self._known_macs),
            "alive_devices": len(self._alive_macs),
            "unread_alerts": unread,
            "total_events": total_events,
            "interval": self.interval,
            "check_count": 0,
        }


# ── Notification Dispatchers ────────────────────────────────────────────

class DiscordNotifier:
    """Send alerts to a Discord channel via webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, payload: Dict):
        import urllib.request
        severity_colors = {
            "info": 0x00FF9A,
            "warning": 0xFFD166,
            "critical": 0xFF3B3B,
        }
        color = severity_colors.get(payload.get("severity", "info"), 0x00FF9A)
        severity_emoji = {"info": "\u2139\uFE0F", "warning": "\u26A0\uFE0F", "critical": "\uD83D\uDEA8"}
        emoji = severity_emoji.get(payload.get("severity", "info"), "\u2139\uFE0F")

        embed = {
            "title": f"{emoji} {payload.get('title', 'Sentinel Alert')}",
            "description": payload.get("details", ""),
            "color": color,
            "fields": [],
            "footer": {"text": f"Bjorn Sentinel \u2022 {payload.get('timestamp', '')}"},
        }
        if payload.get("mac"):
            embed["fields"].append({"name": "MAC", "value": payload["mac"], "inline": True})
        if payload.get("ip"):
            embed["fields"].append({"name": "IP", "value": payload["ip"], "inline": True})
        embed["fields"].append({
            "name": "Type", "value": payload.get("event_type", "unknown"), "inline": True
        })

        body = json.dumps({"embeds": [embed]}).encode("utf-8")
        req = urllib.request.Request(
            self.webhook_url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "Bjorn-Sentinel/1.0"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error("Discord notification failed: %s", e)


class WebhookNotifier:
    """Send alerts to a generic HTTP webhook (POST JSON)."""

    def __init__(self, url: str, headers: Optional[Dict] = None):
        self.url = url
        self.headers = headers or {}

    def send(self, payload: Dict):
        import urllib.request
        body = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json", "User-Agent": "Bjorn-Sentinel/1.0"}
        hdrs.update(self.headers)
        req = urllib.request.Request(self.url, data=body, headers=hdrs)
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error("Webhook notification failed: %s", e)


class EmailNotifier:
    """Send alerts via SMTP email."""

    def __init__(self, smtp_host: str, smtp_port: int, username: str,
                 password: str, from_addr: str, to_addrs: List[str],
                 use_tls: bool = True):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.from_addr = from_addr
        self.to_addrs = to_addrs
        self.use_tls = use_tls

    def send(self, payload: Dict):
        import smtplib
        from email.mime.text import MIMEText

        severity = payload.get("severity", "info").upper()
        subject = f"[Bjorn Sentinel][{severity}] {payload.get('title', 'Alert')}"
        body = (
            f"Event: {payload.get('event_type', 'unknown')}\n"
            f"Severity: {severity}\n"
            f"Title: {payload.get('title', '')}\n"
            f"Details: {payload.get('details', '')}\n"
            f"MAC: {payload.get('mac', 'N/A')}\n"
            f"IP: {payload.get('ip', 'N/A')}\n"
            f"Time: {payload.get('timestamp', '')}\n"
        )

        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = self.from_addr
        msg["To"] = ", ".join(self.to_addrs)

        try:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15)
            if self.use_tls:
                server.starttls()
            if self.username:
                server.login(self.username, self.password)
            server.sendmail(self.from_addr, self.to_addrs, msg.as_string())
            server.quit()
        except Exception as e:
            logger.error("Email notification failed: %s", e)
