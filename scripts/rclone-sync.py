"""
Pulls finished downloads from the seedbox down to local staging folders, one client at
a time. Run via pythonw.exe as the scheduled task action -- pythonw has no console, so
no hidden-window wrapper is needed (unlike a plain console app such as rclone.exe or
powershell.exe launched directly from Task Scheduler).

--min-age 30s skips files still being written remotely; rclone also writes to a
.partial temp name and renames atomically on completion, which independently guards
against Sonarr/Radarr importing a half-copied file.

Since pythonw has no console, an uncaught exception or a silently-failed rclone call
would otherwise vanish with no trace. Every sync's outcome (and any failure) is logged
here instead, and the script exits non-zero on failure so Task Scheduler's Last Run
Result actually reflects what happened -- rclone's own --log-file only tells you
*that* something went wrong if you already knew to go looking.
"""

import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path

RCLONE = Path(r"C:\Users\drcor\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_"
              r"Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.74.3-windows-amd64\rclone.exe")
LOG_PATH = Path(r"D:\appdata\rclone-sync-wrapper.log")

SYNCS = [
    ("seedbox:/home/seedit4me/torrents/qbittorrent/ratio", r"D:\media\downloads\seedbox",
     r"D:\appdata\rclone-sync.log"),
    ("seedbox:/home/seedit4me/torrents/transmission/downloads", r"D:\media\downloads\seedbox-transmission",
     r"D:\appdata\rclone-sync-transmission.log"),
]

log = logging.getLogger("rclone-sync")


def setup_logging():
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stdout))


def main():
    failures = 0
    for remote, local, rclone_log_file in SYNCS:
        result = subprocess.run(
            [str(RCLONE), "sync", remote, local, "--min-age", "30s",
             "--log-file", rclone_log_file, "--log-level", "INFO"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            log.info(f"sync OK: {remote} -> {local}")
        else:
            failures += 1
            log.error(f"sync FAILED (exit {result.returncode}): {remote} -> {local} -- see {rclone_log_file}")
    if failures:
        raise RuntimeError(f"{failures} of {len(SYNCS)} rclone sync(s) failed")


if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        log.exception("rclone-sync failed")
        sys.exit(1)
