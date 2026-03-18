"""credentials.py - Credential storage and management operations."""

import json
import sqlite3
from typing import Any, Dict, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.credentials", level=logging.DEBUG)


class CredentialOps:
    """Credential storage and retrieval operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create credentials table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS creds (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              service      TEXT NOT NULL,
              mac_address  TEXT,
              ip           TEXT,
              hostname     TEXT,
              "user"       TEXT,
              "password"   TEXT,
              port         INTEGER,
              "database"   TEXT,
              extra        TEXT,
              first_seen   TEXT DEFAULT CURRENT_TIMESTAMP,
              last_seen    TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Indexes to support real UPSERT and dedup
        try:
            self.base.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_creds_identity
                ON creds(service, mac_address, ip, "user", "database", port);
            """)
        except Exception:
            pass
        
        # Optional NULL-safe dedup guard for future rows
        try:
            self.base.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_creds_identity_norm
                ON creds(
                  service,
                  COALESCE(mac_address,''),
                  COALESCE(ip,''),
                  COALESCE("user",''),
                  COALESCE("database",''),
                  COALESCE(port,0)
                );
            """)
        except Exception:
            pass
        
        logger.debug("Credentials table created/verified")
    
    # =========================================================================
    # CREDENTIAL OPERATIONS
    # =========================================================================
    
    def insert_cred(self, service: str, mac: Optional[str] = None, ip: Optional[str] = None,
                   hostname: Optional[str] = None, user: Optional[str] = None, 
                   password: Optional[str] = None, port: Optional[int] = None, 
                   database: Optional[str] = None, extra: Optional[Dict[str, Any]] = None):
        """Insert or update a credential identity; last_seen is touched on update"""
        self.base.invalidate_stats_cache()
        
        # NULL-safe normalization to keep a single identity form
        mac_n  = mac or ""
        ip_n   = ip or ""
        user_n = user or ""
        db_n   = database or ""
        port_n = int(port or 0)
        js = json.dumps(extra, ensure_ascii=False) if extra else None
        
        try:
            self.base.execute("""
                INSERT INTO creds(service,mac_address,ip,hostname,"user","password",port,"database",extra)
                VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(service, mac_address, ip, "user", "database", port) DO UPDATE SET
                  "password"=excluded."password",
                  hostname=COALESCE(excluded.hostname, creds.hostname),
                  last_seen=CURRENT_TIMESTAMP,
                  extra=COALESCE(excluded.extra, creds.extra);
            """, (service, mac_n, ip_n, hostname, user_n, password, port_n, db_n, js))
        except sqlite3.OperationalError:
            # Fallback if unique index not available: manual upsert
            row = self.base.query_one("""
                SELECT id FROM creds
                 WHERE service=? AND COALESCE(mac_address,'')=? AND COALESCE(ip,'')=?
                   AND COALESCE("user",'')=? AND COALESCE("database",'')=? AND COALESCE(port,0)=?
                 LIMIT 1
            """, (service, mac_n, ip_n, user_n, db_n, port_n))
            if row:
                self.base.execute("""
                    UPDATE creds
                       SET "password"=?,
                           hostname=COALESCE(?, hostname),
                           last_seen=CURRENT_TIMESTAMP,
                           extra=COALESCE(?, extra)
                     WHERE id=?
                """, (password, hostname, js, row["id"]))
            else:
                self.base.execute("""
                    INSERT INTO creds(service,mac_address,ip,hostname,"user","password",port,"database",extra)
                    VALUES(?,?,?,?,?,?,?,?,?)
                """, (service, mac_n, ip_n, hostname, user_n, password, port_n, db_n, js))
    
    def list_creds_grouped(self) -> List[Dict[str, Any]]:
        """List all credential rows grouped/sorted by service/ip/user/port for UI"""
        return self.base.query("""
            SELECT service, mac_address, ip, hostname, "user", "password", port, "database", last_seen
            FROM creds
            ORDER BY service, ip, "user", port
        """)
