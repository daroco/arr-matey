---
name: new-machine-setup
description: Guide a fresh Windows + Docker Desktop machine through full setup of this stack, from nothing to a fully wired, running system. Use when setting this repo up on a new/different machine, or reinstalling from scratch.
---

# Set up this stack on a new machine

This doesn't replace `scripts/setup.py` or `scripts/provision.py` — it orchestrates
them and picks up exactly where they explicitly stop. `setup.py` only writes `.env`;
`provision.py` only wires up API connections between already-running containers.
Everything in between (bringing the stack up in the right order) and everything after
(the handful of steps that need real human credentials or judgment) is what this skill
actually walks through. README.md is the full narrative version of every step below —
this is the condensed, sequenced, "do this now" version for an agent to execute.

## Before starting: figure out which branches apply

Ask the user (skip anything they've already told you):
1. **`DOWNLOAD_MODE`**: `seedbox` (needs a seedbox subscription, more setup, proper
   ratio management) or `local` (a qBittorrent container does the downloading, zero
   extra cost, not suitable for ratio-obligated private trackers). See README section 1.
2. **Public exposure**: should Jellyfin/Seerr be reachable from outside the LAN
   (port-forward + real domain + Caddy TLS, README section 6), or LAN-only?
3. **Domain**: a real domain they already own (needs Cloudflare + Pi-hole for LAN-wide
   resolution, or works fine LAN-only without Pi-hole via hosts-file entries) or the
   free `<lan-ip>.nip.io` fallback (zero setup, works immediately, LAN-only by nature)?
4. **The trace dashboard** (`dashboard/`, README section 10) — worth it if they want
   plain-language stall diagnoses instead of manually tracing a stuck request through
   four apps. Optional, can be added later.

`setup.py` (next step) asks most of these again in more detail — this is just enough
to know which sections below to skip.

## Steps

1. **Prerequisites** (README section 1): Docker Desktop installed, with the drive that
   will hold `MEDIA_ROOT`/`CONFIG_ROOT` enabled under Settings → Resources → File
   Sharing. Seedbox mode additionally needs `winget install Rclone.Rclone` on the host.

2. **Write `.env` — have the user run this themselves, not you.** It's a real
   interactive wizard (`input()` prompts, no non-interactive flags), so it needs to run
   in a session with real stdin, not a background tool call. Tell them:
   ```
   ! python scripts/setup.py
   ```
   (the `!` prefix runs it in their own terminal session, not yours). Wait for them to
   confirm it's done before continuing — don't try to answer its prompts for them.

3. **Read the resulting `.env`** yourself once it exists. This is now the source of
   truth for every branch below — `DOWNLOAD_MODE`, `DOMAIN`, `MEDIA_ROOT`,
   `CONFIG_ROOT`, whether seedbox or VPN vars are filled in — rather than re-asking the
   user things `setup.py` already captured.

4. **Create the host directories** if they don't already exist:
   `${MEDIA_ROOT}/movies`, `${MEDIA_ROOT}/tv`, `${MEDIA_ROOT}/downloads`,
   `${CONFIG_ROOT}` — all as subfolders of the *same* `MEDIA_ROOT`/`CONFIG_ROOT`, not
   separate drives/mounts (the hardlink warning in README section 9 explains why this
   matters — verify with the user they understand this before proceeding if they
   deviated from `setup.py`'s defaults).

5. **Bring the stack up**:
   ```bash
   docker compose up -d
   ```
   Local mode + VPN wanted: layer the overlay instead —
   `docker compose -f compose.yaml -f compose.vpn.yml up -d`.

6. **`local` mode only — qBittorrent's one-time first-run step, before `provision.py`**
   (README "Local mode" section): the image ignores `QBT_USER`/`QBT_PASS` from `.env`
   entirely and generates a random temp password on first boot instead.
   - `docker logs qbittorrent` → find `A temporary password is provided for this
     session: <password>`
   - Log into `http://localhost:8080` as `admin` with that password
   - Options → Web UI → Authentication: set the password to exactly `.env`'s `QBT_PASS`
   - Options → Downloads: set Default Save Path to `/media/downloads` — **not
     optional**, the image's own default isn't under the `${MEDIA_ROOT}:/media` mount,
     which silently breaks hardlinking the same way splitting the mount does.

7. **Wire up API connections**:
   ```bash
   pip install -r scripts/requirements.txt
   python scripts/provision.py
   ```
   Idempotent, safe to re-run. This handles far more than its README description
   suggests — Prowlarr↔Sonarr/Radarr, download client creation, **indexer routing by
   `downloadClientId`**, Bazarr connections, and (seedbox mode) **Remote Path
   Mappings and the seedbox's own client ratio/privacy settings**. Confirm it exits
   clean; re-run once if indexer sync briefly overwrote something (expected, it
   self-heals within the same run per its own README section).

8. **The genuinely manual steps** — `provision.py` deliberately skips these three
   because they need real credentials or human judgment, not because it forgot:
   - **Prowlarr indexer accounts** (`:9696` → Indexers → +): the user's own
     tracker/indexer accounts. Nothing to automate here.
   - **FlareSolverr tagging** for Cloudflare-protected indexers (README section 4,
     step 1): add the proxy under Indexer Proxies with a tag, then add that same tag
     to each protected indexer. A proxy with no tag shows "Disabled" even if it tests
     fine — that's normal, not a bug.
   - **Seerr's initial Jellyfin connection** (`:5055`, README section 4 step 5): needs
     a live Jellyfin admin login to establish. Jellyfin URL is `http://jellyfin:8096`
     (container-to-container — Jellyfin is a `compose.yaml` service, not a host
     process, so this is *not* `host.docker.internal`). Also add Sonarr/Radarr as
     request targets here (hostname + API key + root folders `/media/tv` /
     `/media/movies`) if `provision.py`'s wiring didn't already cover it.

9. **Public exposure wanted?** (README section 6, or use the `expose-service` skill
   for the exact pattern once the first hostname is live):
   - Register the domain with Cloudflare, create the `CF_API_TOKEN` (`Zone:DNS:Edit`,
     scoped to the one zone), fill in `.env`'s `CF_API_TOKEN`/`ACME_EMAIL`/`CF_ZONE`/
     `DDNS_RECORDS`.
   - Free port 443 on the host if something else already owns it (`docker ps`, check
     for existing `443` publishers).
   - `docker compose up -d --build caddy` (note `--build` — this uses
     `Dockerfile.caddy`, not stock `caddy:latest`). Check
     `docker logs caddy` for `certificate obtained successfully`.
   - Forward TCP 443 only at the router (never 80) to this machine's reserved LAN IP.
   - Schedule `scripts/ddns-update.py` (step 11 below covers Task Scheduler).
   - Point Jellyfin's own Dashboard → Networking → Known Proxies at Caddy's address,
     and set Published Server URLs, per README section 6 step 9.

10. **Real domain + LAN-wide resolution wanted** (skip entirely if using nip.io, which
    already resolves LAN-wide with zero setup): needs Pi-hole as the network's DNS
    (README section 5) — point the router at Pi-hole, add a Local DNS record per
    hostname, free up port 80 on the Pi-hole container if it also wants it, reserve
    this PC's LAN IP. If a hostname works everywhere except the machine running the
    stack itself, that's a known Docker Desktop loopback quirk — use hosts-file
    entries for that one machine rather than chasing it (README section 5's own
    hosts-file steps).

11. **Register scheduled tasks** — needs an **elevated** PowerShell session; tell the
    user to open one rather than attempting it from a standard session (a failed
    `Register-ScheduledTask` with "Access is denied" means this). All scheduled
    automation in this repo runs via `pythonw.exe`, never a bare `.exe` or
    `python.exe`, so no console window flashes on each run:
    - **`seedbox` mode**: `scripts/rclone-sync.py` and `scripts/seedbox-cleanup.py`
      (every few minutes / every 30 minutes — README section 9 steps 7–8). `local`
      mode needs neither (qBittorrent already writes straight into
      `${MEDIA_ROOT}/downloads`).
    - **Public exposure**: `scripts/ddns-update.py`, every few minutes.
    - **Dashboard**, if wanted (step 12): its own At-log-on task.

12. **Trace dashboard, if wanted** (README section 10 setup steps 1–7): install its
    requirements, optionally add `stats.{$DOMAIN}` the same way as step 9 above (its
    own DNS record + `DDNS_RECORDS` entry), then register its Task Scheduler task
    (At-log-on trigger, no execution time limit — different shape from the periodic
    tasks in step 11, see README for the exact `Register-ScheduledTask` invocation).
    Once registered, restarting it after any future code change needs the
    `dashboard-restart` skill (`Stop-ScheduledTask`/`Start-ScheduledTask`, not
    `Stop-Process`).

## Verification

Don't consider this done until each of these is actually confirmed, not assumed:

1. `docker compose ps` — every expected service `Up`, none crash-looping.
2. Root folders set in Sonarr/Radarr (Settings → Media Management) — Seerr's own root
   folder dropdown mirrors these and stays empty until they exist.
3. Prowlarr → Sonarr/Radarr app sync is green, indexers appear in both apps.
4. **Seedbox mode only**: `docker exec sonarr curl http://caddy:8090/qbittorrent/api/v2/app/version`
   succeeds with no credentials (confirms the Caddy Basic-Auth shim reaches the
   seedbox) — same check for Transmission's `/rpc` (expect `409` + session-id, not a
   connection error, that's its normal handshake).
5. Submit one real test request in Seerr and watch it actually reach Pending →
   Processing — if the trace dashboard is set up, its "Never grabbed" rule doubles as
   a live check that Sonarr/Radarr can find real releases at all.
6. Every LAN hostname resolves and shows that app's login page (a `307`/redirect to
   `/login`, not a connection error — see the Troubleshooting entry on this in README).
7. **Public exposure only**: confirm from a genuinely external vantage point (phone on
   cellular, not the LAN — see `diagnose-connectivity` skill), not just from the LAN.
8. Reboot the host once and confirm every container comes back on its own
   (`restart: unless-stopped` + Docker Desktop's "Start when you log in" setting,
   README section 7) and every registered scheduled task is still present.
