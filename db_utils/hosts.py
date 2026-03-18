"""hosts.py - Host and network device management operations."""

import time
import sqlite3
from typing import Any, Dict, Iterable, List, Optional
from db_utils.base import _validate_identifier
import logging

from logger import Logger

logger = Logger(name="db_utils.hosts", level=logging.DEBUG)


class HostOps:
    """Host management and tracking operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create hosts and related tables"""
        # Main hosts table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS hosts (
              mac_address TEXT PRIMARY KEY,
              ips         TEXT,
              hostnames   TEXT,
              alive       INTEGER DEFAULT 0,
              ports       TEXT,
              vendor      TEXT,
              essid       TEXT,
              previous_hostnames TEXT,
              previous_ips       TEXT,
              previous_ports     TEXT,
              previous_essids    TEXT,
              first_seen  INTEGER,
              last_seen   INTEGER,
              updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_hosts_alive ON hosts(alive);")
        
        # Hostname history table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS hostnames_history(
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              mac_address TEXT NOT NULL,
              hostname    TEXT NOT NULL,
              first_seen  TEXT DEFAULT CURRENT_TIMESTAMP,
              last_seen   TEXT DEFAULT CURRENT_TIMESTAMP,
              is_current  INTEGER DEFAULT 1,
              UNIQUE(mac_address, hostname)
            );
        """)
        
        # Guarantee a single current hostname per MAC
        try:
            # One and only one "current" hostname row per MAC in history
            self.base.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_hostname_current
                ON hostnames_history(mac_address)
                WHERE is_current=1;
            """)
        except Exception:
            pass

        # Uniqueness for real MACs only (allows legacy stubs in old DBs but our scanner no longer writes them)
        try:
            self.base.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_hosts_real_mac
                ON hosts(mac_address)
                WHERE instr(mac_address, ':') > 0;
            """)
        except Exception:
            pass
        
        logger.debug("Hosts tables created/verified")
    
    # =========================================================================
    # HOST CRUD OPERATIONS
    # =========================================================================
    
    def get_all_hosts(self) -> List[Dict[str, Any]]:
        """Get all hosts with current/previous IPs/ports/essids ordered by liveness then MAC"""
        return self.base.query("""
            SELECT mac_address, ips, previous_ips,
                hostnames, previous_hostnames,
                alive,
                ports, previous_ports,
                vendor, essid, previous_essids,
                first_seen, last_seen
            FROM hosts
            ORDER BY alive DESC, mac_address;
        """)
    
    def update_host(self, mac_address: str, ips: Optional[str] = None,
                    hostnames: Optional[str] = None, alive: Optional[int] = None,
                    ports: Optional[str] = None, vendor: Optional[str] = None,
                    essid: Optional[str] = None):
        """
        Partial upsert of the host row. None/'' fields do not erase existing values.
        For automatic tracking of previous_* fields, use update_*_current helpers instead.
        """
        # --- Hardening: normalize and guard ---
        # Always store normalized lowercase MACs; refuse 'ip:' stubs defensively.
        mac_address = (mac_address or "").strip().lower()
        if mac_address.startswith("ip:"):
            raise ValueError("stub MAC not allowed (scanner runs in no-stub mode)")

        self.base.invalidate_stats_cache()
        
        now = int(time.time())
        
        self.base.execute("""
            INSERT INTO hosts(mac_address, ips, hostnames, alive, ports, vendor, essid, 
                            first_seen, last_seen, updated_at)
            VALUES(?, ?, ?, COALESCE(?, 0), ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(mac_address) DO UPDATE SET
              ips       = COALESCE(NULLIF(excluded.ips, ''), hosts.ips),
              hostnames = COALESCE(NULLIF(excluded.hostnames, ''), hosts.hostnames),
              alive     = COALESCE(excluded.alive, hosts.alive),
              ports     = COALESCE(NULLIF(excluded.ports, ''), hosts.ports),
              vendor    = COALESCE(NULLIF(excluded.vendor, ''), hosts.vendor),
              essid     = COALESCE(NULLIF(excluded.essid, ''), hosts.essid),
              last_seen = ?,
              updated_at= CURRENT_TIMESTAMP;
        """, (mac_address, ips, hostnames, alive, ports, vendor, essid, now, now, now))
    
    # =========================================================================
    # HOSTNAME OPERATIONS
    # =========================================================================
    
    def update_hostname(self, mac_address: str, new_hostname: str):
        """Update current hostname + track previous/current in both hosts and history tables"""
        new_hostname = (new_hostname or "").strip()
        if not new_hostname:
            return
        
        with self.base.transaction(immediate=True):
            row = self.base.query(
                "SELECT hostnames, previous_hostnames FROM hosts WHERE mac_address=? LIMIT 1;",
                (mac_address,)
            )
            curr = (row[0]["hostnames"] or "") if row else ""
            prev = (row[0]["previous_hostnames"] or "") if row else ""
            
            curr_list = [h for h in curr.split(';') if h]
            prev_list = [h for h in prev.split(';') if h]
            
            if new_hostname in curr_list:
                curr_list = [new_hostname] + [h for h in curr_list if h != new_hostname]
                next_curr = ';'.join(curr_list)
                next_prev = ';'.join(prev_list)
            else:
                merged_prev = list(dict.fromkeys(curr_list + prev_list))[:50]  # cap at 50
                next_curr = new_hostname
                next_prev = ';'.join(merged_prev)
            
            self.base.execute("""
                INSERT INTO hosts(mac_address, hostnames, previous_hostnames, updated_at)
                VALUES(?,?,?,CURRENT_TIMESTAMP)
                ON CONFLICT(mac_address) DO UPDATE SET
                  hostnames = excluded.hostnames,
                  previous_hostnames = excluded.previous_hostnames,
                  updated_at = CURRENT_TIMESTAMP;
            """, (mac_address, next_curr, next_prev))
            
            # Update hostname history table
            self.base.execute("""
                UPDATE hostnames_history
                SET is_current=0, last_seen=CURRENT_TIMESTAMP
                WHERE mac_address=? AND is_current=1;
            """, (mac_address,))
            
            self.base.execute("""
                INSERT INTO hostnames_history(mac_address, hostname, is_current)
                VALUES(?,?,1)
                ON CONFLICT(mac_address, hostname) DO UPDATE SET
                  is_current=1, last_seen=CURRENT_TIMESTAMP;
            """, (mac_address, new_hostname))
    
    def get_current_hostname(self, mac_address: str) -> Optional[str]:
        """Get the current hostname from history when available; fallback to hosts.hostnames"""
        row = self.base.query("""
            SELECT hostname FROM hostnames_history
            WHERE mac_address=? AND is_current=1 LIMIT 1;
        """, (mac_address,))
        if row:
            return row[0]["hostname"]
        
        row = self.base.query("SELECT hostnames FROM hosts WHERE mac_address=? LIMIT 1;", (mac_address,))
        if row and row[0]["hostnames"]:
            return row[0]["hostnames"].split(';', 1)[0]
        return None
    
    def record_hostname_seen(self, mac_address: str, hostname: str):
        """Alias for update_hostname: mark a hostname as seen/current"""
        self.update_hostname(mac_address, hostname)
    
    def list_hostname_history(self, mac_address: str) -> List[Dict[str, Any]]:
        """Return the full hostname history for a MAC (current first)"""
        return self.base.query("""
            SELECT hostname, first_seen, last_seen, is_current
              FROM hostnames_history
             WHERE mac_address=?
             ORDER BY is_current DESC, last_seen DESC, first_seen DESC;
        """, (mac_address,))
    
    # =========================================================================
    # IP OPERATIONS
    # =========================================================================
    
    def update_ips_current(self, mac_address: str, current_ips: Iterable[str], cap_prev: int = 200):
        """Replace current IP set and roll removed IPs into previous_ips (deduped, size-capped)"""
        cur_set = {ip.strip() for ip in (current_ips or []) if ip}
        row = self.base.query("SELECT ips, previous_ips FROM hosts WHERE mac_address=? LIMIT 1;", (mac_address,))
        prev_cur = set(self._parse_list(row[0]["ips"])) if row else set()
        prev_prev = set(self._parse_list(row[0]["previous_ips"])) if row else set()
        
        removed = prev_cur - cur_set
        prev_prev |= removed
        
        if len(prev_prev) > cap_prev:
            prev_prev = set(sorted(prev_prev, key=self._sort_ip_key)[:cap_prev])
        
        ips_sorted = ";".join(sorted(cur_set, key=self._sort_ip_key))
        prev_sorted = ";".join(sorted(prev_prev, key=self._sort_ip_key))
        
        self.base.execute("""
            INSERT INTO hosts(mac_address, ips, previous_ips, updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(mac_address) DO UPDATE SET
              ips = excluded.ips,
              previous_ips = excluded.previous_ips,
              updated_at = CURRENT_TIMESTAMP;
        """, (mac_address, ips_sorted, prev_sorted))
    
    # =========================================================================
    # PORT OPERATIONS
    # =========================================================================
    
    def update_ports_current(self, mac_address: str, current_ports: Iterable[int], cap_prev: int = 500):
        """Replace current port set and roll removed ports into previous_ports (deduped, size-capped)"""
        cur_set = set(int(p) for p in (current_ports or []) if str(p).isdigit())
        row = self.base.query("SELECT ports, previous_ports FROM hosts WHERE mac_address=? LIMIT 1;", (mac_address,))
        prev_cur = set(int(p) for p in self._parse_list(row[0]["ports"])) if row else set()
        prev_prev = set(int(p) for p in self._parse_list(row[0]["previous_ports"])) if row else set()
        
        removed = prev_cur - cur_set
        prev_prev |= removed
        
        if len(prev_prev) > cap_prev:
            prev_prev = set(sorted(prev_prev)[:cap_prev])
        
        ports_sorted = ";".join(str(p) for p in sorted(cur_set))
        prev_sorted = ";".join(str(p) for p in sorted(prev_prev))
        
        self.base.execute("""
            INSERT INTO hosts(mac_address, ports, previous_ports, updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(mac_address) DO UPDATE SET
              ports = excluded.ports,
              previous_ports = excluded.previous_ports,
              updated_at = CURRENT_TIMESTAMP;
        """, (mac_address, ports_sorted, prev_sorted))
    
    # =========================================================================
    # ESSID OPERATIONS
    # =========================================================================
    
    def update_essid_current(self, mac_address: str, new_essid: Optional[str], cap_prev: int = 50):
        """Update current ESSID and move previous one into previous_essids if it changed"""
        new_essid = (new_essid or "").strip()
        
        row = self.base.query(
            "SELECT essid, previous_essids FROM hosts WHERE mac_address=? LIMIT 1;",
            (mac_address,)
        )
        
        if row:
            old = (row[0]["essid"] or "").strip()
            prev_prev = self._parse_list(row[0]["previous_essids"]) or []
        else:
            old = ""
            prev_prev = []
        
        if old and new_essid and new_essid == old:
            essid = new_essid
            prev_joined = ";".join(prev_prev)
        else:
            if old and old not in prev_prev:
                prev_prev = [old] + prev_prev
                prev_prev = prev_prev[:cap_prev]
            essid = new_essid
            prev_joined = ";".join(prev_prev)
        
        self.base.execute("""
            INSERT INTO hosts(mac_address, essid, previous_essids, updated_at)
            VALUES(?,?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(mac_address) DO UPDATE SET
              essid = excluded.essid,
              previous_essids = excluded.previous_essids,
              updated_at = CURRENT_TIMESTAMP;
        """, (mac_address, essid, prev_joined))
    
    # =========================================================================
    # IP STUB MERGING
    # =========================================================================
    
    def merge_ip_stub_into_real(self, ip: str, real_mac: str, 
                                hostname: Optional[str] = None, essid_hint: Optional[str] = None):
        """
        Merge a host 'IP:<ip>' stub with the host at 'real_mac' (if present) or rename the stub.
        - Unifies ips, hostnames, ports, vendor, essid, first_seen/last_seen, alive.
        - Updates tables that have a 'mac_address' column to point to the real MAC.
        - SSID tolerance (if one of the two is empty, keep the present one).
        - If the host 'real_mac' doesn't exist yet, simply rename the stub -> real_mac.
        """
        if not real_mac or ':' not in real_mac:
            return  # nothing to do if we don't have a real MAC
        
        now = int(time.time())
        stub_key = f"IP:{ip}".lower()
        real_key = real_mac.lower()
        
        with self.base._lock:
            con = self.base._conn
            cur = con.cursor()
            
            # Retrieve stub candidates (by mac=IP:ip) + fallback by ip contained and mac 'IP:%'
            cur.execute("""
                SELECT * FROM hosts
                WHERE lower(mac_address)=? 
                OR (lower(mac_address) LIKE 'ip:%' AND (ips LIKE '%'||?||'%'))
                ORDER BY lower(mac_address)=? DESC
                LIMIT 1
            """, (stub_key, ip, stub_key))
            stub = cur.fetchone()
            
            # Nothing to merge?
            cur.execute("SELECT * FROM hosts WHERE lower(mac_address)=? LIMIT 1", (real_key,))
            real = cur.fetchone()
            
            if not stub and not real:
                # No record: create the real one directly
                cur.execute("""INSERT OR IGNORE INTO hosts
                            (mac_address, ips, hostnames, ports, vendor, essid, alive, first_seen, last_seen)
                            VALUES (?,?,?,?,?,?,1,?,?)""",
                            (real_key, ip, hostname or None, None, None, essid_hint or None, now, now))
                con.commit()
                return
            
            if stub and not real:
                # Rename the stub -> real MAC
                ips_merged   = self._union_semicol(stub['ips'], ip, sort_ip=True)
                hosts_merged = self._union_semicol(stub['hostnames'], hostname)
                essid_final  = stub['essid'] or essid_hint
                vendor_final = stub['vendor']
                
                cur.execute("""UPDATE hosts SET
                                mac_address=?,
                                ips=?,
                                hostnames=?,
                                essid=COALESCE(?, essid),
                                alive=1,
                                last_seen=? 
                            WHERE lower(mac_address)=?""",
                            (real_key, ips_merged, hosts_merged, essid_final, now, stub['mac_address'].lower()))
                
                # Redirect references from other tables (if they exist)
                self._redirect_mac_references(cur, stub['mac_address'].lower(), real_key)
                con.commit()
                return
            
            if stub and real:
                # Full merge into the real, then delete stub
                ips_merged   = self._union_semicol(real['ips'], stub['ips'], sort_ip=True)
                ips_merged   = self._union_semicol(ips_merged, ip, sort_ip=True)
                hosts_merged = self._union_semicol(real['hostnames'], stub['hostnames'])
                hosts_merged = self._union_semicol(hosts_merged, hostname)
                ports_merged = self._union_semicol(real['ports'], stub['ports'])
                vendor_final = real['vendor'] or stub['vendor']
                essid_final  = real['essid'] or stub['essid'] or essid_hint
                first_seen   = min(int(real['first_seen'] or now), int(stub['first_seen'] or now))
                last_seen    = max(int(real['last_seen'] or now), int(stub['last_seen'] or now), now)
                
                cur.execute("""UPDATE hosts SET
                                ips=?,
                                hostnames=?,
                                ports=?,
                                vendor=COALESCE(?, vendor),
                                essid=COALESCE(?, essid),
                                alive=1,
                                first_seen=?,
                                last_seen=?
                            WHERE lower(mac_address)=?""",
                            (ips_merged, hosts_merged, ports_merged, vendor_final, essid_final,
                            first_seen, last_seen, real_key))
                
                # Redirect references to real_key then delete stub
                self._redirect_mac_references(cur, stub['mac_address'].lower(), real_key)
                cur.execute("DELETE FROM hosts WHERE lower(mac_address)=?", (stub['mac_address'].lower(),))
                con.commit()
                return
            
            # No stub but a real exists already: ensure current IP/hostname are unified
            if real and not stub:
                ips_merged   = self._union_semicol(real['ips'], ip, sort_ip=True)
                hosts_merged = self._union_semicol(real['hostnames'], hostname)
                essid_final  = real['essid'] or essid_hint
                cur.execute("""UPDATE hosts SET
                                ips=?,
                                hostnames=?,
                                essid=COALESCE(?, essid),
                                alive=1,
                                last_seen=?
                            WHERE lower(mac_address)=?""",
                                (ips_merged, hosts_merged, essid_final, now, real_key))
                con.commit()
    
    def _redirect_mac_references(self, cur, old_mac: str, new_mac: str):
        """Redirect mac_address references in all relevant tables"""
        try:
            # Discover all tables with a mac_address column
            cur.execute("""SELECT name FROM sqlite_master 
                        WHERE type='table' AND name NOT LIKE 'sqlite_%'""")
            for (tname,) in cur.fetchall():
                if tname == 'hosts':
                    continue
                try:
                    _validate_identifier(tname, "table name")
                    cur.execute(f"PRAGMA table_info({tname})")
                    cols = [r[1].lower() for r in cur.fetchall()]
                    if 'mac_address' in cols:
                        cur.execute(f"""UPDATE {tname}
                                        SET mac_address=?
                                        WHERE lower(mac_address)=?""",
                                    (new_mac, old_mac))
                except Exception:
                    pass
        except Exception:
            pass
    
    # =========================================================================
    # HELPER METHODS
    # =========================================================================
    
    def _parse_list(self, s: Optional[str]) -> List[str]:
        """Parse a semicolon-separated string into a list, ignoring empties"""
        return [x for x in (s or "").split(";") if x]
    
    def _sort_ip_key(self, ip: str):
        """Return a sortable key for IPv4 addresses; non-IPv4 sorts last"""
        if ip and ip.count(".") == 3:
            try:
                return tuple(int(x) for x in ip.split("."))
            except Exception:
                return (0, 0, 0, 0)
        return (0, 0, 0, 0)
    
    def _union_semicol(self, *values: Optional[str], sort_ip: bool = False) -> str:
        """Union deduplicated of semicolon-separated lists (ignores empties)"""
        def _key(x):
            if sort_ip and x.count('.') == 3:
                try:
                    return tuple(map(int, x.split('.')))
                except Exception:
                    return (0, 0, 0, 0)
            return x
        
        s = set()
        for v in values:
            if not v:
                continue
            for it in str(v).split(';'):
                it = it.strip()
                if it:
                    s.add(it)
        if not s:
            return ""
        return ';'.join(sorted(s, key=_key))
