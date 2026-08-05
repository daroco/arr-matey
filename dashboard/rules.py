"""
The diagnostic rule engine. Each rule is a small function over an already-built Trace
(no extra API calls -- correlate.py already did the fetching), so evaluation is fast
and deterministic. See the approved plan's "Diagnostic rules" table for the full
rationale behind each one; the comments below focus on what changed after Phase 0's
live verification.

R_healthy_awaiting_cleanup (the suppressor) MUST run before R_thin_swarm/
R_awaiting_rclone and short-circuit them if it fires -- confirmed against live data
during Phase 0 that qBittorrent's "done" state has three flavors (uploading,
stalledUP, pausedUP) and only pausedUP is the "stopped, awaiting cleanup" state;
uploading/stalledUP are just ordinary ongoing seeding and must never be flagged.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from .models import Diagnosis, Severity

log = logging.getLogger("dashboard.rules")


def _age_minutes(iso_ts):
    if not iso_ts:
        return None
    ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - ts).total_seconds() / 60


def _download_state_row(db, download_id):
    return db.conn.execute(
        "SELECT * FROM download_state WHERE download_id = ?", (download_id,)
    ).fetchone()


def _queue_entry_for(download_id, sonarr_queue, radarr_queue):
    for rec in sonarr_queue:
        if (rec.get("downloadId") or "").lower() == download_id:
            return "sonarr", rec
    for rec in radarr_queue:
        if (rec.get("downloadId") or "").lower() == download_id:
            return "radarr", rec
    return None, None


# Confirmed live against a real stuck movie (The Departed): Radarr/Sonarr refuse to
# auto-import when the release was only matched to the movie/episode via grab
# history (by internal ID) rather than by parsing the filename itself -- a safety
# check against silently importing the wrong file, not a real problem with the file.
# The fix is Manual Import, either by hand in the arr's UI or (if unambiguous) via
# GET /api/v3/manualimport + POST /api/v3/command ManualImport, which is exactly
# what dashboard/actions/arr_actions.py's execute_manual_import automates.
MANUAL_IMPORT_PHRASE = "manual import required"


def r_healthy_awaiting_cleanup(attempt, db):
    """The suppressor. Fires (as an OK-severity diagnosis, not a problem) when the
    torrent is paused/stopped at its seed target AND already confirmed imported --
    exactly the state scripts/seedbox-cleanup.py is designed to pick up on its next
    scheduled pass, never a stall. Returns the Diagnosis if it fires, else None."""
    t = attempt.torrent
    if t is None or t.state_normalized != "complete_paused":
        return None
    was_imported = any(s.stage.value == "imported" and s.status == "done" for s in attempt.stages)
    if not was_imported:
        return None
    row = _download_state_row(db, attempt.download_id)
    since = row["state_since"] if row else None
    return Diagnosis(
        rule_id="healthy_awaiting_cleanup", severity=Severity.OK,
        headline="Done, awaiting next cleanup pass",
        detail="Seeding stopped at its ratio/time target and already imported -- "
               "scripts/seedbox-cleanup.py will remove it from the seedbox once it next runs.",
        evidence=[{"label": "client state", "detail": t.state_raw}],
        suggested_actions=[("run_seedbox_cleanup", {})],
        since=since,
    )


def r_thin_swarm(attempt, db, cfg):
    t = attempt.torrent
    if t is None or t.state_normalized != "downloading" or t.seeders > 0:
        return None
    row = _download_state_row(db, attempt.download_id)
    since = row["state_since"] if row else None
    age = _age_minutes(since)
    if age is None or age < cfg.stall_minutes:
        return None
    severity = Severity.ERROR if age > 12 * 60 else Severity.WARNING
    return Diagnosis(
        rule_id="thin_swarm", severity=severity,
        headline="No seeders",
        detail=f"Downloading with 0 seeders for {int(age)} min. The swarm for this "
               f"specific release may just be thin -- check the client's tracker view, "
               f"or raise this indexer's Minimum Seeders to reject thin releases before they grab.",
        evidence=[{"label": "seeders", "detail": "0"}, {"label": "client state", "detail": t.state_raw}],
        suggested_actions=[("arr_bump_min_seeders", {"indexer_name": attempt.indexer})],
        since=since,
    )


def r_awaiting_rclone(attempt, db, cfg, staging_root, wrapper_log_summary):
    t = attempt.torrent
    if t is None or t.state_normalized not in ("seeding", "complete_paused"):
        return None
    was_imported = any(s.stage.value == "imported" and s.status == "done" for s in attempt.stages)
    if was_imported:
        return None
    synced = any(s.stage.value == "synced_local" and s.status == "done" for s in attempt.stages)
    if synced:
        return None
    row = _download_state_row(db, attempt.download_id)
    since = row["state_since"] if row else None

    # Escalate to error if a successful rclone run finished *after* this torrent
    # completed and the file is still missing -- that's a broken sync, not a slow one.
    severity = Severity.WARNING
    detail = "Finished on the seedbox but hasn't synced down to local staging yet."
    if wrapper_log_summary and since and "sync OK:" in wrapper_log_summary:
        detail += " A successful rclone run has completed since -- if this persists, the sync may be broken for this file specifically."
        severity = Severity.ERROR
    return Diagnosis(
        rule_id="awaiting_rclone", severity=severity,
        headline="Awaiting rclone sync",
        detail=detail,
        evidence=[{"label": "expected local path", "detail": str(staging_root / t.name)},
                   {"label": "last rclone run", "detail": wrapper_log_summary or "none seen"}],
        suggested_actions=[("run_rclone_sync", {})],
        since=since,
    )


def r_import_failed(attempt, snap):
    was_imported = False
    for s in attempt.stages:
        if s.stage.value == "imported":
            if s.status == "blocked":
                detail = "; ".join(e.get("detail", "") for e in s.evidence if e.get("detail")) or "no message recorded"
                return Diagnosis(
                    rule_id="import_failed", severity=Severity.ERROR,
                    headline="Import failed",
                    detail=detail,
                    evidence=s.evidence,
                    since=s.entered_at,
                )
            was_imported = s.status == "done"
    if was_imported:
        # Sonarr/Radarr's queue can keep a lingering entry (with an old
        # statusMessages warning) for a downloadId that already completed
        # successfully -- confirmed live: a fully-imported, in-library movie briefly
        # showed a stale "importBlocked" queue entry from earlier in its own history.
        # Only trust the queue's statusMessages when we don't already have proof of a
        # successful import.
        return None
    arr, queue_entry = _queue_entry_for(attempt.download_id, snap.sonarr_queue, snap.radarr_queue)
    if queue_entry and queue_entry.get("statusMessages"):
        all_texts = [m for sm in queue_entry["statusMessages"] for m in sm.get("messages", [])]
        titles = [m.get("title", "") for m in queue_entry["statusMessages"]]
        detail = "; ".join(titles)

        if any(MANUAL_IMPORT_PHRASE in t.lower() for t in all_texts):
            params = {"arr": arr, "download_id": attempt.download_id}
            if arr == "radarr":
                params["movie_id"] = queue_entry.get("movieId")
            else:
                params["series_id"] = queue_entry.get("seriesId")
            return Diagnosis(
                rule_id="manual_import_required", severity=Severity.WARNING,
                headline="Manual import required",
                detail=f"{detail} -- the file is fully downloaded, {arr.title()} just won't "
                       f"auto-confirm which {'movie' if arr == 'radarr' else 'episode(s)'} it "
                       f"belongs to. Safe to auto-import if the filename genuinely matches.",
                evidence=[{"label": "trackedDownloadState", "detail": queue_entry.get("trackedDownloadState", "")},
                           {"label": "file", "detail": queue_entry.get("title", "")}],
                suggested_actions=[("arr_manual_import", params)],
            )

        return Diagnosis(
            rule_id="import_blocked", severity=Severity.WARNING,
            headline="Import blocked",
            detail=detail,
            evidence=[{"label": "trackedDownloadState", "detail": queue_entry.get("trackedDownloadState", "")}],
        )
    return None


def r_seerr_stale_root_folder(snap):
    """Global, always-on -- runs independent of any single request so it warns
    before the next request fails, not after. Confirmed live during Phase 0: Seerr's
    settings/{service} activeDirectory can silently drift from arr's actual root
    folders if MEDIA_ROOT is ever moved (Seerr caches it, doesn't read it live)."""
    out = []
    pairs = [
        ("sonarr", snap.seerr_settings_sonarr, snap.sonarr_root_folders),
        ("radarr", snap.seerr_settings_radarr, snap.radarr_root_folders),
    ]
    for service, settings, root_folders in pairs:
        active = settings.get("activeDirectory")
        if not active or not root_folders:
            continue
        valid_paths = {rf["path"] for rf in root_folders}
        if active not in valid_paths:
            out.append(Diagnosis(
                rule_id=f"seerr_stale_root_folder_{service}", severity=Severity.ERROR,
                headline=f"Seerr's {service} root folder is stale",
                detail=f"Seerr has \"{active}\" cached, but {service} doesn't have that as a root "
                       f"folder anymore (has: {', '.join(sorted(valid_paths))}). Every new "
                       f"{service} request will fail until this is fixed.",
                evidence=[{"label": "Seerr's cached activeDirectory", "detail": active},
                           {"label": f"{service}'s actual root folders", "detail": ", ".join(sorted(valid_paths))}],
                suggested_actions=[("seerr_fix_root_folder", {"service": service})],
            ))
    return out


def r_source_unreachable(db, threshold=3):
    out = []
    rows = db.conn.execute(
        "SELECT * FROM source_health WHERE consecutive_failures >= ? AND source != '__poll__'",
        (threshold,),
    ).fetchall()
    for row in rows:
        out.append(Diagnosis(
            rule_id=f"source_unreachable_{row['source']}", severity=Severity.ERROR,
            headline=f"{row['source']} unreachable",
            detail=f"{row['consecutive_failures']} consecutive failed polls. Last error: {row['last_error']}. "
                   f"Traces touching this source may show stale or incomplete data.",
            since=row["last_error_at"],
        ))
    return out


def r_download_mode_mismatch(cfg):
    msg = cfg.download_mode_mismatch()
    if not msg:
        return None
    return Diagnosis(rule_id="download_mode_mismatch", severity=Severity.ERROR,
                      headline="DOWNLOAD_MODE / COMPOSE_PROFILES mismatch", detail=msg)


def evaluate_attempt(attempt, db, cfg, is_seedbox, wrapper_log_summary=None):
    """Runs the attempt-level rules in priority order, suppressor first. Returns as
    soon as the suppressor fires (a healthy state shouldn't also show a stall
    warning) -- otherwise collects every rule that fires, since e.g. thin-swarm and
    import-failed are not mutually exclusive in principle (though rare in practice)."""
    suppressor = r_healthy_awaiting_cleanup(attempt, db)
    if suppressor:
        return [suppressor]

    out = []
    d = r_thin_swarm(attempt, db, cfg)
    if d:
        out.append(d)
    if is_seedbox:
        staging_root = cfg.staging_root_qbt if (attempt.torrent and attempt.torrent.client == "qbittorrent") else cfg.staging_root_transmission
        d = r_awaiting_rclone(attempt, db, cfg, staging_root, wrapper_log_summary)
        if d:
            out.append(d)
    return out


def r_never_grabbed(trace):
    """Replaces what used to be a static, un-investigated template fallback ("Never
    grabbed / no grab has happened yet") with a real diagnosis carrying a genuine
    next step: a live interactive-search check (arr_actions.execute_why_not_grabbed)
    that answers the actual question -- confirmed live against a real case (a kids'
    movie special) where the answer was "zero releases found on any indexer at all,"
    not a quality-profile rejection. That distinction matters and wasn't previously
    knowable from the dashboard without leaving it."""
    if trace.attempts or trace.match_confidence == "unmatched" or not trace.targets:
        return None
    target = next((t for t in trace.targets if not t.has_file), trace.targets[0])
    params = {"arr": "radarr" if trace.media_type == "movie" else "sonarr"}
    if trace.media_type == "movie":
        params["movie_id"] = target.arr_id
    else:
        params["episode_id"] = target.arr_id
    return Diagnosis(
        rule_id="never_grabbed", severity=Severity.WARNING,
        headline="Never grabbed",
        detail="Matched in Sonarr/Radarr, but no grab has happened yet. Could be a thin/nonexistent "
               "release, or genuinely nothing has searched for it yet -- the button below runs a live "
               "search and shows exactly what it finds (or doesn't).",
        suggested_actions=[("arr_why_not_grabbed", params)],
    )


def evaluate_trace(trace, snap, db, cfg, is_seedbox, wrapper_log_summary=None):
    for attempt in trace.attempts.values():
        attempt.diagnoses = evaluate_attempt(attempt, db, cfg, is_seedbox, wrapper_log_summary)
        d = r_import_failed(attempt, snap)
        if d:
            attempt.diagnoses.append(d)
    d = r_never_grabbed(trace)
    if d:
        trace.diagnoses.append(d)
    return trace


def evaluate_global(snap, db, cfg):
    out = []
    out.extend(r_seerr_stale_root_folder(snap))
    out.extend(r_source_unreachable(db))
    d = r_download_mode_mismatch(cfg)
    if d:
        out.append(d)
    return out
