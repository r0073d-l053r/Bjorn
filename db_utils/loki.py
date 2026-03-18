"""loki.py - HID script and job tracking operations."""
import logging

from logger import Logger

logger = Logger(name="db_utils.loki", level=logging.DEBUG)


class LokiOps:
    def __init__(self, base):
        self.base = base

    def create_tables(self):
        """Create all Loki tables."""

        # User-saved HID scripts
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS loki_scripts (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL UNIQUE,
                description   TEXT DEFAULT '',
                content       TEXT NOT NULL DEFAULT '',
                category      TEXT DEFAULT 'general',
                target_os     TEXT DEFAULT 'any',
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Job execution history
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS loki_jobs (
                id            TEXT PRIMARY KEY,
                script_id     INTEGER,
                script_name   TEXT DEFAULT '',
                status        TEXT DEFAULT 'pending',
                output        TEXT DEFAULT '',
                error         TEXT DEFAULT '',
                started_at    TEXT,
                finished_at   TEXT,
                created_at    TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.base.execute(
            "CREATE INDEX IF NOT EXISTS idx_loki_jobs_status "
            "ON loki_jobs(status)"
        )

        logger.debug("Loki tables created/verified")
