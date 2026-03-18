"""schedules.py - Script scheduling and trigger operations."""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from logger import Logger

logger = Logger(name="db_utils.schedules", level=logging.DEBUG)


class ScheduleOps:
    """Script schedule and trigger management operations"""

    def __init__(self, base):
        self.base = base

    def create_tables(self):
        """Create script_schedules and script_triggers tables"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS script_schedules (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                script_name     TEXT NOT NULL,
                schedule_type   TEXT NOT NULL DEFAULT 'recurring',
                interval_seconds INTEGER,
                run_at          TEXT,
                args            TEXT DEFAULT '',
                conditions      TEXT,
                enabled         INTEGER DEFAULT 1,
                last_run_at     TEXT,
                next_run_at     TEXT,
                run_count       INTEGER DEFAULT 0,
                last_status     TEXT,
                last_error      TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.base.execute("""
            CREATE INDEX IF NOT EXISTS idx_sched_next
            ON script_schedules(next_run_at) WHERE enabled=1;
        """)
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS script_triggers (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                script_name     TEXT NOT NULL,
                trigger_name    TEXT NOT NULL,
                conditions      TEXT NOT NULL,
                args            TEXT DEFAULT '',
                enabled         INTEGER DEFAULT 1,
                last_fired_at   TEXT,
                fire_count      INTEGER DEFAULT 0,
                cooldown_seconds INTEGER DEFAULT 60,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.base.execute("""
            CREATE INDEX IF NOT EXISTS idx_trig_enabled
            ON script_triggers(enabled) WHERE enabled=1;
        """)
        logger.debug("Schedule and trigger tables created/verified")

    # =========================================================================
    # SCHEDULE OPERATIONS
    # =========================================================================

    def add_schedule(self, script_name: str, schedule_type: str,
                     interval_seconds: Optional[int] = None,
                     run_at: Optional[str] = None, args: str = '',
                     conditions: Optional[str] = None) -> int:
        """Insert a new schedule entry and return its id"""
        next_run_at = None
        if schedule_type == 'recurring' and interval_seconds:
            next_run_at = (datetime.utcnow() + timedelta(seconds=interval_seconds)).strftime('%Y-%m-%d %H:%M:%S')
        elif run_at:
            next_run_at = run_at

        self.base.execute("""
            INSERT INTO script_schedules
                (script_name, schedule_type, interval_seconds, run_at, args, conditions, next_run_at)
            VALUES (?, ?, ?, ?, ?, ?, ?);
        """, (script_name, schedule_type, interval_seconds, run_at, args, conditions, next_run_at))

        rows = self.base.query("SELECT last_insert_rowid() AS id;")
        return rows[0]['id'] if rows else 0

    def update_schedule(self, id: int, **kwargs) -> None:
        """Update schedule fields; recompute next_run_at if interval changes"""
        if not kwargs:
            return
        sets = []
        params = []
        for key, value in kwargs.items():
            sets.append(f"{key}=?")
            params.append(value)
        sets.append("updated_at=datetime('now')")
        params.append(id)
        self.base.execute(
            f"UPDATE script_schedules SET {', '.join(sets)} WHERE id=?;",
            tuple(params)
        )
        # Recompute next_run_at if interval changed
        if 'interval_seconds' in kwargs:
            row = self.get_schedule(id)
            if row and row['schedule_type'] == 'recurring' and kwargs['interval_seconds']:
                next_run = (datetime.utcnow() + timedelta(seconds=kwargs['interval_seconds'])).strftime('%Y-%m-%d %H:%M:%S')
                self.base.execute(
                    "UPDATE script_schedules SET next_run_at=?, updated_at=datetime('now') WHERE id=?;",
                    (next_run, id)
                )

    def delete_schedule(self, id: int) -> None:
        """Delete a schedule by id"""
        self.base.execute("DELETE FROM script_schedules WHERE id=?;", (id,))

    def list_schedules(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """List all schedules, optionally filtered to enabled only"""
        if enabled_only:
            return self.base.query(
                "SELECT * FROM script_schedules WHERE enabled=1 ORDER BY id;"
            )
        return self.base.query("SELECT * FROM script_schedules ORDER BY id;")

    def get_schedule(self, id: int) -> Optional[Dict[str, Any]]:
        """Get a single schedule by id"""
        return self.base.query_one(
            "SELECT * FROM script_schedules WHERE id=?;", (id,)
        )

    def get_due_schedules(self) -> List[Dict[str, Any]]:
        """Get schedules that are due to run"""
        return self.base.query("""
            SELECT * FROM script_schedules
            WHERE enabled=1
              AND next_run_at <= datetime('now')
              AND (last_status IS NULL OR last_status != 'running')
            ORDER BY next_run_at;
        """)

    def mark_schedule_run(self, id: int, status: str, error: Optional[str] = None) -> None:
        """Mark a schedule as run, update counters, recompute next_run_at"""
        row = self.get_schedule(id)
        if not row:
            return

        now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

        if row['schedule_type'] == 'recurring' and row['interval_seconds']:
            next_run = (datetime.utcnow() + timedelta(seconds=row['interval_seconds'])).strftime('%Y-%m-%d %H:%M:%S')
            self.base.execute("""
                UPDATE script_schedules
                SET last_run_at=?, last_status=?, last_error=?,
                    run_count=run_count+1, next_run_at=?, updated_at=datetime('now')
                WHERE id=?;
            """, (now, status, error, next_run, id))
        else:
            # oneshot: disable after run
            self.base.execute("""
                UPDATE script_schedules
                SET last_run_at=?, last_status=?, last_error=?,
                    run_count=run_count+1, enabled=0, updated_at=datetime('now')
                WHERE id=?;
            """, (now, status, error, id))

    def toggle_schedule(self, id: int, enabled: bool) -> None:
        """Enable or disable a schedule"""
        self.base.execute(
            "UPDATE script_schedules SET enabled=?, updated_at=datetime('now') WHERE id=?;",
            (1 if enabled else 0, id)
        )

    # =========================================================================
    # TRIGGER OPERATIONS
    # =========================================================================

    def add_trigger(self, script_name: str, trigger_name: str, conditions: str,
                    args: str = '', cooldown_seconds: int = 60) -> int:
        """Insert a new trigger and return its id"""
        self.base.execute("""
            INSERT INTO script_triggers
                (script_name, trigger_name, conditions, args, cooldown_seconds)
            VALUES (?, ?, ?, ?, ?);
        """, (script_name, trigger_name, conditions, args, cooldown_seconds))

        rows = self.base.query("SELECT last_insert_rowid() AS id;")
        return rows[0]['id'] if rows else 0

    def update_trigger(self, id: int, **kwargs) -> None:
        """Update trigger fields"""
        if not kwargs:
            return
        sets = []
        params = []
        for key, value in kwargs.items():
            sets.append(f"{key}=?")
            params.append(value)
        params.append(id)
        self.base.execute(
            f"UPDATE script_triggers SET {', '.join(sets)} WHERE id=?;",
            tuple(params)
        )

    def delete_trigger(self, id: int) -> None:
        """Delete a trigger by id"""
        self.base.execute("DELETE FROM script_triggers WHERE id=?;", (id,))

    def list_triggers(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        """List all triggers, optionally filtered to enabled only"""
        if enabled_only:
            return self.base.query(
                "SELECT * FROM script_triggers WHERE enabled=1 ORDER BY id;"
            )
        return self.base.query("SELECT * FROM script_triggers ORDER BY id;")

    def get_trigger(self, id: int) -> Optional[Dict[str, Any]]:
        """Get a single trigger by id"""
        return self.base.query_one(
            "SELECT * FROM script_triggers WHERE id=?;", (id,)
        )

    def get_active_triggers(self) -> List[Dict[str, Any]]:
        """Get all enabled triggers"""
        return self.base.query(
            "SELECT * FROM script_triggers WHERE enabled=1 ORDER BY id;"
        )

    def mark_trigger_fired(self, id: int) -> None:
        """Record that a trigger has fired"""
        self.base.execute("""
            UPDATE script_triggers
            SET last_fired_at=datetime('now'), fire_count=fire_count+1
            WHERE id=?;
        """, (id,))

    def is_trigger_on_cooldown(self, id: int) -> bool:
        """Check if a trigger is still within its cooldown period"""
        row = self.base.query_one("""
            SELECT 1 AS on_cooldown FROM script_triggers
            WHERE id=?
              AND last_fired_at IS NOT NULL
              AND datetime(last_fired_at, '+' || cooldown_seconds || ' seconds') > datetime('now');
        """, (id,))
        return row is not None
