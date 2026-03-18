"""comments.py - Comment and status message operations."""

import json
import os
from typing import Any, Dict, List, Optional, Tuple
import logging

from logger import Logger

logger = Logger(name="db_utils.comments", level=logging.DEBUG)


class CommentOps:
    """Comment and status message management operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create comments table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS comments (
              id         INTEGER PRIMARY KEY AUTOINCREMENT,
              text       TEXT NOT NULL,
              status     TEXT NOT NULL,
              theme      TEXT DEFAULT 'general',
              lang       TEXT DEFAULT 'fr',
              weight     INTEGER DEFAULT 1,
              created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        try:
            self.base.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_comments_dedup
                ON comments(text, status, theme, lang);
            """)
        except Exception:
            pass
        
        logger.debug("Comments table created/verified")
    
    # =========================================================================
    # COMMENT OPERATIONS
    # =========================================================================
    
    def count_comments(self) -> int:
        """Return total number of comment rows"""
        row = self.base.query_one("SELECT COUNT(1) c FROM comments;")
        return int(row["c"]) if row else 0
    
    def insert_comments(self, comments: List[Tuple[str, str, str, str, int]]):
        """Batch insert of comments (dedup via UNIQUE or INSERT OR IGNORE semantics)"""
        if not comments:
            return
        self.base.executemany(
            "INSERT OR IGNORE INTO comments(text,status,theme,lang,weight) VALUES(?,?,?,?,?)",
            comments
        )
    
    def import_comments_from_json(
        self,
        json_path: str,
        lang: Optional[str] = None,
        default_theme: str = "general",
        default_weight: int = 1,
        clear_existing: bool = False
    ) -> int:
        """
        Import comments from a JSON mapping {status: [strings]}.
        Lang is auto-detected from args, shared_data.lang, or filename.
        """
        if not json_path or not os.path.exists(json_path):
            return 0
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0
        
        if not isinstance(data, dict):
            return 0
        
        # Determine language
        if not lang:
            # From filename (comments.xx.json)
            base = os.path.basename(json_path).lower()
            if "comments." in base:
                parts = base.split(".")
                if len(parts) >= 3:
                    lang = parts[-2]
        
        # Fallback
        lang = (lang or "en").lower()
        
        rows: List[Tuple[str, str, str, str, int]] = []
        for status, items in data.items():
            if not isinstance(items, list):
                continue
            for txt in items:
                t = str(txt).strip()
                if not t:
                    continue
                rows.append((t, str(status), str(status), lang, int(default_weight)))
        
        if not rows:
            return 0
        
        with self.base.transaction(immediate=True):
            if clear_existing:
                self.base.execute("DELETE FROM comments;")
            self.insert_comments(rows)
        
        return len(rows)
    
    def random_comment_for(self, status: str, lang: str = "en") -> Optional[Dict[str, Any]]:
        """Pick a random comment for the given status/language"""
        rows = self.base.query("""
            SELECT id, text, status, theme, lang, weight
              FROM comments
             WHERE status=? AND lang=?
             ORDER BY RANDOM()
             LIMIT 1;
        """, (status, lang))
        return rows[0] if rows else None