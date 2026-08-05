"""
Two Sonarr/Radarr-side fixes:

- Bump an indexer's Minimum Seeders. Reuses clients/base.py's get_field/set_field
  (copied verbatim from scripts/provision.py's dynamic-fields helpers). Indexer lookup
  is by exact name match against history's grabbed-event "indexer" field -- confirmed
  live during Phase 0 that these match exactly (e.g. both say "TorrentLeech (Prowlarr)"),
  so no fuzzy matching is needed; preview() blocks cleanly if a name isn't found rather
  than guessing.
- Fix a season-number mismatch (rules.py's season_number_mismatch diagnosis) by
  monitoring every season Sonarr itself has and triggering a series search, bypassing
  whatever season numbers Seerr requested.
"""

from ..clients.arr import ArrClient
from ..clients.base import arr_api, get_field, set_field
from ..models import ActionResult, PreviewResult

DEFAULT_BUMP_TO = 3


def _find_indexer(indexers, name):
    return next((i for i in indexers if i["name"] == name), None)


def preview_bump_min_seeders(cfg, snap, params):
    name = params.get("indexer_name")
    new_value = int(params.get("value", DEFAULT_BUMP_TO))
    if not name:
        return PreviewResult(summary="No indexer specified.", blocked_reason="missing indexer_name")

    indexer = _find_indexer(snap.sonarr_indexers, name) or _find_indexer(snap.radarr_indexers, name)
    if indexer is None:
        return PreviewResult(summary=f"No indexer named \"{name}\" found in Sonarr or Radarr.",
                              blocked_reason="indexer not found")
    current = get_field(indexer["fields"], "minimumSeeders", default=1)
    if current >= new_value:
        return PreviewResult(summary=f"\"{name}\" already requires {current} seeders (>= {new_value}). Nothing to do.",
                              blocked_reason="already at or above target")
    return PreviewResult(
        summary=f"Will raise \"{name}\"'s Minimum Seeders from {current} to {new_value}. "
                f"Note: a Prowlarr full indexer sync can push this back to Prowlarr's own value later.",
        before={"minimumSeeders": current}, after={"minimumSeeders": new_value},
    )


def preview_fix_season_mismatch(cfg, snap, params):
    """Companion to rules.py's season_number_mismatch diagnosis. Bypasses Seerr's
    (wrong, for this show) requested season numbers entirely and just monitors
    every season Sonarr itself actually has -- confirmed live against MythBusters'
    original run, where Sonarr groups seasons by year (2003-2018) while Seerr's
    TMDB-sourced request used ordinal numbers (1-16); neither app has a season
    remapping feature, so working from Sonarr's own season list is the only thing
    that actually works."""
    series_id = int(params["series_id"])
    series = snap.sonarr_series_by_id.get(series_id)
    if series is None:
        return PreviewResult(summary="Series not found in the current snapshot.", blocked_reason="not found")
    unmonitored = [s["seasonNumber"] for s in series.get("seasons", []) if not s["monitored"]]
    if not unmonitored:
        return PreviewResult(summary=f"Every season of \"{series['title']}\" is already monitored. "
                                      f"If it's still not grabbing, this isn't a season-mismatch issue.",
                              blocked_reason="nothing to monitor")
    return PreviewResult(
        summary=f"Will monitor all {len(series['seasons'])} of Sonarr's actual seasons for "
                f"\"{series['title']}\" (ignoring whatever season numbers Seerr requested) and "
                f"trigger a full series search. This can take several minutes for a long-running "
                f"show and may grab a lot at once.",
        before={"currently unmonitored": unmonitored},
        after={"monitored": [s["seasonNumber"] for s in series["seasons"]]},
    )


def execute_fix_season_mismatch(cfg, params):
    series_id = int(params["series_id"])
    series = arr_api(cfg.sonarr_base, cfg.sonarr_key, "GET", f"/api/v3/series/{series_id}")
    if series is None:
        return ActionResult(ok=False, message="Series not found.")
    for s in series["seasons"]:
        s["monitored"] = True
    series["monitored"] = True
    arr_api(cfg.sonarr_base, cfg.sonarr_key, "PUT", f"/api/v3/series/{series_id}", json=series)

    cmd = arr_api(cfg.sonarr_base, cfg.sonarr_key, "POST", "/api/v3/command",
                  json={"name": "SeriesSearch", "seriesId": series_id})
    # Deliberately not polling this to completion like Prowlarr's sync -- a full
    # series search across many seasons can run for several minutes (confirmed live:
    # still "started" after 2+ minutes for a 17-season show), and blocking the
    # request that long would make this feel broken. The dashboard's own poller
    # will pick up new grabs on its normal cadence regardless of how the search was
    # triggered -- no special wiring needed for the trace to update.
    return ActionResult(ok=True, message=f"Monitored all {len(series['seasons'])} seasons and "
                                          f"triggered a search (command #{cmd['id']}). Grabs will "
                                          f"appear here as Sonarr finds them -- this can take a while.")


def preview_manual_import(cfg, snap, params):
    """Companion to rules.py's manual_import_required diagnosis. Confirmed live
    against a real stuck movie (The Departed): GET /api/v3/manualimport returns the
    file Sonarr/Radarr would import along with its own confident best-guess match
    (movie/series+episodes, quality, languages) -- the fix is just confirming that
    guess back via POST /api/v3/command ManualImport. Preview refuses to proceed
    (blocked_reason) on anything that isn't a single, unambiguous, rejection-free
    candidate -- this is exactly the situation Sonarr/Radarr's own safety check
    exists for, so a second layer of caution here is deliberate, not excessive."""
    arr = params["arr"]
    download_id = params["download_id"]
    base, key = (cfg.radarr_base, cfg.radarr_key) if arr == "radarr" else (cfg.sonarr_base, cfg.sonarr_key)
    candidates = arr_api(base, key, "GET", "/api/v3/manualimport", params={"downloadId": download_id}) or []

    if not candidates:
        return PreviewResult(summary="Nothing to import -- Sonarr/Radarr no longer lists this download.",
                              blocked_reason="no candidates")
    bad = [c for c in candidates if c.get("rejections")]
    if bad:
        return PreviewResult(
            summary=f"{arr.title()} flags {len(bad)} rejection(s) on this file -- not safe to auto-confirm.",
            blocked_reason="; ".join(r.get("reason", "?") for c in bad for r in c["rejections"]),
        )
    if arr == "radarr":
        titles = {c.get("movie", {}).get("title") for c in candidates}
        if len(titles) != 1 or None in titles:
            return PreviewResult(summary="Movie match is ambiguous or missing.", blocked_reason="ambiguous match")
        summary = f"Will import as \"{titles.pop()}\" ({len(candidates)} file(s))."
    else:
        titles = {c.get("series", {}).get("title") for c in candidates}
        if len(titles) != 1 or None in titles or any(not c.get("episodes") for c in candidates):
            return PreviewResult(summary="Series/episode match is ambiguous or missing.", blocked_reason="ambiguous match")
        ep_count = sum(len(c["episodes"]) for c in candidates)
        summary = f"Will import as \"{titles.pop()}\" ({ep_count} episode(s) across {len(candidates)} file(s))."

    return PreviewResult(summary=summary, before={"files": [c["path"] for c in candidates]})


def execute_manual_import(cfg, params):
    arr = params["arr"]
    download_id = params["download_id"]
    base, key = (cfg.radarr_base, cfg.radarr_key) if arr == "radarr" else (cfg.sonarr_base, cfg.sonarr_key)
    candidates = arr_api(base, key, "GET", "/api/v3/manualimport", params={"downloadId": download_id}) or []
    if not candidates or any(c.get("rejections") for c in candidates):
        return ActionResult(ok=False, message="Candidates changed since preview (now empty or rejected) -- aborting.")

    files = []
    for c in candidates:
        entry = {
            "id": c["id"], "path": c["path"], "quality": c["quality"], "languages": c["languages"],
            "releaseGroup": c.get("releaseGroup"), "indexerFlags": c.get("indexerFlags", 0),
        }
        if arr == "radarr":
            movie_id = c.get("movie", {}).get("id")
            if not movie_id:
                return ActionResult(ok=False, message="Movie match disappeared since preview -- aborting.")
            entry["movieId"] = movie_id
        else:
            episodes = c.get("episodes") or []
            if not episodes:
                return ActionResult(ok=False, message="Episode match disappeared since preview -- aborting.")
            entry["episodeIds"] = [e["id"] for e in episodes]
        files.append(entry)

    cmd = arr_api(base, key, "POST", "/api/v3/command", json={"name": "ManualImport", "files": files, "importMode": "auto"})
    return ActionResult(ok=True, message=f"Manual import submitted ({len(files)} file(s), command #{cmd['id']}). "
                                          f"Check the trace shortly to confirm it landed.")


def preview_why_not_grabbed(cfg, snap, params):
    """No mutation at all -- this is a live investigative search
    (GET /api/v3/release), not a fix. Confirmed live: a genuinely never-grabbed kids'
    movie special returned zero candidate releases from any indexer, definitively
    ruling out "quality profile rejected everything" as the cause for that case --
    exactly the kind of question this dashboard couldn't previously answer without
    leaving it to check Sonarr/Radarr by hand."""
    return PreviewResult(
        summary="Runs a live search against every enabled indexer and shows exactly what it finds "
                "(or doesn't) -- no download is triggered, nothing is changed. Can take 10-30+ seconds.",
    )


def execute_why_not_grabbed(cfg, params):
    arr = params["arr"]
    base, key = (cfg.radarr_base, cfg.radarr_key) if arr == "radarr" else (cfg.sonarr_base, cfg.sonarr_key)
    id_param = {"movieId": params["movie_id"]} if arr == "radarr" else {"episodeId": params["episode_id"]}
    releases = arr_api(base, key, "GET", "/api/v3/release", params=id_param) or []

    if not releases:
        return ActionResult(ok=True, message="Zero releases found on any enabled indexer.",
                             detail="Nothing to reject -- no indexer has this at all right now. Not a "
                                    "quality profile or custom format issue; the release may not exist "
                                    "on any of your configured indexers, or hasn't been indexed yet.")

    rejected = [r for r in releases if r.get("rejected")]
    clean = [r for r in releases if not r.get("rejected")]
    lines = [f"{len(releases)} release(s) found: {len(clean)} not rejected, {len(rejected)} rejected."]
    if clean:
        lines.append("Not rejected (should be gettable via a manual/automatic search):")
        for r in clean[:5]:
            lines.append(f"  - {r.get('title', '?')}")
    if rejected:
        lines.append("Rejected, with reasons:")
        for r in rejected[:8]:
            reasons = "; ".join(x.get("reason", "?") if isinstance(x, dict) else str(x) for x in r.get("rejections", []))
            lines.append(f"  - {r.get('title', '?')}: {reasons}")
    return ActionResult(ok=True, message=f"{len(releases)} release(s), {len(clean)} not rejected.", detail="\n".join(lines))


def execute_bump_min_seeders(cfg, params):
    name = params["indexer_name"]
    new_value = int(params.get("value", DEFAULT_BUMP_TO))

    for service, base, key in (("sonarr", cfg.sonarr_base, cfg.sonarr_key), ("radarr", cfg.radarr_base, cfg.radarr_key)):
        arr = ArrClient(service, base, key)
        indexers = arr.indexers()
        indexer = _find_indexer(indexers, name)
        if indexer is None:
            continue
        set_field(indexer["fields"], "minimumSeeders", new_value)
        arr.update_indexer(indexer["id"], indexer)
        # Re-GET to confirm it actually stuck rather than assuming the PUT worked --
        # the plan explicitly calls this out given Prowlarr sync can clobber it.
        confirmed = get_field(arr.indexer(indexer["id"])["fields"], "minimumSeeders")
        if confirmed == new_value:
            return ActionResult(ok=True, message=f"{service}: \"{name}\" Minimum Seeders is now {confirmed}.")
        return ActionResult(ok=False, message=f"PUT succeeded but re-check shows {confirmed}, not {new_value} -- something else may have overwritten it immediately.")

    return ActionResult(ok=False, message=f"No indexer named \"{name}\" found in Sonarr or Radarr.")
