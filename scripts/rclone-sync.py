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

If NTFY_TOPIC is set, one push notification fires per *video* file as it's copied down
(sidecars like nfo/srt/sfv and anything under a "Sample" path/filename are logged but
not notified -- ntfy.sh's free-tier daily quota was getting exhausted by sidecar/sample
noise before the real files even had a chance, and failures were silent since a 429
response isn't a network error; notify_ntfy now logs a warning on any non-2xx response
instead of assuming success). subprocess.run() blocks until rclone's whole multi-file
sync exits, so notifying only after that call returns would batch every notification up
until the entire sync finishes -- no good for a run that can take hours. Instead rclone
runs via Popen and gets polled every POLL_SECONDS while alive, tailing the new bytes it
has appended to its own --log-file since the last poll (byte offset tracked in binary
mode -- text-mode seek/tell offsets aren't reliably mixable with manual length math
across encodings). Notifications are best-effort: a failed POST is logged but never
fails the sync itself. rclone logs large files (roughly >250MB, which is most actual
video files) as "Multi-thread Copied (...)" instead of plain "Copied (...)" -- COPIED_RE
matches both, or every real download silently stops notifying while small sidecar files
keep working, masking the gap.
"""

import logging
import logging.handlers
import re
import subprocess
import sys
import time
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

COPIED_RE = re.compile(r"INFO\s*:\s*(.+):\s*(?:Multi-thread )?Copied \((new|replaced existing)\)\s*$")
POLL_SECONDS = 10

# Only the actual video file is notification-worthy -- every sync also copies down
# nfo/srt/sfv sidecars and sample clips, one "Copied" line each. Notifying on all of
# those burned through ntfy.sh's free-tier daily quota before the real files even got
# a chance (404 attempts in one day, most of them sidecars) -- silently, since a 429
# response isn't a network error and wasn't being checked at all.
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov"}
SAMPLE_RE = re.compile(r"(?:^|[\\/.])sample(?:[\\/.]|$)", re.IGNORECASE)

log = logging.getLogger("rclone-sync")


def notify_ntfy(title, message):
    if not NTFY_TOPIC:
        return
    try:
        r = requests.post(
            NTFY_SERVER,
            json={"topic": NTFY_TOPIC, "title": title, "message": message},
            timeout=10,
        )
        if not r.ok:
            log.warning(f"ntfy notification rejected ({r.status_code}): {title} -- {r.text[:200]}")
    except requests.RequestException:
        log.exception(f"ntfy notification failed: {title}")


def notify_new_files(rclone_log_file, offset):
    """Reads any new *complete* lines appended since offset, notifies on file
    completions, and returns the byte offset up to the last complete line -- a
    trailing partial line (still being written) is left for the next poll."""
    path = Path(rclone_log_file)
    if not path.exists():
        return offset
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    if not data:
        return offset
    last_newline = data.rfind(b"\n")
    if last_newline == -1:
        return offset
    complete = data[:last_newline + 1]
    for line in complete.decode("utf-8", errors="replace").splitlines():
        m = COPIED_RE.search(line)
        if m:
            file_path = m.group(1)
            name = Path(file_path).name
            log.info(f"copied down: {file_path}")
            if Path(name).suffix.lower() in VIDEO_EXTENSIONS and not SAMPLE_RE.search(file_path):
                notify_ntfy("File synced", name)
    return offset + len(complete)


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
        proc = subprocess.Popen(
            [str(RCLONE), "sync", remote, local, "--min-age", "30s",
             "--log-file", rclone_log_file, "--log-level", "INFO"],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        while proc.poll() is None:
            time.sleep(POLL_SECONDS)
            offset = notify_new_files(rclone_log_file, offset)
        offset = notify_new_files(rclone_log_file, offset)  # catch anything written just before exit
        if proc.returncode == 0:
            log.info(f"sync OK: {remote} -> {local}")
        else:
            failures += 1
            log.error(f"sync FAILED (exit {proc.returncode}): {remote} -> {local} -- see {rclone_log_file}")
    if failures:
        raise RuntimeError(f"{failures} of {len(SYNCS)} rclone sync(s) failed")


if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        log.exception("rclone-sync failed")
        sys.exit(1)
