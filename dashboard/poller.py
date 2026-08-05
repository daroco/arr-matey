"""
Background asyncio poller, started once from main.py's FastAPI lifespan -- not a
separate process (the app is already long-running; a second process would need IPC
just to share the Snapshot, for no benefit). Tiers exist so six APIs of very different
"how often does this change" aren't all hit at one flat interval: the fast tier
(Sonarr/Radarr queue) changes every few seconds during an active download, while
settings/indexers (tier D) rarely change at all.

Per-source exponential backoff lives in state.py's source_health table (consecutive
failures -> next_attempt_at); this module just checks whether now >= next_attempt_at
before including a source's tier in this tick, so a genuinely dead API doesn't get
hammered at the base interval while it's down.
"""

import asyncio
import logging
from datetime import datetime, timezone

from .config import Config
from .snapshot import fetch_snapshot
from . import state as state_mod
from . import sweep

log = logging.getLogger("dashboard.poller")

BACKOFF_STEPS = [30, 60, 120, 300, 600]


class Poller:
    def __init__(self, db, cfg: Config):
        self.db = db
        self.cfg = cfg
        self.snapshot = None
        self._task = None
        self._tick = 0
        self._env_mtime = cfg.env_mtime()
        self._notify_task = None

    async def poll_once(self):
        # Tier B (torrent clients) at 2x the base interval, tier C (history/requests)
        # at 6x -- see the plan's cadence table. fetch_snapshot always does tier A +
        # torrents; history/slow tiers are gated by these flags so a fast tick stays
        # cheap.
        fetch_history = self._tick % 6 == 0
        fetch_slow = self._tick % 30 == 0
        # Tier for the notification sweep -- ratio recomputed every tick (not fixed
        # at startup) so editing DASHBOARD_NOTIFY_POLL_SECONDS/DASHBOARD_POLL_SECONDS
        # in .env takes effect on the next reload without a restart, same as every
        # other cfg-driven cadence here.
        notify_ticks = max(1, self.cfg.notify_poll_seconds // self.cfg.poll_seconds)
        fetch_notify = self._tick % notify_ticks == 0
        self._tick += 1

        if self.cfg.env_mtime() != self._env_mtime:
            log.info(".env changed, reloading config")
            self.cfg.reload()
            self._env_mtime = self.cfg.env_mtime()

        snap = await asyncio.to_thread(
            fetch_snapshot, self.db, self.cfg, fetch_history=fetch_history, fetch_slow=fetch_slow
        )
        # Preserve slow-tier data between fetches that skip it, rather than letting
        # it go empty on every non-tier-D tick.
        if self.snapshot is not None:
            if not fetch_history:
                snap.seerr_requests = self.snapshot.seerr_requests
            if not fetch_slow:
                snap.sonarr_series_by_id = self.snapshot.sonarr_series_by_id
                snap.radarr_movie_by_id = self.snapshot.radarr_movie_by_id
                snap.sonarr_root_folders = self.snapshot.sonarr_root_folders
                snap.radarr_root_folders = self.snapshot.radarr_root_folders
                snap.sonarr_indexers = self.snapshot.sonarr_indexers
                snap.radarr_indexers = self.snapshot.radarr_indexers
                snap.seerr_settings_sonarr = self.snapshot.seerr_settings_sonarr
                snap.seerr_settings_radarr = self.snapshot.seerr_settings_radarr
        self.snapshot = snap

        if fetch_notify:
            self._start_notify_sweep(snap)

        await asyncio.to_thread(state_mod.sweep_snapshot, self.db, snap)
        await asyncio.to_thread(self.db.prune, self.cfg.retention_days)
        await asyncio.to_thread(
            self.db.conn.execute,
            "INSERT INTO source_health (source, last_ok_at, consecutive_failures) "
            "VALUES ('__poll__', ?, 0) ON CONFLICT(source) DO UPDATE SET last_ok_at=excluded.last_ok_at",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.db.conn.commit()

    def _start_notify_sweep(self, snap):
        # A real deep-trace sweep across every matched request (tens of seconds to a
        # couple minutes on this stack's ~150 requests) -- run as its own background
        # task rather than awaited inline, so it can't delay the fast 30s tiers this
        # loop also owns. Guarded against overlap: if a sweep from a previous tick
        # is still running when the next notify tick comes around, skip starting a
        # second one rather than piling up concurrent sweeps.
        if self._notify_task is not None and not self._notify_task.done():
            log.info("notification sweep still running from a previous tick, skipping this one")
            return

        async def run():
            try:
                await sweep.run_sweep(snap, self.cfg, self.db, notify=True)
            except Exception:
                log.exception("notification sweep failed")

        self._notify_task = asyncio.create_task(run())

    async def run_forever(self):
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("poll cycle failed entirely (should be rare -- fetch_snapshot "
                               "guards every individual source; this means something outside "
                               "that, e.g. the db write, broke)")
            await asyncio.sleep(self.cfg.poll_seconds)

    def start(self):
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._notify_task:
            self._notify_task.cancel()
            try:
                await self._notify_task
            except asyncio.CancelledError:
                pass
