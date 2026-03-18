# -*- coding: utf-8 -*-
"""presence_join.py - Discord webhook notification when a target host joins the network."""

import requests
from typing import Optional
import logging
import datetime

from logger import Logger
from shared import SharedData  # only if executed directly for testing

logger = Logger(name="PresenceJoin", level=logging.DEBUG)

# --- Metadata (truth is in DB; here for reference/consistency) --------------
b_class      = "PresenceJoin"
b_module     = "presence_join"
b_status     = "PresenceJoin"
b_port       = None
b_service    = None
b_parent     = None
b_priority   = 90
b_cooldown   = 0              # not needed: on_join only fires on join transition
b_rate_limit = None
b_trigger    = "on_join"      # <-- Host JOINED the network (OFF -> ON since last scan)
b_requires   = None  # Configure via DB to restrict to specific MACs if needed
b_enabled    = 1
b_action     = "normal"
b_category   = "notification"
b_name       = "Presence Join"
b_description = "Sends a Discord webhook notification when a host joins the network."
b_author     = "Bjorn Team"
b_version    = "1.0.0"
b_timeout = 30
b_max_retries = 1
b_stealth_level = 10
b_risk_level = "low"
b_tags = ["presence", "discord", "notification"]
b_icon = "PresenceJoin.png"

DISCORD_WEBHOOK_URL = ""  # Configure via shared_data or DB

class PresenceJoin:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def _send(self, text: str) -> None:
        url = getattr(self.shared_data, 'discord_webhook_url', None) or DISCORD_WEBHOOK_URL
        if not url or "webhooks/" not in url:
            logger.error("PresenceJoin: DISCORD_WEBHOOK_URL missing/invalid.")
            return
        try:
            r = requests.post(url, json={"content": text}, timeout=6)
            if r.status_code < 300:
                logger.info("PresenceJoin: webhook sent.")
            else:
                logger.error(f"PresenceJoin: HTTP {r.status_code}: {r.text}")
        except Exception as e:
            logger.error(f"PresenceJoin: webhook error: {e}")

    def execute(self, ip: Optional[str], port: Optional[str], row: dict, status_key: str):
        """
        Called by the orchestrator when the scheduler detected the join.
        ip/port = host targets (if known), row = host info.
        """
        try:
            mac  = row.get("MAC Address") or row.get("mac_address") or "MAC"
            host = row.get("hostname") or (row.get("hostnames") or "").split(";")[0] if row.get("hostnames") else None
            name = f"{host} ({mac})" if host else mac
            ip_s = (ip or (row.get("IPs") or "").split(";")[0] or "").strip()
            # EPD live status
            self.shared_data.comment_params = {"mac": mac, "host": host or "unknown", "ip": ip_s or "?"}

            # Add timestamp in UTC
            timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            
            msg  = f"✅ **Presence detected**\n"
            msg += f"- Host: {host or 'unknown'}\n"
            msg += f"- MAC: {mac}\n"
            if ip_s:
                msg += f"- IP: {ip_s}\n"
            msg += f"- Time: {timestamp}"
            
            self._send(msg)
            return "success"
        except Exception as e:
            logger.error(f"PresenceJoin error: {e}")
            return "failed"


if __name__ == "__main__":
    sd = SharedData()
    logger.info("PresenceJoin ready (direct mode).")
