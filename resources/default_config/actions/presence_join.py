# -*- coding: utf-8 -*-
"""presence_join.py - Discord webhook notification when a target host joins the network."""

import requests
from typing import Optional
import logging
from datetime import datetime, timezone
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
b_requires   = {"any":[{"mac_is":"60:57:c8:51:63:fb"}]}  # adapt as needed

# Replace with your webhook
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1416433823456956561/MYc2mHuqgK_U8tA96fs2_-S1NVchPzGOzan9EgLr4i8yOQa-3xJ6Z-vMejVrpPfC3OfD"

class PresenceJoin:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def _send(self, text: str) -> None:
        if not DISCORD_WEBHOOK_URL or "webhooks/" not in DISCORD_WEBHOOK_URL:
            logger.error("PresenceJoin: DISCORD_WEBHOOK_URL missing/invalid.")
            return
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=6)
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
            
            # Add timestamp in UTC
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            
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
