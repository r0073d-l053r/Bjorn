"""config.py - Configuration management operations."""

import json
import ast
from typing import Any, Dict
import logging

from logger import Logger

logger = Logger(name="db_utils.config", level=logging.DEBUG)


class ConfigOps:
    """Configuration key-value store operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create config table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS config (
              key   TEXT PRIMARY KEY,
              value TEXT
            );
        """)
        logger.debug("Config table created/verified")
    
    def get_config(self) -> Dict[str, Any]:
        """Load config as typed dict (tries JSON, then literal_eval, then raw)"""
        rows = self.base.query("SELECT key, value FROM config;")
        out: Dict[str, Any] = {}
        for r in rows:
            k = r["key"]
            raw = r["value"]
            try:
                v = json.loads(raw)
            except Exception:
                try:
                    v = ast.literal_eval(raw)
                except Exception:
                    v = raw
            out[k] = v
        return out
    
    def save_config(self, config: Dict[str, Any]) -> None:
        """Save the full config mapping to the database (JSON-serialized)"""
        if not config:
            return
        pairs = []
        for k, v in config.items():
            try:
                s = json.dumps(v, ensure_ascii=False)
            except Exception:
                s = json.dumps(str(v), ensure_ascii=False)
            pairs.append((str(k), s))
        
        with self.base.transaction():
            self.base.execute("DELETE FROM config;")
            self.base.executemany("INSERT INTO config(key,value) VALUES(?,?);", pairs)
        
        logger.info(f"Saved {len(pairs)} config entries")
