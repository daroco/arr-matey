# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A Docker Compose home-lab stack (Windows + Docker Desktop, host paths on `D:`) that
automates movie/TV requests end-to-end: **Seerr** (request UI, container still named
`jellyseerr` — the project renamed from Jellyseerr to Seerr, same app/config) → **Sonarr /
Radarr** (grab logic) → **Prowlarr** (indexer aggregation, **FlareSolverr** solves
Cloudflare-protected indexers) → a download client actually torrents it → finished files
land in the media library → the user's existing **host-installed Jellyfin** (not part of
this stack, untouched) serves it → **Bazarr** backfills subtitles. **Caddy** fronts
everything with clean hostnames instead of `ip:port`.

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
2. **Public HTTPS** — exactly four hostnames (`watch.<domain>` → host Jellyfin,
   `<domain>` apex + `jellyseerr.<domain>` → Seerr, `stats.<domain>` → the trace
   dashboard), each with a real Let's Encrypt cert
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

`scripts/` — all standalone, stdlib-plus-`requirements.txt` (`requests`,
`python-dotenv`, `PyYAML`), each reads `.env` directly rather than relying on shell
env vars:
- `setup.py` — interactive `.env` wizard, no dependencies, doesn't touch Docker/APIs.
- `provision.py` — idempotent bootstrap wiring every *arr connection via API.
- `rclone-sync.py` / `seedbox-cleanup.py` — seedbox-mode-only scheduled tasks.
- `ddns-update.py` — keeps the four public Cloudflare A records pointed at the current
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
fixing that one instance by hand -- three real cases (season-number mismatch, manual
import required, never grabbed) already went through that exact loop.

## Repo-specific skills

`.claude/skills/` has five skills for the recurring tasks this repo's own history keeps
needing: `caddy-reload`, `expose-service` (new public hostname), `dashboard-restart`,
`diagnose-connectivity` (systematic checklist for "can't connect to X" -- covers the VPN
hijacking / Cloudflare outage / hairpin-NAT gotchas below), and `dashboard-add-rule`.
Reach for these before re-deriving the same debugging path from scratch.

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
- **Cloudflare's own API can have real outages independent of DNS/edge health** — check
  `cloudflarestatus.com` before assuming a local config problem when only API calls
  (not the actual proxied sites) are failing with `521`s or timeouts.
