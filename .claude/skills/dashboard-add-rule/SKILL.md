---
name: dashboard-add-rule
description: The pattern for adding a new diagnostic rule + one-click fix action to the trace dashboard (dashboard/), for when a new kind of stuck-request pattern shows up that the dashboard doesn't already explain. Use whenever the dashboard shows a stuck request with no real explanation, or a static/generic message instead of a specific diagnosis.
---

# Add a new dashboard rule + fix action

The dashboard's whole value is turning "this is stuck, no idea why" into a specific,
evidence-backed diagnosis with a one-click (preview-then-confirm) fix. When you hit a
new stuck-request pattern it doesn't already explain, follow this pattern rather than
just fixing that one instance by hand and moving on -- three real cases already went
through this exact loop (see below), and there will be more.

## The loop

1. **Investigate the real cause by hand first**, against the live APIs, before
   writing any dashboard code. Don't guess at a Sonarr/Radarr/Seerr API shape --
   confirm it live. This is not optional: every rule in `dashboard/rules.py` that
   skipped this step would have shipped wrong (the "never grabbed" rule specifically
   exists *because* guessing "probably a quality profile issue" turned out to be
   wrong for a real case -- the real answer was "zero releases found anywhere").

2. **Add the rule** in `dashboard/rules.py` (or `dashboard/correlate.py` if it's
   structural, like the season-numbering mismatch, rather than a torrent/import
   state check). A rule is a small pure function over an already-built `Trace`/
   `Attempt` -- no new API calls at evaluation time; if answering the question needs
   a live call (like an interactive search), that belongs in the *action*, not the
   rule (the rule just decides *when* to offer the button).

3. **Add the fix action** in `dashboard/actions/<module>.py` -- a `preview_*`/
   `execute_*` pair, registered in `dashboard/actions/__init__.py`. Every action:
   - `preview()` must be **provably read-only** -- if it's genuinely just
     investigative (no mutation at all, like "search now and show why"), say so
     explicitly in the preview text rather than implying a fix is about to happen.
   - Re-fetch and re-validate in `execute()` rather than trusting params blindly --
     assume the state could have changed since `preview()` ran.
   - Block cleanly (`blocked_reason`) on anything ambiguous rather than guessing --
     Sonarr/Radarr's own refusal to auto-import an ID-only match is itself an example
     of "when in doubt, make a human confirm," and the dashboard's actions should
     hold the same bar.

4. **Wire the `suggested_actions`** on the `Diagnosis` with the exact params the
   action needs (e.g. `[("arr_manual_import", {"arr": "radarr", "download_id": ...,
   "movie_id": ...})]`).

5. **Restart and verify against the real, live case that prompted this** (see
   `dashboard-restart`) -- not a synthetic test. Every rule in this file was verified
   against a real stuck request before being considered done.

## Three real, worked examples (read these before writing a new one)

- **Season-number mismatch** (`correlate.py`'s `season_number_mismatch` diagnosis +
  `arr_actions.py`'s `arr_fix_season_mismatch`): a show (MythBusters) where Sonarr
  groups seasons by year (TVDB) but Seerr requested ordinal seasons (TMDB) -- zero
  episodes ever resolved, previously showing as a self-contradictory "unmatched" when
  the series *was* actually matched. Fix: monitor every season Sonarr itself has,
  ignore what Seerr requested, trigger a search.
- **Manual import required** (`rules.py`'s `manual_import_required` +
  `arr_actions.py`'s `arr_manual_import`): a fully-downloaded file (The Departed)
  that Sonarr/Radarr refuse to auto-import because the match came from grab history
  (by ID) rather than filename parsing -- a safety check, not a real problem. Fix:
  confirm the arr's own best-guess match via `GET /api/v3/manualimport` +
  `POST /api/v3/command ManualImport`, but only when there's exactly one unambiguous,
  rejection-free candidate.
- **Why hasn't this grabbed** (`rules.py`'s `never_grabbed` +
  `arr_actions.py`'s `arr_why_not_grabbed`): a request matched fine but never
  attempted a grab. Previously a static, uninvestigated label. Fix isn't really a
  fix -- it's a live `GET /api/v3/release` interactive search that tells you the
  actual answer (nothing exists on any indexer, vs. releases exist but got rejected
  and why, vs. releases exist and *weren't* rejected).

## What ties all three together

Every one of these was previously either a **misleading static message** or a
**contradictory-looking pair of labels** (see also: the "processing" vs. "never
grabbed" wording clash, fixed by giving the list page and detail page clearly
different, non-overlapping vocabulary -- `MEDIA_STATUS_DISPLAY` in `correlate.py`).
When a stuck request doesn't have a real, specific, evidence-backed explanation,
that's the signal to add a rule here rather than explaining it in chat and letting
the next occurrence be just as opaque.
