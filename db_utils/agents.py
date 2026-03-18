"""agents.py - C2 agent management operations."""

import json
import os
import sqlite3
from typing import List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.agents", level=logging.DEBUG)


class AgentOps:
    """C2 agent tracking and command history operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create C2 agent tables"""
        # Agents table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS agents (
                id TEXT PRIMARY KEY,
                hostname TEXT,
                platform TEXT,
                os_version TEXT,
                architecture TEXT,
                ip_address TEXT,
                first_seen TIMESTAMP,
                last_seen TIMESTAMP,
                status TEXT,
                notes TEXT
            );
        """)
        
        # Indexes for performance
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_agents_last_seen ON agents(last_seen);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);")
        
        # Commands table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT,
                command TEXT,
                timestamp TIMESTAMP,
                response TEXT,
                success BOOLEAN,
                FOREIGN KEY (agent_id) REFERENCES agents (id)
            );
        """)
        
        # Agent keys (versioned for rotation)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS agent_keys (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id    TEXT NOT NULL,
                key_b64     TEXT NOT NULL,
                version     INTEGER NOT NULL,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                rotated_at  TIMESTAMP,
                revoked_at  TIMESTAMP,
                active      INTEGER DEFAULT 1,
                UNIQUE(agent_id, version)
            );
        """)
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_agent_keys_active ON agent_keys(agent_id, active);")
        
        # Loot table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS loot (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT,
                filename TEXT,
                filepath TEXT,
                size INTEGER,
                timestamp TIMESTAMP,
                hash TEXT,
                FOREIGN KEY (agent_id) REFERENCES agents (id)
            );
        """)
        
        # Telemetry table
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id TEXT,
                cpu_percent REAL,
                mem_percent REAL,
                disk_percent REAL,
                uptime INTEGER,
                timestamp TIMESTAMP,
                FOREIGN KEY (agent_id) REFERENCES agents (id)
            );
        """)
        
        logger.debug("C2 agent tables created/verified")
    
    # =========================================================================
    # AGENT OPERATIONS
    # =========================================================================
    
    def save_agent(self, agent_data: dict) -> None:
        """
        Upsert an agent preserving first_seen and updating last_seen.
        Status field expected as str (e.g. 'online'/'offline').
        """
        agent_id     = agent_data.get('id')
        hostname     = agent_data.get('hostname')
        platform_    = agent_data.get('platform')
        os_version   = agent_data.get('os_version')
        arch         = agent_data.get('architecture')
        ip_address   = agent_data.get('ip_address')
        status       = agent_data.get('status') or 'offline'
        notes        = agent_data.get('notes')
        
        if not agent_id:
            raise ValueError("save_agent: 'id' is required in agent_data")
        
        # Upsert that preserves first_seen and updates last_seen to NOW
        self.base.execute("""
            INSERT INTO agents (id, hostname, platform, os_version, architecture, ip_address,
                                first_seen, last_seen, status, notes)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                hostname   = COALESCE(excluded.hostname,   agents.hostname),
                platform   = COALESCE(excluded.platform,   agents.platform),
                os_version = COALESCE(excluded.os_version, agents.os_version),
                architecture = COALESCE(excluded.architecture, agents.architecture),
                ip_address = COALESCE(excluded.ip_address, agents.ip_address),
                first_seen = COALESCE(agents.first_seen, excluded.first_seen, CURRENT_TIMESTAMP),
                last_seen  = CURRENT_TIMESTAMP,
                status     = COALESCE(excluded.status, agents.status),
                notes      = COALESCE(excluded.notes,  agents.notes)
        """, (agent_id, hostname, platform_, os_version, arch, ip_address, status, notes))
        
        # Optionally refresh zombie counter
        try:
            self._refresh_zombie_counter()
        except Exception:
            pass
    
    def save_command(self, agent_id: str, command: str,
                     response: str | None = None, success: bool = False) -> None:
        """Record a command history entry"""
        if not agent_id or not command:
            raise ValueError("save_command: 'agent_id' and 'command' are required")
        self.base.execute("""
            INSERT INTO commands (agent_id, command, timestamp, response, success)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?)
        """, (agent_id, command, response, 1 if success else 0))
    
    def save_telemetry(self, agent_id: str, telemetry: dict) -> None:
        """Record a telemetry snapshot for an agent"""
        if not agent_id:
            raise ValueError("save_telemetry: 'agent_id' is required")
        self.base.execute("""
            INSERT INTO telemetry (agent_id, cpu_percent, mem_percent, disk_percent, uptime, timestamp)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (
            agent_id,
            telemetry.get('cpu_percent'),
            telemetry.get('mem_percent'),
            telemetry.get('disk_percent'),
            telemetry.get('uptime')
        ))
    
    def save_loot(self, loot: dict) -> None:
        """
        Record a retrieved file (loot).
        Expected: {'agent_id', 'filename', 'filepath', 'size', 'hash'}
        Timestamp is added database-side.
        """
        if not loot or not loot.get('agent_id') or not loot.get('filename'):
            raise ValueError("save_loot: 'agent_id' and 'filename' are required")
        
        self.base.execute("""
            INSERT INTO loot (agent_id, filename, filepath, size, timestamp, hash)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
        """, (
            loot.get('agent_id'),
            loot.get('filename'),
            loot.get('filepath'),
            int(loot.get('size') or 0),
            loot.get('hash')
        ))
    
    def get_agent_history(self, agent_id: str) -> List[dict]:
        """
        Return the 100 most recent commands for an agent (most recent first).
        """
        if not agent_id:
            return []
        rows = self.base.query("""
            SELECT command, timestamp, response, success
            FROM commands
            WHERE agent_id = ?
            ORDER BY datetime(timestamp) DESC
            LIMIT 100
        """, (agent_id,))
        # Normalize success to bool
        for r in rows:
            r['success'] = bool(r.get('success'))
        return rows
    
    def purge_stale_agents(self, threshold_seconds: int) -> int:
        """
        Delete agents whose last_seen is older than now - threshold_seconds.
        Returns the number of deleted rows.
        """
        if not threshold_seconds or threshold_seconds <= 0:
            return 0
        
        return self.base.execute("""
            DELETE FROM agents
            WHERE last_seen IS NOT NULL
            AND datetime(last_seen) < datetime('now', ?)
        """, (f'-{threshold_seconds} seconds',))
    
    def get_stale_agents(self, threshold_seconds: int) -> list[dict]:
        """
        Return the list of agents whose last_seen is older than now - threshold_seconds.
        Useful for detecting/purging inactive agents.
        """
        if not threshold_seconds or threshold_seconds <= 0:
            return []
        
        rows = self.base.query("""
            SELECT *
            FROM agents
            WHERE last_seen IS NOT NULL
            AND datetime(last_seen) < datetime('now', ?)
        """, (f'-{threshold_seconds} seconds',))
        
        return rows or []
    
    # =========================================================================
    # AGENT KEY MANAGEMENT
    # =========================================================================
    
    def get_active_key(self, agent_id: str) -> str | None:
        """Return the active key (base64) for an agent, or None"""
        row = self.base.query_one("""
            SELECT key_b64 FROM agent_keys
            WHERE agent_id=? AND active=1
            ORDER BY version DESC
            LIMIT 1
        """, (agent_id,))
        return row["key_b64"] if row else None
    
    def list_keys(self, agent_id: str) -> list[dict]:
        """List all keys for an agent (versions, states)"""
        return self.base.query("""
            SELECT id, agent_id, key_b64, version, created_at, rotated_at, revoked_at, active
            FROM agent_keys
            WHERE agent_id=?
            ORDER BY version DESC
        """, (agent_id,))
    
    def _next_key_version(self, agent_id: str) -> int:
        """Get next key version number for an agent"""
        row = self.base.query_one("SELECT COALESCE(MAX(version),0) AS v FROM agent_keys WHERE agent_id=?", (agent_id,))
        return int(row["v"] or 0) + 1
    
    def save_new_key(self, agent_id: str, key_b64: str) -> int:
        """
        Record a first key for an agent (if no existing key).
        Returns the version created.
        """
        v = self._next_key_version(agent_id)
        self.base.execute("""
            INSERT INTO agent_keys(agent_id, key_b64, version, active)
            VALUES(?,?,?,1)
        """, (agent_id, key_b64, v))
        return v
    
    def rotate_key(self, agent_id: str, new_key_b64: str) -> int:
        """
        Rotation: disable old active key (rotated_at), insert new one in version+1 active=1.
        Returns the new version.
        """
        with self.base.transaction():
            # Disable existing active key
            self.base.execute("""
                UPDATE agent_keys
                   SET active=0, rotated_at=CURRENT_TIMESTAMP
                 WHERE agent_id=? AND active=1
            """, (agent_id,))
            # Insert new
            v = self._next_key_version(agent_id)
            self.base.execute("""
                INSERT INTO agent_keys(agent_id, key_b64, version, active)
                VALUES(?,?,?,1)
            """, (agent_id, new_key_b64, v))
        return v
    
    def revoke_keys(self, agent_id: str) -> int:
        """
        Total revocation: active=0 + revoked_at now for all agent keys.
        Returns the number of affected rows.
        """
        return self.base.execute("""
            UPDATE agent_keys
               SET active=0, revoked_at=CURRENT_TIMESTAMP
             WHERE agent_id=? AND active=1
        """, (agent_id,))
    
    def verify_client_key(self, agent_id: str, key_b64: str) -> bool:
        """True if the provided key matches an active key for this agent"""
        row = self.base.query_one("""
            SELECT 1 FROM agent_keys
            WHERE agent_id=? AND key_b64=? AND active=1
            LIMIT 1
        """, (agent_id, key_b64))
        return bool(row)
    
    def migrate_keys_from_file(self, json_path: str) -> int:
        """
        One-shot migration from a keys.json in format {agent_id: key_b64}.
        For each agent: if no active key, create it in version 1.
        Returns the number of keys inserted.
        """
        if not json_path or not os.path.exists(json_path):
            return 0
        inserted = 0
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return 0
            with self.base.transaction():
                for agent_id, key_b64 in data.items():
                    if not self.get_active_key(agent_id):
                        self.save_new_key(agent_id, key_b64)
                        inserted += 1
        except Exception:
            pass
        return inserted
    
    # =========================================================================
    # HELPER METHODS
    # =========================================================================
    
    def _refresh_zombie_counter(self) -> None:
        """
        Update stats.zombie_count with the number of online agents.
        Won't fail if the column doesn't exist yet.
        """
        try:
            row = self.base.query_one("SELECT COUNT(*) AS c FROM agents WHERE LOWER(status)='online';")
            count = int(row['c'] if row else 0)
            updated = self.base.execute("UPDATE stats SET zombie_count=? WHERE id=1;", (count,))
            if not updated:
                # Ensure singleton row exists
                self.base.execute("INSERT OR IGNORE INTO stats(id) VALUES(1);")
                self.base.execute("UPDATE stats SET zombie_count=? WHERE id=1;", (count,))
        except sqlite3.OperationalError:
            # Column absent: add it properly and retry
            try:
                self.base.execute("ALTER TABLE stats ADD COLUMN zombie_count INTEGER DEFAULT 0;")
                self.base.execute("UPDATE stats SET zombie_count=0 WHERE id=1;")
                row = self.base.query_one("SELECT COUNT(*) AS c FROM agents WHERE LOWER(status)='online';")
                count = int(row['c'] if row else 0)
                self.base.execute("UPDATE stats SET zombie_count=? WHERE id=1;", (count,))
            except Exception:
                pass
