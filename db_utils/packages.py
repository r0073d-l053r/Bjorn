"""packages.py - Custom package tracking operations."""

import logging
from typing import Any, Dict, List, Optional

from logger import Logger

logger = Logger(name="db_utils.packages", level=logging.DEBUG)


class PackageOps:
    """Custom package management operations"""

    def __init__(self, base):
        self.base = base

    def create_tables(self):
        """Create custom_packages table"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS custom_packages (
                name         TEXT PRIMARY KEY,
                version      TEXT,
                installed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                installed_by TEXT DEFAULT 'user'
            );
        """)
        logger.debug("Packages table created/verified")

    # =========================================================================
    # PACKAGE OPERATIONS
    # =========================================================================

    def add_package(self, name: str, version: str) -> None:
        """Insert or replace a package record"""
        self.base.execute("""
            INSERT OR REPLACE INTO custom_packages (name, version)
            VALUES (?, ?);
        """, (name, version))

    def remove_package(self, name: str) -> None:
        """Delete a package by name"""
        self.base.execute("DELETE FROM custom_packages WHERE name=?;", (name,))

    def list_packages(self) -> List[Dict[str, Any]]:
        """List all tracked packages"""
        return self.base.query(
            "SELECT * FROM custom_packages ORDER BY name;"
        )

    def get_package(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a single package by name"""
        return self.base.query_one(
            "SELECT * FROM custom_packages WHERE name=?;", (name,)
        )
