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

If NTFY_TOPIC is set, one push notification fires per file as it's copied down --
parsed from the new tail of rclone's own --log-file after each sync (only the bytes
appended by *this* run, since the log file persists across runs and re-parsing the
whole thing every time would re-notify on old entries). Notifications are best-effort:
a failed POST is logged but never fails the sync itself.
"""

import logging
import logging.handlers
import re
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

RCLONE = Path(r"C:\Users\drcor\AppData\Local\Microsoft\WinGet\Packages\Rclone.Rclone_"
              r"Microsoft.Winget.Source_8wekyb3d8bbwe\rclone-v1.74.3-windows-amd64\rclone.exe")
ENV_PATH = Path(r"C:\Users\drcor\acquisitions\.env")

_env = dotenv_values(ENV_PATH)
MEDIA_ROOT = Path(_env["MEDIA_ROOT"])
CONFIG_ROOT = Path(_env["CONFIG_ROOT"])
NTFY_SERVER = _env.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = _env.get("NTFY_TOPIC", "")

LOG_PATH = CONFIG_ROOT / "rclone-sync-wrapper.log"

SYNCS = [
    ("seedbox:/home/seedit4me/torrents/qbittorrent/ratio", str(MEDIA_ROOT / "downloads" / "seedbox"),
     str(CONFIG_ROOT / "rclone-sync.log")),
    ("seedbox:/home/seedit4me/torrents/transmission/downloads", str(MEDIA_ROOT / "downloads" / "seedbox-transmission"),
     str(CONFIG_ROOT / "rclone-sync-transmission.log")),
]

COPIED_RE = re.compile(r"INFO\s*:\s*(.+):\s*Copied \((new|replaced existing)\)\s*$")

log = logging.getLogger("rclone-sync")


def notify_ntfy(title, message):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            NTFY_SERVER,
            json={"topic": NTFY_TOPIC, "title": title, "message": message},
            timeout=10,
        )
    except requests.RequestException:
        log.exception(f"ntfy notification failed: {title}")


def notify_new_files(rclone_log_file, offset):
    path = Path(rclone_log_file)
    if not path.exists():
        return
    with open(path, encoding="utf-8", errors="replace") as f:
        f.seek(offset)
        new_lines = f.readlines()
    for line in new_lines:
        m = COPIED_RE.search(line.rstrip("\n"))
        if m:
            file_path = m.group(1)
            name = Path(file_path).name
            log.info(f"copied down: {file_path}")
            notify_ntfy("File synced", name)


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
        offset = Path(rclone_log_file).stat().st_size if Path(rclone_log_file).exists() else 0
        result = subprocess.run(
            [str(RCLONE), "sync", remote, local, "--min-age", "30s",
             "--log-file", rclone_log_file, "--log-level", "INFO"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        notify_new_files(rclone_log_file, offset)
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
