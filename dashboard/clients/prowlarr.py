"""
Prowlarr client -- just the one action this dashboard needs (force an indexer resync),
lifting scripts/provision.py's exact command-poll loop (POST /api/v1/command, then poll
GET /api/v1/command/{id} until completed/failed).
"""

import time

from .base import arr_api, SourceError


class ProwlarrClient:
    def __init__(self, base, key):
        self.base = base
        self.key = key

    def trigger_app_indexer_sync(self, deadline_seconds=30):
        try:
            cmd = arr_api(self.base, self.key, "POST", "/api/v1/command", json={"name": "ApplicationIndexerSync"})
            command_id = cmd["id"]
            deadline = time.monotonic() + deadline_seconds
            while time.monotonic() < deadline:
                status = arr_api(self.base, self.key, "GET", f"/api/v1/command/{command_id}")
                if status["status"] in ("completed", "failed"):
                    return status["status"] == "completed", status
                time.sleep(1)
            return False, {"status": "timed_out"}
        except Exception as e:
            raise SourceError("prowlarr", e) from e
