"""backups.py - Backup registry and management operations."""

from typing import Any, Dict, List
import logging

from logger import Logger

logger = Logger(name="db_utils.backups", level=logging.DEBUG)


class BackupOps:
    """Backup registry and management operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create backups registry table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS backups (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              filename     TEXT UNIQUE NOT NULL,
              description  TEXT,
              date         TEXT,
              type         TEXT DEFAULT 'User Backup',
              is_default   INTEGER DEFAULT 0,
              is_restore   INTEGER DEFAULT 0,
              is_github    INTEGER DEFAULT 0,
              created_at   TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        logger.debug("Backups table created/verified")
    
    # =========================================================================
    # BACKUP OPERATIONS
    # =========================================================================
    
    def add_backup(self, filename: str, description: str, date: str,
                   type_: str = "User Backup", is_default: bool = False,
                   is_restore: bool = False, is_github: bool = False):
        """Insert or update a backup registry entry"""
        self.base.execute("""
            INSERT INTO backups(filename,description,date,type,is_default,is_restore,is_github)
            VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(filename) DO UPDATE SET
                description=excluded.description,
                date=excluded.date,
                type=excluded.type,
                is_default=excluded.is_default,
                is_restore=excluded.is_restore,
                is_github=excluded.is_github;
        """, (filename, description, date, type_, int(is_default),
              int(is_restore), int(is_github)))
    
    def list_backups(self) -> List[Dict[str, Any]]:
        """List all backups ordered by date descending"""
        return self.base.query("""
            SELECT filename, description, date, type,
                   is_default, is_restore, is_github
            FROM backups 
            ORDER BY date DESC;
        """)
    
    def delete_backup(self, filename: str) -> None:
        """Delete a backup entry by filename"""
        self.base.execute("DELETE FROM backups WHERE filename=?;", (filename,))
    
    def clear_default_backup(self) -> None:
        """Clear the default flag on all backups"""
        self.base.execute("UPDATE backups SET is_default=0;")
    
    def set_default_backup(self, filename: str) -> None:
        """Set the default flag on a specific backup"""
        self.clear_default_backup()
        self.base.execute("UPDATE backups SET is_default=1 WHERE filename=?;", (filename,))
