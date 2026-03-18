#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
odin_eye.py -- Network traffic analyzer and credential hunter for BJORN.
Uses pyshark to capture and analyze packets in real-time.
"""

import os
import json
try:
    import pyshark
    HAS_PYSHARK = True
except ImportError:
    pyshark = None
    HAS_PYSHARK = False

import re
import threading
import time
import logging
from datetime import datetime

from collections import defaultdict
from typing import Any, Dict, List, Optional

from logger import Logger

logger = Logger(name="odin_eye.py")

# -------------------- Action metadata --------------------
b_class       = "OdinEye"
b_module      = "odin_eye"
b_status      = "odin_eye"
b_port        = None
b_service     = "[]"
b_trigger     = "on_start"
b_parent      = None
b_action      = "normal"
b_priority    = 30
b_cooldown    = 0
b_rate_limit  = None
b_timeout     = 600
b_max_retries = 1
b_stealth_level = 4  # Capturing is passive, but pyshark can be resource intensive
b_risk_level  = "low"
b_enabled     = 1
b_tags        = ["sniff", "pcap", "creds", "network"]
b_category    = "recon"
b_name        = "Odin Eye"
b_description = "Passive network analyzer that hunts for credentials and data patterns."
b_author      = "Bjorn Team"
b_version     = "2.0.1"
b_icon        = "OdinEye.png"

b_args = {
    "interface": {
        "type": "select",
        "label": "Network Interface",
        "choices": ["auto", "wlan0", "eth0"],
        "default": "auto",
        "help": "Interface to listen on."
    },
    "filter": {
        "type": "text", 
        "label": "BPF Filter", 
        "default": "(http or ftp or smtp or pop3 or imap or telnet) and not broadcast"
    },
    "max_packets": {
        "type": "number", 
        "label": "Max packets", 
        "min": 100, 
        "max": 100000, 
        "step": 100, 
        "default": 1000
    },
    "save_creds": {
        "type": "checkbox",
        "label": "Save Credentials",
        "default": True
    }
}

CREDENTIAL_PATTERNS = {
    'http': {
        'username': [r'username=([^&]+)', r'user=([^&]+)', r'login=([^&]+)'],
        'password': [r'password=([^&]+)', r'pass=([^&]+)']
    },
    'ftp': {
        'username': [r'USER\s+(.+)', r'USERNAME\s+(.+)'],
        'password': [r'PASS\s+(.+)']
    },
    'smtp': {
        'auth': [r'AUTH\s+PLAIN\s+(.+)', r'AUTH\s+LOGIN\s+(.+)']
    }
}

class OdinEye:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.capture = None
        self.stop_event = threading.Event()
        self.statistics = defaultdict(int)
        self.credentials: List[Dict[str, Any]] = []
        self.lock = threading.Lock()

    def process_packet(self, packet):
        """Analyze a single packet for patterns and credentials."""
        try:
            with self.lock:
                self.statistics['total_packets'] += 1
                if hasattr(packet, 'highest_layer'):
                    self.statistics[packet.highest_layer] += 1

            if hasattr(packet, 'tcp'):
                # HTTP
                if hasattr(packet, 'http'):
                    self._analyze_http(packet)
                # FTP
                elif hasattr(packet, 'ftp'):
                    self._analyze_ftp(packet)
                # SMTP
                elif hasattr(packet, 'smtp'):
                    self._analyze_smtp(packet)
                
                # Payload generic check
                if hasattr(packet.tcp, 'payload'):
                    self._analyze_payload(packet.tcp.payload)

        except Exception as e:
            logger.debug(f"Packet processing error: {e}")

    def _analyze_http(self, packet):
        if hasattr(packet.http, 'request_uri'):
            uri = packet.http.request_uri
            for field in ['username', 'password']:
                for pattern in CREDENTIAL_PATTERNS['http'][field]:
                    m = re.findall(pattern, uri, re.I)
                    if m:
                        self._add_cred('HTTP', field, m[0], getattr(packet.ip, 'src', 'unknown'))

    def _analyze_ftp(self, packet):
        if hasattr(packet.ftp, 'request_command'):
            cmd = packet.ftp.request_command.upper()
            if cmd in ['USER', 'PASS']:
                field = 'username' if cmd == 'USER' else 'password'
                self._add_cred('FTP', field, packet.ftp.request_arg, getattr(packet.ip, 'src', 'unknown'))

    def _analyze_smtp(self, packet):
        if hasattr(packet.smtp, 'command_line'):
            line = packet.smtp.command_line
            for pattern in CREDENTIAL_PATTERNS['smtp']['auth']:
                m = re.findall(pattern, line, re.I)
                if m:
                    self._add_cred('SMTP', 'auth', m[0], getattr(packet.ip, 'src', 'unknown'))

    def _analyze_payload(self, payload):
        patterns = {
            'email': r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            'credit_card': r'\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b'
        }
        for name, pattern in patterns.items():
            m = re.findall(pattern, payload)
            if m:
                self.shared_data.log_milestone(b_class, "PatternFound", f"{name} detected in traffic")

    def _add_cred(self, proto, field, value, source):
        with self.lock:
            cred = {
                'protocol': proto,
                'type': field,
                'value': value,
                'timestamp': datetime.now().isoformat(),
                'source': source
            }
            if cred not in self.credentials:
                self.credentials.append(cred)
                logger.success(f"OdinEye: Credential found! [{proto}] {field}={value}")
                self.shared_data.log_milestone(b_class, "Credential", f"{proto} {field} captured")

    def execute(self, ip, port, row, status_key) -> str:
        """Standard entry point."""
        # Reset per-run state to prevent accumulation across reused instances
        self.credentials.clear()
        self.statistics.clear()

        iface = getattr(self.shared_data, "odin_eye_interface", "auto")
        if iface == "auto":
            iface = None # pyshark handles None as default
        
        bpf_filter = getattr(self.shared_data, "odin_eye_filter", b_args["filter"]["default"])
        max_pkts = int(getattr(self.shared_data, "odin_eye_max_packets", 1000))
        timeout = int(getattr(self.shared_data, "odin_eye_timeout", 300))
        _fallback_dir = os.path.join(getattr(self.shared_data, "data_dir", "/home/bjorn/Bjorn/data"), "output", "packets")
        output_dir = getattr(self.shared_data, "odin_eye_output", _fallback_dir)

        logger.info(f"OdinEye: Starting capture on {iface or 'default'} (filter: {bpf_filter})")
        self.shared_data.log_milestone(b_class, "Startup", f"Sniffing on {iface or 'any'}")
        # EPD live status
        self.shared_data.comment_params = {"iface": iface or "any", "filter": bpf_filter[:30]}

        if not HAS_PYSHARK:
            logger.error("OdinEye requires pyshark but it is not installed.")
            return "failed"

        try:
            self.capture = pyshark.LiveCapture(interface=iface, bpf_filter=bpf_filter)
            
            start_time = time.time()
            packet_count = 0
            
            # Use sniff_continuously for real-time processing
            for packet in self.capture.sniff_continuously():
                if self.shared_data.orchestrator_should_exit:
                    break
                
                if time.time() - start_time > timeout:
                    logger.info("OdinEye: Timeout reached.")
                    break
                
                packet_count += 1
                if packet_count >= max_pkts:
                    logger.info("OdinEye: Max packets reached.")
                    break

                self.process_packet(packet)
                
                # Periodic progress update (every 50 packets)
                if packet_count % 50 == 0:
                    prog = int((packet_count / max_pkts) * 100)
                    self.shared_data.bjorn_progress = f"{prog}%"
                    # EPD live status update
                    self.shared_data.comment_params = {"packets": str(packet_count), "creds": str(len(self.credentials))}
                    self.shared_data.log_milestone(b_class, "Status", f"Captured {packet_count} packets")

        except Exception as e:
            logger.error(f"Capture error: {e}")
            self.shared_data.log_milestone(b_class, "Error", str(e))
            return "failed"
        finally:
            if self.capture:
                try: self.capture.close()
                except Exception: pass
            
            # Save results
            if self.credentials or self.statistics['total_packets'] > 0:
                os.makedirs(output_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                with open(os.path.join(output_dir, f"odin_recon_{ts}.json"), 'w') as f:
                    json.dump({
                        "stats": dict(self.statistics),
                        "credentials": self.credentials
                    }, f, indent=4)
                self.shared_data.log_milestone(b_class, "Complete", f"Capture finished. {len(self.credentials)} creds found.")
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}

        return "success"

if __name__ == "__main__":
    from init_shared import shared_data
    eye = OdinEye(shared_data)
    eye.execute("0.0.0.0", None, {}, "odin_eye")