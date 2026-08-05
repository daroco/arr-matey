"""
The join engine: Seerr request -> arr item -> grabs -> torrent -> staging -> library.
See the approved plan's "Correlation: the join chain" section for the full reasoning;
key points reconfirmed live during Phase 0:

  - media.externalServiceId is the real Sonarr series id / Radarr movie id -- NOT
    tmdbId. Confirmed by cross-checking a live request (tmdbId 235) against its
    externalServiceId (177) and GET /api/v3/movie/177 (tmdbId 235, same movie,
    different id space entirely).
  - downloadId (the grab's torrent infohash) is the join key into torrents_by_hash,
    and is case-inconsistent across sources (uppercase in history, lowercase from the
    clients) -- always .lower() both sides.
  - One grab can cover many episodes (a season pack) -- confirmed live: a single
    downloadId had three downloadFolderImported events, one per episode. Attempts
    live at the Trace level for exactly this reason, not nested under one episode.

Two entry points, deliberately different cost profiles:
  - build_index(): cheap, uses only the already-fetched Snapshot, no new API calls.
    Powers the request list page, which renders every request at once.
  - build_trace_detail(): one specific request, makes the live history/series or
    history/movie call (bypassing the global 250-record history horizon for just this
    item) plus episode expansion for TV. Only called when a user opens one request.
"""

import logging

from . import state as state_mod
from .clients import local_fs
from .clients.arr import ArrClient
from .models import Attempt, Diagnosis, Severity, Stage, StageState, Target, Trace

log = logging.getLogger("dashboard.correlate")

# Best-effort labels for Seerr's status integers, inferred from observed live data
# during Phase 0 (a fully-available movie showed request.status==media.status==5;
# two in-progress TV shows showed request.status==2, media.status==3) cross-referenced
# against Overseerr/Jellyseerr's well-known enum values. Not verified against Seerr's
# own source -- if a label ever looks wrong for a real request, check
# seerr-team/seerr's MediaRequestStatus/MediaStatus enums directly rather than
# trusting this table blindly.
MEDIA_STATUS_LABELS = {1: "unknown", 2: "pending", 3: "processing", 4: "partially available", 5: "available"}
# Natural-language text for what actually renders in the UI. Kept separate from the
# raw labels above (which stay stable machine-readable keys used for the status
# filter's <option value=...> matching) so wording can be improved freely without
# touching filter logic. Deliberately does NOT claim download activity for
# "processing" -- that's Seerr's own status the moment a request is approved and
# handed to Sonarr/Radarr, before any actual search/grab has necessarily happened;
# claiming more than that is exactly what produced the confusing "processing" (list)
# vs. "never grabbed" (detail page, computed from real grab evidence) contradiction
# seen live on a real request (Blaze and the Monster Machines) -- both were true, in
# different domains, worded in a way that looked like a bug.
MEDIA_STATUS_DISPLAY = {
    1: "Unknown", 2: "Awaiting approval", 3: "Sent to Sonarr/Radarr",
    4: "Partially available", 5: "Available",
}
# Only 1-3 are confidently the classic Overseerr PENDING_APPROVAL/APPROVED/DECLINED
# triad. Live data showed a fully-available movie with request.status == media.status
# == 5, which those three values can't explain -- Seerr's request.status may have
# grown to share MediaStatus's 1-5 domain since the Overseerr/Jellyseerr merger.
# Falling back to the media labels for 4/5 is an inference from that one data point,
# not a confirmed mapping -- reconfirm against seerr-team/seerr's actual enum if a
# label here ever looks wrong.
REQUEST_STATUS_LABELS = {1: "pending approval", 2: "approved", 3: "declined", 4: "partially available", 5: "available"}


def _arr_item(req, snap):
    """Returns (kind, arr_id, arr_obj) or (kind, None, None) if the join can't be
    made -- e.g. Seerr's request predates the item existing in Sonarr/Radarr, or a
    source was unreachable this poll. match_confidence stays 'exact' here always;
    v1 doesn't implement the tvdbId/tmdbId/title fallback the plan flags as a nice-to-have
    for when externalServiceId is missing -- that's a known, documented simplification,
    not an oversight."""
    media = req.get("media", {})
    kind = "movie" if req.get("type") == "movie" else "tv"
    ext_id = media.get("externalServiceId")
    if kind == "movie":
        return kind, ext_id, snap.radarr_movie_by_id.get(ext_id)
    return kind, ext_id, snap.sonarr_series_by_id.get(ext_id)


def build_index(snap):
    """One lightweight row per Seerr request -- title, status, and a rollup derived
    purely from Seerr's own status fields (no per-episode expansion, no extra API
    calls). This is what the request list page renders."""
    rows = []
    for req in snap.seerr_requests:
        media = req.get("media", {})
        kind, arr_id, arr_obj = _arr_item(req, snap)
        rows.append({
            "id": req["id"],
            "media_type": kind,
            "title": (arr_obj or {}).get("title") or media.get("externalServiceSlug") or f"#{media.get('tmdbId')}",
            "requested_by": (req.get("requestedBy") or {}).get("displayName")
                or (req.get("requestedBy") or {}).get("jellyfinUsername") or "?",
            "requested_at": req.get("createdAt"),
            "request_status_raw": req.get("status"),
            "request_status_label": REQUEST_STATUS_LABELS.get(req.get("status"), str(req.get("status"))),
            "media_status_raw": media.get("status"),
            "media_status_label": MEDIA_STATUS_LABELS.get(media.get("status"), str(media.get("status"))),
            "media_status_display": MEDIA_STATUS_DISPLAY.get(media.get("status"), str(media.get("status"))),
            "arr_matched": arr_obj is not None,
            "tmdb_id": media.get("tmdbId"),
            "stalled": None,   # filled in only by the (expensive) stalled-only filter path
        })
    return rows


def filter_and_sort_index(rows, q=None, media_type=None, status=None, sort=None, stalled_ids=None):
    """The cheap filters/sort -- everything here operates on build_index()'s
    already-computed rows, no new API calls. `stalled_ids`, if given (a set of
    request ids), is the one exception: it comes from the expensive concurrent
    diagnosis sweep in main.py's /partials/requests route, computed only when the
    "stalled only" filter is actually requested, not on every page load."""
    out = rows
    if q:
        q_lower = q.lower()
        out = [r for r in out if q_lower in r["title"].lower()]
    if media_type and media_type != "all":
        out = [r for r in out if r["media_type"] == media_type]
    if status and status != "all":
        out = [r for r in out if (r["media_status_label"] if r["arr_matched"] else "unmatched") == status]
    if stalled_ids is not None:
        out = [r for r in out if r["id"] in stalled_ids]
        for r in out:
            r["stalled"] = True

    sort = sort or "newest"
    if sort == "newest":
        out = sorted(out, key=lambda r: r["requested_at"] or "", reverse=True)
    elif sort == "oldest":
        out = sorted(out, key=lambda r: r["requested_at"] or "")
    elif sort == "title":
        out = sorted(out, key=lambda r: r["title"].lower())
    elif sort == "status":
        out = sorted(out, key=lambda r: r["media_status_label"])
    return out


def build_service_health(snap, db):
    """At-a-glance per-service summary for the health section -- entirely from
    already-fetched Snapshot data (Tier D) plus source_health, no new API calls of
    its own. Each entry: name, reachable, stats (a small ordered list of label/value
    pairs meaningful for that specific service, not a generic blob)."""
    health_rows = {r["source"]: r for r in state_mod.all_source_health(db)}

    def reachable(source):
        row = health_rows.get(source)
        return row is None or row["consecutive_failures"] == 0

    services = []

    series = list(snap.sonarr_series_by_id.values())
    missing_series = sum(
        1 for s in series
        for se in s.get("seasons", [])
        if se["monitored"] and se.get("statistics", {}).get("episodeFileCount", 0) < se.get("statistics", {}).get("episodeCount", 0)
    )
    services.append({
        "name": "Sonarr", "reachable": reachable("sonarr"),
        "stats": [
            ("series", len(series)),
            ("seasons missing episodes", missing_series),
            ("in queue", len(snap.sonarr_queue)),
            ("indexers", len(snap.sonarr_indexers)),
        ],
    })

    movies = list(snap.radarr_movie_by_id.values())
    missing_movies = sum(1 for m in movies if m.get("monitored") and not m.get("hasFile"))
    services.append({
        "name": "Radarr", "reachable": reachable("radarr"),
        "stats": [
            ("movies", len(movies)),
            ("missing", missing_movies),
            ("in queue", len(snap.radarr_queue)),
            ("indexers", len(snap.radarr_indexers)),
        ],
    })

    all_indexers = snap.sonarr_indexers + snap.radarr_indexers
    disabled = sum(1 for i in all_indexers if not i.get("enable", True))
    services.append({
        "name": "Prowlarr", "reachable": reachable("prowlarr"),
        "stats": [("indexers wired to Sonarr/Radarr", len(all_indexers)), ("disabled", disabled)],
    })

    total_requests = len(snap.seerr_requests)
    pending = sum(1 for r in snap.seerr_requests if r.get("status") == 1)
    services.append({
        "name": "Seerr", "reachable": reachable("seerr"),
        "stats": [("requests (last 500)", total_requests), ("pending approval", pending)],
    })

    for s in services:
        s["category"] = "service"

    torrent_clients = []
    qbt_torrents = [t for t in snap.torrents_by_hash.values() if t.client == "qbittorrent"]
    qbt_active = sum(1 for t in qbt_torrents if t.state_normalized == "downloading")
    qbt_errors = sum(1 for t in qbt_torrents if t.state_normalized == "error")
    torrent_clients.append({
        "name": "qBittorrent", "reachable": reachable("qbittorrent"), "category": "torrent_client",
        "stats": [("torrents", len(qbt_torrents)), ("downloading", qbt_active), ("errored", qbt_errors)],
    })

    tr_torrents = [t for t in snap.torrents_by_hash.values() if t.client == "transmission"]
    if tr_torrents or "transmission" in health_rows:
        tr_active = sum(1 for t in tr_torrents if t.state_normalized == "downloading")
        torrent_clients.append({
            "name": "Transmission", "reachable": reachable("transmission"), "category": "torrent_client",
            "stats": [("torrents", len(tr_torrents)), ("downloading", tr_active)],
        })

    return services + torrent_clients


def _group_history_by_download_id(history_records):
    groups = {}
    for rec in history_records:
        did = rec.get("downloadId")
        if not did:
            continue
        did = did.lower()
        groups.setdefault(did, []).append(rec)
    return groups


def _stage_for_attempt(attempt, torrent, cfg, is_seedbox):
    """Builds the ordered StageState list for one Attempt. Mode-aware: seedbox mode
    gets COMPLETE_REMOTE + SYNCED_LOCAL as two stages; local mode collapses them into
    one DOWNLOADED stage, since there's no remote/local distinction there."""
    stages = []

    stages.append(StageState(Stage.GRABBED, "done" if attempt.grabbed_at else "unknown",
                              entered_at=attempt.grabbed_at,
                              evidence=[{"label": "grabbed event", "detail": attempt.indexer or ""}]))

    if torrent is None:
        # Torrent no longer visible on any known client -- either it was already
        # cleaned up (scripts/seedbox-cleanup.py deletes on the seedbox once
        # confirmed-imported, so this is the *expected* long-term state for anything
        # older than the cleanup interval) or genuinely missing. attempt.stages
        # already has its IMPORTED entry populated at this point (see
        # build_attempts_for_history, which computes IMPORTED before calling this),
        # so use it rather than showing a misleading "unknown" for something history
        # already proves succeeded.
        was_imported = any(s.stage == Stage.IMPORTED and s.status == "done" for s in attempt.stages)
        inferred = "done" if was_imported else "unknown"
        evidence = [{"label": "no matching torrent on any known client", "detail":
                     "inferred done from import history" if was_imported else attempt.download_id}]
        stages.append(StageState(Stage.DOWNLOADING, inferred, evidence=evidence))
        if is_seedbox:
            stages.append(StageState(Stage.COMPLETE_REMOTE, inferred))
            stages.append(StageState(Stage.SYNCED_LOCAL, inferred))
        else:
            stages.append(StageState(Stage.DOWNLOADED, inferred))
    else:
        downloading = torrent.state_normalized == "downloading"
        complete = torrent.state_normalized in ("seeding", "complete_paused")
        stages.append(StageState(
            Stage.DOWNLOADING,
            "active" if downloading else ("done" if complete else "unknown"),
            evidence=[{"label": f"client state: {torrent.state_raw}", "detail": f"{torrent.progress:.0%} complete, {torrent.seeders} seeders"}],
        ))

        if is_seedbox:
            stages.append(StageState(Stage.COMPLETE_REMOTE, "done" if complete else ("active" if downloading else "unknown")))
            staging_root = cfg.staging_root_qbt if torrent.client == "qbittorrent" else cfg.staging_root_transmission
            synced = complete and local_fs.is_synced_locally(torrent.name, staging_root)
            stages.append(StageState(
                Stage.SYNCED_LOCAL,
                "done" if synced else ("blocked" if complete else "unknown"),
                evidence=[{"label": "local staging check", "detail": str(staging_root / torrent.name)}],
            ))
        else:
            stages.append(StageState(Stage.DOWNLOADED, "done" if complete else ("active" if downloading else "unknown")))

    return stages


def build_attempts_for_history(history_records, snap, cfg, is_seedbox):
    """Groups an item's history into one Attempt per downloadId, attaches the live
    torrent view if still present on a client, and computes each attempt's stage
    track. Shared by the Sonarr and Radarr paths in build_trace_detail()."""
    grouped = _group_history_by_download_id(history_records)
    attempts = {}
    for download_id, recs in grouped.items():
        recs_sorted = sorted(recs, key=lambda r: r["date"])
        grabbed = next((r for r in recs_sorted if r["eventType"] == "grabbed"), None)
        imported_events = [r for r in recs_sorted if r["eventType"] == "downloadFolderImported"]
        failed_events = [r for r in recs_sorted if r["eventType"] in ("downloadFailed", "importFailed")]

        name = (grabbed or recs_sorted[0]).get("sourceTitle", download_id)
        attempt = Attempt(
            download_id=download_id,
            torrent_name=name,
            client=(grabbed or {}).get("data", {}).get("downloadClientName") if grabbed else None,
            indexer=(grabbed or {}).get("data", {}).get("indexer") if grabbed else None,
            grabbed_at=grabbed["date"] if grabbed else None,
        )
        attempt.covers = [r.get("episodeId") for r in imported_events if r.get("episodeId")]
        attempt.torrent = snap.torrents_by_hash.get(download_id)

        if imported_events:
            attempt.stages.append(StageState(Stage.IMPORTED, "done", entered_at=imported_events[-1]["date"],
                                               evidence=[{"label": "downloadFolderImported", "detail": e["data"].get("importedPath", "")} for e in imported_events]))
        elif failed_events:
            last_fail = failed_events[-1]
            attempt.stages.append(StageState(Stage.IMPORTED, "blocked", entered_at=last_fail["date"],
                                               evidence=[{"label": last_fail["eventType"], "detail": last_fail.get("data", {}).get("message", "")}]))
        else:
            attempt.stages.append(StageState(Stage.IMPORTED, "unknown"))

        attempt.stages = _stage_for_attempt(attempt, attempt.torrent, cfg, is_seedbox) + attempt.stages
        attempts[download_id] = attempt
    return attempts


def matched_request_ids(snap):
    """Every Seerr request that resolved to a real Sonarr/Radarr item -- the
    candidate set for the expensive "stalled only" filter in main.py, which
    shouldn't waste a deep trace on requests that never even matched."""
    return [r["id"] for r in snap.seerr_requests if _arr_item(r, snap)[2] is not None]


def build_trace_detail(request_id, snap, cfg):
    """The expensive, on-demand, single-request deep trace. Returns None if the
    request isn't in the current snapshot (e.g. deleted since the last poll)."""
    req = next((r for r in snap.seerr_requests if r["id"] == request_id), None)
    if req is None:
        return None

    media = req.get("media", {})
    kind, arr_id, arr_obj = _arr_item(req, snap)
    trace = Trace(
        seerr_request_id=req["id"], media_type=kind,
        title=(arr_obj or {}).get("title") or media.get("externalServiceSlug", "?"),
        requested_by=(req.get("requestedBy") or {}).get("displayName") or "?",
        requested_at=req.get("createdAt"),
        request_status_raw=req.get("status"), media_status_raw=media.get("status"),
        match_confidence="exact" if arr_obj else "unmatched",
    )
    if arr_obj is None:
        return trace  # nothing further to trace -- render as "never reached Sonarr/Radarr"

    is_seedbox = cfg.download_mode != "local"

    if kind == "movie":
        radarr = ArrClient("radarr", cfg.radarr_base, cfg.radarr_key)
        history = radarr.history_for_movie(arr_id)
        trace.attempts = build_attempts_for_history(history, snap, cfg, is_seedbox)
        target = Target(kind="movie", arr_id=arr_id, title=arr_obj.get("title", "?"),
                         has_file=arr_obj.get("hasFile", False))
        target.attempt_ids = list(trace.attempts.keys())
        trace.targets = [target]
    else:
        sonarr = ArrClient("sonarr", cfg.sonarr_base, cfg.sonarr_key)
        history = sonarr.history_for_series(arr_id)
        trace.attempts = build_attempts_for_history(history, snap, cfg, is_seedbox)

        requested_seasons = {s["seasonNumber"] for s in req.get("seasons", [])}
        for season_number in sorted(requested_seasons):
            for ep in sonarr.episodes_for_series(arr_id, season_number=season_number):
                target = Target(
                    kind="episode", arr_id=ep["id"], title=ep.get("title", "?"),
                    season_number=ep["seasonNumber"], episode_number=ep["episodeNumber"],
                    has_file=ep.get("hasFile", False),
                )
                target.attempt_ids = [
                    did for did, att in trace.attempts.items() if ep["id"] in att.covers
                ]
                trace.targets.append(target)

        if not trace.targets:
            # Confirmed live against a real request (MythBusters original run):
            # Sonarr's own season numbers for a show can be non-ordinal (year-based --
            # 2003, 2004, ... rather than 1, 2, 3), while Seerr's requested seasons
            # come from its own metadata source (TMDB) and stay ordinal. The series
            # WAS matched correctly (arr_obj is not None) -- this isn't an unmatched
            # request, it's a season-numbering mismatch between the two metadata
            # sources for this specific show, and it's worth saying so explicitly
            # rather than rendering a silent, confusing "0 episodes" page.
            actual_seasons = sorted({s["seasonNumber"] for s in arr_obj.get("seasons", [])})
            trace.diagnoses.append(Diagnosis(
                rule_id="season_number_mismatch", severity=Severity.WARNING,
                headline="Requested seasons don't exist in Sonarr for this series",
                detail=f"Seerr requested season(s) {sorted(requested_seasons)}, but Sonarr has "
                       f"season(s) {actual_seasons} for \"{arr_obj.get('title')}\" -- likely a "
                       f"numbering mismatch between Seerr's metadata source and Sonarr's (e.g. "
                       f"year-based vs. ordinal seasons), not a real \"never requested\" gap.",
                suggested_actions=[("arr_fix_season_mismatch", {"series_id": arr_id})],
            ))

    total = len(trace.targets)
    available = sum(1 for t in trace.targets if t.has_file)
    if total == 0:
        trace.rollup_status = "no episodes resolved"
    elif available == total:
        trace.rollup_status = "in_library"
    elif available > 0:
        trace.rollup_status = f"{available}/{total} available"
    elif trace.attempts:
        trace.rollup_status = "in progress"
    else:
        trace.rollup_status = "never grabbed"

    return trace
