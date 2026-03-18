"""studio.py - Actions Studio visual editor operations."""

import json
import re
from typing import Dict, List, Optional
import logging

from logger import Logger
from db_utils.base import _validate_identifier

logger = Logger(name="db_utils.studio", level=logging.DEBUG)


class StudioOps:
    """Actions Studio visual editor and workflow operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create Actions Studio tables"""
        # Studio actions (extended action metadata for visual editor)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS actions_studio (
                b_class         TEXT PRIMARY KEY,
                b_priority      INTEGER DEFAULT 50,
                studio_x        REAL,
                studio_y        REAL,
                studio_locked   INTEGER DEFAULT 0,
                studio_color    TEXT,
                studio_metadata TEXT,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Migration: ensure b_priority exists on pre-existing databases
        self.base._ensure_column("actions_studio", "b_priority", "b_priority INTEGER DEFAULT 50")

        # Studio edges (relationships between actions)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS studio_edges (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                from_action     TEXT NOT NULL,
                to_action       TEXT NOT NULL,
                edge_type       TEXT DEFAULT 'requires',
                edge_label      TEXT,
                edge_metadata   TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (from_action) REFERENCES actions_studio(b_class) ON DELETE CASCADE,
                FOREIGN KEY (to_action) REFERENCES actions_studio(b_class) ON DELETE CASCADE,
                UNIQUE(from_action, to_action, edge_type)
            );
        """)
        
        # Studio hosts (hosts for test mode)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS studio_hosts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                mac_address     TEXT UNIQUE NOT NULL,
                ips             TEXT,
                hostnames       TEXT,
                alive           INTEGER DEFAULT 1,
                ports           TEXT,
                services        TEXT,
                vulns           TEXT,
                creds           TEXT,
                studio_x        REAL,
                studio_y        REAL,
                is_simulated    INTEGER DEFAULT 1,
                metadata        TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Studio layouts (saved layout snapshots)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS studio_layouts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT UNIQUE NOT NULL,
                description     TEXT,
                layout_data     TEXT NOT NULL,
                screenshot      BLOB,
                is_active       INTEGER DEFAULT 0,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        logger.debug("Actions Studio tables created/verified")
    
    # =========================================================================
    # STUDIO ACTION OPERATIONS
    # =========================================================================
    
    def get_studio_actions(self):
        """Retrieve all studio actions with their positions"""
        return self.base.query("""
            SELECT * FROM actions_studio
            ORDER BY b_priority DESC, b_class
        """)
    
    def get_db_actions(self):
        """Retrieve all actions from the main actions table"""
        return self.base.query("""
            SELECT * FROM actions
            ORDER BY b_priority DESC, b_class
        """)
    
    # Whitelist of columns that can be updated via the studio API
    _STUDIO_UPDATABLE = frozenset({
        'b_priority', 'studio_x', 'studio_y', 'studio_locked', 'studio_color',
        'studio_metadata', 'b_trigger', 'b_requires', 'b_enabled', 'b_timeout',
        'b_max_retries', 'b_cooldown', 'b_rate_limit', 'b_service', 'b_port',
        'b_stealth_level', 'b_risk_level', 'b_tags', 'b_parent', 'b_action',
    })

    def update_studio_action(self, b_class: str, updates: dict):
        """Update a studio action"""
        sets = []
        params = []
        for key, value in updates.items():
            _validate_identifier(key, "column name")
            if key not in self._STUDIO_UPDATABLE:
                logger.warning(f"Ignoring unknown studio column: {key}")
                continue
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return
        params.append(b_class)
        
        self.base.execute(f"""
            UPDATE actions_studio
            SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP
            WHERE b_class = ?
        """, params)
    
    # =========================================================================
    # STUDIO EDGE OPERATIONS
    # =========================================================================
    
    def get_studio_edges(self):
        """Retrieve all studio edges"""
        return self.base.query("SELECT * FROM studio_edges")
    
    def upsert_studio_edge(self, from_action: str, to_action: str, edge_type: str, metadata: dict = None):
        """Create or update a studio edge"""
        meta_json = json.dumps(metadata) if metadata else None
        # Try UPDATE first
        updated = self.base.execute("""
            UPDATE studio_edges
            SET edge_metadata = ?
            WHERE from_action = ? AND to_action = ? AND edge_type = ?
        """, (meta_json, from_action, to_action, edge_type))
        if not updated:
            # If no rows updated, INSERT
            self.base.execute("""
                INSERT OR IGNORE INTO studio_edges(from_action, to_action, edge_type, edge_metadata)
                VALUES(?,?,?,?)
            """, (from_action, to_action, edge_type, meta_json))
    
    def delete_studio_edge(self, edge_id: int):
        """Delete a studio edge"""
        self.base.execute("DELETE FROM studio_edges WHERE id = ?", (edge_id,))
    
    # =========================================================================
    # STUDIO HOST OPERATIONS
    # =========================================================================
    
    def get_studio_hosts(self, include_real: bool = True):
        """Retrieve studio hosts"""
        if include_real:
            # Combine real and simulated hosts
            return self.base.query("""
                SELECT mac_address, ips, hostnames, alive, ports, 
                    NULL as services, NULL as vulns, NULL as creds,
                    NULL as studio_x, NULL as studio_y, 0 as is_simulated
                FROM hosts
                UNION ALL
                SELECT mac_address, ips, hostnames, alive, ports,
                    services, vulns, creds, studio_x, studio_y, is_simulated
                FROM studio_hosts
            """)
        else:
            return self.base.query("SELECT * FROM studio_hosts WHERE is_simulated = 1")
    
    def upsert_studio_host(self, mac_address: str, data: dict):
        """Create or update a simulated host"""
        self.base.execute("""
            INSERT INTO studio_hosts (
                mac_address, ips, hostnames, alive, ports, services,
                vulns, creds, studio_x, studio_y, is_simulated, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mac_address) DO UPDATE SET
                ips = excluded.ips,
                hostnames = excluded.hostnames,
                alive = excluded.alive,
                ports = excluded.ports,
                services = excluded.services,
                vulns = excluded.vulns,
                creds = excluded.creds,
                studio_x = excluded.studio_x,
                studio_y = excluded.studio_y,
                metadata = excluded.metadata
        """, (
            mac_address,
            data.get('ips'),
            data.get('hostnames'),
            data.get('alive', 1),
            data.get('ports'),
            json.dumps(data.get('services', [])),
            json.dumps(data.get('vulns', [])),
            json.dumps(data.get('creds', [])),
            data.get('studio_x'),
            data.get('studio_y'),
            1,  # is_simulated
            json.dumps(data.get('metadata', {}))
        ))
    
    def delete_studio_host(self, mac: str):
        """Delete a studio host"""
        self.base.execute("DELETE FROM studio_hosts WHERE mac_address = ?", (mac,))
    
    # =========================================================================
    # STUDIO LAYOUT OPERATIONS
    # =========================================================================
    
    def save_studio_layout(self, name: str, layout_data: dict, description: str = None):
        """Save a complete layout"""
        self.base.execute("""
            INSERT INTO studio_layouts (name, description, layout_data)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                description = excluded.description,
                layout_data = excluded.layout_data,
                updated_at = CURRENT_TIMESTAMP
        """, (name, description, json.dumps(layout_data)))
    
    def load_studio_layout(self, name: str):
        """Load a saved layout"""
        row = self.base.query_one("SELECT * FROM studio_layouts WHERE name = ?", (name,))
        if row:
            row['layout_data'] = json.loads(row['layout_data'])
        return row
    
    # =========================================================================
    # STUDIO SYNC OPERATIONS
    # =========================================================================
    
    def apply_studio_to_runtime(self):
        """Apply studio configurations to the main actions table"""
        self.base.execute("""
            UPDATE actions
            SET 
                b_trigger = (SELECT b_trigger FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_requires = (SELECT b_requires FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_priority = (SELECT b_priority FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_enabled = (SELECT b_enabled FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_timeout = (SELECT b_timeout FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_max_retries = (SELECT b_max_retries FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_cooldown = (SELECT b_cooldown FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_rate_limit = (SELECT b_rate_limit FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_service = (SELECT b_service FROM actions_studio WHERE actions_studio.b_class = actions.b_class),
                b_port = (SELECT b_port FROM actions_studio WHERE actions_studio.b_class = actions.b_class)
            WHERE b_class IN (SELECT b_class FROM actions_studio)
        """)
    
    def _replace_actions_studio_with_actions(self, vacuum: bool = False):
        """
        Reset actions_studio (delete all rows) then resync from actions via _sync_actions_studio_schema_and_rows().
        Optionally run VACUUM.
        """
        # Ensure table exists so DELETE doesn't fail
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS actions_studio (
                b_class         TEXT PRIMARY KEY,
                b_priority      INTEGER DEFAULT 50,
                studio_x        REAL,
                studio_y        REAL,
                studio_locked   INTEGER DEFAULT 0,
                studio_color    TEXT,
                studio_metadata TEXT,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Total purge
        self.base.execute("DELETE FROM actions_studio;")
        
        # Optional compaction
        if vacuum:
            self.base.execute("VACUUM;")
        
        # Non-destructive resynchronization from actions
        self._sync_actions_studio_schema_and_rows()
    
    def _sync_actions_studio_schema_and_rows(self):
        """
        Sync actions_studio with actions table:
        - Create minimal table if needed
        - Add missing columns from actions
        - Insert missing b_class entries
        - Update NULL fields only (non-destructive)
        """
        # 1) Minimal table: PK + studio_* columns (b_priority must be here so
        #    get_studio_actions() can ORDER BY it before _sync adds action columns)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS actions_studio (
                b_class         TEXT PRIMARY KEY,
                b_priority      INTEGER DEFAULT 50,
                studio_x        REAL,
                studio_y        REAL,
                studio_locked   INTEGER DEFAULT 0,
                studio_color    TEXT,
                studio_metadata TEXT,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # 2) Dynamically add all columns from actions that are missing in actions_studio
        act_cols = [r["name"] for r in self.base.query("PRAGMA table_info(actions);")]
        stu_cols = [r["name"] for r in self.base.query("PRAGMA table_info(actions_studio);")]
        
        # Get column types from actions
        act_col_defs = {r["name"]: r["type"] for r in self.base.query("PRAGMA table_info(actions);")}
        
        for col in act_cols:
            if col == "b_class":
                continue
            if col not in stu_cols:
                _validate_identifier(col, "column name")
                col_type = act_col_defs.get(col, "TEXT") or "TEXT"
                _validate_identifier(col_type.split()[0], "column type")
                self.base.execute(f"ALTER TABLE actions_studio ADD COLUMN {col} {col_type};")
        
        # 3) Insert missing b_class entries, non-destructive
        self.base.execute("""
            INSERT OR IGNORE INTO actions_studio (b_class)
            SELECT b_class FROM actions;
        """)
        
        # 4) Pre-fill only NULL fields from actions (without overwriting)
        for col in act_cols:
            if col == "b_class":
                continue
            _validate_identifier(col, "column name")
            # Only update if the studio value is NULL
            self.base.execute(f"""
                UPDATE actions_studio
                SET {col} = (SELECT a.{col} FROM actions a
                                WHERE a.b_class = actions_studio.b_class)
                WHERE {col} IS NULL
                AND EXISTS (SELECT 1 FROM actions a WHERE a.b_class = actions_studio.b_class);
            """)
        
        # 5) Touch timestamp
        self.base.execute("UPDATE actions_studio SET updated_at = CURRENT_TIMESTAMP;")