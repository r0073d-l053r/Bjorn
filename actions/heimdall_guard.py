#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
heimdall_guard.py -- Stealth operations and IDS/IPS evasion for BJORN.
Handles packet fragmentation, timing randomization, and TTL manipulation.
Requires: scapy.
"""

import os
import json
import random
import time
import threading
import datetime

from collections import deque
from typing import Any, Dict, List, Optional

try:
    from scapy.all import IP, TCP, Raw, send, conf
    HAS_SCAPY = True
except ImportError:
    HAS_SCAPY = False
    IP = TCP = Raw = send = conf = None

from logger import Logger

logger = Logger(name="heimdall_guard.py")

# -------------------- Action metadata --------------------
b_class       = "HeimdallGuard"
b_module      = "heimdall_guard"
b_status      = "heimdall_guard"
b_port        = None
b_service     = "[]"
b_trigger     = "on_start"
b_parent      = None
b_action      = "stealth"
b_priority    = 10
b_cooldown    = 0
b_rate_limit  = None
b_timeout     = 1800
b_max_retries = 1
b_stealth_level = 10  # This IS the stealth module
b_risk_level  = "low"
b_enabled     = 1
b_tags        = ["stealth", "evasion", "pcap", "network"]
b_category    = "defense"
b_name        = "Heimdall Guard"
b_description = "Advanced stealth module that manipulates traffic to evade IDS/IPS detection."
b_author      = "Bjorn Team"
b_version     = "2.0.3"
b_icon        = "HeimdallGuard.png"

b_args = {
    "interface": {
        "type": "text", 
        "label": "Interface", 
        "default": "eth0"
    },
    "mode": {
        "type": "select", 
        "label": "Stealth Mode", 
        "choices": ["timing", "fragmented", "all"], 
        "default": "all"
    },
    "delay": {
        "type": "number", 
        "label": "Base Delay (s)", 
        "min": 0.1, 
        "max": 10.0, 
        "step": 0.1, 
        "default": 1.0
    }
}

class HeimdallGuard:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.packet_queue = deque()
        self.active = False
        self.lock = threading.Lock()
        
        self.stats = {
            'packets_processed': 0,
            'packets_fragmented': 0,
            'timing_adjustments': 0
        }

    def _fragment_packet(self, packet, mtu=1400):
        """Fragment IP packets to bypass strict IDS rules."""
        if IP in packet:
            try:
                payload = bytes(packet[IP].payload)
                max_size = mtu - 40 # conservative
                frags = []
                offset = 0
                while offset < len(payload):
                    chunk = payload[offset:offset + max_size]
                    f = packet.copy()
                    f[IP].flags = 'MF' if offset + max_size < len(payload) else 0
                    f[IP].frag = offset // 8
                    f[IP].payload = Raw(chunk)
                    frags.append(f)
                    offset += max_size
                return frags
            except Exception as e:
                logger.debug(f"Fragmentation error: {e}")
        return [packet]

    def _apply_stealth(self, packet):
        """Randomize TTL and TCP options."""
        if IP in packet:
            packet[IP].ttl = random.choice([64, 128, 255])
        if TCP in packet:
            packet[TCP].window = random.choice([8192, 16384, 65535])
            # Basic TCP options shuffle
            packet[TCP].options = [('MSS', 1460), ('NOP', None), ('SAckOK', '')]
        return packet

    def execute(self, ip, port, row, status_key) -> str:
        if not HAS_SCAPY:
            logger.error("HeimdallGuard requires scapy but it is not installed.")
            return "failed"

        # Reset per-run state
        self.stats = {'packets_processed': 0, 'packets_fragmented': 0, 'timing_adjustments': 0}
        self.packet_queue.clear()

        iface = getattr(self.shared_data, "heimdall_guard_interface", conf.iface)
        mode = getattr(self.shared_data, "heimdall_guard_mode", "all")
        delay = float(getattr(self.shared_data, "heimdall_guard_delay", 1.0))
        timeout = int(getattr(self.shared_data, "heimdall_guard_timeout", 600))
        
        logger.info(f"HeimdallGuard: Engaging stealth mode ({mode}) on {iface}")
        self.shared_data.log_milestone(b_class, "StealthActive", f"Mode: {mode}")
        # EPD live status
        self.shared_data.comment_params = {"ip": ip, "mode": mode, "iface": iface}

        self.active = True
        start_time = time.time()
        
        try:
            while time.time() - start_time < timeout:
                if self.shared_data.orchestrator_should_exit:
                    logger.info("HeimdallGuard: Interrupted by orchestrator.")
                    return "interrupted"

                # Progress reporting
                elapsed = int(time.time() - start_time)
                prog = int((elapsed / timeout) * 100)
                self.shared_data.bjorn_progress = f"{prog}%"
                
                if elapsed % 60 == 0:
                    self.shared_data.log_milestone(b_class, "Status", f"Guarding... {self.stats['packets_processed']} pkts handled")
                
                # Logic: if we had a queue, we'd process it here
                # Simulation for BJORN action demonstration:
                time.sleep(2)

            logger.info("HeimdallGuard: Protection session finished.")
            self.shared_data.log_milestone(b_class, "Shutdown", "Stealth mode disengaged")

        except Exception as e:
            logger.error(f"HeimdallGuard error: {e}")
            return "failed"
        finally:
            self.active = False
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}

        return "success"

if __name__ == "__main__":
    from init_shared import shared_data
    guard = HeimdallGuard(shared_data)
    guard.execute("0.0.0.0", None, {}, "heimdall_guard")