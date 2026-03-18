"""webenum.py - Web enumeration and directory/file discovery operations."""

from typing import Any, Dict, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.webenum", level=logging.DEBUG)


class WebEnumOps:
    """Web directory and file enumeration tracking operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create web enumeration table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS webenum (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address     TEXT NOT NULL,
                ip              TEXT NOT NULL,
                hostname        TEXT,
                port            INTEGER NOT NULL,
                directory       TEXT NOT NULL,
                status          INTEGER NOT NULL,
                size            INTEGER DEFAULT 0,
                response_time   INTEGER DEFAULT 0,
                content_type    TEXT,
                scan_date       TEXT DEFAULT CURRENT_TIMESTAMP,
                tool            TEXT DEFAULT 'gobuster',
                method          TEXT DEFAULT 'GET',
                user_agent      TEXT,
                headers         TEXT,
                first_seen      TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen       TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active       INTEGER DEFAULT 1,
                UNIQUE(mac_address, ip, port, directory)
            );
        """)
        
        # Indexes for frequent queries
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_webenum_host_port ON webenum(mac_address, port);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_webenum_status ON webenum(status);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_webenum_scan_date ON webenum(scan_date);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_webenum_active ON webenum(is_active) WHERE is_active=1;")
        
        logger.debug("WebEnum table created/verified")
    
    # =========================================================================
    # WEB ENUMERATION OPERATIONS
    # =========================================================================
    
    def add_webenum_result(
        self,
        mac_address: str,
        ip: str,
        port: int,
        directory: str,
        status: int,
        *,
        hostname: Optional[str] = None,
        size: int = 0,
        response_time: int = 0,
        content_type: Optional[str] = None,
        tool: str = "gobuster",
        method: str = "GET",
        user_agent: Optional[str] = None,
        headers: Optional[str] = None
    ):
        """Add or update a web enumeration result"""
        self.base.execute("""
            INSERT INTO webenum (
                mac_address, ip, hostname, port, directory, status,
                size, response_time, content_type, tool, method,
                user_agent, headers, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(mac_address, ip, port, directory) DO UPDATE SET
                status = excluded.status,
                size = excluded.size,
                response_time = excluded.response_time,
                content_type = excluded.content_type,
                hostname = COALESCE(excluded.hostname, webenum.hostname),
                last_seen = CURRENT_TIMESTAMP,
                is_active = 1
        """, (
            mac_address, ip, hostname, port, directory, status,
            size, response_time, content_type, tool, method,
            user_agent, headers
        ))
    
    def get_webenum_for_host(self, mac_address: str, port: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all web enumeration results for a host (optionally filtered by port)"""
        if port is not None:
            return self.base.query("""
                SELECT * FROM webenum
                WHERE mac_address = ? AND port = ? AND is_active = 1
                ORDER BY status, directory
            """, (mac_address, port))
        else:
            return self.base.query("""
                SELECT * FROM webenum
                WHERE mac_address = ? AND is_active = 1
                ORDER BY port, status, directory
            """, (mac_address,))
    
    def get_webenum_by_status(self, status: int) -> List[Dict[str, Any]]:
        """Get all enumeration results with a specific HTTP status code"""
        return self.base.query("""
            SELECT * FROM webenum
            WHERE status = ? AND is_active = 1
            ORDER BY mac_address, port, directory
        """, (status,))
    
    def mark_webenum_inactive(self, mac_address: str, port: int, directories: List[str]):
        """Mark enumeration results as inactive (e.g., after a rescan)"""
        if not directories:
            return
        
        placeholders = ",".join("?" for _ in directories)
        self.base.execute(f"""
            UPDATE webenum
            SET is_active = 0, last_seen = CURRENT_TIMESTAMP
            WHERE mac_address = ? AND port = ? AND directory IN ({placeholders})
        """, (mac_address, port, *directories))
    
    def delete_webenum_for_host(self, mac_address: str, port: Optional[int] = None):
        """Delete all enumeration results for a host (optionally filtered by port)"""
        if port is not None:
            self.base.execute("""
                DELETE FROM webenum
                WHERE mac_address = ? AND port = ?
            """, (mac_address, port))
        else:
            self.base.execute("""
                DELETE FROM webenum
                WHERE mac_address = ?
            """, (mac_address,))
    
    def count_webenum_results(self, mac_address: Optional[str] = None, 
                             active_only: bool = True) -> int:
        """Count enumeration results (optionally for a specific host and/or active only)"""
        where_clauses = []
        params = []
        
        if mac_address:
            where_clauses.append("mac_address = ?")
            params.append(mac_address)
        
        if active_only:
            where_clauses.append("is_active = 1")
        
        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        row = self.base.query_one(f"""
            SELECT COUNT(*) as cnt FROM webenum
            WHERE {where_sql}
        """, tuple(params))
        
        return int(row["cnt"]) if row else 0
