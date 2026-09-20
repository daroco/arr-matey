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

    Grouped and batched by CATEGORY (the diagnosis headline: "No seeders", "Import
    failed", ...), one push per category per sweep listing every title it hit. This
    replaced an earlier per-title grouping: that one already stopped the per-diagnosis
    flood (a season pack spans several attempts that each trip the same rule --
    Magic School Bus alone fired 8 pushes for one stuck show before batching at all),
    but a sweep that finds the same problem across six shows still produced six pushes
    that read identically. One "No seeders (6)" push naming the six shows is what a
    person actually wants to see on their phone. A title that trips two different
    rules appears in both category pushes; repeats of one title within a category
    (several attempts of one show) collapse to an (xN) suffix."""
    candidate_ids = correlate.matched_request_ids(snap)
    is_seedbox = cfg.download_mode != "local"
    wrapper_summary, _ = local_fs.tail_wrapper_log(cfg.rclone_wrapper_log) if is_seedbox else (None, [])
    sem = asyncio.Semaphore(CONCURRENCY)
    stalled_ids = set()
    touched_keys = set()
    # headline -> {"severity": str, "titles": [(title, request_id), ...]}, flushed to
    # one push (and one in-app notification row) per headline/category.
    pending = defaultdict(lambda: {"severity": "info", "titles": []})

    async def handle_diagnosis(scope_type, scope_key, d, title, request_id):
        touched_keys.add((scope_type, scope_key, d.rule_id))
        if not notify:
            return
        detail_json = json.dumps({"headline": d.headline, "detail": d.detail})
        _id, _first_seen, is_new = await asyncio.to_thread(
            state_mod.upsert_diagnosis, db, scope_type, scope_key, d.rule_id, d.severity.value, detail_json
        )
        if is_new and d.severity.value != "ok":
            group = pending[d.headline]
            if SEVERITY_RANK[d.severity.value] > SEVERITY_RANK[group["severity"]]:
                group["severity"] = d.severity.value
            group["titles"].append((title, request_id))

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
    for headline, group in pending.items():
        titles = [t for t, _ in group["titles"]]
        counts = Counter(titles)
        unique = list(dict.fromkeys(titles))   # de-dupe, keep first-seen order
        parts = [f"{t} (x{counts[t]})" if counts[t] > 1 else t for t in unique]
        msg_title = f"Dashboard: {headline}" + (f" ({len(unique)})" if len(unique) > 1 else "")
        message = "; ".join(parts)
        notify_ntfy(cfg.ntfy_server, cfg.ntfy_topic, msg_title[:200], message[:1000], cfg.ntfy_token)
        # In-app notification list mirrors this exactly, independent of whether ntfy
        # is even configured (NTFY_TOPIC blank is a real, supported setup -- see
        # notify.py -- and the in-app list should still work on its own). The row can
        # only link back to a request when the category hit exactly one title; a
        # multi-title push names them all in the message instead.
        request_ids = {rid for _, rid in group["titles"] if rid is not None}
        state_mod.insert_notification(
            db, title=unique[0] if len(unique) == 1 else f"{len(unique)} titles",
            request_id=next(iter(request_ids)) if len(request_ids) == 1 else None,
            severity=group["severity"], headline=msg_title[len("Dashboard: "):], message=message,
        )


def _clear_untouched(db, touched_keys):
    rows = db.conn.execute(
        "SELECT DISTINCT scope_type, scope_key, rule_id FROM diagnosis WHERE cleared_at IS NULL"
    ).fetchall()
    for row in rows:
        key = (row["scope_type"], row["scope_key"], row["rule_id"])
        if key not in touched_keys:
            state_mod.clear_diagnosis(db, *key)
