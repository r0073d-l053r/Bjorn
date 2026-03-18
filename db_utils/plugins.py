"""plugins.py - Plugin configuration and hook tracking operations."""

import json
import logging
from typing import Any, Dict, List, Optional

from logger import Logger

logger = Logger(name="db_utils.plugins", level=logging.DEBUG)


class PluginOps:
    """Plugin configuration and hook registration operations."""

    def __init__(self, base):
        self.base = base

    def create_tables(self):
        """Create plugin_configs and plugin_hooks tables."""

        self.base.execute("""
            CREATE TABLE IF NOT EXISTS plugin_configs (
                plugin_id    TEXT PRIMARY KEY,
                enabled      INTEGER DEFAULT 1,
                config_json  TEXT DEFAULT '{}',
                meta_json    TEXT DEFAULT '{}',
                installed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)

        self.base.execute("""
            CREATE TABLE IF NOT EXISTS plugin_hooks (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_id    TEXT NOT NULL,
                hook_name    TEXT NOT NULL,
                UNIQUE(plugin_id, hook_name),
                FOREIGN KEY (plugin_id) REFERENCES plugin_configs(plugin_id)
                    ON DELETE CASCADE
            );
        """)

        self.base.execute(
            "CREATE INDEX IF NOT EXISTS idx_plugin_hooks_hook "
            "ON plugin_hooks(hook_name);"
        )

        logger.debug("Plugin tables created/verified")

    # ── Config CRUD ──────────────────────────────────────────────────

    def get_plugin_config(self, plugin_id: str) -> Optional[Dict[str, Any]]:
        """Get plugin config row. Returns dict with parsed config_json and meta."""
        row = self.base.query_one(
            "SELECT * FROM plugin_configs WHERE plugin_id=?;", (plugin_id,)
        )
        if row:
            try:
                row["config"] = json.loads(row.get("config_json") or "{}")
            except Exception:
                row["config"] = {}
            try:
                row["meta"] = json.loads(row.get("meta_json") or "{}")
            except Exception:
                row["meta"] = {}
        return row

    def save_plugin_config(self, plugin_id: str, config: dict) -> None:
        """Update config_json for a plugin."""
        self.base.execute("""
            UPDATE plugin_configs
            SET config_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE plugin_id = ?;
        """, (json.dumps(config, ensure_ascii=False), plugin_id))

    def upsert_plugin(self, plugin_id: str, enabled: int, config: dict, meta: dict) -> None:
        """Insert or update a plugin record."""
        self.base.execute("""
            INSERT INTO plugin_configs (plugin_id, enabled, config_json, meta_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(plugin_id) DO UPDATE SET
                enabled = excluded.enabled,
                meta_json = excluded.meta_json,
                updated_at = CURRENT_TIMESTAMP;
        """, (plugin_id, enabled, json.dumps(config, ensure_ascii=False),
              json.dumps(meta, ensure_ascii=False)))

    def delete_plugin(self, plugin_id: str) -> None:
        """Delete plugin and its hooks (CASCADE)."""
        self.base.execute("DELETE FROM plugin_configs WHERE plugin_id=?;", (plugin_id,))

    def list_plugins(self) -> List[Dict[str, Any]]:
        """List all registered plugins."""
        rows = self.base.query("SELECT * FROM plugin_configs ORDER BY plugin_id;")
        for r in rows:
            try:
                r["config"] = json.loads(r.get("config_json") or "{}")
            except Exception:
                r["config"] = {}
            try:
                r["meta"] = json.loads(r.get("meta_json") or "{}")
            except Exception:
                r["meta"] = {}
        return rows

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        """Toggle plugin enabled state."""
        self.base.execute(
            "UPDATE plugin_configs SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE plugin_id=?;",
            (1 if enabled else 0, plugin_id)
        )

    # ── Hook CRUD ────────────────────────────────────────────────────

    def set_plugin_hooks(self, plugin_id: str, hooks: List[str]) -> None:
        """Replace all hooks for a plugin."""
        with self.base.transaction():
            self.base.execute("DELETE FROM plugin_hooks WHERE plugin_id=?;", (plugin_id,))
            for h in hooks:
                self.base.execute(
                    "INSERT OR IGNORE INTO plugin_hooks(plugin_id, hook_name) VALUES(?,?);",
                    (plugin_id, h)
                )

    def get_hooks_for_event(self, hook_name: str) -> List[str]:
        """Get all plugin_ids subscribed to a given hook."""
        rows = self.base.query(
            "SELECT plugin_id FROM plugin_hooks WHERE hook_name=?;", (hook_name,)
        )
        return [r["plugin_id"] for r in rows]

    def get_hooks_for_plugin(self, plugin_id: str) -> List[str]:
        """Get all hooks a plugin subscribes to."""
        rows = self.base.query(
            "SELECT hook_name FROM plugin_hooks WHERE plugin_id=?;", (plugin_id,)
        )
        return [r["hook_name"] for r in rows]
