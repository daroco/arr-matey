"""
SQLite persistence for the trace dashboard. Only what genuinely can't be recomputed
live from an API call gets stored here -- see the "Data model" section of the approved
plan (C:\\Users\\drcor\\.claude\\plans\\refactored-watching-pixel.md) for the reasoning
behind each table. Traces/stages/diagnoses themselves are computed fresh on every page
render from the in-memory Snapshot; caching those would only add staleness bugs.

Single uvicorn worker (see run.py) keeps this a single-writer situation -- WAL mode is
for read/write concurrency between the poller task and request handlers within that one
process, not for multiple processes.
"""

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_ok_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT
);

CREATE TABLE IF NOT EXISTS download_state (
    download_id TEXT PRIMARY KEY,
    arr TEXT NOT NULL,
    torrent_name TEXT,
    client TEXT,
    category TEXT,
    indexer TEXT,
    series_id INTEGER,
    movie_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_state TEXT,
    state_since TEXT,
    progress REAL,
    size_left INTEGER,
    seeders INTEGER,
    progress_since TEXT,
    grabbed_at TEXT,
    imported_at TEXT,
    synced_local_at TEXT,
    failed_at TEXT,
    removed_at TEXT
);

CREATE TABLE IF NOT EXISTS request_state (
    seerr_request_id INTEGER PRIMARY KEY,
    media_type TEXT,
    tmdb_id INTEGER,
    tvdb_id INTEGER,
    title TEXT,
    requested_by TEXT,
    arr_item_id INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status_signature TEXT,
    status_since TEXT
);

CREATE TABLE IF NOT EXISTS diagnosis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_type TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    cleared_at TEXT,
    detail_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS diagnosis_open_unique
    ON diagnosis(scope_type, scope_key, rule_id)
    WHERE cleared_at IS NULL;

CREATE TABLE IF NOT EXISTS action_run (
    id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL,
    target_json TEXT,
    params_json TEXT,
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    preview_json TEXT,
    result_text TEXT,
    error_text TEXT,
    exit_code INTEGER,
    log_tail TEXT
);

CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    title TEXT NOT NULL,
    request_id INTEGER,
    severity TEXT NOT NULL,
    headline TEXT NOT NULL,
    message TEXT NOT NULL,
    read_at TEXT
);

CREATE TABLE IF NOT EXISTS session (
    token TEXT PRIMARY KEY,
    jellyfin_user_id TEXT NOT NULL,
    username TEXT NOT NULL,
    is_admin INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path):
        self.path = path
        self._local = threading.local()
        # Force schema creation now (via the same lazy path every other connection
        # uses) so a caller can rely on it existing immediately after construction --
        # this also happens to be what makes ":memory:" usable for tests: sqlite
        # gives every new connection to ":memory:" its own independent empty database,
        # so schema creation has to happen on every connection, not just once up
        # front against a throwaway one (which for a real file path would persist
        # fine, but silently wouldn't for ":memory:").
        _ = self.conn

    def _connect(self):
        conn = sqlite3.connect(self.path, check_same_thread=False, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL" if self.path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        return conn

    @property
    def conn(self):
        # One connection per thread (FastAPI/uvicorn with workers=1 still uses a
        # thread pool for sync code via asyncio.to_thread) -- sqlite3 connections
        # aren't safe to share across threads even with check_same_thread=False for
        # concurrent use, only for handoff.
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return self._local.conn

    def prune(self, retention_days):
        """Delete finished/cleared rows older than retention_days. Called once per
        poll cycle from poller.py, not on every request."""
        cutoff_sql = f"datetime('now', '-{int(retention_days)} days')"
        with self.conn:
            self.conn.execute(
                f"DELETE FROM download_state WHERE removed_at IS NOT NULL AND removed_at < {cutoff_sql}"
            )
            self.conn.execute(
                f"DELETE FROM action_run WHERE finished_at IS NOT NULL AND finished_at < {cutoff_sql}"
            )
            self.conn.execute(
                f"DELETE FROM diagnosis WHERE cleared_at IS NOT NULL AND cleared_at < {cutoff_sql}"
            )
            # Unread notifications are kept regardless of age -- pruning something
            # the user hasn't seen yet would be indistinguishable from it never
            # having fired at all.
            self.conn.execute(
                f"DELETE FROM notification WHERE read_at IS NOT NULL AND read_at < {cutoff_sql}"
            )
            self.conn.execute(f"DELETE FROM session WHERE expires_at < datetime('now')")
