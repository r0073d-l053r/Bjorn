"""software.py - Detected software (CPE) inventory operations."""

from typing import List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.software", level=logging.DEBUG)


class SoftwareOps:
    """Detected software (CPE) tracking operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create detected software tables"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS detected_software (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              mac_address TEXT NOT NULL,
              ip          TEXT,
              hostname    TEXT,
              port        INTEGER NOT NULL DEFAULT 0,
              cpe         TEXT NOT NULL,
              first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
              last_seen   TEXT DEFAULT CURRENT_TIMESTAMP,
              is_active   INTEGER DEFAULT 1,
              UNIQUE(mac_address, port, cpe)
            );
        """)
        
        # Migration for detected_software
        self.base.execute("""
            UPDATE detected_software SET port = 0 WHERE port IS NULL
        """)
        
        # Detected software history (immutable log)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS detected_software_history (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              mac_address TEXT NOT NULL,
              ip          TEXT,
              hostname    TEXT,
              port        INTEGER NOT NULL DEFAULT 0,
              cpe         TEXT NOT NULL,
              event       TEXT NOT NULL,
              seen_at     TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        logger.debug("Software detection tables created/verified")
    
    # =========================================================================
    # SOFTWARE CRUD OPERATIONS
    # =========================================================================
    
    def add_detected_software(self, mac_address: str, cpe: str, ip: Optional[str] = None,
                              hostname: Optional[str] = None, port: Optional[int] = None) -> None:
        """Upsert a (mac, port, cpe) tuple and record history (new/seen)"""
        p = int(port or 0)
        existed = self.base.query(
            "SELECT id FROM detected_software WHERE mac_address=? AND port=? AND cpe=? LIMIT 1",
            (mac_address, p, cpe)
        )
        if existed:
            self.base.execute("""
                UPDATE detected_software
                   SET ip=COALESCE(?, detected_software.ip),
                       hostname=COALESCE(?, detected_software.hostname),
                       last_seen=CURRENT_TIMESTAMP,
                       is_active=1
                 WHERE mac_address=? AND port=? AND cpe=?
            """, (ip, hostname, mac_address, p, cpe))
            self.base.execute("""
                INSERT INTO detected_software_history(mac_address, ip, hostname, port, cpe, event)
                VALUES(?,?,?,?,?,'seen')
            """, (mac_address, ip, hostname, p, cpe))
        else:
            self.base.execute("""
                INSERT INTO detected_software(mac_address, ip, hostname, port, cpe, is_active)
                VALUES(?,?,?,?,?,1)
            """, (mac_address, ip, hostname, p, cpe))
            self.base.execute("""
                INSERT INTO detected_software_history(mac_address, ip, hostname, port, cpe, event)
                VALUES(?,?,?,?,?,'new')
            """, (mac_address, ip, hostname, p, cpe))
    
    def update_detected_software_status(self, mac_address: str, current_cpes: List[str]) -> None:
        """Mark absent CPEs as inactive, present ones as seen, insert new ones as needed"""
        rows = self.base.query(
            "SELECT cpe FROM detected_software WHERE mac_address=? AND is_active=1",
            (mac_address,)
        )
        existing = {r['cpe'] for r in rows}
        cur = set(current_cpes)
        
        # Inactive
        for cpe in (existing - cur):
            self.base.execute("""
                UPDATE detected_software
                   SET is_active=0, last_seen=CURRENT_TIMESTAMP
                 WHERE mac_address=? AND cpe=? AND is_active=1
            """, (mac_address, cpe))
            self.base.execute("""
                INSERT INTO detected_software_history(mac_address, port, cpe, event)
                SELECT mac_address, port, cpe, 'inactive'
                  FROM detected_software
                 WHERE mac_address=? AND cpe=? LIMIT 1
            """, (mac_address, cpe))
        
        # New
        for cpe in (cur - existing):
            self.add_detected_software(mac_address, cpe)
        
        # Seen
        for cpe in (cur & existing):
            self.base.execute("""
                UPDATE detected_software
                   SET last_seen=CURRENT_TIMESTAMP
                 WHERE mac_address=? AND cpe=? AND is_active=1
            """, (mac_address, cpe))
            self.base.execute("""
                INSERT INTO detected_software_history(mac_address, port, cpe, event)
                SELECT mac_address, port, cpe, 'seen'
                  FROM detected_software
                 WHERE mac_address=? AND cpe=? LIMIT 1
            """, (mac_address, cpe))
    
    # =========================================================================
    # MIGRATION HELPER
    # =========================================================================
    
    def migrate_cpe_from_vulnerabilities(self) -> int:
        """
        Migrate historical CPE entries wrongly stored in `vulnerabilities.vuln_id`
        into `detected_software`. Returns the number of rows migrated.
        """
        rows = self.base.query("""
            SELECT id, mac_address, ip, hostname, COALESCE(port,0) AS port, vuln_id
              FROM vulnerabilities
             WHERE LOWER(vuln_id) LIKE 'cpe:%' OR UPPER(vuln_id) LIKE 'CPE:%'
        """)
        moved = 0
        for r in rows:
            vid = r['vuln_id']
            cpe = vid.split(':', 1)[1] if vid.upper().startswith('CPE:') else vid
            try:
                self.add_detected_software(r['mac_address'], cpe, r.get('ip'), r.get('hostname'), r.get('port'))
                self.base.execute("DELETE FROM vulnerabilities WHERE id=?", (r['id'],))
                moved += 1
            except Exception:
                # Best-effort migration; keep moving on errors
                pass
        return moved
