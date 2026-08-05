"""
Fixes rules.py's unextracted_archive diagnosis: extracts multi-part RAR archives
sitting in local staging (an old scene-release packaging style nothing in this
pipeline auto-handles -- see rules.py's docstring for the full story, confirmed live
against Scrubs S08 and several MythBusters S20 episodes) via 7-Zip, then triggers
Sonarr/Radarr to rescan the folder and import the now-visible video files.

Host-only by construction, same as script_actions.py's rclone/cleanup triggers --
7z.exe is a Windows binary, and this whole action only makes sense run from the same
host that owns the staging filesystem.
"""

import subprocess

from ..clients import local_fs
from ..clients.base import arr_api
from ..models import ActionResult, PreviewResult

SEVENZIP = r"C:\Program Files\7-Zip\7z.exe"

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


def execute_extract_archive(cfg, params):
    staging_root = _staging_root(cfg, params["client"])
    rars = local_fs.find_unextracted_rars(params["torrent_name"], staging_root)
    if not rars:
        return ActionResult(ok=False, message="No unextracted archives found -- state changed since preview.")

    results = []
    failed = []
    for rar in rars:
        proc = subprocess.run(
            [SEVENZIP, "x", str(rar), f"-o{rar.parent}", "-y"],
            capture_output=True, text=True, timeout=600,
        )
        ok = "Everything is Ok" in proc.stdout
        results.append(f"{rar.name}: {'OK' if ok else 'FAILED'}")
        if not ok:
            failed.append(rar.name)

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
