#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""thor_hammer.py - Fast TCP banner grab and service fingerprinting per port."""

import logging
import socket
import time
from typing import Dict, Optional, Tuple

from logger import Logger
from actions.bruteforce_common import ProgressTracker

logger = Logger(name="thor_hammer.py", level=logging.DEBUG)

# -------------------- Action metadata (AST-friendly) --------------------
b_class = "ThorHammer"
b_module = "thor_hammer"
b_status = "ThorHammer"
b_port = None
b_parent = None
b_service = '["ssh","ftp","telnet","http","https","smb","mysql","postgres","mssql","rdp","vnc"]'
b_trigger = "on_port_change"
b_priority = 35
b_action = "normal"
b_cooldown = 1200
b_rate_limit = "24/86400"
b_enabled = 0  # keep disabled by default; enable via Actions UI/DB when ready.
b_timeout = 300
b_max_retries = 2
b_stealth_level = 5
b_risk_level = "low"
b_tags = ["banner", "fingerprint", "service", "tcp"]
b_category = "recon"
b_name = "Thor Hammer"
b_description = "Fast TCP banner grab and service fingerprinting per port."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "ThorHammer.png"


def _guess_service_from_port(port: int) -> str:
    mapping = {
        21: "ftp",
        22: "ssh",
        23: "telnet",
        25: "smtp",
        53: "dns",
        80: "http",
        110: "pop3",
        139: "netbios-ssn",
        143: "imap",
        443: "https",
        445: "smb",
        1433: "mssql",
        3306: "mysql",
        3389: "rdp",
        5432: "postgres",
        5900: "vnc",
        8080: "http",
    }
    return mapping.get(int(port), "")


class ThorHammer:
    def __init__(self, shared_data):
        self.shared_data = shared_data

    def _connect_and_banner(self, ip: str, port: int, timeout_s: float, max_bytes: int) -> Tuple[bool, str]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout_s)
        try:
            if s.connect_ex((ip, int(port))) != 0:
                return False, ""
            try:
                data = s.recv(max_bytes)
                banner = (data or b"").decode("utf-8", errors="ignore").strip()
            except Exception:
                banner = ""
            return True, banner
        finally:
            try:
                s.close()
            except Exception:
                pass

    def execute(self, ip, port, row, status_key) -> str:
        if self.shared_data.orchestrator_should_exit:
            return "interrupted"

        try:
            port_i = int(port) if str(port).strip() else None
        except Exception:
            port_i = None

        # If port is missing, try to infer from row 'Ports' and fingerprint a few.
        ports_to_check = []
        if port_i:
            ports_to_check = [port_i]
        else:
            ports_txt = str(row.get("Ports") or row.get("ports") or "")
            for p in ports_txt.split(";"):
                p = p.strip()
                if p.isdigit():
                    ports_to_check.append(int(p))
            ports_to_check = ports_to_check[:12]  # Pi Zero guard

        if not ports_to_check:
            return "failed"

        timeout_s = float(getattr(self.shared_data, "thor_connect_timeout_s", 1.5))
        max_bytes = int(getattr(self.shared_data, "thor_banner_max_bytes", 1024))
        source = str(getattr(self.shared_data, "thor_source", "thor_hammer"))

        mac = (row.get("MAC Address") or row.get("mac_address") or row.get("mac") or "").strip()
        hostname = (row.get("Hostname") or row.get("hostname") or "").strip()
        if ";" in hostname:
            hostname = hostname.split(";", 1)[0].strip()

        self.shared_data.bjorn_orch_status = "ThorHammer"
        self.shared_data.bjorn_status_text2 = ip
        self.shared_data.comment_params = {"ip": ip, "port": str(ports_to_check[0])}

        progress = ProgressTracker(self.shared_data, len(ports_to_check))

        try:
            any_open = False
            for p in ports_to_check:
                if self.shared_data.orchestrator_should_exit:
                    return "interrupted"

                ok, banner = self._connect_and_banner(ip, p, timeout_s=timeout_s, max_bytes=max_bytes)
                any_open = any_open or ok

                service = _guess_service_from_port(p)
                product = ""
                version = ""
                fingerprint = banner[:200] if banner else ""
                confidence = 0.4 if ok else 0.1
                state = "open" if ok else "closed"

                self.shared_data.comment_params = {
                    "ip": ip,
                    "port": str(p),
                    "open": str(int(ok)),
                    "svc": service or "?",
                }

                # Persist to DB if method exists.
                try:
                    if hasattr(self.shared_data, "db") and hasattr(self.shared_data.db, "upsert_port_service"):
                        self.shared_data.db.upsert_port_service(
                            mac_address=mac or "",
                            ip=ip,
                            port=int(p),
                            protocol="tcp",
                            state=state,
                            service=service or None,
                            product=product or None,
                            version=version or None,
                            banner=banner or None,
                            fingerprint=fingerprint or None,
                            confidence=float(confidence),
                            source=source,
                        )
                except Exception as e:
                    logger.error(f"DB upsert_port_service failed for {ip}:{p}: {e}")

                progress.advance(1)

            progress.set_complete()
            return "success"
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
            self.shared_data.bjorn_status_text2 = ""


# -------------------- Optional CLI (debug/manual) --------------------
if __name__ == "__main__":
    import argparse
    from shared import SharedData

    parser = argparse.ArgumentParser(description="ThorHammer (service fingerprint)")
    parser.add_argument("--ip", required=True)
    parser.add_argument("--port", default="22")
    args = parser.parse_args()

    sd = SharedData()
    act = ThorHammer(sd)
    row = {"MAC Address": sd.get_raspberry_mac() or "__GLOBAL__", "Hostname": "", "Ports": args.port}
    print(act.execute(args.ip, args.port, row, "ThorHammer"))

