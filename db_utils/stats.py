"""stats.py - Statistics tracking and display operations."""

import time
import sqlite3
from typing import Dict
import logging

from logger import Logger

logger = Logger(name="db_utils.stats", level=logging.DEBUG)


class StatsOps:
    """Statistics tracking and display operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create stats table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS stats (
              id INTEGER PRIMARY KEY CHECK (id=1),
              total_open_ports       INTEGER DEFAULT 0,
              alive_hosts_count      INTEGER DEFAULT 0,
              all_known_hosts_count  INTEGER DEFAULT 0,
              vulnerabilities_count  INTEGER DEFAULT 0,
              actions_count          INTEGER DEFAULT 0,
              zombie_count           INTEGER DEFAULT 0
            );
        """)
        logger.debug("Stats table created/verified")
    
    def ensure_stats_initialized(self):
        """Ensure the singleton row in `stats` exists"""
        row = self.base.query("SELECT 1 FROM stats WHERE id=1")
        if not row:
            self.base.execute("INSERT INTO stats(id) VALUES(1);")
    
    # =========================================================================
    # STATS OPERATIONS
    # =========================================================================
    
    def get_livestats(self) -> Dict[str, int]:
        """Return the live counters maintained in the `stats` singleton row"""
        row = self.base.query("""
            SELECT total_open_ports, alive_hosts_count, all_known_hosts_count, vulnerabilities_count
            FROM stats WHERE id=1
        """)[0]
        return {
            "total_open_ports": int(row["total_open_ports"]),
            "alive_hosts_count": int(row["alive_hosts_count"]),
            "all_known_hosts_count": int(row["all_known_hosts_count"]),
            "vulnerabilities_count": int(row["vulnerabilities_count"]),
        }
    
    def update_livestats(self, total_open_ports: int, alive_hosts_count: int,
                        all_known_hosts_count: int, vulnerabilities_count: int):
        """Update the live stats counters (touch in-place)"""
        self.base.invalidate_stats_cache()
        self.base.execute("""
            UPDATE stats
            SET total_open_ports = ?,
                alive_hosts_count = ?,
                all_known_hosts_count = ?,
                vulnerabilities_count = ?
            WHERE id = 1;
        """, (int(total_open_ports), int(alive_hosts_count),
              int(all_known_hosts_count), int(vulnerabilities_count)))
    
    def get_stats(self) -> Dict[str, int]:
        """Compatibility alias to retrieve stats; ensures the singleton row exists"""
        self.ensure_stats_initialized()
        row = self.base.query("SELECT total_open_ports, alive_hosts_count, all_known_hosts_count, vulnerabilities_count FROM stats WHERE id=1;")
        r = row[0]
        return {
            "total_open_ports": int(r["total_open_ports"]),
            "alive_hosts_count": int(r["alive_hosts_count"]),
            "all_known_hosts_count": int(r["all_known_hosts_count"]),
            "vulnerabilities_count": int(r["vulnerabilities_count"]),
        }
    
    def set_stats(self, total_open_ports: int, alive_hosts_count: int, 
                  all_known_hosts_count: int, vulnerabilities_count: int):
        """Compatibility alias that forwards to update_livestats"""
        self.update_livestats(total_open_ports, alive_hosts_count, all_known_hosts_count, vulnerabilities_count)
    
    def get_display_stats(self) -> Dict[str, int]:
        """
        Cached bundle of counters for quick UI refresh using stats table.
        """
        now = time.time()
        
        # Serve from cache when valid
        if self.base._stats_cache['data'] and (now - self.base._stats_cache['timestamp']) < self.base._cache_ttl:
            return self.base._stats_cache['data'].copy()
        
        # Compute fresh counters
        with self.base._lock:
            try:
                # Use stats table for pre-calculated values
                result = self.base.query_one("""
                    SELECT 
                        s.total_open_ports,
                        s.alive_hosts_count,
                        s.all_known_hosts_count,
                        s.vulnerabilities_count,
                        COALESCE(s.actions_count, 
                            (SELECT COUNT(*) FROM actions WHERE b_enabled = 1)
                        ) as actions_count,
                        COALESCE(s.zombie_count, 0) as zombie_count,
                        (SELECT COUNT(*) FROM creds) as creds
                    FROM stats s
                    WHERE s.id = 1
                """)
                
                if result:
                    stats = {
                        'alive_hosts_count': int(result['alive_hosts_count'] or 0),
                        'all_known_hosts_count': int(result['all_known_hosts_count'] or 0),
                        'total_open_ports': int(result['total_open_ports'] or 0),
                        'vulnerabilities_count': int(result['vulnerabilities_count'] or 0),
                        'credentials_count': int(result['creds'] or 0),
                        'actions_count': int(result['actions_count'] or 0),
                        'zombie_count': int(result['zombie_count'] or 0)
                    }
                else:
                    # Fallback if no stats row
                    stats = {
                        'alive_hosts_count': 0,
                        'all_known_hosts_count': 0,
                        'total_open_ports': 0,
                        'vulnerabilities_count': 0,
                        'credentials_count': 0,
                        'actions_count': 0,
                        'zombie_count': 0
                    }
                
                # Update cache
                self.base._stats_cache = {'data': stats, 'timestamp': now}
                return stats
                
            except Exception:
                return {
                    'alive_hosts_count': 0,
                    'all_known_hosts_count': 0,
                    'total_open_ports': 0,
                    'vulnerabilities_count': 0,
                    'credentials_count': 0,
                    'actions_count': 0,
                    'zombie_count': 0
                }
