"""base.py - Base database connection and transaction management."""

import re
import sqlite3
import time
from contextlib import contextmanager
from threading import RLock
from typing import Any, Dict, Iterable, List, Optional, Tuple
import logging

from logger import Logger

logger = Logger(name="db_utils.base", level=logging.DEBUG)

# Regex for valid SQLite identifiers: alphanumeric + underscore, must start with letter/underscore
_SAFE_IDENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _validate_identifier(name: str, kind: str = "identifier") -> str:
    """Validate that a SQL identifier (table/column name) is safe against injection."""
    if not name or not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"Invalid SQL {kind}: {name!r}")
    return name


class DatabaseBase:
    """
    Base database manager providing connection, transaction, and query primitives.
    All specialized operation modules inherit access to these primitives.
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        
        # Connection with optimized settings for constrained devices (e.g., Raspberry Pi)
        self._conn = sqlite3.connect(
            self.db_path, 
            check_same_thread=False, 
            isolation_level=None  # Autocommit mode (we manage transactions explicitly)
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = RLock()
        
        # Small in-process cache for frequently refreshed UI counters
        self._cache_ttl = 5.0  # seconds
        self._stats_cache = {'data': None, 'timestamp': 0}
        
        # Apply PRAGMA tuning
        with self._lock:
            cur = self._conn.cursor()
            # Optimize SQLite for Raspberry Pi / flash storage
            cur.execute("PRAGMA journal_mode=WAL;")
            cur.execute("PRAGMA synchronous=NORMAL;")
            cur.execute("PRAGMA foreign_keys=ON;")
            cur.execute("PRAGMA cache_size=2000;")      # Increase page cache
            cur.execute("PRAGMA temp_store=MEMORY;")    # Use RAM for temporary objects
            cur.close()
        
        logger.info(f"DatabaseBase initialized: {db_path}")
    
    # =========================================================================
    # CORE CONCURRENCY + SQL PRIMITIVES
    # =========================================================================
    
    @contextmanager
    def _cursor(self):
        """Thread-safe cursor context manager"""
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()
    
    @contextmanager
    def transaction(self, immediate: bool = True):
        """Transactional block with automatic rollback on error"""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE;" if immediate else "BEGIN;")
                yield
                self._conn.execute("COMMIT;")
            except Exception:
                self._conn.execute("ROLLBACK;")
                raise
    
    def execute(self, sql: str, params: Iterable[Any] = (), many: bool = False) -> int:
        """Execute a DML statement. Supports batch mode via `many=True`"""
        with self._cursor() as c:
            if many and params and isinstance(params, (list, tuple)) and isinstance(params[0], (list, tuple)):
                c.executemany(sql, params)
                return c.rowcount if c.rowcount is not None else 0
            c.execute(sql, params)
            return c.rowcount if c.rowcount is not None else 0
    
    def executemany(self, sql: str, seq_of_params: Iterable[Iterable[Any]]) -> int:
        """Convenience wrapper around `execute(..., many=True)`"""
        return self.execute(sql, seq_of_params, many=True)
    
    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        """Execute a SELECT and return rows as list[dict]"""
        with self._cursor() as c:
            c.execute(sql, params)
            rows = c.fetchall()
            return [dict(r) for r in rows]
    
    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        """Execute a SELECT and return a single row as dict (or None)"""
        with self._cursor() as c:
            c.execute(sql, params)
            row = c.fetchone()
            return dict(row) if row else None
    
    # =========================================================================
    # CACHE MANAGEMENT
    # =========================================================================
    
    def invalidate_stats_cache(self):
        """Invalidate the small in-memory stats cache"""
        self._stats_cache = {'data': None, 'timestamp': 0}
    
    # =========================================================================
    # SCHEMA HELPERS
    # =========================================================================
    
    def _table_exists(self, name: str) -> bool:
        """Return True if a table exists in the current database"""
        row = self.query("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return bool(row)
    
    def _column_names(self, table: str) -> List[str]:
        """Return a list of column names for a given table (empty if table missing)"""
        _validate_identifier(table, "table name")
        with self._cursor() as c:
            c.execute(f"PRAGMA table_info({table});")
            return [r[1] for r in c.fetchall()]

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        """Add a column with the provided DDL if it does not exist yet"""
        _validate_identifier(table, "table name")
        _validate_identifier(column, "column name")
        cols = self._column_names(table) if self._table_exists(table) else []
        if column not in cols:
            self.execute(f"ALTER TABLE {table} ADD COLUMN {ddl};")
    
    # =========================================================================
    # MAINTENANCE OPERATIONS
    # =========================================================================
    
    _VALID_CHECKPOINT_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}

    def checkpoint(self, mode: str = "TRUNCATE") -> Tuple[int, int, int]:
        """
        Force a WAL checkpoint. Returns (busy, log_frames, checkpointed_frames).
        mode ∈ {PASSIVE, FULL, RESTART, TRUNCATE}
        """
        mode = (mode or "PASSIVE").upper()
        if mode not in self._VALID_CHECKPOINT_MODES:
            mode = "PASSIVE"
        with self._cursor() as c:
            c.execute(f"PRAGMA wal_checkpoint({mode});")
            row = c.fetchone()
            if not row:
                return (0, 0, 0)
            vals = tuple(row)
            return (int(vals[0]), int(vals[1]), int(vals[2]))
    
    def optimize(self) -> None:
        """Run PRAGMA optimize to help the query planner update statistics"""
        self.execute("PRAGMA optimize;")
    
    def vacuum(self) -> None:
        """Vacuum the database to reclaim space (use sparingly on flash media)"""
        self.execute("VACUUM;")
