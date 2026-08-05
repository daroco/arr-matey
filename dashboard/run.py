"""
Entrypoint. This module uses relative imports (it's part of the dashboard/ package,
unlike scripts/*.py's standalone files), so it must be run with `-m`, not as a bare
file path: `python -m dashboard.run` for local testing, or
`pythonw.exe -m dashboard.run` (working directory set to the repo root) from the Task
Scheduler task -- see README's dashboard section for the exact task setup. pythonw for
the same no-console-window reason every other scheduled script in this repo uses it --
see scripts/rclone-sync.py's docstring for the full explanation.

uvicorn.run(..., log_config=None) matters specifically under pythonw: uvicorn's
default logging config writes to stdout, which doesn't exist under pythonw -- without
log_config=None here, startup errors before setup_logging() attaches its own handler
would vanish silently, the exact failure mode this repo's other scheduled scripts'
docstrings already warn about.

host="0.0.0.0" (not 127.0.0.1) is required for Caddy's host.docker.internal ->
this-process routing to work at all -- Docker Desktop's host.docker.internal resolves
to the host's real network interface, not loopback.
"""

import logging
import logging.handlers
import sys

import uvicorn

from .config import Config
from .main import create_app

log = logging.getLogger("dashboard")


def setup_logging(cfg):
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        cfg.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stdout))


if __name__ == "__main__":
    _cfg = Config()
    setup_logging(_cfg)
    try:
        app = create_app()
        uvicorn.run(app, host="0.0.0.0", port=_cfg.dashboard_port, workers=1, log_config=None)
    except Exception:
        log.exception("dashboard failed to start")
        sys.exit(1)
