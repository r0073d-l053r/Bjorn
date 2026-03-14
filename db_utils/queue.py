# db_utils/queue.py
# Action queue management operations

import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional
import logging

from logger import Logger

logger = Logger(name="db_utils.queue", level=logging.DEBUG)


class QueueOps:
    """Action queue scheduling and execution tracking operations"""
    
    def __init__(self, base):
        self.base = base
    
    def create_tables(self):
        """Create action queue table and indexes"""
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS action_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action_name TEXT NOT NULL,
                mac_address TEXT NOT NULL,
                ip TEXT NOT NULL,
                port INTEGER,
                hostname TEXT,
                service TEXT,
                priority INTEGER DEFAULT 50,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                scheduled_for TEXT,
                started_at TEXT,
                completed_at TEXT,
                expires_at TEXT,
                trigger_source TEXT,
                dependencies TEXT,
                conditions TEXT,
                result_summary TEXT,
                error_message TEXT,
                tags TEXT,
                metadata TEXT,
                FOREIGN KEY (mac_address) REFERENCES hosts(mac_address)
            );
        """)
        
        # Optimized indexes for queue operations
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_queue_pending ON action_queue(status) WHERE status='pending';")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_queue_scheduled ON action_queue(scheduled_for) WHERE status='scheduled';")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_queue_mac_action ON action_queue(mac_address, action_name);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_queue_key_status ON action_queue(action_name, mac_address, port, status);")
        self.base.execute("CREATE INDEX IF NOT EXISTS idx_queue_key_time   ON action_queue(action_name, mac_address, port, completed_at);")
        
        # Unique constraint for a single upcoming schedule per action/target
        self.base.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_next_scheduled
              ON action_queue(action_name,
                              COALESCE(mac_address,''),
                              COALESCE(service,''),
                              COALESCE(port,-1))
            WHERE status='scheduled';
        """)
        
        # Circuit breaker table for ORCH-01
        self.base.execute("""
            CREATE TABLE IF NOT EXISTS action_circuit_breaker (
                action_name TEXT NOT NULL,
                mac_address TEXT NOT NULL DEFAULT '',
                failure_streak INTEGER NOT NULL DEFAULT 0,
                last_failure_at TEXT,
                circuit_status TEXT NOT NULL DEFAULT 'closed',
                opened_at TEXT,
                cooldown_until TEXT,
                PRIMARY KEY (action_name, mac_address)
            );
        """)

        logger.debug("Action queue table created/verified")
    
    # =========================================================================
    # QUEUE RETRIEVAL OPERATIONS
    # =========================================================================
    
    def get_next_queued_action(self) -> Optional[Dict[str, Any]]:
        """
        Fetch the next action to execute from the queue.
        Priority is dynamically boosted: +1 per 5 minutes since creation, capped at +100.
        """
        rows = self.base.query("""
            SELECT *,
                MIN(100, priority + CAST((strftime('%s','now') - strftime('%s',created_at))/300 AS INTEGER)) AS priority_effective
            FROM action_queue 
            WHERE status = 'pending'
            AND (scheduled_for IS NULL OR scheduled_for <= datetime('now'))
            ORDER BY priority_effective DESC,
                    COALESCE(scheduled_for, created_at) ASC
            LIMIT 1
        """)
        return rows[0] if rows else None
    
    def list_action_queue(self, statuses: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        """List queue entries with a computed `priority_effective` column for pending items"""
        order_sql = """
            CASE status
            WHEN 'running'   THEN 1
            WHEN 'pending'   THEN 2
            WHEN 'scheduled' THEN 3
            WHEN 'failed'    THEN 4
            WHEN 'success'   THEN 5
            WHEN 'expired'   THEN 6
            WHEN 'cancelled' THEN 7
            ELSE 99
            END ASC,
            priority_effective DESC,
            COALESCE(scheduled_for, created_at) ASC
        """
        
        select_sql = """
            SELECT *,
                MIN(100, priority + CAST((strftime('%s','now') - strftime('%s',created_at))/300 AS INTEGER)) AS priority_effective
            FROM action_queue
        """
        
        if statuses:
            in_clause = ",".join("?" for _ in statuses)
            return self.base.query(f"""
                {select_sql}
                WHERE status IN ({in_clause})
                ORDER BY {order_sql}
            """, tuple(statuses))
        
        return self.base.query(f"""
            {select_sql}
            ORDER BY {order_sql}
        """)
    
    def get_upcoming_actions_summary(self) -> List[Dict[str, Any]]:
        """Summary: next run per action_name from the schedule"""
        return self.base.query("""
            SELECT action_name, MIN(scheduled_for) AS next_run_at
            FROM action_queue
            WHERE status='scheduled' AND scheduled_for IS NOT NULL
            GROUP BY action_name
            ORDER BY next_run_at ASC
        """)
    
    # =========================================================================
    # QUEUE UPDATE OPERATIONS
    # =========================================================================
    
    def update_queue_status(self, queue_id: int, status: str, error_msg: str = None, result: str = None):
        """Update queue entry status with retry management on failure/expiry"""
        self.base.invalidate_stats_cache()
        
        if status == 'running':
            self.base.execute(
                "UPDATE action_queue SET status=?, started_at=CURRENT_TIMESTAMP WHERE id=?",
                (status, queue_id)
            )
        elif status in ('failed', 'expired'):
            self.base.execute("""
                UPDATE action_queue 
                SET status=?, 
                    completed_at=CURRENT_TIMESTAMP, 
                    error_message=?, 
                    result_summary=COALESCE(?, result_summary),
                    retry_count = MIN(retry_count + 1, max_retries)
                WHERE id=?
            """, (status, error_msg, result, queue_id))
        elif status in ('success', 'cancelled'):
            self.base.execute("""
                UPDATE action_queue 
                SET status=?, 
                    completed_at=CURRENT_TIMESTAMP, 
                    error_message=?, 
                    result_summary=COALESCE(?, result_summary)
                WHERE id=?
            """, (status, error_msg, result, queue_id))
            
            # When execution succeeds, supersede old failed/expired attempts
            if status == 'success':
                row = self.base.query_one("""
                    SELECT action_name, mac_address, port,
                           COALESCE(completed_at, started_at, created_at) AS ts
                    FROM action_queue WHERE id=? LIMIT 1
                """, (queue_id,))
                if row:
                    try:
                        self.supersede_old_attempts(row['action_name'], row['mac_address'], row['port'], row['ts'])
                    except Exception:
                        pass
    
    def promote_due_scheduled_to_pending(self) -> int:
        """Promote scheduled actions that are due (returns number of rows affected)"""
        self.base.invalidate_stats_cache()
        return self.base.execute("""
            UPDATE action_queue
               SET status='pending'
             WHERE status='scheduled'
               AND scheduled_for <= CURRENT_TIMESTAMP
        """)
    
    # =========================================================================
    # QUEUE INSERTION OPERATIONS
    # =========================================================================
    
    def ensure_scheduled_occurrence(
        self,
        action_name: str,
        next_run_at: str,
        mac: Optional[str] = "",
        ip: Optional[str] = "",
        *,
        port: Optional[int] = None,
        hostname: Optional[str] = None,
        service: Optional[str] = None,
        priority: int = 40,
        trigger: str = "scheduler",
        tags: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_retries: Optional[int] = None,
    ) -> bool:
        """
        Ensure a single upcoming 'scheduled' row exists for the given action/target.
        Returns True if inserted, False if already present (enforced by unique partial index).
        """
        js_tags = json.dumps(list(tags)) if tags is not None and not isinstance(tags, str) else (tags if isinstance(tags, str) else None)
        js_meta = json.dumps(metadata, ensure_ascii=False) if metadata else None
        
        try:
            self.base.execute("""
                INSERT INTO action_queue(
                    action_name, mac_address, ip, port, hostname, service,
                    priority, status, scheduled_for, trigger_source, tags, metadata, max_retries
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                action_name, mac or "", ip or "", port, hostname, service,
                int(priority), "scheduled", next_run_at, trigger, js_tags, js_meta, max_retries
            ))
            self.base.invalidate_stats_cache()
            return True
        except sqlite3.IntegrityError:
            return False
    
    def queue_action(self, action_name: str, mac: str, ip: str, port: int = None,
                     priority: int = 50, trigger: str = None, metadata: Dict = None) -> None:
        """Quick enqueue of a 'pending' action"""
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        self.base.execute("""
            INSERT INTO action_queue 
                (action_name, mac_address, ip, port, priority, trigger_source, metadata)
            VALUES (?,?,?,?,?,?,?)
        """, (action_name, mac, ip, port, priority, trigger, meta_json))
    
    def queue_action_at(
        self,
        action_name: str,
        mac: Optional[str] = "",
        ip: Optional[str] = "",
        *,
        port: Optional[int] = None,
        hostname: Optional[str] = None,
        service: Optional[str] = None,
        priority: int = 50,
        status: str = "pending",
        scheduled_for: Optional[str] = None,
        trigger: Optional[str] = "scheduler",
        tags: Optional[Iterable[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        max_retries: Optional[int] = None,
    ) -> None:
        """Generic enqueue that can publish 'pending' or 'scheduled' items with a date"""
        js_tags = json.dumps(list(tags)) if tags is not None and not isinstance(tags, str) else (tags if isinstance(tags, str) else None)
        js_meta = json.dumps(metadata, ensure_ascii=False) if metadata else None
        self.base.execute("""
            INSERT INTO action_queue(
                action_name, mac_address, ip, port, hostname, service,
                priority, status, scheduled_for, trigger_source, tags, metadata, max_retries
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            action_name, mac or "", ip or "", port, hostname, service,
            int(priority), status, scheduled_for, trigger, js_tags, js_meta, max_retries
        ))
    
    # =========================================================================
    # HISTORY AND STATUS OPERATIONS
    # =========================================================================
    
    def supersede_old_attempts(self, action_name: str, mac_address: str,
                               port: Optional[int] = None, ref_ts: Optional[str] = None) -> int:
        """
        Mark as 'superseded' all old attempts (failed|expired) for the triplet (action, mac, port)
        earlier than or equal to ref_ts (if provided). Returns affected row count.
        """
        params: List[Any] = [action_name, mac_address, port]
        time_clause = ""
        if ref_ts:
            time_clause = " AND datetime(COALESCE(completed_at, started_at, created_at)) <= datetime(?)"
            params.append(ref_ts)
        
        return self.base.execute(f"""
            UPDATE action_queue
               SET status='superseded',
                   error_message = COALESCE(error_message, 'superseded by newer success'),
                   completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP)
             WHERE action_name = ?
               AND mac_address = ?
               AND COALESCE(port,0) = COALESCE(?,0)
               AND status IN ('failed','expired')
               {time_clause}
        """, tuple(params))
    
    def list_attempt_history(self, action_name: str, mac_address: str,
                             port: Optional[int] = None, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Return history of attempts for (action, mac, port), most recent first.
        """
        return self.base.query("""
            SELECT action_name, mac_address, port, status, retry_count, max_retries,
                   COALESCE(completed_at, started_at, scheduled_for, created_at) AS ts
              FROM action_queue
             WHERE action_name=? AND mac_address=? AND COALESCE(port,0)=COALESCE(?,0)
             ORDER BY datetime(ts) DESC
             LIMIT ?
        """, (action_name, mac_address, port, int(limit)))
    
    def get_action_status_from_queue(
        self,
        action_name: str,
        mac_address: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Return the latest status row for an action (optionally filtered by MAC).
        """
        if mac_address:
            rows = self.base.query("""
                SELECT status, created_at, started_at, completed_at,
                    error_message, result_summary, retry_count, max_retries,
                    mac_address, port, hostname, service, priority
                FROM action_queue
                WHERE mac_address=? AND action_name=?
                ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
                LIMIT 1
            """, (mac_address, action_name))
        else:
            rows = self.base.query("""
                SELECT status, created_at, started_at, completed_at,
                    error_message, result_summary, retry_count, max_retries,
                    mac_address, port, hostname, service, priority
                FROM action_queue
                WHERE action_name=?
                ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
                LIMIT 1
            """, (action_name,))
        return rows[0] if rows else None
    
    def get_last_action_status_from_queue(self, mac_address: str, action_name: str) -> Optional[Dict[str, str]]:
        """
        Return {'status': 'success|failed|running|pending', 'raw': 'status_YYYYMMDD_HHMMSS'}
        based only on action_queue.
        """
        rows = self.base.query(
            """
            SELECT status,
                   COALESCE(completed_at, started_at, scheduled_for, created_at) AS ts
              FROM action_queue
             WHERE mac_address=? AND action_name=?
             ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
             LIMIT 1
            """,
            (mac_address, action_name)
        )
        if not rows:
            return None
        status = rows[0]["status"]
        ts = self._format_ts_for_raw(rows[0]["ts"])
        return {"status": status, "raw": f"{status}_{ts}"}
    
    def get_last_action_statuses_for_mac(self, mac_address: str) -> Dict[str, Dict[str, str]]:
        """
        Map action_name -> {'status':..., 'raw':...} from the latest queue rows for a MAC.
        """
        rows = self.base.query(
            """
            SELECT action_name, status,
                   COALESCE(completed_at, started_at, scheduled_for, created_at) AS ts
              FROM (
                    SELECT action_name, status, completed_at, started_at, scheduled_for, created_at,
                           ROW_NUMBER() OVER (
                             PARTITION BY action_name
                             ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
                           ) AS rn
                    FROM action_queue
                   WHERE mac_address=?
               )
             WHERE rn=1
            """,
            (mac_address,)
        )
        out: Dict[str, Dict[str, str]] = {}
        for r in rows:
            ts = self._format_ts_for_raw(r["ts"])
            st = r["status"]
            out[r["action_name"]] = {"status": st, "raw": f"{st}_{ts}"}
        return out
    
    # =========================================================================
    # HELPER METHODS
    # =========================================================================
    
    # =========================================================================
    # CIRCUIT BREAKER OPERATIONS (ORCH-01)
    # =========================================================================

    def record_circuit_breaker_failure(self, action_name: str, mac: str = '',
                                       threshold: int = 3) -> None:
        """Increment failure streak; open circuit if streak >= threshold."""
        now_str = self.base.query_one("SELECT datetime('now') AS ts")['ts']
        # Upsert the row
        self.base.execute("""
            INSERT INTO action_circuit_breaker (action_name, mac_address, failure_streak,
                                                last_failure_at, circuit_status)
            VALUES (?, ?, 1, ?, 'closed')
            ON CONFLICT(action_name, mac_address) DO UPDATE SET
                failure_streak = failure_streak + 1,
                last_failure_at = excluded.last_failure_at
        """, (action_name, mac or '', now_str))

        # Check if we need to open the circuit
        row = self.base.query_one(
            "SELECT failure_streak FROM action_circuit_breaker WHERE action_name=? AND mac_address=?",
            (action_name, mac or '')
        )
        if row and row['failure_streak'] >= threshold:
            streak = row['failure_streak']
            cooldown_secs = min(2 ** streak * 60, 3600)
            self.base.execute("""
                UPDATE action_circuit_breaker
                SET circuit_status = 'open',
                    opened_at = ?,
                    cooldown_until = datetime(?, '+' || ? || ' seconds')
                WHERE action_name=? AND mac_address=?
            """, (now_str, now_str, str(cooldown_secs), action_name, mac or ''))

    def record_circuit_breaker_success(self, action_name: str, mac: str = '') -> None:
        """Reset failure streak and close circuit on success."""
        self.base.execute("""
            INSERT INTO action_circuit_breaker (action_name, mac_address, failure_streak,
                                                circuit_status)
            VALUES (?, ?, 0, 'closed')
            ON CONFLICT(action_name, mac_address) DO UPDATE SET
                failure_streak = 0,
                circuit_status = 'closed',
                opened_at = NULL,
                cooldown_until = NULL
        """, (action_name, mac or ''))

    def is_circuit_open(self, action_name: str, mac: str = '') -> bool:
        """Return True if circuit is open AND cooldown hasn't expired.
        If cooldown has expired, transition to half_open and return False."""
        row = self.base.query_one(
            "SELECT circuit_status, cooldown_until FROM action_circuit_breaker "
            "WHERE action_name=? AND mac_address=?",
            (action_name, mac or '')
        )
        if not row:
            return False
        status = row['circuit_status']
        if status == 'closed':
            return False
        if status == 'open':
            cooldown = row.get('cooldown_until')
            if cooldown:
                # Check if cooldown has expired
                expired = self.base.query_one(
                    "SELECT datetime('now') >= datetime(?) AS expired",
                    (cooldown,)
                )
                if expired and expired['expired']:
                    # Transition to half_open
                    self.base.execute("""
                        UPDATE action_circuit_breaker SET circuit_status='half_open'
                        WHERE action_name=? AND mac_address=?
                    """, (action_name, mac or ''))
                    return False  # Allow one attempt through
            return True  # Still in cooldown
        # half_open: allow one attempt through
        return False

    def get_circuit_breaker_status(self, action_name: str, mac: str = '') -> Optional[Dict[str, Any]]:
        """Return full circuit breaker status dict."""
        row = self.base.query_one(
            "SELECT * FROM action_circuit_breaker WHERE action_name=? AND mac_address=?",
            (action_name, mac or '')
        )
        return dict(row) if row else None

    def reset_circuit_breaker(self, action_name: str, mac: str = '') -> None:
        """Manual reset of circuit breaker."""
        self.base.execute("""
            DELETE FROM action_circuit_breaker WHERE action_name=? AND mac_address=?
        """, (action_name, mac or ''))

    # =========================================================================
    # CONCURRENCY OPERATIONS (ORCH-02)
    # =========================================================================

    def count_running_actions(self, action_name: Optional[str] = None) -> int:
        """Count currently running actions, optionally filtered by action_name."""
        if action_name:
            row = self.base.query_one(
                "SELECT COUNT(*) AS cnt FROM action_queue WHERE status='running' AND action_name=?",
                (action_name,)
            )
        else:
            row = self.base.query_one(
                "SELECT COUNT(*) AS cnt FROM action_queue WHERE status='running'"
            )
        return int(row['cnt']) if row else 0

    # =========================================================================
    # HELPER METHODS
    # =========================================================================

    def _format_ts_for_raw(self, ts_db: Optional[str]) -> str:
        """
        Convert SQLite 'YYYY-MM-DD HH:MM:SS' to 'YYYYMMDD_HHMMSS'.
        Fallback to current UTC when no timestamp is available.
        """
        from datetime import datetime as _dt
        ts = (ts_db or "").strip()
        if not ts:
            return _dt.utcnow().strftime("%Y%m%d_%H%M%S")
        return ts.replace("-", "").replace(":", "").replace(" ", "_")