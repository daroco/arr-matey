---
name: dashboard-restart
description: Stop and restart the trace dashboard (dashboard/) process after a code change, and verify it came back up healthy. Use whenever dashboard/ source has been edited and needs to be reloaded -- it's not auto-reloading.
---

# Restart the dashboard

`dashboard/` runs as a plain Windows process (not a container -- see its README
section for why), with no auto-reload, registered as the `acquisitions-dashboard` Task
Scheduler task (At-log-on trigger, so it survives reboot/logoff). Every code change
needs a manual restart to take effect.

## Steps

1. **Stop it via Task Scheduler, not `Stop-Process`.** Once registered, the process
   runs under Task Scheduler's own session context -- `Get-Process python |
   Stop-Process -Force` from an ordinary shell can fail silently against it: the old
   process survives, and a subsequent `Start-ScheduledTask` then fails to bind the
   port (still held by the still-alive old process) and exits just as silently,
   leaving hours-old code serving requests with no obvious error. Confirmed live: this
   exact sequence left a stale process running for hours, only caught when a
   genuinely new route 404'd. `Get-Process python` also doesn't reliably show
   Task-Scheduler-launched processes from an unrelated shell's session context, so
   don't use it to check either.

   ```powershell
   Stop-ScheduledTask -TaskName "acquisitions-dashboard"
   Start-Sleep -Seconds 2
   try { Invoke-WebRequest -Uri "http://127.0.0.1:8099/health" -TimeoutSec 3 -UseBasicParsing | Out-Null; Write-Output "STILL UP -- did not actually stop" }
   catch { Write-Output "STOPPED (expected)" }
   ```

2. **Start it back up.**

   ```powershell
   Start-ScheduledTask -TaskName "acquisitions-dashboard"
   ```

   Startup isn't instant -- `lifespan()` runs one full synchronous poll cycle across
   every source before the server starts accepting connections, which can take
   15-20+ seconds (longer if any source is slow/timing out that cycle). Don't
   conclude a restart failed from a health check a few seconds in; poll instead of a
   single fixed sleep:

   ```bash
   until curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8099/health 2>/dev/null | grep -q 200; do sleep 2; done
   echo "UP"
   ```

3. **Confirm it's genuinely a fresh process**, not the health check passing against
   something already up from before. Tail `<CONFIG_ROOT>\dashboard\dashboard.log`
   (e.g. `D:\appdata\dashboard\dashboard.log`) and confirm the newest `Started server
   process [PID]` / `Application startup complete.` lines are more recent than when
   you started this restart -- a PID from hours ago there means the stop didn't
   actually take effect.

4. **If it's reachable via Caddy** (`stats.{$DOMAIN}`), confirm that path too, since a
   process bound only to `127.0.0.1` instead of `0.0.0.0` would pass step 2 but still
   be unreachable from `host.docker.internal`:

   ```bash
   curl -s http://stats.correll.tv/health   # LAN
   ```

## Why not just edit and let it pick up changes

There's no file-watcher/reload wired in (`uvicorn.run(..., reload=False)` implicitly,
since `reload=True` isn't set in `run.py`) -- deliberately, since the production path
is a Task Scheduler task, not a dev server. Always restart after any change under
`dashboard/`.

## If the task isn't registered yet (fresh setup only)

The steps above assume `acquisitions-dashboard` already exists as a Task Scheduler
task (see README section 10's Setup for `Register-ScheduledTask`, which needs an
elevated session to run once). Before that registration exists, there's nothing for
`Stop-ScheduledTask` to target -- fall back to running it directly for
development (`cd` to the repo root, `py -m dashboard.run`, foreground or
backgrounded), and register the real task once you're done iterating.
