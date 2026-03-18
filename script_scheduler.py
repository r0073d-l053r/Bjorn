"""script_scheduler.py - Background daemon for scheduled scripts and conditional triggers."""

import json
import threading
import time
import subprocess
import os
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

from logger import Logger

logger = Logger(name="script_scheduler", level=logging.DEBUG)


def evaluate_conditions(node: dict, db) -> bool:
    """Recursively evaluate a condition tree (AND/OR groups + leaf conditions)."""
    if not node or not isinstance(node, dict):
        return False

    node_type = node.get("type", "condition")

    if node_type == "group":
        op = node.get("op", "AND").upper()
        children = node.get("children", [])
        if not children:
            return True
        results = [evaluate_conditions(c, db) for c in children]
        return all(results) if op == "AND" else any(results)

    # Leaf condition
    source = node.get("source", "")

    if source == "action_result":
        return _eval_action_result(node, db)
    elif source == "hosts_with_port":
        return _eval_hosts_with_port(node, db)
    elif source == "hosts_alive":
        return _eval_hosts_alive(node, db)
    elif source == "cred_found":
        return _eval_cred_found(node, db)
    elif source == "has_vuln":
        return _eval_has_vuln(node, db)
    elif source == "db_count":
        return _eval_db_count(node, db)
    elif source == "time_after":
        return _eval_time_after(node)
    elif source == "time_before":
        return _eval_time_before(node)

    logger.warning(f"Unknown condition source: {source}")
    return False


def _compare(actual, check, expected):
    """Generic numeric comparison."""
    try:
        actual = float(actual)
        expected = float(expected)
    except (ValueError, TypeError):
        return str(actual) == str(expected)

    if check == "eq": return actual == expected
    if check == "neq": return actual != expected
    if check == "gt": return actual > expected
    if check == "lt": return actual < expected
    if check == "gte": return actual >= expected
    if check == "lte": return actual <= expected
    return False


def _eval_action_result(node, db):
    """Check last result of a specific action in the action_queue."""
    action = node.get("action", "")
    check = node.get("check", "eq")
    value = node.get("value", "success")
    row = db.query_one(
        "SELECT status FROM action_queue WHERE action_name=? ORDER BY updated_at DESC LIMIT 1",
        (action,)
    )
    if not row:
        return False
    return _compare(row["status"], check, value)


def _eval_hosts_with_port(node, db):
    """Count alive hosts with a specific port open."""
    port = str(node.get("port", ""))
    check = node.get("check", "gt")
    value = node.get("value", 0)
    # ports column is semicolon-separated
    rows = db.query(
        "SELECT COUNT(1) c FROM hosts WHERE alive=1 AND (ports LIKE ? OR ports LIKE ? OR ports LIKE ? OR ports=?)",
        (f"{port};%", f"%;{port};%", f"%;{port}", port)
    )
    count = rows[0]["c"] if rows else 0
    return _compare(count, check, value)


def _eval_hosts_alive(node, db):
    """Count alive hosts."""
    check = node.get("check", "gt")
    value = node.get("value", 0)
    row = db.query_one("SELECT COUNT(1) c FROM hosts WHERE alive=1")
    count = row["c"] if row else 0
    return _compare(count, check, value)


def _eval_cred_found(node, db):
    """Check if credentials exist for a service."""
    service = node.get("service", "")
    row = db.query_one("SELECT COUNT(1) c FROM creds WHERE service=?", (service,))
    return (row["c"] if row else 0) > 0


def _eval_has_vuln(node, db):
    """Check if any vulnerabilities exist."""
    row = db.query_one("SELECT COUNT(1) c FROM vulnerabilities WHERE active=1")
    return (row["c"] if row else 0) > 0


def _eval_db_count(node, db):
    """Count rows in a whitelisted table with simple conditions."""
    ALLOWED_TABLES = {"hosts", "creds", "vulnerabilities", "action_queue", "services"}
    table = node.get("table", "")
    if table not in ALLOWED_TABLES:
        logger.warning(f"db_count: table '{table}' not in whitelist")
        return False

    where = node.get("where", {})
    check = node.get("check", "gt")
    value = node.get("value", 0)

    # Build parameterized WHERE clause
    conditions = []
    params = []
    for k, v in where.items():
        # Only allow simple alphanumeric column names
        if k.isalnum():
            conditions.append(f"{k}=?")
            params.append(v)

    sql = f"SELECT COUNT(1) c FROM {table}"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)

    row = db.query_one(sql, tuple(params))
    count = row["c"] if row else 0
    return _compare(count, check, value)


def _eval_time_after(node):
    """Check if current time is after a given hour:minute."""
    hour = int(node.get("hour", 0))
    minute = int(node.get("minute", 0))
    now = datetime.now()
    return (now.hour, now.minute) >= (hour, minute)


def _eval_time_before(node):
    """Check if current time is before a given hour:minute."""
    hour = int(node.get("hour", 23))
    minute = int(node.get("minute", 59))
    now = datetime.now()
    return (now.hour, now.minute) < (hour, minute)


class ScriptSchedulerDaemon(threading.Thread):
    """Lightweight 30s tick daemon for script schedules and conditional triggers."""

    MAX_PENDING_EVENTS = 100
    MAX_CONCURRENT_SCRIPTS = 4

    def __init__(self, shared_data):
        super().__init__(daemon=True, name="ScriptScheduler")
        self.shared_data = shared_data
        self.db = shared_data.db
        self._stop = threading.Event()
        self.check_interval = 30
        self._pending_action_events = []
        self._events_lock = threading.Lock()
        self._active_threads = 0
        self._threads_lock = threading.Lock()

    def run(self):
        logger.info("ScriptSchedulerDaemon started (30s tick)")
        # Initial delay to let the system boot
        if self._stop.wait(10):
            return
        while not self._stop.is_set():
            try:
                self._check_schedules()
                self._check_triggers()
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}")
            self._stop.wait(self.check_interval)
        logger.info("ScriptSchedulerDaemon stopped")

    def stop(self):
        self._stop.set()

    def notify_action_complete(self, action_name: str, mac: str, success: bool):
        """Called from orchestrator when an action finishes. Queues an event for next tick."""
        with self._events_lock:
            if len(self._pending_action_events) >= self.MAX_PENDING_EVENTS:
                self._pending_action_events.pop(0)
            self._pending_action_events.append({
                "action": action_name,
                "mac": mac,
                "success": success,
            })

    def _check_schedules(self):
        """Query due schedules and fire each in a separate thread."""
        try:
            due = self.db.get_due_schedules()
        except Exception as e:
            logger.error(f"Failed to query due schedules: {e}")
            return

        for sched in due:
            sched_id = sched["id"]
            script_name = sched["script_name"]
            args = sched.get("args", "") or ""

            # Check conditions if any
            conditions_raw = sched.get("conditions")
            if conditions_raw:
                try:
                    conditions = json.loads(conditions_raw) if isinstance(conditions_raw, str) else conditions_raw
                    if conditions and not evaluate_conditions(conditions, self.db):
                        logger.debug(f"Schedule {sched_id} conditions not met, skipping")
                        continue
                except Exception as e:
                    logger.warning(f"Schedule {sched_id} condition eval failed: {e}")

            # Respect concurrency limit
            with self._threads_lock:
                if self._active_threads >= self.MAX_CONCURRENT_SCRIPTS:
                    logger.debug(f"Skipping schedule {sched_id}: max concurrent scripts reached")
                    continue

            logger.info(f"Firing scheduled script: {script_name} (schedule={sched_id})")
            self.db.mark_schedule_run(sched_id, "running")

            threading.Thread(
                target=self._run_with_tracking,
                args=(sched_id, script_name, args),
                daemon=True
            ).start()

    def _run_with_tracking(self, sched_id: int, script_name: str, args: str):
        """Thread wrapper that tracks active count for concurrency limiting."""
        with self._threads_lock:
            self._active_threads += 1
        try:
            self._execute_scheduled(sched_id, script_name, args)
        finally:
            with self._threads_lock:
                self._active_threads = max(0, self._active_threads - 1)

    def _execute_scheduled(self, sched_id: int, script_name: str, args: str):
        """Run the script and record result. When sched_id is 0 (trigger-fired), skip schedule updates."""
        process = None
        try:
            # Look up the action in DB to determine format and path
            action = None
            for a in self.db.list_actions():
                if a["b_class"] == script_name or a["b_module"] == script_name:
                    action = a
                    break

            if not action:
                if sched_id > 0:
                    self.db.mark_schedule_run(sched_id, "error", f"Action {script_name} not found")
                return

            module_name = action["b_module"]
            script_path = os.path.join(self.shared_data.actions_dir, f"{module_name}.py")

            if not os.path.exists(script_path):
                if sched_id > 0:
                    self.db.mark_schedule_run(sched_id, "error", f"Script file not found: {script_path}")
                return

            # Detect format for custom scripts
            from web_utils.script_utils import _detect_script_format
            is_custom = module_name.startswith("custom/")
            fmt = _detect_script_format(script_path) if is_custom else "bjorn"

            # Build command
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["BJORN_EMBEDDED"] = "1"

            if fmt == "free":
                cmd = ["sudo", "python3", "-u", script_path]
            else:
                runner_path = os.path.join(self.shared_data.current_dir, "action_runner.py")
                cmd = ["sudo", "python3", "-u", runner_path, module_name, action["b_class"]]
            if args:
                cmd.extend(args.split())

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, env=env, cwd=self.shared_data.current_dir
            )

            # Wait for completion
            stdout, _ = process.communicate(timeout=3600)  # 1h max
            exit_code = process.returncode

            if exit_code == 0:
                if sched_id > 0:
                    self.db.mark_schedule_run(sched_id, "success")
                logger.info(f"Scheduled script {script_name} completed successfully")
            else:
                last_lines = (stdout or "").strip().split('\n')[-3:]
                error_msg = '\n'.join(last_lines) if last_lines else f"Exit code {exit_code}"
                if sched_id > 0:
                    self.db.mark_schedule_run(sched_id, "error", error_msg)
                logger.warning(f"Scheduled script {script_name} failed (code={exit_code})")

        except subprocess.TimeoutExpired:
            if process:
                process.kill()
                process.wait()
            if sched_id > 0:
                self.db.mark_schedule_run(sched_id, "error", "Timeout (1h)")
            logger.error(f"Scheduled script {script_name} timed out")
        except Exception as e:
            if sched_id > 0:
                self.db.mark_schedule_run(sched_id, "error", str(e))
            logger.error(f"Error executing scheduled script {script_name}: {e}")
        finally:
            # Ensure subprocess resources are released
            if process:
                try:
                    if process.stdout:
                        process.stdout.close()
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                except Exception:
                    pass

    def _check_triggers(self):
        """Evaluate conditions for active triggers."""
        try:
            triggers = self.db.get_active_triggers()
        except Exception as e:
            logger.error(f"Failed to query triggers: {e}")
            return

        for trig in triggers:
            trig_id = trig["id"]
            try:
                if self.db.is_trigger_on_cooldown(trig_id):
                    continue

                conditions = trig.get("conditions", "")
                if isinstance(conditions, str):
                    conditions = json.loads(conditions)

                if not conditions:
                    continue

                if evaluate_conditions(conditions, self.db):
                    # Respect concurrency limit
                    with self._threads_lock:
                        if self._active_threads >= self.MAX_CONCURRENT_SCRIPTS:
                            logger.debug(f"Skipping trigger {trig_id}: max concurrent scripts")
                            continue

                    script_name = trig["script_name"]
                    args = trig.get("args", "") or ""
                    logger.info(f"Trigger '{trig['trigger_name']}' fired -> {script_name}")
                    self.db.mark_trigger_fired(trig_id)

                    threading.Thread(
                        target=self._run_with_tracking,
                        args=(0, script_name, args),
                        daemon=True
                    ).start()
            except Exception as e:
                logger.warning(f"Trigger {trig_id} eval error: {e}")

        # Clear consumed events
        with self._events_lock:
            self._pending_action_events.clear()
