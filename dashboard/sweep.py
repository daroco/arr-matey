"""
Concurrent, real-diagnosis sweep across every matched request -- the actual work
behind both main.py's on-demand "stalled only" filter and poller.py's background
notification sweep, factored out once so the two can't drift out of sync with each
other (they need to agree on what counts as "stalled").
"""

import asyncio
import json
import logging
from collections import Counter, defaultdict

from . import correlate, rules
from . import state as state_mod
from .clients import local_fs
from .notify import notify_ntfy

log = logging.getLogger("dashboard.sweep")

# Bounds how many concurrent deep traces run at once -- unbounded gather() over
# ~150 requests would mean 150 simultaneous history/episode calls against a
# home-lab Sonarr/Radarr/Prowlarr, closer to a self-inflicted DoS than a filter.
CONCURRENCY = 8

SEVERITY_RANK = {"ok": 0, "info": 1, "warning": 2, "error": 3}


async def run_sweep(snap, cfg, db, *, notify=False):
    """Evaluates every matched request's real diagnoses concurrently (bounded).
    Returns the set of request ids with at least one open (non-OK) diagnosis --
    what the "stalled only" filter renders.

    notify=True additionally persists every diagnosis via state.py's
    upsert_diagnosis()/clear_diagnosis() and fires an ntfy push for genuinely NEW
    diagnoses (first time seen, or re-firing after a previous clear) -- never a
    repeat push for something already known and still open, and diagnoses that stop
    firing get cleared so a later re-occurrence is treated as fresh again rather
    than staying silently "already notified" forever.

    Grouped and batched by title exactly like rclone-sync.py's own notify_new_files/
    flush_pending (same problem, same fix: a season pack spans several attempts, each
    of which can independently trip the same one or two rule ids, so per-diagnosis
    pushing turned one real problem into a wall of near-duplicate notifications --
    confirmed live, Magic School Bus alone fired 8 separate pushes across 4 attempts x
    2 rule ids for what a person reads as a single "this show is stuck" situation).
    Collapses to one push per title per sweep, deduped with an (xN) count suffix for
    anything that repeated across attempts."""
    candidate_ids = correlate.matched_request_ids(snap)
    is_seedbox = cfg.download_mode != "local"
    wrapper_summary, _ = local_fs.tail_wrapper_log(cfg.rclone_wrapper_log) if is_seedbox else (None, [])
    sem = asyncio.Semaphore(CONCURRENCY)
    stalled_ids = set()
    touched_keys = set()
    # title -> {"request_id": int|None, "severity": str, "headlines": [str, ...]},
    # flushed to one push (and one in-app notification row) per title.
    pending = defaultdict(lambda: {"request_id": None, "severity": "info", "headlines": []})

    async def handle_diagnosis(scope_type, scope_key, d, title, request_id):
        touched_keys.add((scope_type, scope_key, d.rule_id))
        if not notify:
            return
        detail_json = json.dumps({"headline": d.headline, "detail": d.detail})
        _id, _first_seen, is_new = await asyncio.to_thread(
            state_mod.upsert_diagnosis, db, scope_type, scope_key, d.rule_id, d.severity.value, detail_json
        )
        if is_new and d.severity.value != "ok":
            group = pending[title]
            group["request_id"] = request_id
            if SEVERITY_RANK[d.severity.value] > SEVERITY_RANK[group["severity"]]:
                group["severity"] = d.severity.value
            group["headlines"].append(d.headline)

    async def check(request_id):
        # One slow/failed source must not take the whole sweep down -- same
        # guard() principle snapshot.py already applies to the background poller.
        async with sem:
            try:
                trace = await asyncio.to_thread(correlate.build_trace_detail, request_id, snap, cfg)
            except Exception:
                log.warning(f"sweep: request {request_id} failed to trace, skipping", exc_info=True)
                return
            if trace is None:
                return
            trace = rules.evaluate_trace(trace, snap, db, cfg, is_seedbox, wrapper_summary)
            problems = list(trace.diagnoses) + [d for att in trace.attempts.values() for d in att.diagnoses]
            for d in trace.diagnoses:
                await handle_diagnosis("request", str(request_id), d, trace.title, request_id)
            for att in trace.attempts.values():
                for d in att.diagnoses:
                    await handle_diagnosis("attempt", att.download_id, d, trace.title, request_id)
            if any(d.severity.value != "ok" for d in problems):
                stalled_ids.add(request_id)

    await asyncio.gather(*[check(rid) for rid in candidate_ids])

    if notify:
        await asyncio.to_thread(_flush_pending, cfg, db, pending)
        await asyncio.to_thread(_clear_untouched, db, touched_keys)

    return stalled_ids


def _flush_pending(cfg, db, pending):
    for title, group in pending.items():
        headlines = group["headlines"]
        counts = Counter(headlines)
        unique = list(dict.fromkeys(headlines))   # de-dupe, keep first-seen order
        if len(unique) == 1 and counts[unique[0]] == 1:
            msg_title, message = f"Dashboard: {unique[0]}", title
        else:
            parts = [f"{h} (x{counts[h]})" if counts[h] > 1 else h for h in unique]
            msg_title, message = f"Dashboard: {title}", "; ".join(parts)
        notify_ntfy(cfg.ntfy_server, cfg.ntfy_topic, msg_title[:200], message[:1000])
        # In-app notification list mirrors this exactly, independent of whether ntfy
        # is even configured (NTFY_TOPIC blank is a real, supported setup -- see
        # notify.py -- and the in-app list should still work on its own).
        state_mod.insert_notification(
            db, title=title, request_id=group["request_id"], severity=group["severity"],
            headline=msg_title[len("Dashboard: "):], message=message,
        )


def _clear_untouched(db, touched_keys):
    rows = db.conn.execute(
        "SELECT DISTINCT scope_type, scope_key, rule_id FROM diagnosis WHERE cleared_at IS NULL"
    ).fetchall()
    for row in rows:
        key = (row["scope_type"], row["scope_key"], row["rule_id"])
        if key not in touched_keys:
            state_mod.clear_diagnosis(db, *key)
