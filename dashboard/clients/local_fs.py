"""
Local filesystem checks: whether a finished seedbox torrent has actually synced down
into local staging yet, and tailing the log files scripts/rclone-sync.py and
scripts/seedbox-cleanup.py already write -- this dashboard reads their output, it never
runs or interferes with their own scheduled cadence in the background (see poller.py's
module docstring). Log line formats copied exactly from scripts/rclone-sync.py:
COPIED_RE, and the "sync OK: {remote} -> {local}" / "sync FAILED (exit N): ..." lines
in rclone-sync-wrapper.log.
"""

import re
from pathlib import Path

COPIED_RE = re.compile(r"INFO\s*:\s*(.+):\s*(?:Multi-thread )?Copied \((new|replaced existing)\)\s*$")
SYNC_OK_RE = re.compile(r"sync OK: (.+) -> (.+)$")
SYNC_FAILED_RE = re.compile(r"sync FAILED \(exit (\d+)\): (.+) -> (.+) -- see")


def staging_path_for(torrent_name, staging_root):
    """A finished torrent's content usually lands as either a single file or a
    directory named after the torrent under the staging root -- check both."""
    p = Path(staging_root) / torrent_name
    return p


def is_synced_locally(torrent_name, staging_root):
    p = staging_path_for(torrent_name, staging_root)
    if p.is_file():
        return True
    if p.is_dir():
        return any(p.rglob("*"))
    return False


def tail_wrapper_log(path, max_lines=200):
    """Returns (last_run_summary: str|None, lines: list[str]) from
    rclone-sync-wrapper.log. last_run_summary is the most recent "sync OK"/"sync
    FAILED" line found, used by rules.py to decide whether a pending rclone stage is
    just slow or actually broken (a successful run finishing *after* the torrent
    completed with the file still missing is the broken case)."""
    path = Path(path)
    if not path.exists():
        return None, []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    last_summary = None
    for line in reversed(lines):
        if "sync OK:" in line or "sync FAILED" in line:
            last_summary = line
            break
    return last_summary, lines


def tail_cleanup_log(path, max_lines=200):
    path = Path(path)
    if not path.exists():
        return [], None
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    last_run = lines[-1] if lines else None
    return lines, last_run
