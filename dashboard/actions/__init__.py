"""
The fix-action registry. Every action has a preview() (provably read-only -- GET,
diff, done) and an execute() (the actual mutation), both looked up by id from ACTIONS
and dispatched by jobs.py. API-based actions (seerr/prowlarr/arr_actions) are plain
sync functions run via asyncio.to_thread; the two script-triggering actions
(script_actions) are async generators that yield log lines as they stream from the
subprocess, so jobs.py can update the job's live log_tail incrementally instead of
only seeing output once the whole thing finishes.
"""

from dataclasses import dataclass
from typing import Callable, Optional

from . import arr_actions, extract_actions, prowlarr_actions, script_actions, seerr_actions


@dataclass
class Action:
    id: str
    label: str
    description: str
    requires_mode: Optional[str]   # None | "seedbox" | "local"
    single_flight: bool
    is_subprocess: bool             # True -> execute is an async generator of log lines
    preview: Callable
    execute: Callable


ACTIONS = {
    "seerr_fix_root_folder": Action(
        id="seerr_fix_root_folder", label="Fix Seerr's root folder",
        description="Push the corrected activeDirectory to Seerr's Sonarr/Radarr settings.",
        requires_mode=None, single_flight=True, is_subprocess=False,
        preview=seerr_actions.preview_fix_root_folder, execute=seerr_actions.execute_fix_root_folder,
    ),
    "seerr_retry_request": Action(
        id="seerr_retry_request", label="Retry request",
        description="POST /api/v1/request/{id}/retry.",
        requires_mode=None, single_flight=False, is_subprocess=False,
        preview=seerr_actions.preview_retry_request, execute=seerr_actions.execute_retry_request,
    ),
    "prowlarr_indexer_sync": Action(
        id="prowlarr_indexer_sync", label="Force Prowlarr indexer resync",
        description="Runs ApplicationIndexerSync and waits for it to complete.",
        requires_mode=None, single_flight=True, is_subprocess=False,
        preview=prowlarr_actions.preview_sync, execute=prowlarr_actions.execute_sync,
    ),
    "arr_bump_min_seeders": Action(
        id="arr_bump_min_seeders", label="Raise Minimum Seeders",
        description="Bumps an indexer's Minimum Seeders field.",
        requires_mode=None, single_flight=False, is_subprocess=False,
        preview=arr_actions.preview_bump_min_seeders, execute=arr_actions.execute_bump_min_seeders,
    ),
    "arr_why_not_grabbed": Action(
        id="arr_why_not_grabbed", label="Search now and show why",
        description="Runs a live interactive search and shows exactly which releases were found and why each was rejected (or that none exist at all). Read-only, no mutation.",
        requires_mode=None, single_flight=False, is_subprocess=False,
        preview=arr_actions.preview_why_not_grabbed, execute=arr_actions.execute_why_not_grabbed,
    ),
    "arr_manual_import": Action(
        id="arr_manual_import", label="Confirm and import",
        description="Confirms Sonarr/Radarr's own best-guess match for a file stuck needing Manual Import and imports it.",
        requires_mode=None, single_flight=False, is_subprocess=False,
        preview=arr_actions.preview_manual_import, execute=arr_actions.execute_manual_import,
    ),
    "arr_fix_season_mismatch": Action(
        id="arr_fix_season_mismatch", label="Monitor Sonarr's real seasons + search",
        description="Monitors every season Sonarr actually has (ignoring Seerr's requested season numbers) and triggers a series search.",
        requires_mode=None, single_flight=True, is_subprocess=False,
        preview=arr_actions.preview_fix_season_mismatch, execute=arr_actions.execute_fix_season_mismatch,
    ),
    "local_extract_archive": Action(
        id="local_extract_archive", label="Extract archive(s) and import",
        description="Extracts unextracted multi-part RAR archives sitting in local staging with 7-Zip, then triggers a Sonarr/Radarr rescan.",
        requires_mode="seedbox", single_flight=False, is_subprocess=False,
        preview=extract_actions.preview_extract_archive, execute=extract_actions.execute_extract_archive,
    ),
    "run_rclone_sync": Action(
        id="run_rclone_sync", label="Run rclone sync now",
        description="Runs scripts/rclone-sync.py immediately instead of waiting for its schedule.",
        requires_mode="seedbox", single_flight=True, is_subprocess=True,
        preview=script_actions.preview_rclone_sync, execute=script_actions.execute_rclone_sync,
    ),
    "run_seedbox_cleanup": Action(
        id="run_seedbox_cleanup", label="Run seedbox cleanup now",
        description="Runs scripts/seedbox-cleanup.py immediately.",
        requires_mode="seedbox", single_flight=True, is_subprocess=True,
        preview=script_actions.preview_seedbox_cleanup, execute=script_actions.execute_seedbox_cleanup,
    ),
}
