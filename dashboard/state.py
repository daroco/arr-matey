"""
Persisted first-seen/last-seen/state-transition bookkeeping. This is the entire
mechanism behind "stalled for 3d 4h" -- no *arr or torrent-client API reports "how long
has this been true," so it has to be observed: state_since/progress_since only move
forward when the value actually changes between polls, not on every poll.

download_state is also the durable fix for scripts/seedbox-cleanup.py's own 250-record
history-page horizon (see that script's fetch_imported_hashes()) -- every poll merges
page 1 of history into this table, so anything ever seen by the dashboard stays
traceable long after it scrolls off the API's single page. A first-run backfill
(walk_backfill()) softens the gap for anything that finished before the dashboard's
first run.
"""

from .models import utcnow_iso


def upsert_download(db, *, download_id, arr, torrent_name=None, client=None, category=None,
                     indexer=None, series_id=None, movie_id=None, torrent_state=None,
                     progress=None, size_left=None, seeders=None, grabbed_at=None,
                     imported_at=None, synced_local_at=None, failed_at=None):
    now = utcnow_iso()
    row = db.conn.execute(
        "SELECT last_state, state_since, progress, progress_since FROM download_state WHERE download_id = ?",
        (download_id,),
    ).fetchone()

    if row is None:
        state_since = now if torrent_state else None
        progress_since = now if progress is not None else None
        with db.conn:
            db.conn.execute(
                """INSERT INTO download_state
                   (download_id, arr, torrent_name, client, category, indexer, series_id,
                    movie_id, first_seen_at, last_seen_at, last_state, state_since,
                    progress, size_left, seeders, progress_since, grabbed_at, imported_at,
                    synced_local_at, failed_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (download_id, arr, torrent_name, client, category, indexer, series_id, movie_id,
                 now, now, torrent_state, state_since, progress, size_left, seeders,
                 progress_since, grabbed_at, imported_at, synced_local_at, failed_at),
            )
        return

    prev_state, prev_state_since, prev_progress, prev_progress_since = row
    state_since = prev_state_since if torrent_state == prev_state else now
    # Only advance progress_since when progress genuinely moved -- comparing floats
    # directly is fine here since both sides come from the same API's own reported
    # value each poll, not independently computed.
    progress_since = prev_progress_since if progress == prev_progress else now

    with db.conn:
        db.conn.execute(
            """UPDATE download_state SET
                 torrent_name = COALESCE(?, torrent_name),
                 client = COALESCE(?, client),
                 category = COALESCE(?, category),
                 indexer = COALESCE(?, indexer),
                 series_id = COALESCE(?, series_id),
                 movie_id = COALESCE(?, movie_id),
                 last_seen_at = ?,
                 last_state = ?,
                 state_since = ?,
                 progress = ?,
                 size_left = ?,
                 seeders = ?,
                 progress_since = ?,
                 grabbed_at = COALESCE(grabbed_at, ?),
                 imported_at = COALESCE(?, imported_at),
                 synced_local_at = COALESCE(?, synced_local_at),
                 failed_at = COALESCE(?, failed_at),
                 removed_at = NULL
               WHERE download_id = ?""",
            (torrent_name, client, category, indexer, series_id, movie_id, now, torrent_state,
             state_since, progress, size_left, seeders, progress_since, grabbed_at, imported_at,
             synced_local_at, failed_at, download_id),
        )


def mark_removed(db, download_id):
    with db.conn:
        db.conn.execute(
            "UPDATE download_state SET removed_at = ? WHERE download_id = ? AND removed_at IS NULL",
            (utcnow_iso(), download_id),
        )


def get_download(db, download_id):
    return db.conn.execute(
        "SELECT * FROM download_state WHERE download_id = ?", (download_id,)
    ).fetchone()


def upsert_diagnosis(db, scope_type, scope_key, rule_id, severity, detail_json):
    """Insert-or-touch an open diagnosis row. first_seen_at is preserved across
    repeated firings (that's the "since" clock); last_seen_at always advances."""
    now = utcnow_iso()
    existing = db.conn.execute(
        "SELECT id FROM diagnosis WHERE scope_type=? AND scope_key=? AND rule_id=? AND cleared_at IS NULL",
        (scope_type, scope_key, rule_id),
    ).fetchone()
    with db.conn:
        if existing:
            db.conn.execute(
                "UPDATE diagnosis SET last_seen_at=?, severity=?, detail_json=? WHERE id=?",
                (now, severity, detail_json, existing[0]),
            )
            first_seen_at = db.conn.execute(
                "SELECT first_seen_at FROM diagnosis WHERE id=?", (existing[0],)
            ).fetchone()[0]
            return existing[0], first_seen_at, False   # is_new=False -- already known, don't re-notify
        cur = db.conn.execute(
            """INSERT INTO diagnosis (scope_type, scope_key, rule_id, severity, first_seen_at,
                                       last_seen_at, detail_json)
               VALUES (?,?,?,?,?,?,?)""",
            (scope_type, scope_key, rule_id, severity, now, now, detail_json),
        )
        return cur.lastrowid, now, True   # is_new=True -- either genuinely new, or re-firing after
                                           # a previous clear_diagnosis() -- both are worth a fresh notification


def clear_diagnosis(db, scope_type, scope_key, rule_id):
    with db.conn:
        db.conn.execute(
            "UPDATE diagnosis SET cleared_at = ? WHERE scope_type=? AND scope_key=? AND rule_id=? AND cleared_at IS NULL",
            (utcnow_iso(), scope_type, scope_key, rule_id),
        )


def open_diagnoses_for(db, scope_type, scope_key):
    return db.conn.execute(
        "SELECT * FROM diagnosis WHERE scope_type=? AND scope_key=? AND cleared_at IS NULL",
        (scope_type, scope_key),
    ).fetchall()


def insert_notification(db, *, title, request_id, severity, headline, message):
    """One row per batched push actually sent (see sweep.py's _flush_pending) --
    this is the in-app mirror of exactly what went out over ntfy, so the two never
    show different histories of the same event."""
    with db.conn:
        db.conn.execute(
            """INSERT INTO notification (created_at, title, request_id, severity, headline, message)
               VALUES (?,?,?,?,?,?)""",
            (utcnow_iso(), title, request_id, severity, headline, message),
        )


def list_notifications(db, limit=200):
    return db.conn.execute(
        "SELECT * FROM notification ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()


def count_unread_notifications(db):
    return db.conn.execute(
        "SELECT COUNT(*) FROM notification WHERE read_at IS NULL"
    ).fetchone()[0]


def mark_all_notifications_read(db):
    with db.conn:
        db.conn.execute(
            "UPDATE notification SET read_at = ? WHERE read_at IS NULL", (utcnow_iso(),)
        )


def record_source_health(db, source, ok, error=None, next_attempt_at=None):
    now = utcnow_iso()
    with db.conn:
        if ok:
            db.conn.execute(
                """INSERT INTO source_health (source, last_ok_at, consecutive_failures, next_attempt_at)
                   VALUES (?, ?, 0, NULL)
                   ON CONFLICT(source) DO UPDATE SET last_ok_at=excluded.last_ok_at,
                       consecutive_failures=0, next_attempt_at=NULL""",
                (source, now),
            )
        else:
            db.conn.execute(
                """INSERT INTO source_health (source, last_error_at, last_error, consecutive_failures, next_attempt_at)
                   VALUES (?, ?, ?, 1, ?)
                   ON CONFLICT(source) DO UPDATE SET last_error_at=excluded.last_error_at,
                       last_error=excluded.last_error,
                       consecutive_failures=consecutive_failures + 1,
                       next_attempt_at=excluded.next_attempt_at""",
                (source, now, str(error)[:500], next_attempt_at),
            )


def all_source_health(db):
    return db.conn.execute("SELECT * FROM source_health").fetchall()


def sweep_snapshot(db, snap):
    """Runs every poll cycle (see poller.py) -- merges every currently-visible torrent
    into download_state so state_since/first_seen_at keep advancing for EVERY
    download, not just the ones a user happens to be looking at right now. This is
    what makes "stalled for 3d 4h" possible: rules.py reads state_since from here,
    it never recomputes it from a point-in-time API field (none of them have one)."""
    seen_hashes = set()
    for h, torrent in snap.torrents_by_hash.items():
        seen_hashes.add(h)
        upsert_download(
            db, download_id=h, arr="", torrent_name=torrent.name, client=torrent.client,
            category=torrent.category, torrent_state=torrent.state_normalized,
            progress=torrent.progress, size_left=torrent.size_left, seeders=torrent.seeders,
        )
    # Anything previously tracked but no longer on any client this poll -- mark
    # removed_at (not deleted outright; retained until db.prune()'s retention window
    # so a just-cleaned-up download is still explainable for a while, not just gone).
    tracked = db.conn.execute(
        "SELECT download_id FROM download_state WHERE removed_at IS NULL"
    ).fetchall()
    for row in tracked:
        if row["download_id"] not in seen_hashes:
            mark_removed(db, row["download_id"])
