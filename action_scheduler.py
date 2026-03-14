# action_scheduler.py testsdd
# Smart Action Scheduler for Bjorn - queue-only implementation
# Handles trigger evaluation, requirements checking, and queue management.
#
# Invariants we enforce:
#   - At most ONE "active" row per (action_name, mac_address, COALESCE(port,0))
#     where active ∈ {'scheduled','pending','running'}.
#   - Retries for failed entries are coordinated by cleanup_queue() (with backoff)
#     and never compete with trigger-based enqueues.
#
# Runtime knobs (from shared.py):
#   shared_data.retry_success_actions : bool (default False)
#   shared_data.retry_failed_actions  : bool (default True)
#
# These take precedence over cooldown / rate-limit for NON-interval triggers.

from __future__ import annotations

import json
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from init_shared import shared_data
from logger import Logger
from ai_engine import get_or_create_ai_engine

logger = Logger(name="action_scheduler.py")

# ---------- UTC helpers (match SQLite's UTC CURRENT_TIMESTAMP) ----------
def _utcnow() -> datetime:
    """Naive UTC datetime to compare with SQLite TEXT timestamps (UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

def _utcnow_str() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' string to compare against SQLite TEXT."""
    return _utcnow().strftime("%Y-%m-%d %H:%M:%S")

def _db_ts(dt: datetime) -> str:
    """Format any datetime as 'YYYY-MM-DD HH:MM:SS' (UTC expected)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# Service → fallback ports (used when port_services table has nothing for a host)
SERVICE_PORTS: Dict[str, List[str]] = {
    "ssh": ["22"],
    "http": ["80", "8080"],
    "https": ["443"],
    "smb": ["445"],
    "ftp": ["21"],
    "telnet": ["23"],
    "mysql": ["3306"],
    "mssql": ["1433"],
    "postgres": ["5432"],
    "rdp": ["3389"],
    "vnc": ["5900"],
}


class ActionScheduler:
    """
    Smart scheduler that evaluates triggers and enqueues actions.
    Does NOT execute actions - that's the orchestrator's job.
    """

    def __init__(self, shared_data_):
        self.shared_data = shared_data_
        self.db = shared_data_.db

        # Controller MAC for global actions
        self.ctrl_mac = (self.shared_data.get_raspberry_mac() or "__GLOBAL__").lower()
        self._ensure_host_exists(self.ctrl_mac)

        # Runtime flags
        self.running = True
        self.check_interval = 5  # seconds between iterations
        self._stop_event = threading.Event()
        self._error_backoff = 1.0

        # Action definition cache
        self._action_definitions: Dict[str, Dict[str, Any]] = {}
        self._last_cache_refresh = 0.0
        self._cache_ttl = 60.0  # seconds

        # Memory for global actions
        self._last_global_runs: Dict[str, float] = {}
        # Actions Studio last source type
        self._last_source_is_studio: Optional[bool] = None
        # Enforce DB invariants (idempotent)
        self._ensure_db_invariants()
        
        # Throttling for priorities
        self._last_priority_update = 0.0
        self._priority_update_interval = 60.0 # seconds
        
        # Initialize AI engine for recommendations ONLY in AI mode.
        # Uses singleton so model weights are loaded only once across the process.
        self.ai_engine = None
        if self.shared_data.operation_mode == "AI":
            self.ai_engine = get_or_create_ai_engine(self.shared_data)
            if self.ai_engine is None:
                logger.info_throttled(
                    "AI engine unavailable in scheduler; continuing heuristic-only",
                    key="scheduler_ai_init_failed",
                    interval_s=300.0,
                )

        logger.info("ActionScheduler initialized")

    # --------------------------------------------------------------------- loop

    def run(self):
        """Main scheduler loop."""
        logger.info("ActionScheduler starting main loop")
        while self.running and not self.shared_data.orchestrator_should_exit:
            try:
                # If the user toggles AI mode at runtime, enable/disable AI engine without restart.
                if self.shared_data.operation_mode == "AI" and self.ai_engine is None:
                    self.ai_engine = get_or_create_ai_engine(self.shared_data)
                    if self.ai_engine:
                        logger.info("Scheduler: AI engine enabled (singleton)")
                    else:
                        logger.info_throttled(
                            "Scheduler: AI engine unavailable; staying heuristic-only",
                            key="scheduler_ai_enable_failed",
                            interval_s=300.0,
                        )
                elif self.shared_data.operation_mode != "AI" and self.ai_engine is not None:
                    self.ai_engine = None

                # Refresh action cache if needed
                self._refresh_cache_if_needed()
                # Keep queue consistent with current enable/disable flags.
                self._cancel_queued_disabled_actions()

                # 1) Promote scheduled actions that are due
                self._promote_scheduled_to_pending()

                # 2) Publish next scheduled occurrences for interval actions
                self._publish_all_upcoming()

                # 3) Evaluate global on_start actions
                self._evaluate_global_actions()

                # 4) Evaluate per-host triggers
                self.evaluate_all_triggers()

                # 5) Queue maintenance
                self.cleanup_queue()
                self.update_priorities()

                self._error_backoff = 1.0
                if self._stop_event.wait(self.check_interval):
                    break

            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")
                if self._stop_event.wait(self._error_backoff):
                    break
                self._error_backoff = min(self._error_backoff * 2.0, 15.0)

        logger.info("ActionScheduler stopped")

    # ----------------------------------------------------------------- priorities

    def update_priorities(self):
        """
        Update priorities of pending actions.
        1. Increase priority over time (starvation prevention) with MIN(100) cap.
        2. [AI Mode] Boost priority of actions recommended by AI engine.
        """
        now = time.time()
        if now - self._last_priority_update < self._priority_update_interval:
            return

        try:
            # 1. Anti-starvation aging: +1 per minute for actions waiting >1 hour.
            #    julianday is portable across all SQLite builds.
            #    MIN(100) cap prevents unbounded priority inflation.
            affected = self.db.execute(
                """
                UPDATE action_queue
                SET priority = MIN(100, priority + 1)
                WHERE status='pending'
                  AND julianday('now') - julianday(created_at) > 0.0417
                """
            )

            self._last_priority_update = now

            if affected and affected > 0:
                logger.debug(f"Aged {affected} pending actions in queue")

            # 2. AI Recommendation Boost
            if self.shared_data.operation_mode == "AI" and self.ai_engine:
                self._apply_ai_priority_boost()
            elif self.shared_data.operation_mode == "AI" and not self.ai_engine:
                logger.warning("Operation mode is AI, but ai_engine is not initialized!")
                
        except Exception as e:
            logger.error(f"Failed to update priorities: {e}")

    def _apply_ai_priority_boost(self):
        """Boost priority of actions recommended by AI engine."""
        try:
            if not self.ai_engine:
                logger.warning("AI Boost skipped: ai_engine is None")
                return

            # Get list of unique hosts with pending actions
            hosts = self.db.query("""
                SELECT DISTINCT mac_address FROM action_queue 
                WHERE status='pending'
            """)
            
            if not hosts:
                return

            for row in hosts:
                mac = row['mac_address']
                if not mac:
                    continue
                
                # Get available actions for this host
                available = [
                    r['action_name'] for r in self.db.query("""
                        SELECT DISTINCT action_name FROM action_queue
                        WHERE mac_address=? AND status='pending'
                    """, (mac,))
                ]
                
                if not available:
                    continue
                
                # Get host context
                host_data = self.db.get_host_by_mac(mac)
                if not host_data:
                    continue
                
                context = {
                    'mac': mac,
                    'hostname': (host_data.get('hostnames') or '').split(';')[0],
                    'ports': [
                        int(p) for p in (host_data.get('ports') or '').split(';') 
                        if p.isdigit()
                    ]
                }
                
                # Ask AI for recommendation
                recommended_action, confidence, debug = self.ai_engine.choose_action(
                    host_context=context,
                    available_actions=available,
                    exploration_rate=0.0  # No exploration in scheduler
                )
                if not isinstance(debug, dict):
                    debug = {}

                threshold = self._get_ai_confirm_threshold()
                if recommended_action and confidence >= threshold:  # Only boost if confident
                    # Boost recommended action
                    boost_amount = int(20 * confidence)  # Scale boost by confidence
                    
                    affected = self.db.execute("""
                        UPDATE action_queue
                        SET priority = priority + ?
                        WHERE mac_address=? AND action_name=? AND status='pending'
                    """, (boost_amount, mac, recommended_action))
                    
                    if affected and affected > 0:
                        # NEW: Update metadata to reflect AI influence
                        try:
                            # We fetch all matching IDs to update their metadata
                            rows = self.db.query("""
                                SELECT id, metadata FROM action_queue
                                WHERE mac_address=? AND action_name=? AND status='pending'
                            """, (mac, recommended_action))
                            
                            for row in rows:
                                meta = json.loads(row['metadata'] or '{}')
                                meta['decision_method'] = f"ai_boosted ({debug.get('method', 'unknown')})"
                                meta['decision_origin'] = "ai_boosted"
                                meta['decision_scope'] = "priority_boost"
                                meta['ai_confidence'] = confidence
                                meta['ai_threshold'] = threshold
                                meta['ai_method'] = str(debug.get('method', 'unknown'))
                                meta['ai_recommended_action'] = recommended_action
                                meta['ai_model_loaded'] = bool(getattr(self.ai_engine, "model_loaded", False))
                                meta['ai_reason'] = "priority_boost_applied"
                                meta['ai_debug'] = debug  # Includes all_scores and input_vector
                                self.db.execute("UPDATE action_queue SET metadata=? WHERE id=?", 
                                               (json.dumps(meta), row['id']))
                        except Exception as meta_e:
                            logger.error(f"Failed to update metadata for AI boost: {meta_e}")

                        logger.info(
                            f"[AI_BOOST] action={recommended_action} mac={mac} boost={boost_amount} "
                            f"conf={float(confidence):.2f} thr={float(threshold):.2f} "
                            f"method={debug.get('method', 'unknown')}"
                        )
            
        except Exception as e:
            logger.error(f"Error applying AI priority boost: {e}")

    def stop(self):
        """Stop the scheduler."""
        logger.info("Stopping ActionScheduler...")
        self.running = False
        self._stop_event.set()

    # --------------------------------------------------------------- definitions

    def _get_ai_confirm_threshold(self) -> float:
        """Return normalized AI confirmation threshold in [0.0, 1.0]."""
        try:
            raw = float(getattr(self.shared_data, "ai_confirm_threshold", 0.3))
        except Exception:
            raw = 0.3
        return max(0.0, min(1.0, raw))

    def _annotate_decision_metadata(
        self,
        metadata: Dict[str, Any],
        action_name: str,
        context: Dict[str, Any],
        decision_scope: str,
    ) -> None:
        """
        Fill metadata with a consistent decision trace:
        decision_method/origin + AI method/confidence/threshold/reason.
        """
        metadata.setdefault("decision_method", "heuristic")
        metadata.setdefault("decision_origin", "heuristic")
        metadata["decision_scope"] = decision_scope

        threshold = self._get_ai_confirm_threshold()
        metadata["ai_threshold"] = threshold

        if self.shared_data.operation_mode != "AI":
            metadata["ai_reason"] = "ai_mode_disabled"
            return

        if not self.ai_engine:
            metadata["ai_reason"] = "ai_engine_unavailable"
            return

        try:
            recommended, confidence, debug = self.ai_engine.choose_action(
                host_context=context,
                available_actions=[action_name],
                exploration_rate=0.0,
            )

            ai_method = str((debug or {}).get("method", "unknown"))
            confidence_f = float(confidence or 0.0)
            model_loaded = bool(getattr(self.ai_engine, "model_loaded", False))

            metadata["ai_method"] = ai_method
            metadata["ai_confidence"] = confidence_f
            metadata["ai_recommended_action"] = recommended or ""
            metadata["ai_model_loaded"] = model_loaded

            if recommended == action_name and confidence_f >= threshold:
                metadata["decision_method"] = f"ai_confirmed ({ai_method})"
                metadata["decision_origin"] = "ai_confirmed"
                metadata["ai_reason"] = "recommended_above_threshold"
            elif recommended != action_name:
                metadata["decision_origin"] = "heuristic"
                metadata["ai_reason"] = "recommended_different_action"
            else:
                metadata["decision_origin"] = "heuristic"
                metadata["ai_reason"] = "confidence_below_threshold"

        except Exception as e:
            metadata["ai_reason"] = "ai_check_failed"
            logger.debug(f"AI decision annotation failed for {action_name}: {e}")

    def _log_queue_decision(
        self,
        action_name: str,
        mac: str,
        metadata: Dict[str, Any],
        target_port: Optional[int] = None,
        target_service: Optional[str] = None,
    ) -> None:
        """Emit a compact, explicit queue-decision log line."""
        decision = str(metadata.get("decision_method", "heuristic"))
        origin = str(metadata.get("decision_origin", "heuristic"))
        ai_method = str(metadata.get("ai_method", "n/a"))
        ai_reason = str(metadata.get("ai_reason", "n/a"))
        ai_conf = metadata.get("ai_confidence")
        ai_thr = metadata.get("ai_threshold")
        scope = str(metadata.get("decision_scope", "unknown"))

        conf_txt = f"{float(ai_conf):.2f}" if isinstance(ai_conf, (int, float)) else "n/a"
        thr_txt = f"{float(ai_thr):.2f}" if isinstance(ai_thr, (int, float)) else "n/a"
        model_loaded = bool(metadata.get("ai_model_loaded", False))
        port_txt = "None" if target_port is None else str(target_port)
        svc_txt = target_service if target_service else "None"

        logger.info(
            f"[QUEUE_DECISION] scope={scope} action={action_name} mac={mac} port={port_txt} service={svc_txt} "
            f"decision={decision} origin={origin} ai_method={ai_method} conf={conf_txt} thr={thr_txt} "
            f"model_loaded={model_loaded} reason={ai_reason}"
        )

# ---------- replace this method ----------
    def _refresh_cache_if_needed(self):
        """Refresh action definitions cache if expired or source flipped."""
        now = time.time()
        use_studio = bool(getattr(self.shared_data, "use_actions_studio", False))

        # Refresh if TTL expired or the source changed (actions ↔ studio_actions)
        if (now - self._last_cache_refresh > self._cache_ttl) or (self._last_source_is_studio != use_studio):
            self._refresh_action_cache(use_studio=use_studio)
            self._last_cache_refresh = now
            self._last_source_is_studio = use_studio


# ---------- replace this method ----------
    def _refresh_action_cache(self, use_studio: Optional[bool] = None):
        """Reload action definitions from database, from 'actions' or 'studio' view."""
        if use_studio is None:
            use_studio = bool(getattr(self.shared_data, "use_actions_studio", False))

        try:
            if use_studio:
                # Primary: studio
                actions = self.db.list_studio_actions()
                source = "studio"
            else:
                # Primary: plain actions
                actions = self.db.list_actions()
                source = "actions"

            # Build cache (expect same action schema: b_class, b_trigger, b_action, etc.)
            self._action_definitions = {a["b_class"]: a for a in actions}
            # Runtime truth: orchestrator loads from `actions`, so align b_enabled to it
            # even when scheduler uses `actions_studio` as source.
            self._overlay_runtime_enabled_flags()
            logger.info(f"Refreshed action cache from '{source}': {len(self._action_definitions)} actions")

        except AttributeError as e:
            # Fallback if the chosen method isn't available on the DB adapter
            if use_studio and hasattr(self.db, "list_actions"):
                logger.warning(f"DB has no list_studio_actions(); falling back to list_actions(): {e}")
                try:
                    actions = self.db.list_actions()
                    self._action_definitions = {a["b_class"]: a for a in actions}
                    logger.info(f"Refreshed action cache from 'actions' (fallback): {len(self._action_definitions)} actions")
                    return
                except Exception as ee:
                    logger.error(f"Fallback list_actions() failed: {ee}")
            else:
                logger.error(f"Action cache refresh failed (no suitable DB method): {e}")

        except Exception as e:
            logger.error(f"Failed to refresh action cache: {e}")

    def _is_action_enabled(self, action_def: Dict[str, Any]) -> bool:
        """Parse b_enabled robustly across int/bool/string/null values."""
        raw = action_def.get("b_enabled", 1)
        if raw is None:
            return True
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return int(raw) == 1
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "on"}:
            return True
        if s in {"0", "false", "no", "off"}:
            return False
        try:
            return int(float(s)) == 1
        except Exception:
            # Conservative default: keep action enabled when value is malformed.
            return True

    def _overlay_runtime_enabled_flags(self):
        """
        Override cached `b_enabled` with runtime `actions` table values.
        This keeps scheduler decisions aligned with orchestrator loaded actions.
        """
        try:
            runtime_rows = self.db.list_actions()
            runtime_map = {r.get("b_class"): r.get("b_enabled", 1) for r in runtime_rows}
            for action_name, action_def in self._action_definitions.items():
                if action_name in runtime_map:
                    action_def["b_enabled"] = runtime_map[action_name]
        except Exception as e:
            logger.warning(f"Could not overlay runtime b_enabled flags: {e}")

    def _cancel_queued_disabled_actions(self):
        """Cancel pending/scheduled queue entries for currently disabled actions."""
        try:
            disabled = [
                name for name, definition in self._action_definitions.items()
                if not self._is_action_enabled(definition)
            ]
            if not disabled:
                return

            placeholders = ",".join("?" for _ in disabled)
            affected = self.db.execute(
                f"""
                UPDATE action_queue
                SET status='cancelled',
                    completed_at=CURRENT_TIMESTAMP,
                    error_message=COALESCE(error_message, 'disabled_by_config')
                WHERE status IN ('scheduled','pending')
                  AND action_name IN ({placeholders})
                """,
                tuple(disabled),
            )
            if affected:
                logger.info(f"Cancelled {affected} queued action(s) because b_enabled=0")
        except Exception as e:
            logger.error(f"Failed to cancel queued disabled actions: {e}")


    # ------------------------------------------------------------------ helpers

    def _ensure_db_invariants(self):
        """
        Create a partial UNIQUE index that forbids more than one active entry
        for the same (action_name, mac_address, COALESCE(port,0)).
        """
        try:
            self.db.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS uq_action_active
                ON action_queue(action_name, mac_address, COALESCE(port,0))
                WHERE status IN ('scheduled','pending','running')
                """
            )
        except Exception as e:
            # If the SQLite build does not support partial/expression indexes,
            # we still have app-level guards (NOT EXISTS inserts). But this
            # index is recommended to make the invariant bulletproof.
            logger.warning(f"Could not create unique partial index (fallback to app-level guards): {e}")

    def _promote_scheduled_to_pending(self):
        """Promote due scheduled actions to pending status."""
        try:
            promoted = self.db.promote_due_scheduled_to_pending()
            if promoted:
                logger.debug(f"Promoted {promoted} scheduled action(s) to pending")
        except Exception as e:
            logger.error(f"Failed to promote scheduled actions: {e}")

    def _ensure_host_exists(self, mac: str):
        """Ensure host exists in database (idempotent)."""
        if not mac:
            return
        try:
            self.db.execute(
                """
                INSERT INTO hosts (mac_address, alive, updated_at)
                VALUES (?, 0, CURRENT_TIMESTAMP)
                ON CONFLICT(mac_address) DO UPDATE SET
                  updated_at = CURRENT_TIMESTAMP
                """,
                (mac,),
            )
        except Exception:
            pass

    # ---------------------------------------------------------- interval logic

    def _parse_interval_seconds(self, trigger: str) -> int:
        """Parse interval from trigger string 'on_interval:SECONDS'."""
        if not trigger or not trigger.startswith("on_interval:"):
            return 0
        try:
            return max(0, int(trigger.split(":", 1)[1] or 0))
        except Exception:
            return 0

    def _publish_all_upcoming(self):
        """
        Publish next scheduled occurrence for all interval actions.

        NOTE: By design, the runtime flags do not cancel interval publishing.
        """
        # Global interval actions
        for action in self._action_definitions.values():
            if (action.get("b_action") or "normal") != "global":
                continue
            if not self._is_action_enabled(action):
                continue

            trigger = (action.get("b_trigger") or "").strip()
            interval = self._parse_interval_seconds(trigger)
            if interval <= 0:
                continue

            self._publish_next_schedule_for_global(action, interval)

        # Per-host interval actions
        try:
            hosts = self.db.get_all_hosts()
        except Exception:
            hosts = []

        for host in hosts:
            if not host.get("alive"):
                continue

            mac = host.get("mac_address") or ""
            if not mac:
                continue

            for action in self._action_definitions.values():
                if (action.get("b_action") or "normal") == "global":
                    continue
                if not self._is_action_enabled(action):
                    continue

                trigger = (action.get("b_trigger") or "").strip()
                interval = self._parse_interval_seconds(trigger)
                if interval <= 0:
                    continue

                self._publish_next_schedule_for_host(host, action, interval)

    def _publish_next_schedule_for_global(self, action_def: Dict[str, Any], interval: int):
        """Publish next scheduled occurrence for a global action."""
        try:
            action_name = action_def["b_class"]
            mac = self.ctrl_mac

            # Already active?
            active = self.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE action_name=? AND mac_address=?
                  AND status IN ('scheduled','pending','running')
                LIMIT 1
                """,
                (action_name, mac),
            )
            if active:
                return

            # Next occurrence immediately after last completion, else now (UTC)
            last = self._get_last_global_execution_time(action_name)
            next_run = _utcnow() if not last else (last + timedelta(seconds=interval))
            scheduled_for = _db_ts(next_run)

            metadata = {
                "interval": interval,
                "is_global": True,
                "decision_method": "heuristic",
                "decision_origin": "heuristic",
            }
            self._annotate_decision_metadata(
                metadata=metadata,
                action_name=action_name,
                context={"mac": mac, "hostname": "Bjorn-C2", "ports": []},
                decision_scope="scheduled_global",
            )

            inserted = self.db.ensure_scheduled_occurrence(
                action_name=action_name,
                next_run_at=scheduled_for,
                mac=mac,
                ip="0.0.0.0",
                priority=int(action_def.get("b_priority", 40) or 40),
                trigger="scheduler",
                tags=action_def.get("b_tags", []),
                metadata=metadata,
                max_retries=int(action_def.get("b_max_retries", 3) or 3),
            )
            if inserted:
                logger.debug(f"Scheduled global '{action_name}' at {scheduled_for}")

        except Exception as e:
            logger.error(f"Failed to publish global schedule: {e}")

    def _publish_next_schedule_for_host(self, host: Dict[str, Any], action_def: Dict[str, Any], interval: int):
        """Publish next scheduled occurrence for a per-host action."""
        try:
            mac = host.get("mac_address") or ""
            if not mac:
                return

            self._ensure_host_exists(mac)
            action_name = action_def["b_class"]

            # Already active?
            active = self.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE action_name=? AND mac_address=?
                  AND status IN ('scheduled','pending','running')
                LIMIT 1
                """,
                (action_name, mac),
            )
            if active:
                return

            # Next occurrence immediately after last completion, else now (UTC)
            last = self._get_last_execution_time(mac, action_name)
            next_run = _utcnow() if not last else (last + timedelta(seconds=interval))
            scheduled_for = _db_ts(next_run)

            metadata = {
                "interval": interval,
                "is_global": False,
                "decision_method": "heuristic",
                "decision_origin": "heuristic",
            }
            self._annotate_decision_metadata(
                metadata=metadata,
                action_name=action_name,
                context={
                    "mac": mac,
                    "hostname": (host.get("hostnames") or "").split(";")[0],
                    "ports": [int(p) for p in (host.get("ports") or "").split(";") if p.isdigit()],
                },
                decision_scope="scheduled_host",
            )

            inserted = self.db.ensure_scheduled_occurrence(
                action_name=action_name,
                next_run_at=scheduled_for,
                mac=mac,
                ip=(host.get("ips") or "").split(";")[0] if host.get("ips") else "",
                priority=int(action_def.get("b_priority", 40) or 40),
                trigger="scheduler",
                tags=action_def.get("b_tags", []),
                metadata=metadata,
                max_retries=int(action_def.get("b_max_retries", 3) or 3),
            )
            if inserted:
                logger.debug(f"Scheduled '{action_name}' for {mac} at {scheduled_for}")

        except Exception as e:
            logger.error(f"Failed to publish host schedule: {e}")

    # ------------------------------------------------------------ global start

    def _evaluate_global_actions(self):
        """Evaluate and queue global actions with on_start trigger."""
        self._globals_lock = getattr(self, "_globals_lock", threading.Lock())

        with self._globals_lock:
            try:
                for action in self._action_definitions.values():
                    if (action.get("b_action") or "normal") != "global":
                        continue
                    if not self._is_action_enabled(action):
                        continue

                    trigger = (action.get("b_trigger") or "").strip()
                    if trigger != "on_start":
                        continue

                    action_name = action["b_class"]

                    # Already executed at least once?
                    last = self._get_last_global_execution_time(action_name)
                    if last is not None:
                        continue

                    # Already queued?
                    existing = self.db.query(
                        """
                        SELECT 1 FROM action_queue
                        WHERE action_name=? AND status IN ('scheduled','pending','running')
                        LIMIT 1
                        """,
                        (action_name,),
                    )
                    if existing:
                        continue

                    # Queue the action
                    if self._queue_global_action(action):
                        self._last_global_runs[action_name] = time.time()

            except Exception as e:
                logger.error(f"Error evaluating global actions: {e}")

    def _queue_global_action(self, action_def: Dict[str, Any]) -> bool:
        """Queue a global action for execution (idempotent insert)."""
        action_name = action_def["b_class"]
        mac = self.ctrl_mac
        ip = "0.0.0.0"
        timeout = int(action_def.get("b_timeout", 300) or 300)
        expires_at = _db_ts(_utcnow() + timedelta(seconds=timeout))

        metadata = {
            "trigger": action_def.get("b_trigger", ""),
            "requirements": action_def.get("b_requires", ""),
            "timeout": timeout,
            "is_global": True,
            "decision_method": "heuristic",
            "decision_origin": "heuristic",
        }

        # Global context (controller itself)
        context = {
            "mac": mac,
            "hostname": "Bjorn-C2",
            "ports": []  # Global actions usually don't target specific ports on controller
        }
        self._annotate_decision_metadata(
            metadata=metadata,
            action_name=action_name,
            context=context,
            decision_scope="queue_global",
        )
        ai_conf = metadata.get("ai_confidence")
        if isinstance(ai_conf, (int, float)) and metadata.get("decision_origin") == "ai_confirmed":
            action_def["b_priority"] = int(action_def.get("b_priority", 50) or 50) + int(20 * float(ai_conf))

        try:
            self._ensure_host_exists(mac)
            # Guard with NOT EXISTS to avoid races
            affected = self.db.execute(
                """
                INSERT INTO action_queue (
                    action_name, mac_address, ip, port, hostname, service,
                    priority, status, max_retries, expires_at,
                    trigger_source, tags, metadata
                )
                SELECT ?, ?, ?, NULL, NULL, NULL,
                       ?, 'pending', ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM action_queue
                    WHERE action_name=? AND mac_address=? AND COALESCE(port,0)=0
                      AND status IN ('scheduled','pending','running')
                )
                """,
                (
                    action_name,
                    mac,
                    ip,
                    int(action_def.get("b_priority", 50) or 50),
                    int(action_def.get("b_max_retries", 3) or 3),
                    expires_at,
                    action_def.get("b_trigger", ""),
                    json.dumps(action_def.get("b_tags", [])),
                    json.dumps(metadata),
                    action_name,
                    mac,
                ),
            )
            if affected and affected > 0:
                self._log_queue_decision(action_name=action_name, mac=mac, metadata=metadata)
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to queue global action {action_name}: {e}")
            return False

    # ------------------------------------------------------------- host path

    def evaluate_all_triggers(self):
        """Evaluate triggers for all hosts."""
        hosts = self.db.get_all_hosts()  # include dead hosts for on_leave trigger
        for host in hosts:
            mac = host["mac_address"]

            for action_name, action_def in self._action_definitions.items():
                # Skip global actions
                if (action_def.get("b_action") or "normal") == "global":
                    continue

                # Skip disabled actions
                if not self._is_action_enabled(action_def):
                    continue

                trigger = (action_def.get("b_trigger") or "").strip()
                if not trigger:
                    continue

                # Skip interval triggers (handled elsewhere)
                if trigger.startswith("on_interval:"):
                    continue

                # Evaluate trigger
                if not evaluate_trigger(trigger, host, action_def):
                    continue

                # Evaluate requirements
                requires = action_def.get("b_requires", "")
                if requires and not evaluate_requirements(requires, host, action_def):
                    continue

                # Resolve target port/service
                target_port, target_service = self._resolve_target_port_service(mac, host, action_def)

                # Decide if we should enqueue
                if not self._should_queue_action(mac, action_name, action_def, target_port):
                    continue

                # Queue the action
                self._queue_action(host, action_def, target_port, target_service)

    def _resolve_target_port_service(
        self, mac: str, host: Dict[str, Any], action_def: Dict[str, Any]
    ) -> Tuple[Optional[int], Optional[str]]:
        """Resolve target port and service for action (service wins over port when present)."""
        ports = _normalize_ports(host.get("ports"))
        target_port: Optional[int] = None
        target_service: Optional[str] = None

        # Try b_service first
        if action_def.get("b_service"):
            try:
                services = (
                    json.loads(action_def["b_service"])
                    if isinstance(action_def["b_service"], str)
                    else action_def["b_service"]
                )
            except Exception:
                services = []

            if services:
                for svc in services:
                    row = self.db.query(
                        "SELECT port FROM port_services "
                        "WHERE mac_address=? AND state='open' AND LOWER(service)=? "
                        "ORDER BY last_seen DESC LIMIT 1",
                        (mac, str(svc).lower()),
                    )
                    if row:
                        target_port = int(row[0]["port"])
                        target_service = str(svc).lower()
                        break

        # Fallback to b_port
        if target_port is None and action_def.get("b_port"):
            if str(action_def["b_port"]) in ports:
                target_port = int(action_def["b_port"])

        return target_port, target_service

    # ----------------------------------------------------- re-queue policy core

    def _get_last_status(self, mac: str, action_name: str, target_port: Optional[int]) -> Optional[str]:
        """
        Return last known status for (mac, action, port), considering the
        chronological fields (completed_at > started_at > scheduled_for > created_at).
        """
        self_port = 0 if target_port is None else int(target_port)
        row = self.db.query(
            """
            SELECT status
            FROM action_queue
            WHERE mac_address=? AND action_name=? AND COALESCE(port,0)=?
            ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
            LIMIT 1
            """,
            (mac, action_name, self_port),
        )
        return row[0]["status"] if row else None

    def _should_queue_action(
        self, mac: str, action_name: str, action_def: Dict[str, Any], target_port: Optional[int]
    ) -> bool:
        """
        Decide if we should enqueue a new job.

        Evaluation order:
          0) no duplicate active job
          1) runtime flags (retry_success_actions / retry_failed_actions)
          1-bis) do NOT enqueue if a retryable failed exists (let cleanup_queue() handle it)
          2) cooldown
          3) rate limit
        """
        self_port = 0 if target_port is None else int(target_port)

        # Circuit breaker check (ORCH-01)
        if self.db.is_circuit_open(action_name, mac):
            logger.debug(f"Circuit breaker open for {action_name}/{mac}, skipping")
            return False

        # Global concurrency limit check (ORCH-02)
        running_count = self.db.count_running_actions()
        max_concurrent = int(getattr(self.shared_data, 'semaphore_slots', 5))
        if running_count >= max_concurrent:
            logger.debug(f"Concurrency limit reached ({running_count}/{max_concurrent}), skipping {action_name}")
            return False

        # Per-action concurrency limit (ORCH-02)
        requires_raw = action_def.get("b_requires", "")
        if requires_raw:
            try:
                req_obj = json.loads(requires_raw) if isinstance(requires_raw, str) else requires_raw
                if isinstance(req_obj, dict) and "max_concurrent" in req_obj:
                    max_per_action = int(req_obj["max_concurrent"])
                    running_for_action = self.db.count_running_actions(action_name=action_name)
                    if running_for_action >= max_per_action:
                        logger.debug(f"Per-action concurrency limit for {action_name} ({running_for_action}/{max_per_action})")
                        return False
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # 0) Duplicate protection (active)
        existing = self.db.query(
            """
            SELECT 1 FROM action_queue
            WHERE mac_address=? AND action_name=? AND COALESCE(port,0)=?
              AND status IN ('scheduled','pending','running')
            LIMIT 1
            """,
            (mac, action_name, self_port),
        )
        if existing:
            return False

        # 1) Runtime flags take precedence
        allow_success = bool(getattr(self.shared_data, "retry_success_actions", False))
        allow_failed  = bool(getattr(self.shared_data, "retry_failed_actions", True))
        last_status = self._get_last_status(mac, action_name, target_port)

        if last_status == "success" and not allow_success:
            return False
        if last_status == "failed" and not allow_failed:
            return False

        # 1-bis) If a retryable failed exists, let cleanup_queue() requeue it (avoid duplicates)
        if allow_failed:
            retryable = self.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE mac_address=? AND action_name=? AND COALESCE(port,0)=?
                  AND status='failed'
                  AND retry_count < max_retries
                  AND COALESCE(error_message,'') != 'expired'
                LIMIT 1
                """,
                (mac, action_name, self_port),
            )
            if retryable:
                return False

        # 2) Cooldown (UTC)
        cooldown = int(action_def.get("b_cooldown", 0) or 0)
        if cooldown > 0:
            last_exec = self._get_last_execution_time(mac, action_name)
            if last_exec and (_utcnow() - last_exec).total_seconds() < cooldown:
                return False

        # 3) Rate limit (UTC)
        rate_limit = (action_def.get("b_rate_limit") or "").strip()
        if rate_limit and not self._check_rate_limit(mac, action_name, rate_limit):
            return False

        return True

    def _queue_action(
        self, host: Dict[str, Any], action_def: Dict[str, Any], target_port: Optional[int], target_service: Optional[str]
    ) -> bool:
        """Queue action for execution (idempotent insert with NOT EXISTS guard)."""
        action_name = action_def["b_class"]
        mac = host["mac_address"]
        timeout = int(action_def.get("b_timeout", 300) or 300)
        expires_at = _db_ts(_utcnow() + timedelta(seconds=timeout))
        self_port = 0 if target_port is None else int(target_port)

        metadata = {
            "trigger": action_def.get("b_trigger", ""),
            "requirements": action_def.get("b_requires", ""),
            "is_global": False,
            "timeout": timeout,
            "decision_method": "heuristic",
            "decision_origin": "heuristic",
            "ports_snapshot": host.get("ports") or "",
        }

        context = {
            "mac": mac,
            "hostname": (host.get("hostnames") or "").split(";")[0],
            "ports": [int(p) for p in (host.get("ports") or "").split(";") if p.isdigit()],
        }
        self._annotate_decision_metadata(
            metadata=metadata,
            action_name=action_name,
            context=context,
            decision_scope="queue_host",
        )
        ai_conf = metadata.get("ai_confidence")
        if isinstance(ai_conf, (int, float)) and metadata.get("decision_origin") == "ai_confirmed":
            # Apply small priority boost only when AI confirmed this exact action.
            action_def["b_priority"] = int(action_def.get("b_priority", 50) or 50) + int(20 * float(ai_conf))

        try:
            affected = self.db.execute(
                """
                INSERT INTO action_queue (
                    action_name, mac_address, ip, port, hostname, service,
                    priority, status, max_retries, expires_at,
                    trigger_source, tags, metadata
                )
                SELECT ?, ?, ?, ?, ?, ?,
                       ?, 'pending', ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM action_queue
                    WHERE mac_address=? AND action_name=? AND COALESCE(port,0)=?
                      AND status IN ('scheduled','pending','running')
                )
                """,
                (
                    action_name,
                    mac,
                    (host.get("ips") or "").split(";")[0] if host.get("ips") else "",
                    target_port,
                    (host.get("hostnames") or "").split(";")[0] if host.get("hostnames") else "",
                    target_service,
                    int(action_def.get("b_priority", 50) or 50),
                    int(action_def.get("b_max_retries", 3) or 3),
                    expires_at,
                    action_def.get("b_trigger", ""),
                    json.dumps(action_def.get("b_tags", [])),
                    json.dumps(metadata),
                    mac,
                    action_name,
                    self_port,
                ),
            )
            if affected and affected > 0:
                self._log_queue_decision(
                    action_name=action_name,
                    mac=mac,
                    metadata=metadata,
                    target_port=target_port,
                    target_service=target_service,
                )
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to queue {action_name} for {mac}: {e}")
            return False

    # ------------------------------------------------------------- last times

    def _get_last_execution_time(self, mac: str, action_name: str) -> Optional[datetime]:
        """Get last execution time (DB read only; naive UTC)."""
        row = self.db.query(
            """
            SELECT completed_at FROM action_queue
            WHERE mac_address=? AND action_name=? AND status IN ('success','failed')
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            (mac, action_name),
        )
        if row and row[0].get("completed_at"):
            try:
                val = row[0]["completed_at"]
                if isinstance(val, str):
                    return datetime.fromisoformat(val)
                elif isinstance(val, datetime):
                    return val
            except Exception:
                return None
        return None

    def _get_last_global_execution_time(self, action_name: str) -> Optional[datetime]:
        """Get last global action execution time (naive UTC)."""
        row = self.db.query(
            """
            SELECT completed_at FROM action_queue
            WHERE action_name=? AND status IN ('success','failed')
            ORDER BY completed_at DESC
            LIMIT 1
            """,
            (action_name,),
        )
        if row and row[0].get("completed_at"):
            try:
                val = row[0]["completed_at"]
                if isinstance(val, str):
                    return datetime.fromisoformat(val)
                elif isinstance(val, datetime):
                    return val
            except Exception:
                return None
        return None

    # ------------------------------------------------------------- constraints

    def _check_rate_limit(self, mac: str, action_name: str, rate_limit: str) -> bool:
        """
        Check "X/SECONDS" rate-limit (count based on created_at).
        Returns True if action is allowed to queue.
        """
        try:
            max_count, period = rate_limit.split("/")
            max_count = int(max_count)
            period = int(period)
            since = _db_ts(_utcnow() - timedelta(seconds=period))

            count = self.db.query(
                """
                SELECT COUNT(*) AS c FROM action_queue
                WHERE mac_address=? AND action_name=? AND created_at >= ?
                """,
                (mac, action_name, since),
            )[0]["c"]

            return int(count) < max_count
        except Exception:
            # Invalid format -> do not block
            return True

    # -------------------------------------------------------------- maintenance

    def cleanup_queue(self):
        """Clean up queue: timeouts, retries, purge old entries."""
        try:
            now_iso = _utcnow_str()

            # 1) Expire pending actions
            self.db.execute(
                """
                UPDATE action_queue
                SET status='failed',
                    completed_at=CURRENT_TIMESTAMP,
                    error_message=COALESCE(error_message,'expired')
                WHERE status='pending'
                  AND expires_at IS NOT NULL
                  AND expires_at < ?
                """,
                (now_iso,),
            )

            # 2) Timeout running actions
            self.db.execute(
                """
                UPDATE action_queue
                SET status='failed',
                    completed_at=CURRENT_TIMESTAMP,
                    error_message=COALESCE(error_message,'timeout')
                WHERE status='running'
                  AND started_at IS NOT NULL
                  AND datetime(started_at, '+' || COALESCE(
                        CAST(json_extract(metadata, '$.timeout') AS INTEGER), 900
                    ) || ' seconds') <= datetime('now')
                """
            )

            # 3) Retry failed actions with exponential backoff
            if bool(getattr(self.shared_data, "retry_failed_actions", True)):
                # Only if no active job exists for the same (action, mac, port)
                self.db.execute(
                    """
                    UPDATE action_queue AS a
                    SET status='pending',
                        retry_count = retry_count + 1,
                        scheduled_for = datetime(
                            'now',
                            '+' || (
                                CASE
                                  WHEN (60 * (1 << retry_count)) > 900 THEN 900
                                  ELSE (60 * (1 << retry_count))
                                END
                            ) || ' seconds'
                        ),
                        error_message = NULL,
                        started_at = NULL,
                        completed_at = NULL
                    WHERE a.status='failed'
                      AND a.retry_count < a.max_retries
                      AND COALESCE(a.error_message,'') != 'expired'
                      AND NOT EXISTS (
                        SELECT 1 FROM action_queue b
                        WHERE b.mac_address=a.mac_address
                          AND b.action_name=a.action_name
                          AND COALESCE(b.port,0)=COALESCE(a.port,0)
                          AND b.status IN ('scheduled','pending','running')
                      )
                    """
                )

            # 4) Purge old completed entries
            old_date = _db_ts(_utcnow() - timedelta(days=7))
            self.db.execute(
                """
                DELETE FROM action_queue
                WHERE status IN ('success','failed','cancelled','expired')
                  AND completed_at < ?
                """,
                (old_date,),
            )

        except Exception as e:
            logger.error(f"Failed to cleanup queue: {e}")

    # update_priorities is defined above (line ~166); this duplicate is removed.


# =================================================================== helpers ==

def _normalize_ports(raw) -> List[str]:
    """Normalize ports to list of strings."""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(p).split("/")[0] for p in raw if p is not None and str(p) != ""]
    if isinstance(raw, int):
        return [str(raw)]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("[") and s.endswith("]"):
            try:
                arr = json.loads(s)
                return [str(p).split("/")[0] for p in arr]
            except Exception:
                pass
        if ";" in s:
            return [p.strip().split("/")[0] for p in s.split(";") if p.strip()]
        return [s.split("/")[0]]
    return [str(raw)]


def _has_open_service(mac: str, svc: str, host: Dict[str, Any]) -> bool:
    """Check if service is open for host (port_services first, then fallback list)."""
    svc = (svc or "").lower().strip()

    # Check port_services table first
    rows = shared_data.db.query(
        "SELECT 1 FROM port_services WHERE mac_address=? AND state='open' AND LOWER(service)=? LIMIT 1",
        (mac, svc),
    )
    if rows:
        return True

    # Fallback to known port numbers
    ports = set(_normalize_ports(host.get("ports")))
    for p in SERVICE_PORTS.get(svc, []):
        if p in ports:
            return True
    return False


def _last_presence_event_for_mac(mac: str) -> Optional[str]:
    """Get last presence event for MAC (PresenceJoin/PresenceLeave)."""
    rows = shared_data.db.query(
        """
        SELECT action_name
        FROM action_queue
        WHERE mac_address=?
          AND action_name IN ('PresenceJoin','PresenceLeave')
        ORDER BY datetime(COALESCE(completed_at, started_at, scheduled_for, created_at)) DESC
        LIMIT 1
        """,
        (mac,),
    )
    return rows[0]["action_name"] if rows else None


# --------------------------------------------------------------- trigger eval --

def evaluate_trigger(trigger: str, host: Dict[str, Any], action_def: Dict[str, Any]) -> bool:
    """
    Evaluate trigger condition for host.

    Supported triggers:
    - on_start, on_host_alive, on_host_dead
    - on_port_change, on_new_port:PORT
    - on_service:SERVICE, on_web_service
    - on_success:ACTION, on_failure:ACTION
    - on_cred_found:SERVICE
    - on_mac_is:MAC, on_essid_is:ESSID, on_ip_is:IP
    - on_has_cve[:CVE], on_has_cpe[:CPE]
    - on_all:[...], on_any:[...]
    """
    try:
        mac = host["mac_address"]
        s = (trigger or "").strip()
        if not s:
            return False

        # Combined triggers
        if s.startswith("on_all:"):
            try:
                arr = json.loads(s.split(":", 1)[1])
            except Exception:
                return False
            return all(evaluate_trigger(t, host, action_def) for t in arr)

        if s.startswith("on_any:"):
            try:
                arr = json.loads(s.split(":", 1)[1])
            except Exception:
                return False
            return any(evaluate_trigger(t, host, action_def) for t in arr)

        # Skip interval triggers
        if s.startswith("on_interval:"):
            return False

        # Parse trigger name and parameter
        if ":" in s:
            name, param = s.split(":", 1)
            name = name.strip()
            param = (param or "").strip()
        else:
            name, param = s, ""

        # Aliases
        if name == "on_alive":
            name = "on_host_alive"
        if name == "on_dead":
            name = "on_host_dead"

        # Join/Leave events
        if name == "on_join":
            if not bool(host.get("alive")):
                return False
            last = _last_presence_event_for_mac(mac)
            return last != "PresenceJoin"

        if name == "on_leave":
            if bool(host.get("alive")):
                return False
            last = _last_presence_event_for_mac(mac)
            return last != "PresenceLeave"

        # Basic triggers
        if name == "on_start" or name == "on_new_host":
            r = shared_data.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE mac_address=? AND action_name=?
                  AND status IN ('success','failed')
                LIMIT 1
                """,
                (mac, action_def["b_class"]),
            )
            return not bool(r)

        if name == "on_host_alive":
            return bool(host.get("alive"))

        if name == "on_host_dead":
            return not bool(host.get("alive"))

        # Skip port/service triggers for dead hosts
        if not bool(host.get("alive")) and name in {"on_service", "on_web_service", "on_new_port", "on_port_change"}:
            return False

        # Port triggers
        if name == "on_port_change":
            cur = set(_normalize_ports(host.get("ports")))
            prev = set(_normalize_ports(host.get("previous_ports")))
            return cur != prev

        if name == "on_new_port":
            port = str(param)
            cur = set(_normalize_ports(host.get("ports")))
            prev = set(_normalize_ports(host.get("previous_ports")))
            return port in cur and port not in prev

        # Action status triggers
        if name == "on_success":
            parent = param
            r = shared_data.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE mac_address=? AND action_name=? AND status='success'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (mac, parent),
            )
            return bool(r)

        if name == "on_failure":
            parent = param
            r = shared_data.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE mac_address=? AND action_name=? AND status='failed'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (mac, parent),
            )
            return bool(r)

        # Service triggers
        if name == "on_cred_found":
            service = param.lower()
            r = shared_data.db.query(
                "SELECT 1 FROM creds WHERE mac_address=? AND LOWER(service)=? LIMIT 1",
                (mac, service),
            )
            return bool(r)

        if name == "on_service":
            return _has_open_service(mac, param, host)

        if name == "on_web_service":
            return _has_open_service(mac, "http", host) or _has_open_service(mac, "https", host)

        # Identity triggers
        if name == "on_mac_is":
            return str(mac).lower() == param.lower()

        if name == "on_essid_is":
            return (host.get("essid") or "") == param

        if name == "on_ip_is":
            ips = (host.get("ips") or "").split(";") if host.get("ips") else []
            return param in ips

        # Vulnerability triggers
        if name == "on_has_cve":
            if not param:
                r = shared_data.db.query(
                    "SELECT 1 FROM vulnerabilities WHERE mac_address=? AND is_active=1 LIMIT 1",
                    (mac,),
                )
                return bool(r)
            r = shared_data.db.query(
                "SELECT 1 FROM vulnerabilities WHERE mac_address=? AND vuln_id=? AND is_active=1 LIMIT 1",
                (mac, param),
            )
            return bool(r)

        if name == "on_has_cpe":
            if not param:
                r = shared_data.db.query(
                    "SELECT 1 FROM detected_software WHERE mac_address=? AND is_active=1 LIMIT 1",
                    (mac,),
                )
                return bool(r)
            r = shared_data.db.query(
                "SELECT 1 FROM detected_software WHERE mac_address=? AND cpe=? AND is_active=1 LIMIT 1",
                (mac, param),
            )
            return bool(r)

        # Unknown trigger
        logger.debug(f"Unknown trigger: {name}")
        return False

    except Exception as e:
        logger.error(f"Error evaluating trigger '{trigger}': {e}")
        return False


# ---------------------------------------------------------- requirements eval --

def evaluate_requirements(requires: Any, host: Dict[str, Any], action_def: Dict[str, Any]) -> bool:
    """Evaluate requirements for action."""
    if requires is None:
        return True

    # Already an object
    if isinstance(requires, (dict, list)):
        return evaluate_requirements_object(requires, host, action_def)

    s = str(requires).strip()
    if not s:
        return True

    # JSON string
    if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
        try:
            obj = json.loads(s)
            return evaluate_requirements_object(obj, host, action_def)
        except Exception:
            pass

    # Legacy "Action:status" format
    if ":" in s:
        a, st = s.split(":", 1)
        obj = {"action": a.strip(), "status": st.strip()}
        return evaluate_requirements_object(obj, host, action_def)

    return True


def evaluate_requirements_object(req: Any, host: Dict[str, Any], action_def: Dict[str, Any]) -> bool:
    """
    Evaluate requirements object.

    Supported:
    - {"all": [...]} / {"any": [...]} / {"not": {...}}
    - {"action": "ACTION", "status": "STATUS", "scope": "host|global"}
    - {"has_port": PORT}
    - {"has_cred": "SERVICE"}
    - {"has_cve": "CVE"}
    - {"has_cpe": "CPE"}
    - {"mac_is": "MAC"}
    - {"essid_is": "ESSID"}
    - {"service_is_open": "SERVICE"}
    """
    mac = host["mac_address"]

    # Combinators
    if isinstance(req, dict) and "all" in req:
        return all(evaluate_requirements_object(x, host, action_def) for x in (req.get("all") or []))

    if isinstance(req, dict) and "any" in req:
        return any(evaluate_requirements_object(x, host, action_def) for x in (req.get("any") or []))

    if isinstance(req, dict) and "not" in req:
        return not evaluate_requirements_object(req.get("not"), host, action_def)

    # Atomic requirements
    if isinstance(req, dict):
        if "action" in req:
            action = str(req.get("action") or "").strip()
            status = str(req.get("status") or "success").strip()
            scope = str(req.get("scope") or "host").strip().lower()

            if scope == "global":
                r = shared_data.db.query(
                    """
                    SELECT 1 FROM action_queue
                    WHERE action_name=? AND status=?
                    ORDER BY completed_at DESC LIMIT 1
                    """,
                    (action, status),
                )
                return bool(r)

            # Host scope
            r = shared_data.db.query(
                """
                SELECT 1 FROM action_queue
                WHERE mac_address=? AND action_name=? AND status=?
                ORDER BY completed_at DESC LIMIT 1
                """,
                (mac, action, status),
            )
            return bool(r)

        if "has_port" in req:
            want = str(req.get("has_port"))
            return want in set(_normalize_ports(host.get("ports")))

        if "has_cred" in req:
            svc = str(req.get("has_cred") or "").lower()
            r = shared_data.db.query(
                "SELECT 1 FROM creds WHERE mac_address=? AND LOWER(service)=? LIMIT 1",
                (mac, svc),
            )
            return bool(r)

        if "has_cve" in req:
            cve = str(req.get("has_cve") or "")
            r = shared_data.db.query(
                "SELECT 1 FROM vulnerabilities WHERE mac_address=? AND vuln_id=? AND is_active=1 LIMIT 1",
                (mac, cve),
            )
            return bool(r)

        if "has_cpe" in req:
            cpe = str(req.get("has_cpe") or "")
            r = shared_data.db.query(
                "SELECT 1 FROM detected_software WHERE mac_address=? AND cpe=? AND is_active=1 LIMIT 1",
                (mac, cpe),
            )
            return bool(r)

        if "mac_is" in req:
            return str(mac).lower() == str(req.get("mac_is") or "").lower()

        if "essid_is" in req:
            return (host.get("essid") or "") == str(req.get("essid_is") or "")

        if "service_is_open" in req:
            svc = str(req.get("service_is_open") or "").lower()
            return _has_open_service(mac, svc, host)

    # Default: truthy
    return bool(req)
