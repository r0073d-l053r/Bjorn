# -*- coding: utf-8 -*-
"""presence_left.py - Discord webhook notification when a target host leaves the network."""

import requests
from typing import Optional
import logging
from datetime import datetime, timezone
from logger import Logger
from shared import SharedData  # only if executed directly for testing

logger = Logger(name="PresenceLeave", level=logging.DEBUG)

# --- Metadata (truth is in DB; here for reference/consistency) --------------
b_class      = "PresenceLeave"
b_module     = "presence_left"
b_status     = "PresenceLeave"
b_port       = None
b_service    = None
b_parent     = None
b_priority   = 90
b_cooldown   = 0              # not needed: on_leave only fires on leave transition
b_rate_limit = None
b_trigger    = "on_leave"     # <-- Host LEFT the network (ON -> OFF since last scan)
b_requires   = {"any":[{"mac_is":"60:57:c8:51:63:fb"}]}  # adapt as needed

# Replace with your webhook (can reuse the same as PresenceJoin)
DISCORD_WEBHOOK_URL = "https://discordapp.com/api/webhooks/1416433823456956561/MYc2mHuqgK_U8tA96fs2_-S1NVchPzGOzan9EgLr4i8yOQa-3xJ6Z-vMejVrpPfC3OfD"

class PresenceLeave:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def _send(self, text: str) -> None:
        if not DISCORD_WEBHOOK_URL or "webhooks/" not in DISCORD_WEBHOOK_URL:
            logger.error("PresenceLeave: DISCORD_WEBHOOK_URL missing/invalid.")
            return
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=6)
            if r.status_code < 300:
                logger.info("PresenceLeave: webhook sent.")
            else:
                logger.error(f"PresenceLeave: HTTP {r.status_code}: {r.text}")
        except Exception as e:
            logger.error(f"PresenceLeave: webhook error: {e}")

    def execute(self, ip: Optional[str], port: Optional[str], row: dict, status_key: str):
        """
        Called by the orchestrator when the scheduler detected the disconnection.
        ip/port = last known target (if available), row = host info.
        """
        try:
            mac  = row.get("MAC Address") or row.get("mac_address") or "MAC"
            host = row.get("hostname") or (row.get("hostnames") or "").split(";")[0] if row.get("hostnames") else None
            ip_s = (ip or (row.get("IPs") or "").split(";")[0] or "").strip()

            # Add timestamp in UTC
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            msg  = f"❌ **Presence lost**\n"
            msg += f"- Host: {host or 'unknown'}\n"
            msg += f"- MAC: {mac}\n"
            if ip_s:
                msg += f"- Last IP: {ip_s}\n"
            msg += f"- Time: {timestamp}"

            self._send(msg)
            return "success"
        except Exception as e:
            logger.error(f"PresenceLeave error: {e}")
            return "failed"


if __name__ == "__main__":
    sd = SharedData()
    logger.info("PresenceLeave ready (direct mode).")
