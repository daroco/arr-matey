"""
Async job runner for fix actions. Every action runs as a tracked `action_run` row
rather than blocking the HTTP request that triggered it -- an rclone sync can run for
hours (README section 9), so "click a button, wait for the response" isn't viable.

Single-flight locking is per action_id, in-process (a plain dict of asyncio.Lock),
not cross-process -- this app runs as a single uvicorn worker (see run.py), so that's
sufficient and avoids needing a real distributed lock for a home-lab tool with one
user at a time.
"""

import asyncio
import inspect
import json
import logging
import uuid

from .actions import ACTIONS
from .models import utcnow_iso

log = logging.getLogger("dashboard.jobs")

MAX_LOG_TAIL_LINES = 200


class JobRunner:
    def __init__(self, db, cfg):
        self.db = db
        self.cfg = cfg
        self._locks = {}

    def _lock_for(self, action_id):
        if action_id not in self._locks:
            self._locks[action_id] = asyncio.Lock()
        return self._locks[action_id]

    def start_job(self, action_id, params, requested_by):
        action = ACTIONS[action_id]
        job_id = str(uuid.uuid4())
        now = utcnow_iso()
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO action_run (id, action_id, params_json, requested_by, requested_at, status) VALUES (?,?,?,?,?,?)",
                (job_id, action_id, json.dumps(params), requested_by, now, "pending"),
            )
        asyncio.create_task(self._run(job_id, action, params))
        return job_id

    async def _run(self, job_id, action, params):
        lock = self._lock_for(action.id) if action.single_flight else None
        if lock and lock.locked():
            self._finish(job_id, status="failed", error_text="another run of this action is already in progress")
            return

        async def body():
            self._set_status(job_id, "running", started_at=utcnow_iso())
            try:
                if action.is_subprocess:
                    lines = []
                    async for line in action.execute(self.cfg, params):
                        lines.append(line)
                        self._append_log(job_id, lines[-MAX_LOG_TAIL_LINES:])
                    self._finish(job_id, status="ok", result_text="\n".join(lines[-20:]))
                else:
                    result = await asyncio.to_thread(action.execute, self.cfg, params)
                    # detail is the substantive payload for investigative actions
                    # (e.g. arr_why_not_grabbed's per-release rejection reasons) --
                    # message alone is just a one-line summary. Both are shown.
                    text = result.message + (f"\n\n{result.detail}" if result.detail else "")
                    self._finish(
                        job_id, status="ok" if result.ok else "failed",
                        result_text=text, error_text=None if result.ok else result.message,
                    )
            except Exception as e:
                log.exception(f"job {job_id} ({action.id}) crashed")
                self._finish(job_id, status="failed", error_text=str(e))

        if lock:
            async with lock:
                await body()
        else:
            await body()

    def _set_status(self, job_id, status, **fields):
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.db.conn:
            self.db.conn.execute(
                f"UPDATE action_run SET status = ?{', ' + cols if cols else ''} WHERE id = ?",
                (status, *fields.values(), job_id),
            )

    def _append_log(self, job_id, lines):
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE action_run SET log_tail = ? WHERE id = ?", ("\n".join(lines), job_id)
            )

    def _finish(self, job_id, status, result_text=None, error_text=None):
        self._set_status(job_id, status, finished_at=utcnow_iso(), result_text=result_text, error_text=error_text)

    def reconcile_orphaned(self):
        """Called once from main.py's lifespan on startup -- any job still marked
        'running' from a previous process (crash, restart mid-job) gets marked
        'interrupted' rather than spinning forever. See the plan's verification
        checklist item for exactly this scenario."""
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE action_run SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                (utcnow_iso(),),
            )

    def get_job(self, job_id):
        return self.db.conn.execute("SELECT * FROM action_run WHERE id = ?", (job_id,)).fetchone()

    def any_running(self, action_id):
        row = self.db.conn.execute(
            "SELECT 1 FROM action_run WHERE action_id = ? AND status = 'running' LIMIT 1", (action_id,)
        ).fetchone()
        return row is not None
