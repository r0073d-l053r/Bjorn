"""services.py - Per-port service fingerprinting and tracking."""

from typing import Dict, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.services", level=logging.DEBUG)


class ServiceOps:
    """Per-port service fingerprinting and tracking operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create port services tables"""
        # PORT SERVICES (current view of per-port fingerprinting)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS port_services (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              mac_address  TEXT NOT NULL,
              ip           TEXT,
              port         INTEGER NOT NULL,
              protocol     TEXT DEFAULT 'tcp',
              state        TEXT DEFAULT 'open',
              service      TEXT,
              product      TEXT,
              version      TEXT,
              banner       TEXT,
              fingerprint  TEXT,
              confidence   REAL,
              source       TEXT DEFAULT 'ml',
              first_seen   TEXT DEFAULT CURRENT_TIMESTAMP,
              last_seen    TEXT DEFAULT CURRENT_TIMESTAMP,
              is_current   INTEGER DEFAULT 1,
              UNIQUE(mac_address, port, protocol)
            );
        """)
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_ps_mac_port ON port_services(mac_address, port);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_ps_state ON port_services(state);")
        
        # Per-port service history (immutable log of changes)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS port_service_history (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              mac_address  TEXT NOT NULL,
              ip           TEXT,
              port         INTEGER NOT NULL,
              protocol     TEXT DEFAULT 'tcp',
              state        TEXT,
              service      TEXT,
              product      TEXT,
              version      TEXT,
              banner       TEXT,
              fingerprint  TEXT,
              confidence   REAL,
              source       TEXT,
              seen_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        logger.debug("Port services tables created/verified")
    
    # =========================================================================
    # SERVICE CRUD OPERATIONS
    # =========================================================================
    
    def upsert_port_service(
        self,
        mac_address: str,
        ip: Optional[str],
        port: int,
        *,
        protocol: str = "tcp",
        state: str = "open",
        service: Optional[str] = None,
        product: Optional[str] = None,
        version: Optional[str] = None,
        banner: Optional[str] = None,
        fingerprint: Optional[str] = None,
        confidence: Optional[float] = None,
        source: str = "ml",
        touch_history_on_change: bool = True,
    ):
        """
        Create/update the current (service,fingerprint,...) for a given (mac,port,proto).
        Also refresh hosts.ports aggregate so legacy code keeps working.
        """
        self.base.invalidate_stats_cache()
        
        with self.base.transaction(immediate=True):
            prev = self.base.query(
                """SELECT * FROM port_services
                   WHERE mac_address=? AND port=? AND protocol=? LIMIT 1""",
                (mac_address, int(port), protocol)
            )
            
            if prev:
                p = prev[0]
                changed = any([
                    state != p.get("state"),
                    service != p.get("service"),
                    product != p.get("product"),
                    version != p.get("version"),
                    banner != p.get("banner"),
                    fingerprint != p.get("fingerprint"),
                    (confidence is not None and confidence != p.get("confidence")),
                ])
                
                if touch_history_on_change and changed:
                    self.base.execute("""
                        INSERT INTO port_service_history
                        (mac_address, ip, port, protocol, state, service, product, version, banner, fingerprint, confidence, source)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (mac_address, ip, int(port), protocol, state, service, product, version, banner, fingerprint, confidence, source))
                
                self.base.execute("""
                    UPDATE port_services
                    SET ip=?, state=?, service=?, product=?, version=?, 
                        banner=?, fingerprint=?, confidence=?, source=?,
                        last_seen=CURRENT_TIMESTAMP
                    WHERE mac_address=? AND port=? AND protocol=?
                """, (ip, state, service, product, version, banner, fingerprint, confidence, source,
                      mac_address, int(port), protocol))
            else:
                self.base.execute("""
                    INSERT INTO port_services
                    (mac_address, ip, port, protocol, state, service, product, version, banner, fingerprint, confidence, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (mac_address, ip, int(port), protocol, state, service, product, version, banner, fingerprint, confidence, source))
            
            # Rebuild host ports for compatibility
            self._rebuild_host_ports(mac_address)
    
    def _rebuild_host_ports(self, mac_address: str):
        """Rebuild hosts.ports from current port_services where state='open' (tcp only)"""
        row = self.base.query("SELECT ports, previous_ports FROM hosts WHERE mac_address=? LIMIT 1;", (mac_address,))
        old_ports = set(int(p) for p in (row[0]["ports"].split(";") if row and row[0].get("ports") else []) if str(p).isdigit())
        old_prev = set(int(p) for p in (row[0]["previous_ports"].split(";") if row and row[0].get("previous_ports") else []) if str(p).isdigit())
        
        current_rows = self.base.query(
            "SELECT port FROM port_services WHERE mac_address=? AND state='open' AND protocol='tcp'",
            (mac_address,)
        )
        new_ports = set(int(r["port"]) for r in current_rows)
        
        removed = old_ports - new_ports
        new_prev = old_prev | removed
        
        ports_txt = ";".join(str(p) for p in sorted(new_ports))
        prev_txt = ";".join(str(p) for p in sorted(new_prev))
        
        self.base.execute("""
            INSERT INTO hosts(mac_address, ports, previous_ports, updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(mac_address) DO UPDATE SET
              ports = excluded.ports,
              previous_ports = excluded.previous_ports,
              updated_at = CURRENT_TIMESTAMP;
        """, (mac_address, ports_txt, prev_txt))
    
    # =========================================================================
    # SERVICE QUERY OPERATIONS
    # =========================================================================
    
    def get_services_for_host(self, mac_address: str) -> List[Dict]:
        """Return all per-port service rows for the given host, ordered by port"""
        return self.base.query("""
            SELECT port, protocol, state, service, product, version, confidence, last_seen
            FROM port_services
            WHERE mac_address=?
            ORDER BY port
        """, (mac_address,))
    
    def find_hosts_by_service(self, service: str) -> List[Dict]:
        """Return distinct host MACs that expose the given service (state='open')"""
        return self.base.query("""
            SELECT DISTINCT mac_address FROM port_services
            WHERE service=? AND state='open'
        """, (service,))
    
    def get_service_for_host_port(self, mac_address: str, port: int, protocol: str = "tcp") -> Optional[Dict]:
        """Return the single port_services row for (mac, port, protocol), if any"""
        rows = self.base.query("""
            SELECT * FROM port_services
            WHERE mac_address=? AND port=? AND protocol=? LIMIT 1
        """, (mac_address, int(port), protocol))
        return rows[0] if rows else None
