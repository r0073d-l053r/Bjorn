"""vulnerabilities.py - Vulnerability tracking and CVE metadata operations."""

import json
import time
from typing import Any, Dict, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.vulnerabilities", level=logging.DEBUG)


class VulnerabilityOps:
    """Vulnerability tracking and CVE metadata operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create vulnerability and CVE metadata tables"""
        # CVE metadata cache (NVD/MITRE/EPSS/KEV + Exploit-DB)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS cve_meta (
                cve_id           TEXT PRIMARY KEY,
                description      TEXT,
                cvss_json        TEXT,
                references_json  TEXT,
                last_modified    TEXT,
                affected_json    TEXT,
                solution         TEXT,
                exploits_json    TEXT,
                is_kev           INTEGER DEFAULT 0,
                epss             REAL,
                epss_percentile  REAL,
                updated_at       INTEGER
            );
        """)
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_cve_meta_updated ON cve_meta(updated_at);")
        
        # Vulnerabilities table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS vulnerabilities (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address     TEXT NOT NULL,
                ip              TEXT,
                hostname        TEXT,
                port            INTEGER NOT NULL DEFAULT 0,
                vuln_id         TEXT NOT NULL,
                previous_vulns  TEXT,
                first_seen      TEXT DEFAULT CURRENT_TIMESTAMP,
                last_seen       TEXT DEFAULT CURRENT_TIMESTAMP,
                is_active       INTEGER DEFAULT 1
            );
        """)
        
        # Unique index without COALESCE since port is now NOT NULL
        self.base.execute("""
            DROP INDEX IF EXISTS uq_vuln_identity;
        """)
        
        self.base.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_vuln_identity
            ON vulnerabilities(mac_address, vuln_id, port);
        """)
        
        # Migration: convert NULL to 0
        self.base.execute("""
            UPDATE vulnerabilities SET port = 0 WHERE port IS NULL;
        """)
        
        # Cleanup real duplicates after migration
        self.base.execute("""
            DELETE FROM vulnerabilities 
            WHERE rowid NOT IN (
                SELECT MIN(rowid) 
                FROM vulnerabilities 
                GROUP BY mac_address, vuln_id, port
            );
        """)
        
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_vuln_active ON vulnerabilities(is_active) WHERE is_active=1;")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_vuln_mac_port ON vulnerabilities(mac_address, port);")
        
        # Vulnerability history (immutable log)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS vulnerability_history (
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              mac_address TEXT NOT NULL,
              ip          TEXT,
              hostname    TEXT,
              port        INTEGER,
              vuln_id     TEXT NOT NULL,
              event       TEXT NOT NULL,
              seen_at     TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        logger.debug("Vulnerability tables created/verified")
    
    # =========================================================================
    # CVE METADATA OPERATIONS
    # =========================================================================
    
    def get_cve_meta(self, cve_id: str) -> Optional[Dict[str, Any]]:
        """Get CVE metadata from cache"""
        row = self.base.query_one("SELECT * FROM cve_meta WHERE cve_id=? LIMIT 1;", (cve_id,))
        if not row:
            return None
        # Deserialize JSON fields
        for k in ("cvss_json", "references_json", "affected_json", "exploits_json"):
            if row.get(k):
                try:
                    row[k] = json.loads(row[k])
                except Exception:
                    row[k] = None
        return row
    
    def upsert_cve_meta(self, meta: Dict[str, Any]) -> None:
        """Insert or update CVE metadata"""
        # Serialize JSON fields
        cvss = json.dumps(meta.get("cvss"), ensure_ascii=False) if meta.get("cvss") is not None else None
        refs = json.dumps(meta.get("references"), ensure_ascii=False) if meta.get("references") is not None else None
        aff  = json.dumps(meta.get("affected"), ensure_ascii=False) if meta.get("affected") is not None else None
        exps = json.dumps(meta.get("exploits"), ensure_ascii=False) if meta.get("exploits") is not None else None
        
        self.base.execute("""
            INSERT INTO cve_meta(
                cve_id, description, cvss_json, references_json, last_modified,
                affected_json, solution, exploits_json, is_kev, epss, epss_percentile, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(cve_id) DO UPDATE SET
                description     = excluded.description,
                cvss_json       = excluded.cvss_json,
                references_json = excluded.references_json,
                last_modified   = excluded.last_modified,
                affected_json   = excluded.affected_json,
                solution        = excluded.solution,
                exploits_json   = excluded.exploits_json,
                is_kev          = excluded.is_kev,
                epss            = excluded.epss,
                epss_percentile = excluded.epss_percentile,
                updated_at      = excluded.updated_at;
        """, (
            meta.get("cve_id"),
            meta.get("description"),
            cvss, refs, meta.get("lastModified"),
            aff, meta.get("solution"), exps,
            1 if meta.get("is_kev") else 0,
            meta.get("epss"),
            meta.get("epss_percentile"),
            int(meta.get("updated_at") or time.time())
        ))
    
    def get_cve_meta_bulk(self, cve_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """Get multiple CVE metadata entries at once"""
        if not cve_ids:
            return {}
        placeholders = ",".join("?" for _ in cve_ids)
        rows = self.base.query(f"SELECT * FROM cve_meta WHERE cve_id IN ({placeholders});", tuple(cve_ids))
        out = {}
        for r in rows:
            for k in ("cvss_json","references_json","affected_json","exploits_json"):
                if r.get(k):
                    try:
                        r[k] = json.loads(r[k])
                    except Exception:
                        r[k] = None
            out[r["cve_id"]] = r
        return out
    
    # =========================================================================
    # VULNERABILITY CRUD OPERATIONS
    # =========================================================================
    
    def add_vulnerability(self, mac_address: str, vuln_id: str, ip: Optional[str] = None,
                         hostname: Optional[str] = None, port: Optional[int] = None):
        """Insert/reactivate a vulnerability row and record history (NULL-safe on port)"""
        self.base.invalidate_stats_cache()
        p = int(port or 0)
        
        try:
            # Try to update existing row
            updated = self.base.execute(
                """
                UPDATE vulnerabilities
                SET is_active = 1,
                    ip        = COALESCE(?, ip),
                    hostname  = COALESCE(?, hostname),
                    last_seen = CURRENT_TIMESTAMP
                WHERE mac_address = ? AND vuln_id = ? AND COALESCE(port, 0) = ?
                """,
                (ip, hostname, mac_address, vuln_id, p)
            )
            
            if updated and updated > 0:
                # Seen again
                self.base.execute(
                    """
                    INSERT INTO vulnerability_history(mac_address, ip, hostname, port, vuln_id, event)
                    VALUES(?,?,?,?,?,'seen')
                    """,
                    (mac_address, ip, hostname, p, vuln_id)
                )
                return
            
            # Insert new row (port=0 if unknown)
            self.base.execute(
                """
                INSERT INTO vulnerabilities(mac_address, ip, hostname, port, vuln_id, is_active)
                VALUES(?,?,?,?,?,1)
                """,
                (mac_address, ip, hostname, p, vuln_id)
            )
            self.base.execute(
                """
                INSERT INTO vulnerability_history(mac_address, ip, hostname, port, vuln_id, event)
                VALUES(?,?,?,?,?,'new')
                """,
                (mac_address, ip, hostname, p, vuln_id)
            )
            
        except Exception:
            # Fallback if the query fails for exotic reason
            row = self.base.query_one(
                """
                SELECT id FROM vulnerabilities
                WHERE mac_address=? AND vuln_id=? AND COALESCE(port,0)=?
                LIMIT 1
                """,
                (mac_address, vuln_id, p)
            )
            if row:
                self.base.execute(
                    """
                    UPDATE vulnerabilities
                    SET is_active=1,
                        ip=COALESCE(?, ip),
                        hostname=COALESCE(?, hostname),
                        last_seen=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (ip, hostname, row["id"])
                )
                self.base.execute(
                    """
                    INSERT INTO vulnerability_history(mac_address, ip, hostname, port, vuln_id, event)
                    VALUES(?,?,?,?,?,'seen')
                    """,
                    (mac_address, ip, hostname, p, vuln_id)
                )
            else:
                self.base.execute(
                    """
                    INSERT INTO vulnerabilities(mac_address, ip, hostname, port, vuln_id, is_active)
                    VALUES(?,?,?,?,?,1)
                    """,
                    (mac_address, ip, hostname, p, vuln_id)
                )
                self.base.execute(
                    """
                    INSERT INTO vulnerability_history(mac_address, ip, hostname, port, vuln_id, event)
                    VALUES(?,?,?,?,?,'new')
                    """,
                    (mac_address, ip, hostname, p, vuln_id)
                )
    
    def update_vulnerability_status(self, mac_address: str, current_vulns: List[str]):
        """Update vulnerability presence (new/seen/inactive) and touch timestamps/history"""
        existing = self.base.query(
            "SELECT vuln_id FROM vulnerabilities WHERE mac_address=? AND is_active=1",
            (mac_address,)
        )
        existing_ids = {r['vuln_id'] for r in existing}
        current_set = set(current_vulns)
        
        # Mark inactive
        for vuln_id in (existing_ids - current_set):
            self.base.execute("""
                UPDATE vulnerabilities 
                SET is_active=0, last_seen=CURRENT_TIMESTAMP
                WHERE mac_address=? AND vuln_id=? AND is_active=1
            """, (mac_address, vuln_id))
            
            self.base.execute("""
                INSERT INTO vulnerability_history(mac_address, port, vuln_id, event)
                SELECT mac_address, port, vuln_id, 'inactive'
                FROM vulnerabilities
                WHERE mac_address=? AND vuln_id=? LIMIT 1
            """, (mac_address, vuln_id))
        
        # Add new
        for vuln_id in (current_set - existing_ids):
            self.add_vulnerability(mac_address, vuln_id)
        
        # Seen: refresh last_seen and record history
        for vuln_id in (current_set & existing_ids):
            self.base.execute("""
                UPDATE vulnerabilities
                SET last_seen=CURRENT_TIMESTAMP
                WHERE mac_address=? AND vuln_id=? AND is_active=1
            """, (mac_address, vuln_id))
            
            self.base.execute("""
                INSERT INTO vulnerability_history(mac_address, port, vuln_id, event)
                SELECT mac_address, port, vuln_id, 'seen'
                FROM vulnerabilities
                WHERE mac_address=? AND vuln_id=? LIMIT 1
            """, (mac_address, vuln_id))
    
    def update_vulnerability_status_by_port(self, mac_address: str, port: int, current_vulns: List[str]):
        """Update vulnerability status for a specific port to avoid NULL conflicts"""
        port = int(port) if port is not None else 0
        
        existing = self.base.query(
            "SELECT vuln_id FROM vulnerabilities WHERE mac_address=? AND COALESCE(port, 0)=? AND is_active=1",
            (mac_address, port)
        )
        existing_ids = {r['vuln_id'] for r in existing}
        current_set = set(current_vulns)
        
        # Mark inactive (for this specific port)
        for vuln_id in (existing_ids - current_set):
            self.base.execute("""
                UPDATE vulnerabilities 
                SET is_active=0, last_seen=CURRENT_TIMESTAMP
                WHERE mac_address=? AND vuln_id=? AND COALESCE(port, 0)=? AND is_active=1
            """, (mac_address, vuln_id, port))
            
            self.base.execute("""
                INSERT INTO vulnerability_history(mac_address, ip, hostname, port, vuln_id, event)
                VALUES (?, NULL, NULL, ?, ?, 'inactive')
            """, (mac_address, port, vuln_id))
        
        # Add new (calls your existing method with the port)
        for vuln_id in (current_set - existing_ids):
            self.add_vulnerability(mac_address, vuln_id, port=port)
        
        # Mark as seen (for this specific port)
        for vuln_id in (current_set & existing_ids):
            self.base.execute("""
                UPDATE vulnerabilities
                SET last_seen=CURRENT_TIMESTAMP
                WHERE mac_address=? AND vuln_id=? AND COALESCE(port, 0)=? AND is_active=1
            """, (mac_address, vuln_id, port))
            
            self.base.execute("""
                INSERT INTO vulnerability_history(mac_address, ip, hostname, port, vuln_id, event)
                VALUES (?, NULL, NULL, ?, ?, 'seen')
            """, (mac_address, port, vuln_id))
    
    def save_vulnerabilities(self, mac: str, ip: str, findings: List[Dict]):
        """Separate CPE and CVE, update statuses + record new findings"""
        # Group findings by port to avoid conflicts
        findings_by_port = {}
        for f in findings:
            port = f.get('port', 0)
            if port is None:
                port = 0
            port = int(port) if port != 0 else 0
            
            if port not in findings_by_port:
                findings_by_port[port] = {'cves': set(), 'cpes': set(), 'findings': []}
            
            findings_by_port[port]['findings'].append(f)
            
            vid = str(f.get('vuln_id', ''))
            if vid.upper().startswith('CVE-'):
                findings_by_port[port]['cves'].add(vid)
            elif vid.upper().startswith('CPE:'):
                findings_by_port[port]['cpes'].add(vid.split(':', 1)[1])
            elif vid.lower().startswith('cpe:'):
                findings_by_port[port]['cpes'].add(vid)
        
        # Process CVE by port to avoid conflicts
        all_cve_ids = set()
        for port, data in findings_by_port.items():
            if data['cves']:
                try:
                    self.update_vulnerability_status_by_port(mac, port, sorted(data['cves']))
                    all_cve_ids.update(data['cves'])
                except Exception as e:
                    logger.error(f"Failed to update CVE status for port {port}: {e}")
        
        # Process CPE globally (as before) - delegated to SoftwareOps
        all_cpe_vals = set()
        for port, data in findings_by_port.items():
            all_cpe_vals.update(data['cpes'])
        
        # Note: CPE handling would typically be done by SoftwareOps
        # but we keep the call here for compatibility
        
        logger.debug(f"Processed: {len(all_cve_ids)} CVE across {len(findings_by_port)} ports, {len(all_cpe_vals)} CPE for {mac}")
    
    # =========================================================================
    # VULNERABILITY QUERY OPERATIONS
    # =========================================================================
    
    def get_all_vulns(self) -> List[Dict[str, Any]]:
        """Get all vulnerabilities with host details"""
        return self.base.query("""
            SELECT v.id, v.mac_address, v.ip, v.hostname, v.port, v.vuln_id, v.is_active, v.first_seen, v.last_seen,
                   h.ips AS host_ips, h.hostnames AS host_hostnames, h.ports AS host_ports, h.vendor AS host_vendor
            FROM vulnerabilities v
            LEFT JOIN hosts h ON v.mac_address = h.mac_address
            ORDER BY v.mac_address, v.vuln_id;
        """)
    
    def count_vulnerabilities_alive(self, distinct: bool = False, active_only: bool = True) -> int:
        """Count vulnerabilities for hosts with alive=1"""
        where = ["h.alive = 1"]
        if active_only:
            where.append("v.is_active = 1")
        where_sql = " AND ".join(where)
        
        if distinct:
            sql = f"""
                SELECT COUNT(DISTINCT v.vuln_id) AS c
                FROM vulnerabilities v
                JOIN hosts h ON h.mac_address = v.mac_address
                WHERE {where_sql}
            """
        else:
            sql = f"""
                SELECT COUNT(*) AS c
                FROM vulnerabilities v
                JOIN hosts h ON h.mac_address = v.mac_address
                WHERE {where_sql}
            """
        row = self.base.query(sql)
        return int(row[0]["c"]) if row else 0
    
    def count_distinct_vulnerabilities(self, alive_only: bool = False) -> int:
        """Return the number of distinct vulnerabilities (vuln_id)"""
        if alive_only:
            row = self.base.query("""
                SELECT COUNT(DISTINCT v.vuln_id) AS c
                  FROM vulnerabilities v
                  JOIN hosts h ON h.mac_address = v.mac_address
                 WHERE h.alive = 1
            """)
        else:
            row = self.base.query("SELECT COUNT(DISTINCT vuln_id) AS c FROM vulnerabilities")
        return int(row[0]["c"]) if row else 0
    
    def get_vulnerabilities_for_alive_hosts(self) -> List[str]:
        """Return a list of distinct vuln_id affecting hosts currently marked alive=1"""
        rows = self.base.query("""
            SELECT DISTINCT v.vuln_id
              FROM vulnerabilities v
              JOIN hosts h ON h.mac_address = v.mac_address
             WHERE h.alive = 1
        """)
        return [r["vuln_id"] for r in rows]
    
    def list_vulnerability_history(self, cve_id: str | None = None,
                                   mac: str | None = None, limit: int = 500) -> list[dict]:
        """Return vulnerability history (events) sorted most recent first"""
        where = []
        params: list = []
        if cve_id:
            where.append("vuln_id = ?")
            params.append(cve_id)
        if mac:
            where.append("mac_address = ?")
            params.append(mac)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(int(limit))
        
        return self.base.query(f"""
            SELECT mac_address, ip, hostname, port, vuln_id, event, seen_at
            FROM vulnerability_history
            {where_sql}
            ORDER BY datetime(seen_at) DESC
            LIMIT ?
        """, tuple(params))
    
    # =========================================================================
    # CLEANUP OPERATIONS
    # =========================================================================
    
    def cleanup_vulnerability_duplicates(self):
        """Clean up vulnerability duplicates"""
        self.base.invalidate_stats_cache()
        
        # Delete entries with port NULL if an entry with port=0 exists
        self.base.execute("""
            DELETE FROM vulnerabilities 
            WHERE port IS NULL 
            AND EXISTS (
                SELECT 1 FROM vulnerabilities v2 
                WHERE v2.mac_address = vulnerabilities.mac_address 
                AND v2.vuln_id = vulnerabilities.vuln_id 
                AND v2.port = 0
            )
        """)
        
        # Update remaining NULL ports to 0
        self.base.execute("""
            UPDATE vulnerabilities SET port = 0 WHERE port IS NULL
        """)
        
        # Delete true duplicates (same mac, vuln_id, port) - keep most recent
        self.base.execute("""
            DELETE FROM vulnerabilities 
            WHERE rowid NOT IN (
                SELECT MAX(rowid) 
                FROM vulnerabilities 
                GROUP BY mac_address, vuln_id, COALESCE(port, 0)
            )
        """)
    
    def fix_vulnerability_history_nulls(self):
        """Fix history entries with problematic NULL values"""
        # Update history where ports are NULL but should be 0
        self.base.execute("""
            UPDATE vulnerability_history 
            SET port = 0 
            WHERE port IS NULL 
            AND EXISTS (
                SELECT 1 FROM vulnerabilities v 
                WHERE v.mac_address = vulnerability_history.mac_address 
                AND v.vuln_id = vulnerability_history.vuln_id 
                AND v.port = 0
            )
        """)
        
        # For cases where we can't determine the port, use 0 by default
        self.base.execute("""
            UPDATE vulnerability_history 
            SET port = 0 
            WHERE port IS NULL
        """)
