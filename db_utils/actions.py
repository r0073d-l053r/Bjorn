"""actions.py - Action definition and management operations."""

import json
import sqlite3
from functools import lru_cache
from typing import Any, Dict, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.actions", level=logging.DEBUG)


class ActionOps:
    """Action definition and configuration operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create actions table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS actions (
                b_class         TEXT PRIMARY KEY,
                b_module        TEXT NOT NULL,
                b_port          INTEGER,
                b_status        TEXT,
                b_parent        TEXT,
                b_args          TEXT,
                b_description   TEXT,
                b_name          TEXT,
                b_author        TEXT,
                b_version       TEXT,
                b_icon          TEXT,
                b_docs_url      TEXT,
                b_examples      TEXT,
                b_action        TEXT DEFAULT 'normal',
                b_service       TEXT,
                b_trigger       TEXT,
                b_requires      TEXT,
                b_priority      INTEGER DEFAULT 50,
                b_tags          TEXT,
                b_timeout       INTEGER DEFAULT 300,
                b_max_retries   INTEGER DEFAULT 3,
                b_cooldown      INTEGER DEFAULT 0,
                b_rate_limit    TEXT,
                b_stealth_level INTEGER DEFAULT 5,
                b_risk_level    TEXT DEFAULT 'medium',
                b_enabled       INTEGER DEFAULT 1
            );
        """)
        logger.debug("Actions table created/verified")
    
    # =========================================================================
    # ACTION CRUD OPERATIONS
    # =========================================================================
    
    def sync_actions(self, actions):
        """Sync action definitions to database"""
        if not actions:
            return
        
        def _as_int(x, default=None):
            if x is None:
                return default
            if isinstance(x, (list, tuple)):
                x = x[0] if x else default
            try:
                return int(x)
            except Exception:
                return default
        
        def _as_str(x, default=None):
            if x is None:
                return default
            if isinstance(x, (list, tuple, set, dict)):
                try:
                    return json.dumps(list(x) if not isinstance(x, dict) else x, ensure_ascii=False)
                except Exception:
                    return default
            return str(x)
        
        def _as_json(x):
            if x is None:
                return None
            if isinstance(x, str):
                xs = x.strip()
                if (xs.startswith("{") and xs.endswith("}")) or (xs.startswith("[") and xs.endswith("]")):
                    return xs
                return json.dumps(x, ensure_ascii=False)
            try:
                return json.dumps(x, ensure_ascii=False)
            except Exception:
                return None
        
        with self.base.transaction():
            for a in actions:
                # Normalize fields
                b_service      = a.get("b_service")
                if isinstance(b_service, (list, tuple, set, dict)):
                    b_service = json.dumps(list(b_service) if not isinstance(b_service, dict) else b_service, ensure_ascii=False)
                
                b_tags         = a.get("b_tags")
                if isinstance(b_tags, (list, tuple, set, dict)):
                    b_tags = json.dumps(list(b_tags) if not isinstance(b_tags, dict) else b_tags, ensure_ascii=False)
                
                b_trigger      = a.get("b_trigger")
                if isinstance(b_trigger, (list, tuple, set, dict)):
                    b_trigger = json.dumps(b_trigger, ensure_ascii=False)
                
                b_requires     = a.get("b_requires")
                if isinstance(b_requires, (list, tuple, set, dict)):
                    b_requires = json.dumps(b_requires, ensure_ascii=False)
                
                b_args_json    = _as_json(a.get("b_args"))
                
                # Enriched metadata
                b_name         = _as_str(a.get("b_name"))
                b_description  = _as_str(a.get("b_description"))
                b_author       = _as_str(a.get("b_author"))
                b_version      = _as_str(a.get("b_version"))
                b_icon         = _as_str(a.get("b_icon"))
                b_docs_url     = _as_str(a.get("b_docs_url"))
                b_examples     = _as_json(a.get("b_examples"))
                
                # Typed fields
                b_port          = _as_int(a.get("b_port"))
                b_priority      = _as_int(a.get("b_priority"), 50)
                b_timeout       = _as_int(a.get("b_timeout"), 300)
                b_max_retries   = _as_int(a.get("b_max_retries"), 3)
                b_cooldown      = _as_int(a.get("b_cooldown"), 0)
                b_stealth_level = _as_int(a.get("b_stealth_level"), 5)
                b_enabled       = _as_int(a.get("b_enabled"), 1)
                b_rate_limit    = _as_str(a.get("b_rate_limit"))
                b_risk_level    = _as_str(a.get("b_risk_level"), "medium")
                
                self.base.execute("""
                    INSERT INTO actions (
                        b_class,b_module,b_port,b_status,b_parent,
                        b_action,b_service,b_trigger,b_requires,b_priority,
                        b_tags,b_timeout,b_max_retries,b_cooldown,b_rate_limit,
                        b_stealth_level,b_risk_level,b_enabled,
                        b_args,
                        b_name, b_description, b_author, b_version, b_icon, b_docs_url, b_examples
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                            ?,?,?,?,?,?,?)
                    ON CONFLICT(b_class) DO UPDATE SET
                        b_module        = excluded.b_module,
                        b_port          = COALESCE(excluded.b_port, actions.b_port),
                        b_status        = COALESCE(excluded.b_status, actions.b_status),
                        b_parent        = COALESCE(excluded.b_parent, actions.b_parent),
                        b_action        = COALESCE(excluded.b_action, actions.b_action),
                        b_service       = COALESCE(excluded.b_service, actions.b_service),
                        b_trigger       = COALESCE(excluded.b_trigger, actions.b_trigger),
                        b_requires      = COALESCE(excluded.b_requires, actions.b_requires),
                        b_priority      = COALESCE(excluded.b_priority, actions.b_priority),
                        b_tags          = COALESCE(excluded.b_tags, actions.b_tags),
                        b_timeout       = COALESCE(excluded.b_timeout, actions.b_timeout),
                        b_max_retries   = COALESCE(excluded.b_max_retries, actions.b_max_retries),
                        b_cooldown      = COALESCE(excluded.b_cooldown, actions.b_cooldown),
                        b_rate_limit    = COALESCE(excluded.b_rate_limit, actions.b_rate_limit),
                        b_stealth_level = COALESCE(excluded.b_stealth_level, actions.b_stealth_level),
                        b_risk_level    = COALESCE(excluded.b_risk_level, actions.b_risk_level),
                        -- Keep persisted enable/disable state from DB across restarts.
                        b_enabled       = actions.b_enabled,
                        b_args          = COALESCE(excluded.b_args, actions.b_args),
                        b_name          = COALESCE(excluded.b_name, actions.b_name),
                        b_description   = COALESCE(excluded.b_description, actions.b_description),
                        b_author        = COALESCE(excluded.b_author, actions.b_author),
                        b_version       = COALESCE(excluded.b_version, actions.b_version),
                        b_icon          = COALESCE(excluded.b_icon, actions.b_icon),
                        b_docs_url      = COALESCE(excluded.b_docs_url, actions.b_docs_url),
                        b_examples      = COALESCE(excluded.b_examples, actions.b_examples)
                """, (
                    a.get("b_class"),
                    a.get("b_module"),
                    b_port,
                    a.get("b_status"),
                    a.get("b_parent"),
                    a.get("b_action", "normal"),
                    b_service,
                    b_trigger,
                    b_requires,
                    b_priority,
                    b_tags,
                    b_timeout,
                    b_max_retries,
                    b_cooldown,
                    b_rate_limit,
                    b_stealth_level,
                    b_risk_level,
                    b_enabled,
                    b_args_json,
                    b_name,
                    b_description,
                    b_author,
                    b_version,
                    b_icon,
                    b_docs_url,
                    b_examples
                ))
            
            # Update action counter in stats
            action_count_row = self.base.query_one("SELECT COUNT(*) as cnt FROM actions WHERE b_enabled = 1")
            if action_count_row:
                try:
                    self.base.execute("""
                        UPDATE stats 
                        SET actions_count = ? 
                        WHERE id = 1
                    """, (action_count_row['cnt'],))
                except sqlite3.OperationalError:
                    # Column doesn't exist yet, add it
                    self.base.execute("ALTER TABLE stats ADD COLUMN actions_count INTEGER DEFAULT 0")
                    self.base.execute("""
                        UPDATE stats 
                        SET actions_count = ? 
                        WHERE id = 1
                    """, (action_count_row['cnt'],))
        
        # Invalidate cache so callers immediately see fresh definitions
        type(self).get_action_definition.cache_clear()
        logger.info(f"Synchronized {len(actions)} actions")

    def list_actions(self):
        """List all action definitions ordered by class name"""
        return self.base.query("SELECT * FROM actions ORDER BY b_class;")
    
    def list_studio_actions(self):
        """List all studio action definitions"""
        return self.base.query("SELECT * FROM actions_studio ORDER BY b_class;")
    
    def get_action_by_class(self, b_class: str) -> dict | None:
        """Get action by class name"""
        rows = self.base.query("SELECT * FROM actions WHERE b_class=? LIMIT 1;", (b_class,))
        return rows[0] if rows else None
    
    def delete_action(self, b_class: str) -> None:
        """Delete action by class name"""
        self.base.execute("DELETE FROM actions WHERE b_class=?;", (b_class,))
    
    def upsert_simple_action(self, *, b_class: str, b_module: str, **kw) -> None:
        """Minimal upsert of an action by reusing sync_actions"""
        rec = {"b_class": b_class, "b_module": b_module}
        rec.update(kw)
        self.sync_actions([rec])

    def list_action_cards(self) -> list[dict]:
        """Lightweight descriptor of actions for card-based UIs"""
        rows = self.base.query("""
            SELECT b_class, COALESCE(b_enabled, 0) AS b_enabled
            FROM actions
            ORDER BY b_class;
        """)
        out = []
        for r in rows:
            cls = r["b_class"]
            enabled = int(r["b_enabled"])
            out.append({
                "name": cls,
                "image": f"/actions/actions_icons/{cls}.png",
                "enabled": enabled,
            })
        return out
    
    @lru_cache(maxsize=32)
    def get_action_definition(self, b_class: str) -> Optional[Dict[str, Any]]:
        """Cached lookup of an action definition by class name"""
        row = self.base.query("SELECT * FROM actions WHERE b_class=? LIMIT 1;", (b_class,))
        if not row:
            return None
        r = row[0]
        if r.get("b_args"):
            try:
                r["b_args"] = json.loads(r["b_args"])
            except Exception:
                pass
        return r
