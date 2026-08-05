"""
Builds one Snapshot per poll cycle by calling every source, with per-source error
isolation -- one dead API must never take the whole poll down, same principle
scripts/ddns-update.py already applies per-DNS-record (loop, catch, record the
failure, keep going, only surface a summary at the end). guard() is that principle
factored out so poller.py doesn't have to repeat a try/except around every call.
"""

import logging

from .clients.arr import ArrClient
from .clients.base import SourceError
from .clients.qbittorrent import QbittorrentClient, normalize_state as qbt_normalize
from .clients.seerr import SeerrClient
from .clients.transmission import TransmissionClient, normalize_state as tr_normalize
from .models import Snapshot, TorrentView
from . import state as state_mod

log = logging.getLogger("dashboard.snapshot")


def guard(db, source_name, fn, default):
    """Runs fn(), records source_health either way, and returns `default` (never
    raises) on failure so callers can build a Snapshot unconditionally."""
    try:
        result = fn()
        state_mod.record_source_health(db, source_name, ok=True)
        return result
    except Exception as e:
        original = e.original if isinstance(e, SourceError) else e
        log.warning(f"{source_name} fetch failed: {original}")
        state_mod.record_source_health(db, source_name, ok=False, error=original)
        return default


def build_clients(cfg):
    sonarr = ArrClient("sonarr", cfg.sonarr_base, cfg.sonarr_key)
    radarr = ArrClient("radarr", cfg.radarr_base, cfg.radarr_key)
    seerr = SeerrClient(cfg.seerr_base, cfg.seerr_key)

    if cfg.download_mode == "local":
        qbt = QbittorrentClient(
            "local", local_base=cfg.qbittorrent_local_base,
            local_user=cfg.qbt_user, local_pass=cfg.qbt_pass,
        )
        transmission = None
    else:
        qbt = QbittorrentClient(
            "seedbox", seedbox_host=cfg.seedbox_host, seedbox_basic_auth=cfg.seedbox_basic_auth,
        )
        transmission = TransmissionClient(cfg.seedbox_host, cfg.seedbox_basic_auth)

    return sonarr, radarr, seerr, qbt, transmission


def _torrents_to_views(raw_qbt, raw_transmission):
    by_hash = {}
    for t in raw_qbt:
        h = t["hash"].lower()
        by_hash[h] = TorrentView(
            hash=h, name=t["name"], client="qbittorrent", category=t.get("category", ""),
            state_raw=t["state"], state_normalized=qbt_normalize(t["state"]),
            progress=t.get("progress", 0.0), size_left=t.get("amount_left", 0),
            seeders=t.get("num_seeds", 0), save_path=t.get("save_path", ""),
        )
    for t in raw_transmission:
        h = t["hashString"].lower()
        by_hash[h] = TorrentView(
            hash=h, name=t["name"], client="transmission", category="",
            state_raw=str(t["status"]),
            state_normalized=tr_normalize(t["status"], t.get("isFinished", False)),
            progress=t.get("percentDone", 0.0), size_left=t.get("leftUntilDone", 0),
            seeders=t.get("peersSendingToUs", 0), save_path=t.get("downloadDir", ""),
        )
    return by_hash


def fetch_snapshot(db, cfg, *, fetch_history=True, fetch_slow=True):
    """fetch_history/fetch_slow let poller.py skip the slower tiers on fast-tier
    ticks (see poller.py's cadence table) -- Tier A calls this with both False."""
    sonarr, radarr, seerr, qbt, transmission = build_clients(cfg)
    snap = Snapshot()

    snap.sonarr_queue = guard(db, "sonarr", lambda: sonarr.queue(), [])
    snap.radarr_queue = guard(db, "radarr", lambda: radarr.queue(), [])

    raw_qbt = guard(db, "qbittorrent", lambda: qbt.torrents(), [])
    raw_transmission = (
        guard(db, "transmission", lambda: transmission.torrents(), [])
        if transmission else []
    )
    snap.torrents_by_hash = _torrents_to_views(raw_qbt, raw_transmission)

    if fetch_history:
        snap.seerr_requests = guard(db, "seerr", lambda: seerr.all_requests(), [])

    if fetch_slow:
        series_list = guard(db, "sonarr", lambda: sonarr.all_series(), [])
        snap.sonarr_series_by_id = {s["id"]: s for s in series_list}
        movie_list = guard(db, "radarr", lambda: radarr.all_movies(), [])
        snap.radarr_movie_by_id = {m["id"]: m for m in movie_list}
        snap.sonarr_root_folders = guard(db, "sonarr", lambda: sonarr.root_folders(), [])
        snap.radarr_root_folders = guard(db, "radarr", lambda: radarr.root_folders(), [])
        snap.sonarr_indexers = guard(db, "sonarr", lambda: sonarr.indexers(), [])
        snap.radarr_indexers = guard(db, "radarr", lambda: radarr.indexers(), [])
        settings_sonarr = guard(db, "seerr", lambda: seerr.settings_for("sonarr"), [])
        snap.seerr_settings_sonarr = settings_sonarr[0] if settings_sonarr else {}
        settings_radarr = guard(db, "seerr", lambda: seerr.settings_for("radarr"), [])
        snap.seerr_settings_radarr = settings_radarr[0] if settings_radarr else {}

    return snap
