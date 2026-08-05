---
name: dashboard-restart
description: Stop and restart the trace dashboard (dashboard/) process after a code change, and verify it came back up healthy. Use whenever dashboard/ source has been edited and needs to be reloaded -- it's not auto-reloading.
---

# Restart the dashboard

`dashboard/` runs as a plain Windows process (not a container -- see its README
section for why), and has no auto-reload. Every code change needs a manual restart to
take effect.

## Steps

1. **Stop the running process.** There should only ever be one `python.exe` process
   for this (uvicorn, single worker) -- if `Get-Process python` shows more than one,
   something didn't clean up properly last time; kill all of them before restarting,
   don't layer a new one on top.

   ```powershell
   Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
   Start-Sleep -Seconds 1
   Get-Process python -ErrorAction SilentlyContinue | Measure-Object | Select-Object Count   # expect 0
   ```

2. **Start it via the real entrypoint**, not a bare uvicorn CLI invocation --
   `python -m dashboard.run` is what actually exercises `run.py`'s logging setup
   (`RotatingFileHandler` to `<CONFIG_ROOT>\dashboard\dashboard.log`) and matches
   exactly what the Task Scheduler task runs in production. Needs `-m`: it's a
   package with relative imports, a bare file path fails.

   ```bash
   cd /c/Users/drcor/acquisitions
   nohup py -m dashboard.run > /tmp/dashboard_run.log 2>&1 &
   disown
   sleep 4
   cat /tmp/dashboard_run.log   # should show "Application startup complete."
   ```

   Don't double-background this (no `nohup ... &` *inside* an already-backgrounded
   tool call) -- it detaches the process from anything that can track it later.

3. **Verify it's actually healthy**, not just that a process exists:

   ```bash
   curl -s http://127.0.0.1:8099/health
   ```

   Expect `{"ok":true,"last_poll":"<a recent timestamp>"}`. A stale or missing
   `last_poll` means the background poller didn't start cleanly -- check the log file
   for a traceback.

4. **If it's reachable via Caddy** (`stats.{$DOMAIN}`), confirm that path too, since a
   process bound only to `127.0.0.1` instead of `0.0.0.0` would pass step 3 but still
   be unreachable from `host.docker.internal`:

   ```bash
   curl -s http://stats.correll.tv/health   # LAN
   ```

## Why not just edit and let it pick up changes

There's no file-watcher/reload wired in (`uvicorn.run(..., reload=False)` implicitly,
since `reload=True` isn't set in `run.py`) -- deliberately, since the production path
is a Task Scheduler task, not a dev server. Always restart after any change under
`dashboard/`.
