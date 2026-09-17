# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Docker Compose home-lab stack (Windows + Docker Desktop, host paths on `D:`) that
automates movie/TV requests end-to-end: **Seerr** (request UI, container still named
`jellyseerr` — the project renamed from Jellyseerr to Seerr, same app/config) → **Sonarr /
Radarr** (grab logic) → **Prowlarr** (indexer aggregation, **FlareSolverr** solves
Cloudflare-protected indexers) → a download client actually torrents it → finished files
land in the media library → **Jellyfin** (a `compose.yaml` service, `lscr.io/
linuxserver/jellyfin`, pinned rather than `:latest` — see the service's own comment for
why) serves it → **Bazarr** backfills subtitles. **Caddy** fronts everything with clean
hostnames instead of `ip:port`. Jellyfin was migrated from a native Windows install into
this stack; real users/library/watch-history carried over via a one-time data migration
(see the `jellyfin` service's comment in `compose.yaml` and the container's own
`/config/data/` layout, which nests `data`/`metadata`/`plugins`/`root` one level deeper
than the native Windows layout did — a real gotcha if this ever needs redoing).

`compose.yaml` also has services outside that pipeline entirely: **`satisfactory`**
(`wolveix/satisfactory-server`), a game server added the same way as everything else
here but exposed a completely different way — see "Non-HTTP services" below — and
**`romm`** + **`romm-db`** (RomM, a retro game library at `games.<domain>`), an HTTP app
on the normal Caddy route with a few deliberate deviations from house style, see below.

Mostly infrastructure config (`compose.yaml`, `Caddyfile`, `.env`) plus standalone Python
automation scripts in `scripts/`, plus one real application: `dashboard/`, a FastAPI app
that traces a Seerr request across the whole pipeline (see its own section below).
README.md and ARCHITECTURE.md are long, current, and authoritative — **read them before
assuming anything about the stack's shape**; this repo reshapes often (download-client
architecture, public routes, etc. have all changed significantly across sessions) and
stale assumptions from memory/prior conversations should be verified against the actual
files, not trusted.

The installed Python is **3.9** (no `.python-version` pinned anywhere, but it's what
`py`/`pythonw.exe` actually resolve to on this box) — no `str | None` union syntax
without `from __future__ import annotations` at the top of the file, no other 3.10+-only
syntax.

## Commands

```bash
# First-time setup: interactive wizard, writes .env (stdlib only, no deps needed yet)
python scripts/setup.py

# Bring the stack up (add -f compose.vpn.yml for local-mode VPN, see below)
docker compose up -d

# Rebuild Caddy specifically after editing Dockerfile.caddy or adding a plugin
docker compose up -d --build caddy

# One-shot idempotent API wiring (Prowlarr->Sonarr/Radarr, download clients, indexer
# routing, Bazarr, Seerr) -- safe to re-run any time
pip install -r scripts/requirements.txt
python scripts/provision.py

# Validate a Caddyfile edit BEFORE touching the live container (must use the custom
# acquisitions-caddy image, not stock caddy:latest, or the cloudflare/ratelimit modules
# won't be registered and adapt fails with an unrelated-looking error). On Windows
# Git Bash, prefix with MSYS_NO_PATHCONV=1 or the container-side path gets mangled into
# a Windows path.
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile" \
  --env-file .env acquisitions-caddy:latest caddy fmt --overwrite /etc/caddy/Caddyfile
MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile" \
  --env-file .env acquisitions-caddy:latest caddy adapt --config /etc/caddy/Caddyfile

# Apply a validated Caddyfile change to the running container without recreating it
# (docker compose up -d alone is a no-op here -- only the bind-mounted file changed,
# not the service definition, so Compose won't restart it)
MSYS_NO_PATHCONV=1 docker exec caddy caddy reload --config /etc/caddy/Caddyfile

# Dry-run the seedbox cleanup job before trusting it
python scripts/seedbox-cleanup.py --dry-run

# Run the trace dashboard (needs -m -- it's a package with relative imports, unlike
# the standalone scripts/*.py files; a bare file path fails with an import error)
pip install -r scripts/requirements.txt -r dashboard/requirements.txt
python -m dashboard.run
```

Scheduled scripts (`ddns-update.py`, `rclone-sync.py`, `seedbox-cleanup.py`) run via
Windows Task Scheduler calling `pythonw.exe` (not `python.exe` or a raw `.exe`) so no
console window flashes on each run — this is a deliberate, repo-wide convention, not
per-script. All scheduled automation in this repo is Python for the same reason (see
README section 9).

## Architecture

**Two selectable download-client modes**, switched via `DOWNLOAD_MODE` in `.env` (drives
`scripts/provision.py`'s API wiring) and the separate, Compose-native `COMPOSE_PROFILES`
(actually starts/stops containers) — **the two must stay in sync**; `provision.py`
refuses to run if they disagree.
- **`seedbox`** (default): nothing downloads locally. Two *separate* client instances run
  on a remote seedbox — qBittorrent for a private Hit&Run tracker (DHT/PEX/LSD off, strict
  ratio/seed-time), Transmission for every other indexer (DHT/PEX/LPD on, looser policy).
  Two real client processes exist because those DHT/PEX/LSD settings are per-*instance* in
  qBittorrent, not per-download-client-entry — one instance can't be "off" for one tracker
  and "on" for another. Finished files sync back via a scheduled `rclone` job over SFTP;
  Sonarr/Radarr import via Remote Path Mapping. See README section 9 (long — covers Caddy
  Basic-Auth shim, indexer routing gotchas, seeding policy, cleanup).
- **`local`**: a single `qbittorrent` container in `compose.yaml` (gated behind the
  `local` Compose profile) downloads directly into the shared media volume. No seedbox
  cost, but no VPN by default and no ratio management. Optional VPN via layering
  `compose.vpn.yml` on top (`docker compose -f compose.yaml -f compose.vpn.yml up -d`),
  which reroutes qBittorrent through Gluetun.

**One thing every mode shares and must not violate**: `${MEDIA_ROOT}` is mounted as
*one* Docker volume (`movies/`, `tv/`, `downloads/` as real subfolders under it), never
three separate volume mounts. Sonarr/Radarr's hardlink import can't cross a Docker mount
boundary — split them and hardlinking silently degrades to full copies (no error, just
doubled disk usage forever). This is the single most-repeated warning in README.

**Caddy is the routing/TLS backbone**, built from `Dockerfile.caddy` (not stock
`caddy:latest`) via `xcaddy` to add `caddy-dns/cloudflare` and `mholt/caddy-ratelimit`.
The `Caddyfile` has three distinct route classes:
1. **LAN-only, plain HTTP** — every `*.<domain>` service (Sonarr, Radarr, Prowlarr,
   Bazarr, qBittorrent-if-local). Resolved LAN-wide via Pi-hole local DNS records (a
   separate compose project, not managed here) pointing at the host's LAN IP. Guarded by
   a `lanonly` snippet (`not remote_ip private_ranges` → 403) as defense-in-depth, though
   the real control is that port 80 is never forwarded at the router.
2. **Public HTTPS** — exactly four hostnames (`watch.<domain>` → the `jellyfin` service,
   `<domain>` apex → Seerr, `stats.<domain>` → the trace dashboard, `games.<domain>` →
   RomM; `jellyseerr.<domain>`
   used to also route to Seerr as a pre-rename leftover, retired as redundant), each
   with a real Let's Encrypt cert
   via the **DNS-01** challenge (`acme_dns cloudflare`, needs `CF_API_TOKEN`) specifically
   *because* it only needs port 443 forwarded — port 80 stays unforwarded permanently, so
   every LAN-only route is unreachable from outside by construction. Cloudflare-proxied
   (orange cloud) in the reference deployment, which means `trusted_proxies` (Cloudflare's
   published IP ranges, hardcoded in the global options block, **not** auto-synced) plus
   `client_ip_headers CF-Connecting-IP` are required to recover real visitor IPs — used
   both for Jellyfin/Seerr's own client-IP logging and for the `rate_limit` zones on these
   blocks (300 req/min general, 10 req/min on each app's login endpoint, keyed on
   `{client_ip}` not `{remote_host}`). Both apps gate every request behind their own login
   already (no anonymous access); Seerr can authenticate against Jellyfin's own accounts
   (`mediaServerLogin`), so household credentials work for both without extra setup.
3. **Internal-only** (`:8090`, not published to the host) — a Basic-Auth injection shim
   so Sonarr/Radarr can talk to the seedbox's clients without any per-client-type field
   for a Basic Auth layer distinct from the client's own login. Proxies by host, not
   path, so one shim serves both qBittorrent and Transmission.

**Non-HTTP services (e.g. `satisfactory`) skip Caddy entirely.** This build of Caddy
(`Dockerfile.caddy`) only has the `caddy-dns/cloudflare` and `mholt/caddy-ratelimit`
xcaddy plugins — no L4/TCP-proxy module — and its `servers` block is pinned to
`protocols h1 h2` (HTTP only), so raw TCP/UDP traffic (a game server, anything that
isn't speaking HTTP) physically cannot route through it. The pattern for these is
publish the port(s) directly in `compose.yaml` and forward them at the router, same as
the four public HTTPS hostnames' 443 forward but for whatever ports the service
actually needs — no Cloudflare DNS record, no Caddyfile change, no rate limiting or
IP-hiding (a real tradeoff worth knowing: unlike the HTTPS routes, a directly
port-forwarded service has none of Cloudflare's WAF/origin-hiding). See the global
`self-host-service` skill (`~/.claude/skills/`, not repo-scoped) for the general
version of this decision — HTTP-via-reverse-proxy vs. direct-port-forward — for
whatever gets added next.

**`romm` + `romm-db` (RomM, retro game library) is the other non-pipeline service**, and
the worked example of the *other* branch of that decision: it's a plain HTTP web app, so
it takes the normal Caddy + Cloudflare route (`games.<domain>`, README's RomM section).
Things that deliberately differ from every other service here: the MariaDB data is a named
volume (`romm_db`), not a `${CONFIG_ROOT}` bind mount (InnoDB on a Windows bind mount is a
corruption trap — backup is a `mariadb-dump`, not a folder copy); the image is major-pinned
(`rommapp/romm:5`, it auto-migrates its DB on start); RomM's filesystem watcher is off in
favour of a nightly scheduled rescan (same NTFS/inotify boundary as Jellyfin's realtime
monitor, below); host port is 8085 because 8080 is local-mode qbittorrent's. The library is
`${ROMS_ROOT}` (`D:/Roms`, separate from `MEDIA_ROOT`), laid out `roms/<slug>/` +
`bios/<slug>/`; non-slug folder names (the libretro-style `bios/` folders) are mapped in
`${CONFIG_ROOT}/romm/config/config.yml`. Docker Desktop resolves the bind-mounted Windows
path case-insensitively (verified with a throwaway container), so `Roms/` on disk satisfies
RomM's literal lowercase `roms` check — don't "fix" the casing, and don't try to rename it
while a download into it is running (access denied, seen live).

`scripts/` — all standalone, stdlib-plus-`requirements.txt` (`requests`,
`python-dotenv`, `PyYAML`), each reads `.env` directly rather than relying on shell
env vars:
- `setup.py` — interactive `.env` wizard, no dependencies, doesn't touch Docker/APIs.
- `provision.py` — idempotent bootstrap wiring every *arr connection via API, including
  Sonarr/Radarr's *native* Jellyfin/Emby connection (`MediaBrowser` notification,
  `configure_arr_jellyfin_connection`) so each app tells Jellyfin directly to update its
  library on import/upgrade — needs `JELLYFIN_API_KEY` in `.env` (created by hand in
  Jellyfin's own Dashboard > API Keys, same one-time-manual-step reason as Seerr's
  initial Jellyfin connection). Gated on that key being set; skips with a warning
  otherwise. This replaced an earlier custom dashboard webhook for the same purpose —
  prefer this native connection over reinventing that, if it's ever missing again.
- `rclone-sync.py` / `seedbox-cleanup.py` — seedbox-mode-only scheduled tasks.
- `ddns-update.py` — keeps the three public Cloudflare A records pointed at the current
  WAN IP (`DDNS_RECORDS`, comma-separated); per-record error handling so one failure
  doesn't block the others, only raises/notifies after trying all of them.

**`dashboard/`** (README section 10, ARCHITECTURE diagram 8) — the one real application
in this repo, not a script. Runs as a host process (Task Scheduler, `python -m
dashboard.run`), not a container, since two of its fix-action buttons need real
`subprocess` access to the Windows-only `scripts/rclone-sync.py`/`seedbox-cleanup.py`.
Login is Jellyfin-credential delegation (same mechanism Seerr itself uses — forwards the
submitted username/password to Jellyfin's own `AuthenticateByName`, trusts its answer);
any Jellyfin account can view traces, only accounts where Jellyfin reports
`Policy.IsAdministrator` can run fix actions. Correlation engine
(`dashboard/correlate.py`) joins Seerr → Sonarr/Radarr → torrent client on `downloadId`
(the grab's torrent infohash, lowercased) — the same primitive
`seedbox-cleanup.py`'s `fetch_imported_hashes()` already uses. `dashboard/rules.py`'s
suppressor rule (a torrent paused at its seed target *and* already imported = healthy,
not stalled) is the single most important rule in the set — getting it backwards makes
the dashboard cry wolf on every successful download, since that paused state is this
stack's intended terminal state, not a problem (see the `max_ratio_act=0` warning in
README section 9). The request list's "stalled only" filter runs this same rule
evaluation live across every matched request, concurrently but capped (8 at a time) --
it's genuinely slow (tens of seconds to a couple minutes), not reading a cache, since it's
doing a real deep-trace per request. When a new stuck-request pattern shows up that isn't
already explained by an existing rule, use the `dashboard-add-rule` skill rather than just
fixing that one instance by hand -- four real cases (season-number mismatch, manual
import required, never grabbed, unextracted RAR archive) already went through that exact
loop. `dashboard/sweep.py` runs this same rule evaluation on its own background schedule
(`DASHBOARD_NOTIFY_POLL_SECONDS`) independent of the on-demand "stalled only" filter,
pushes a batched-per-title ntfy notification (see `notify.py`) the first time a diagnosis
appears, and mirrors every push in-app (bell icon + `/notifications`) — any rule added via
the skill above is automatically covered, nothing extra to wire up. Registered as the
`acquisitions-dashboard` Task Scheduler task; restarting after a code change needs
`Stop-ScheduledTask`/`Start-ScheduledTask`, not `Stop-Process` (see `dashboard-restart`
skill) — the process runs under Task Scheduler's own session context, so a plain
`Stop-Process` from an unrelated shell can silently fail to kill it.

`/library` is a second dashboard page, distinct from the per-request tracing above --
"library completeness," which shows/movies Sonarr/Radarr have marked monitored but
don't have a file for yet (`correlate.build_library_gaps`, purely from already-fetched
Snapshot data, no new API calls). Deliberately does **not** run a live release search
for every gap up front -- a single episode's search can take 10-30+ seconds and a show
can have hundreds of gaps -- the "why" for a specific show is an on-demand,
one-click action (`arr_diagnose_series_gap`/`arr_search_series`) from that page instead.
Not a total-inventory view of the whole library, just what's missing.

## Second deployment target: Synology NAS (`compose.nas.yml`)

Everything above describes the Windows + Docker Desktop host. The stack is being moved
to a Synology DS925+ (DSM 7.2, Btrfs, x86 Ryzen, **no iGPU** so still software
transcoding, 4 GB RAM stock) — README's "Running on a Synology NAS" section is the
runbook. **Check which host you're on before applying anything Windows-shaped from this
file**: `uname` answers it. What changes there, all of it confined to the
`compose.nas.yml` overlay (layered via `COMPOSE_FILE` in the NAS's `.env`) plus `.env`:
- **Caddy is on a macvlan network with its own LAN IP** (`CADDY_LAN_IP`), because DSM's
  nginx owns 80/443 on the NAS's address. Pi-hole records and the router's 443 forward
  point at that IP, not the NAS. The macvlan network's explicit name (`acq-lan`) is
  load-bearing: Docker < 28 gives a two-network container the default route of whichever
  network name sorts first, and it must be the LAN one or public HTTPS dies while LAN
  routes keep working. `docker exec caddy ip route` is the check. The Docker Desktop
  "every LAN client is 172.18.0.1" gotcha below does **not** apply there — Caddy sees
  real LAN addresses.
- **`dashboard/` and every `scripts/*.py` run inside the `dashboard` container**
  (`Dockerfile.dashboard`), not as host processes: no Task Scheduler, no `pythonw`, no
  host Python at all. Scheduled jobs are DSM Task Scheduler entries doing
  `docker exec dashboard python scripts/<name>.py`; restart after a code change is
  `docker compose restart dashboard` (repo is bind-mounted, no rebuild), so the
  `dashboard-restart` skill's Stop/Start-ScheduledTask dance is Windows-only.
  `${CONFIG_ROOT}`/`${MEDIA_ROOT}` are mounted into it at their *own host paths* so the
  host-path logic in `dashboard/config.py` and the scripts holds unmodified; the
  `*_BASE_URL` / `JELLYFIN_BASE_URL` / `DASHBOARD_UPSTREAM` overrides swap `localhost`
  and `host.docker.internal` for service names.
- **The filesystem is case-sensitive and inotify works.** RomM's library folder must be
  literally lowercase `roms/` (the "don't fix the casing" note above is a Docker Desktop
  fact, not a NAS one), and Jellyfin's realtime monitor / RomM's watcher are reliable
  there, unlike the NTFS bind-mount situation described below.
- `docker` needs `sudo` on DSM, and `MSYS_NO_PATHCONV=1` is meaningless (no Git Bash).
- Seerr's container ignores `PUID` and runs as UID 1000: its config folder needs
  `chown 1000:1000`, everything else `PUID:PGID` (1026:100 on DSM, not 1000:1000).

## Repo-specific skills

`.claude/skills/` has six skills for the recurring tasks this repo's own history keeps
needing: `caddy-reload`, `expose-service` (new public hostname), `dashboard-restart`,
`diagnose-connectivity` (systematic checklist for "can't connect to X" -- covers the VPN
hijacking / Cloudflare outage / hairpin-NAT gotchas below), `dashboard-add-rule`, and
`new-machine-setup` (orchestrates `scripts/setup.py` + `scripts/provision.py` plus
everything those two deliberately don't touch, for standing this stack up from scratch
on a different machine). Reach for these before re-deriving the same debugging path
from scratch.

## Known operational gotchas (not yet in README, worth knowing before debugging blind)

- **Docker Desktop on Windows collapses every LAN-sourced connection to a published
  container port into one internal bridge-gateway IP** (e.g. `172.18.0.1`), not the real
  LAN device's address. This matters anywhere `remote_ip`/`{client_ip}` is used for
  per-visitor logic (rate limiting, logging) on a route also reachable from the LAN —
  without a `not remote_ip private_ranges` guard, all local traffic shares one identity
  and can trip limits meant for individual internet visitors.
- **A system-wide VPN client on the host (e.g. ProtonVPN) silently hijacks any "what's my
  public IP" check** run from that machine — including `ddns-update.py`'s WAN-IP
  detection — if its route has a lower metric than the real network adapter. This
  corrupts DNS records with the VPN's exit IP instead of the real WAN IP, breaking public
  access with no obvious error. Check `route print -4` for a second `0.0.0.0` default
  route with metric `0` if DDNS behavior looks wrong; the fix is disconnecting the VPN
  (its own app, not just killing the GUI process — the tunnel/service can survive that).
  Happened for real on 2026-09-16 (ProtonVPN, all four records rewritten within one
  5-minute cycle, every public route 522). `ddns-update.py` now refuses to run while a
  VPN-looking default route exists (`vpn_default_routes()`, one ntfy push per VPN
  session via a `ddns-vpn-paused.flag` marker in `${CONFIG_ROOT}`) — a "DDNS paused"
  notification means exactly this, not a script failure. Still fix it by disconnecting.
- **Cloudflare's own API can have real outages independent of DNS/edge health** — check
  `cloudflarestatus.com` before assuming a local config problem when only API calls
  (not the actual proxied sites) are failing with `521`s or timeouts.
- **Caddy does not log successful requests to `docker logs` by default** — no `log`
  directive is configured anywhere in the `Caddyfile`, so only `http.log.error` entries
  ever show up. Zero log lines is *not* evidence that nothing reached Caddy; it just
  means nothing *failed*. Confirmed live: this produced a false "the origin is still
  unreachable" conclusion during a real debugging session, when the actual problem
  (a dead Spectrum modem, confirmed independently via the ISP's own status page) had
  already been fixed. Add a real `log` directive before trusting log-absence as a signal.
- **Jellyfin's `EnableRealtimeMonitor` (per-library, in each library's `options.xml`
  under `${CONFIG_ROOT}/jellyfin/data/root/default/<Library>/`) is unreliable on this
  stack specifically** because `${MEDIA_ROOT}` is a raw Windows NTFS drive bind-mounted
  into a Linux container via Docker Desktop — a boundary `inotify` events don't reliably
  cross. It's enabled on both libraries as of this writing, but Sonarr/Radarr's *native*
  Jellyfin/Emby connection (see `provision.py` above) is the reliable path; real-time
  monitoring is a backup, not the primary mechanism.
- **Always check for an active Jellyfin session (`docker logs jellyfin` for recent
  `SessionManager`/transcode activity) before restarting the `jellyfin` container** —
  restarting it drops any in-progress playback with no warning to the viewer. This has
  actually happened (a live session got cut mid-transcode by a routine restart). A
  restart also takes several minutes to fully come back (keyframe/chapter extraction
  runs on boot for a library this size) — expect `503`s from `/System/Ping` during that
  window, not a crash.
