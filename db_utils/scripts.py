"""scripts.py - Script and project metadata operations."""

from typing import Any, Dict, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.scripts", level=logging.DEBUG)


class ScriptOps:
    """Script and project metadata management operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create scripts metadata table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS scripts (
              name        TEXT PRIMARY KEY,
              type        TEXT NOT NULL,
              path        TEXT NOT NULL,
              main_file   TEXT,
              category    TEXT,
              description TEXT,
              created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        logger.debug("Scripts table created/verified")
    
    # =========================================================================
    # SCRIPT OPERATIONS
    # =========================================================================
    
    def add_script(self, name: str, type_: str, path: str,
                   main_file: Optional[str] = None, category: Optional[str] = None,
                   description: Optional[str] = None):
        """Insert or update a script/project metadata row"""
        self.base.execute("""
            INSERT INTO scripts(name,type,path,main_file,category,description)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(name) DO UPDATE SET
                type=excluded.type,
                path=excluded.path,
                main_file=excluded.main_file,
                category=excluded.category,
                description=excluded.description;
        """, (name, type_, path, main_file, category, description))
    
    def list_scripts(self) -> List[Dict[str, Any]]:
        """List all scripts/projects"""
        return self.base.query("""
            SELECT name, type, path, main_file, category, description, created_at
            FROM scripts
            ORDER BY name;
        """)
    
    def delete_script(self, name: str) -> None:
        """Delete a script/project metadata row by name"""
        self.base.execute("DELETE FROM scripts WHERE name=?;", (name,))
