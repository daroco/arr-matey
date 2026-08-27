"""
Fixes rules.py's unextracted_archive diagnosis: extracts multi-part RAR archives
sitting in local staging (an old scene-release packaging style nothing in this
pipeline auto-handles -- see rules.py's docstring for the full story, confirmed live
against Scrubs S08 and several MythBusters S20 episodes) via 7-Zip, then triggers
Sonarr/Radarr to rescan the folder and import the now-visible video files.

Host-only by construction, same as script_actions.py's rclone/cleanup triggers --
this whole action only makes sense run from the same host that owns the staging
filesystem, whichever OS that host is.
"""

import os
import shutil
import subprocess
from pathlib import Path

from ..clients import local_fs
from ..clients.base import arr_api
from ..models import ActionResult, PreviewResult

# shutil.which finds 7z on PATH where it's actually there (confirmed: Linux's
# p7zip-full package installs it as plain "7z" on PATH). The official Windows
# installer does NOT add itself to PATH though (confirmed live on this machine --
# shutil.which("7z") returns None despite a working install), so Windows needs an
# explicit fallback to its default install location; Linux falls back to the bare
# name so a real "not installed" error surfaces from subprocess itself.
SEVENZIP = shutil.which("7z") or (r"C:\Program Files\7-Zip\7z.exe" if os.name == "nt" else "7z")

# Container-side paths -- constant regardless of the host's actual drive letter,
# since every container mounts ${MEDIA_ROOT} at /media (see compose.yaml and
# scripts/provision.py's identical QBT_LOCAL_STAGING/TRANSMISSION_LOCAL_STAGING
# constants). The rescan command Sonarr/Radarr receive needs this container path,
# not the host path 7z itself operates on.
CONTAINER_STAGING = {
    "qbittorrent": "/media/downloads/seedbox",
    "transmission": "/media/downloads/seedbox-transmission",
}


def _staging_root(cfg, client):
    return cfg.staging_root_qbt if client == "qbittorrent" else cfg.staging_root_transmission


def preview_extract_archive(cfg, snap, params):
    staging_root = _staging_root(cfg, params["client"])
    rars = local_fs.find_unextracted_rars(params["torrent_name"], staging_root)
    if not rars:
        return PreviewResult(summary="No unextracted archives found -- may have already been handled.",
                              blocked_reason="nothing to extract")
    total_compressed = sum(r.stat().st_size for r in rars if r.exists())
    return PreviewResult(
        summary=f"Will extract {len(rars)} archive(s) with 7-Zip, then trigger a Sonarr/Radarr "
                f"rescan of the folder so it can import the results. Needs roughly "
                f"{total_compressed / 1e9:.1f}+ GB of free space temporarily (extracted files "
                f"sit alongside the original archive parts until the normal cleanup pipeline "
                f"clears them out after import).",
        before={"archives": [str(r.name) for r in rars]},
    )


# Where 7-Zip writes while it works. Confirmed live (Barbie 2023, an 8.9 GB archive;
# MythBusters S20E11): extracting straight into the torrent's own folder is a race
# against Sonarr/Radarr's periodic completed-download scan -- it sees a video file
# appear, imports it while 7z is still writing (a preallocated, zero-filled tail),
# and the library ends up with a truncated file that plays fine until it doesn't.
# Cleanup then removes the torrent as "imported" and the RARs are gone for good.
# So: extract into a sibling folder no torrent is named after (the *arr scan only
# looks at folders matching a tracked torrent's name), and move the finished
# files into place only after 7z says "Everything is Ok".
EXTRACT_TMP_DIRNAME = "_extracting"

# An 8.9 GB archive took well over 10 minutes on this box; 600s killed 7z mid-file.
# Generous ceiling -- this runs on the jobs thread, not a request handler.
SEVENZIP_TIMEOUT_SECONDS = 4 * 3600


def execute_extract_archive(cfg, params):
    staging_root = _staging_root(cfg, params["client"])
    rars = local_fs.find_unextracted_rars(params["torrent_name"], staging_root)
    if not rars:
        return ActionResult(ok=False, message="No unextracted archives found -- state changed since preview.")

    results = []
    failed = []
    for rar in rars:
        tmp_dir = Path(staging_root) / EXTRACT_TMP_DIRNAME / params["torrent_name"] / rar.stem
        shutil.rmtree(tmp_dir, ignore_errors=True)  # a previous killed/failed attempt's leftovers
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                [SEVENZIP, "x", str(rar), f"-o{tmp_dir}", "-y"],
                capture_output=True, text=True, timeout=SEVENZIP_TIMEOUT_SECONDS,
            )
            ok = "Everything is Ok" in proc.stdout
        except subprocess.TimeoutExpired:
            ok = False
        if ok:
            # Only now is anything visible where Sonarr/Radarr will look.
            for extracted in tmp_dir.iterdir():
                shutil.move(str(extracted), str(rar.parent / extracted.name))
        else:
            failed.append(rar.name)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        results.append(f"{rar.name}: {'OK' if ok else 'FAILED'}")

    # Tidy the per-torrent temp parent (and _extracting itself) if nothing else is in flight.
    for d in (Path(staging_root) / EXTRACT_TMP_DIRNAME / params["torrent_name"], Path(staging_root) / EXTRACT_TMP_DIRNAME):
        try:
            d.rmdir()
        except OSError:
            pass

    if failed:
        return ActionResult(
            ok=False, message=f"{len(failed)}/{len(rars)} extraction(s) failed.",
            detail="\n".join(results),
        )

    container_path = f"{CONTAINER_STAGING[params['client']]}/{params['torrent_name']}"
    base, key = (cfg.radarr_base, cfg.radarr_key) if params["arr"] == "radarr" else (cfg.sonarr_base, cfg.sonarr_key)
    command_name = "DownloadedMoviesScan" if params["arr"] == "radarr" else "DownloadedEpisodesScan"
    cmd = arr_api(base, key, "POST", "/api/v3/command", json={"name": command_name, "path": container_path})

    return ActionResult(
        ok=True, message=f"Extracted {len(rars)} archive(s), triggered {params['arr']} rescan (command #{cmd['id']}).",
        detail="\n".join(results),
    )
