"""
Dataclasses shared across the dashboard. Snapshot/TorrentView are the normalized,
client-agnostic shapes snapshot.py builds so correlate.py and rules.py never have to
branch on "is this qBittorrent or Transmission" again after this point. Trace/Target/
Attempt/Stage/Diagnosis are correlate.py's/rules.py's output shapes, consumed by the
Jinja2 templates.
"""

# This repo's Python (3.9, see CLAUDE.md/README -- no .python-version is pinned, but
# that's what's actually installed) predates the `str | None` union syntax used below;
# `from __future__ import annotations` defers annotation evaluation to strings so it
# parses fine without requiring 3.10+ or importing typing.Optional everywhere.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


class Stage(str, Enum):
    REQUESTED = "requested"
    APPROVED = "approved"
    SENT_TO_ARR = "sent_to_arr"
    SEARCHING = "searching"
    GRABBED = "grabbed"
    DOWNLOADING = "downloading"
    COMPLETE_REMOTE = "complete_remote"   # seedbox mode only
    SYNCED_LOCAL = "synced_local"          # seedbox mode only
    DOWNLOADED = "downloaded"              # local mode only (collapses the two above)
    IMPORTED = "imported"
    IN_LIBRARY = "in_library"


class Severity(str, Enum):
    OK = "ok"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass
class TorrentView:
    """Normalized shape for a single torrent, regardless of which client/mode it came
    from. hash is always lowercased -- the join key into arr history's downloadId."""
    hash: str
    name: str
    client: str            # "qbittorrent" | "transmission"
    category: str
    state_raw: str          # e.g. "stalledUP", "pausedUP", 0, "downloading"
    state_normalized: str   # "downloading" | "seeding" | "complete_idle" | "complete_paused" | "error" | "unknown"
    progress: float          # 0.0-1.0
    size_left: int
    seeders: int
    save_path: str = ""


@dataclass
class Snapshot:
    """One poll's worth of data from every source, held in memory only -- see db.py's
    module docstring for why this isn't persisted. Every page render reads this, never
    makes an outbound API call itself."""
    fetched_at: str = field(default_factory=utcnow_iso)
    seerr_requests: list = field(default_factory=list)
    sonarr_queue: list = field(default_factory=list)
    radarr_queue: list = field(default_factory=list)
    sonarr_series_by_id: dict = field(default_factory=dict)
    radarr_movie_by_id: dict = field(default_factory=dict)
    sonarr_root_folders: list = field(default_factory=list)
    radarr_root_folders: list = field(default_factory=list)
    sonarr_indexers: list = field(default_factory=list)
    radarr_indexers: list = field(default_factory=list)
    seerr_settings_sonarr: dict = field(default_factory=dict)
    seerr_settings_radarr: dict = field(default_factory=dict)
    torrents_by_hash: dict = field(default_factory=dict)   # hash -> TorrentView
    source_errors: dict = field(default_factory=dict)       # source -> last error str


@dataclass
class Evidence:
    label: str
    detail: str = ""


@dataclass
class StageState:
    stage: Stage
    status: str          # "done" | "active" | "blocked" | "skipped" | "unknown"
    entered_at: str | None = None
    evidence: list = field(default_factory=list)


@dataclass
class Diagnosis:
    rule_id: str
    severity: Severity
    headline: str
    detail: str
    evidence: list = field(default_factory=list)
    suggested_actions: list = field(default_factory=list)   # [(action_id, params)]
    since: str | None = None


@dataclass
class Attempt:
    """One grab (== one downloadId). Lives at the Trace level, not nested under a
    single Target, because one season-pack grab can cover many episodes -- see
    correlate.py."""
    download_id: str
    torrent_name: str
    client: str | None
    indexer: str | None
    covers: list = field(default_factory=list)   # episode/movie ids this attempt covers
    grabbed_at: str | None = None
    stages: list = field(default_factory=list)     # list[StageState]
    diagnoses: list = field(default_factory=list)  # list[Diagnosis]
    torrent: TorrentView | None = None


@dataclass
class Target:
    """Radarr: exactly one per Trace (the movie). Sonarr: one per requested episode."""
    kind: str            # "movie" | "episode"
    arr_id: int           # movieId or episodeId
    title: str
    season_number: int | None = None
    episode_number: int | None = None
    has_file: bool = False
    attempt_ids: list = field(default_factory=list)


@dataclass
class Trace:
    seerr_request_id: int
    media_type: str
    title: str
    requested_by: str
    requested_at: str
    request_status_raw: int
    media_status_raw: int
    targets: list = field(default_factory=list)      # list[Target]
    attempts: dict = field(default_factory=dict)       # download_id -> Attempt
    diagnoses: list = field(default_factory=list)       # request-level (not attempt-level)
    rollup_status: str = "unknown"
    match_confidence: str = "exact"   # "exact" | "fuzzy" -- see correlate.py's fallback join


@dataclass
class PreviewResult:
    summary: str
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    blocked_reason: str | None = None


@dataclass
class ActionResult:
    ok: bool
    message: str
    detail: str = ""
    exit_code: int | None = None
