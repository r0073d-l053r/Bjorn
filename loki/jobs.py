"""
Loki job manager — tracks HIDScript execution jobs.
Each job runs in its own daemon thread.
"""
import uuid
import time
import logging
import traceback
from datetime import datetime
from threading import Thread, Event

from logger import Logger

logger = Logger(name="loki.jobs", level=logging.DEBUG)


class LokiJobManager:
    """Manages HIDScript job lifecycle."""

    def __init__(self, engine):
        self.engine = engine
        self._jobs = {}       # job_id → job dict
        self._threads = {}    # job_id → Thread
        self._stops = {}      # job_id → Event

    def create_job(self, script_name: str, script_content: str) -> str:
        """Create and start a new job. Returns job_id (UUID)."""
        job_id = str(uuid.uuid4())[:8]
        now = datetime.now().isoformat()

        job = {
            "id": job_id,
            "script_name": script_name,
            "status": "pending",
            "output": "",
            "error": "",
            "started_at": None,
            "finished_at": None,
            "created_at": now,
        }
        self._jobs[job_id] = job
        stop = Event()
        self._stops[job_id] = stop

        # Persist to DB
        try:
            db = self.engine.shared_data.db
            db.execute(
                "INSERT INTO loki_jobs (id, script_name, status, created_at) VALUES (?, ?, ?, ?)",
                (job_id, script_name, "pending", now)
            )
        except Exception as e:
            logger.error("DB insert job error: %s", e)

        # Start execution thread
        t = Thread(
            target=self._run_job,
            args=(job_id, script_content, stop),
            daemon=True,
            name=f"loki-job-{job_id}",
        )
        self._threads[job_id] = t
        t.start()

        logger.info("Job %s created: %s", job_id, script_name)
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job."""
        stop = self._stops.get(job_id)
        if stop:
            stop.set()
            job = self._jobs.get(job_id)
            if job and job["status"] == "running":
                job["status"] = "cancelled"
                job["finished_at"] = datetime.now().isoformat()
                self._update_db(job_id, "cancelled", job.get("output", ""), "Cancelled by user")
            logger.info("Job %s cancelled", job_id)
            return True
        return False

    def get_all_jobs(self) -> list:
        """Return list of all jobs (most recent first)."""
        jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return jobs

    def get_job(self, job_id: str) -> dict:
        """Get a single job by ID."""
        return self._jobs.get(job_id)

    def clear_completed(self):
        """Remove finished/failed/cancelled jobs from memory."""
        to_remove = [
            jid for jid, j in self._jobs.items()
            if j["status"] in ("succeeded", "failed", "cancelled")
        ]
        for jid in to_remove:
            self._jobs.pop(jid, None)
            self._threads.pop(jid, None)
            self._stops.pop(jid, None)
        try:
            self.engine.shared_data.db.execute(
                "DELETE FROM loki_jobs WHERE status IN ('succeeded', 'failed', 'cancelled')"
            )
        except Exception as e:
            logger.error("DB clear jobs error: %s", e)

    @property
    def running_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j["status"] == "running")

    # ── Internal ───────────────────────────────────────────────

    def _run_job(self, job_id: str, script_content: str, stop: Event):
        """Execute a HIDScript in this thread."""
        job = self._jobs[job_id]
        job["status"] = "running"
        job["started_at"] = datetime.now().isoformat()
        self._update_db(job_id, "running")

        try:
            from loki.hidscript import HIDScriptParser
            parser = HIDScriptParser(self.engine.hid_controller)
            output_lines = parser.execute(script_content, stop_event=stop, job_id=job_id)

            if stop.is_set():
                job["status"] = "cancelled"
            else:
                job["status"] = "succeeded"

            job["output"] = "\n".join(output_lines)

        except Exception as e:
            job["status"] = "failed"
            job["error"] = str(e)
            job["output"] = traceback.format_exc()
            logger.error("Job %s failed: %s", job_id, e)

        finally:
            job["finished_at"] = datetime.now().isoformat()
            self._update_db(
                job_id, job["status"],
                job.get("output", ""),
                job.get("error", ""),
            )
            logger.info("Job %s finished: %s", job_id, job["status"])

    def _update_db(self, job_id: str, status: str, output: str = "", error: str = ""):
        """Persist job state to database."""
        try:
            db = self.engine.shared_data.db
            db.execute(
                "UPDATE loki_jobs SET status=?, output=?, error=?, "
                "started_at=?, finished_at=? WHERE id=?",
                (status, output, error,
                 self._jobs.get(job_id, {}).get("started_at"),
                 self._jobs.get(job_id, {}).get("finished_at"),
                 job_id)
            )
        except Exception as e:
            logger.error("DB update job error: %s", e)
