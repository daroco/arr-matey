"""
Triggers scripts/rclone-sync.py and scripts/seedbox-cleanup.py as real subprocesses --
deliberately NOT imported, for reasons that all independently rule it out: the
filenames have hyphens (`import rclone-sync` is a SyntaxError), seedbox-cleanup.py
reads `"--dry-run" in sys.argv` at module scope (importing it would fix dry-run to
whatever the web server's own argv happens to be), and both scripts attach a
RotatingFileHandler to a module-level logger at import time (importing them into the
long-running dashboard process would mean two independent loggers fighting over the
same log file). subprocess preserves each script's own exit-code contract untouched.

Uses sys.executable (python.exe), not pythonw.exe, specifically so the scripts' own
`log.addHandler(logging.StreamHandler(sys.stdout))` actually streams somewhere this
process can read it live -- pythonw has no stdout at all.

execute_* are async generators (not plain functions) so jobs.py can update a job's
log_tail as output arrives, rather than only learning the result once a sync that can
run for hours finally exits.
"""

import asyncio
import sys
import time
from pathlib import Path

from ..clients import local_fs
from ..models import ActionResult, PreviewResult

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RCLONE_SYNC_SCRIPT = REPO_ROOT / "scripts" / "rclone-sync.py"
SEEDBOX_CLEANUP_SCRIPT = REPO_ROOT / "scripts" / "seedbox-cleanup.py"


def preview_rclone_sync(cfg, snap, params):
    summary, _ = local_fs.tail_wrapper_log(cfg.rclone_wrapper_log)
    warnings = []
    if cfg.rclone_wrapper_log.exists():
        age_seconds = time.time() - cfg.rclone_wrapper_log.stat().st_mtime
        if age_seconds < 120 and (not summary or ("sync OK" not in summary and "sync FAILED" not in summary)):
            warnings.append(
                "The wrapper log was touched in the last 2 minutes with no matching "
                "\"sync OK\"/\"sync FAILED\" line yet -- a sync may already be running "
                "right now via the scheduled task. This dashboard can't detect that "
                "directly; starting another one won't corrupt anything (rclone sync is "
                "idempotent) but will run redundantly."
            )
    return PreviewResult(
        summary="Will run scripts/rclone-sync.py immediately (both seedbox clients, sequentially).",
        before={"last run": summary or "none seen"}, warnings=warnings,
    )


async def execute_rclone_sync(cfg, params):
    async for line in _stream_subprocess([sys.executable, str(RCLONE_SYNC_SCRIPT)]):
        yield line


def preview_seedbox_cleanup(cfg, snap, params):
    """The preview IS a real dry run -- scripts/seedbox-cleanup.py --dry-run makes
    the exact same decisions the real run would, so there's nothing to reimplement or
    approximate here."""
    try:
        proc = _run_blocking([sys.executable, str(SEEDBOX_CLEANUP_SCRIPT), "--dry-run"], timeout=60)
    except Exception as e:
        return PreviewResult(summary="Dry run failed to complete.", blocked_reason=str(e))
    return PreviewResult(summary="Real output of `seedbox-cleanup.py --dry-run`:", after={"output": proc})


async def execute_seedbox_cleanup(cfg, params):
    async for line in _stream_subprocess([sys.executable, str(SEEDBOX_CLEANUP_SCRIPT)]):
        yield line


def _run_blocking(cmd, timeout):
    import subprocess
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (result.stdout or "") + (result.stderr or "")


async def _stream_subprocess(cmd):
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    async for raw_line in proc.stdout:
        yield raw_line.decode(errors="replace").rstrip("\n")
    await proc.wait()
    yield f"[exit code {proc.returncode}]"
