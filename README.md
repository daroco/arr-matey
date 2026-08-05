# Self-Hosted Media Automation Stack

A Docker Compose stack that lets you request a movie or TV show and have it automatically
searched, downloaded, and dropped into your media library for Jellyfin.

See `ARCHITECTURE.md` for diagrams of the pieces below and how they connect.

**Flow:** Seerr (request) → Sonarr/Radarr (grab logic) → Prowlarr (indexer search,
with FlareSolverr for Cloudflare-protected sites) → a download client actually does the
torrenting → finished files land in your movies/TV folders → your existing host Jellyfin
serves them → Bazarr backfills subtitles. Caddy fronts everything with clean local
hostnames instead of `ip:port`.

**Two selectable download-client modes**, via `DOWNLOAD_MODE` in `.env`:
- **`seedbox`** (default) — everything downloads on a remote seedbox: two separate
  clients there, qBittorrent for a private Hit & Run tracker and Transmission for
  everything else, each with independent ratio/seed-time/DHT-PEX-LSD settings, synced
  back locally via a scheduled rclone job. Needs a seedbox subscription. See section 9.
- **`local`** — a single qBittorrent container in this compose file does the actual
  downloading, no seedbox needed, zero recurring cost. No VPN by default and no ratio
  management — see the Local mode section after section 9.

Run `python scripts/setup.py` for a guided setup (asks which mode, your domain, etc. and
writes `.env` for you) instead of hand-editing `.env.example` — see section 2.

Seerr was Jellyseerr until the project merged with Overseerr and renamed itself — same
app, same config/database, just a new image (`ghcr.io/seerr-team/seerr`) and container
still named `jellyseerr` in this compose file for continuity.

Jellyfin itself is **not** part of this stack — it's assumed to already be running on the
host, untouched.

Built for Windows + Docker Desktop, with host paths on a `D:` drive. Adjust paths if your
setup differs.

---

## Stack components

`<domain>` below is whatever you set `DOMAIN` to in `.env` (your own domain, or a free
`<lan-ip>.nip.io` — see section 2).

| Service | Purpose | Port | LAN hostname (via Caddy) |
|---|---|---|---|
| Prowlarr | Indexer aggregator — searches configured indexers, pushes results to Sonarr/Radarr | 9696 | `prowlarr.<domain>` |
| FlareSolverr | Solves Cloudflare challenges on Prowlarr's behalf for protected indexers | 8191 | — |
| Sonarr | TV show search/grab/organize | 8989 | `sonarr.<domain>` |
| Radarr | Movie search/grab/organize | 7878 | `radarr.<domain>` |
| Bazarr | Subtitle fetching for Sonarr/Radarr libraries | 6767 | `bazarr.<domain>` |
| Seerr | Request front-end — search a title, hit request, it flows to Sonarr/Radarr | 5055 | `jellyseerr.<domain>` |
| Caddy | Reverse proxy — drops port numbers, gives every service above a clean hostname, (internal-only) injects Basic Auth for the seedbox, and terminates the public HTTPS route to Jellyfin | 80, 443 | `watch.<domain>` also routes here to the host Jellyfin install, over both LAN HTTP and public HTTPS — see section 6 |
| qBittorrent | **`local` mode only** — the actual torrent client, no seedbox | 8080 | `qbittorrent.<domain>` |

In `seedbox` mode the actual torrent clients (qBittorrent and Transmission) run on the
remote seedbox, not in this compose file — see section 9. In `local` mode, the
`qbittorrent` row above is the download client (see the Local mode section after
section 9).

Games are intentionally **not** included — there's no mature Sonarr/Radarr-equivalent for
game libraries. Prowlarr's own search UI plus a manual grab on the seedbox works in the
meantime.

---

## 1. Prerequisites

- Docker Desktop, with the drive holding your media enabled under
  **Settings > Resources > File Sharing**
- Decide your `DOWNLOAD_MODE` (section 2) — this determines the rest of this list:
  - **`seedbox`:** a seedbox with qBittorrent and Transmission installable from its app
    catalog, SFTP access (SSH shell access is *not* required — see section 9), and
    enough disk space to hold in-flight downloads for every tracker you use; rclone
    installed on the host (`winget install Rclone.Rclone`) to sync finished seedbox
    downloads down locally (section 9)
  - **`local`:** nothing extra — the download client is a container in this stack (see
    the Local mode section after section 9). Optionally, a VPN provider with a
    WireGuard config if you want one (`compose.vpn.yml`, same section) — not required.
- A domain of your own (or the free `<lan-ip>.nip.io` fallback, no signup needed — see
  section 2) for clean hostnames instead of `ip:port`. Pi-hole (or another local DNS
  resolver) is only needed if you want a real owned domain to resolve LAN-wide rather
  than relying on nip.io or per-machine hosts-file entries — see section 5.

---

## 2. Initial setup

**Recommended:** answer a few questions and let it write `.env` for you:

```bash
python scripts/setup.py
```

It asks for `MEDIA_ROOT`/`CONFIG_ROOT`, your domain (or offers a free `<lan-ip>.nip.io`
if you don't have one), which `DOWNLOAD_MODE` you want, and the matching seedbox or
local-qBittorrent details, then prints the exact next steps to run.

**Or by hand:**

```bash
cp .env.example .env
```

Fill in:

- `MEDIA_ROOT`, `CONFIG_ROOT` — your actual host folders (e.g. `D:/media`, `D:/appdata`).
  `MEDIA_ROOT` must contain `movies/`, `tv/`, and `downloads/` as subfolders of that
  **one** directory — not three separate folders you point three separate env vars at.
  This isn't just tidiness: Sonarr/Radarr's hardlink import (`copyUsingHardlinks`, on by
  default) can only link across a single Docker bind mount. Split them into separate
  mounts and hardlinking silently falls back to full copies — no error, just double disk
  usage on every import (this bit us once; see the Warnings in section 9). This applies
  whether you're in `seedbox` or `local` mode.
- `DOWNLOAD_MODE` (`seedbox` or `local`) and matching `COMPOSE_PROFILES` (blank, or
  `local`) — see the intro above for what differs. Keep the two in sync;
  `scripts/provision.py` checks this itself and refuses to run if they disagree.
- `DOMAIN` — your own domain (pointed at your LAN via Pi-hole, see section 5) or a free
  `<lan-ip>.nip.io` if you don't have one.
- **`seedbox` mode:** `SEEDBOX_URL`/`HOST`/`USER`/`PASS`/`BASIC_AUTH` — see section 9 for
  how these get used (Caddy shim + rclone remote).
- **`local` mode:** `QBT_CATEGORY` — see the Local mode section after section 9.

Create the host directories if they don't already exist, and point your existing Jellyfin
install's libraries at `MEDIA_ROOT/movies` and `MEDIA_ROOT/tv` — that's the only link
between this stack and Jellyfin.

```bash
docker compose up -d
```

If you're in `local` mode, do the one-time qBittorrent first-run step now (see the Local
mode section after section 9) before continuing — `provision.py` needs to be able to
reach it.

Once every container has started **at least once** (so each app has generated its own
config file/API key on disk), the rest of sections 4 and 9 — Prowlarr's connections to
Sonarr/Radarr, the download client(s), indexer routing, Bazarr's connections, Seerr's
connections, and (seedbox mode) Remote Path Mappings and the seedbox's own
qBittorrent/Transmission ratio and privacy settings — can be wired up in one shot instead
of by hand:

```bash
pip install -r scripts/requirements.txt
python scripts/provision.py
```

It's idempotent (safe to re-run any time — after recreating a container wipes a download
client, after rotating the seedbox password, whatever) and self-verifying rather than
destructive: everything is create-if-missing or set-to-desired-state, and it re-checks
indexer routing until it actually observes a stable result rather than trusting a single
pass (touching Prowlarr's Applications connection reliably kicks off a background sync
that briefly overwrites Sonarr/Radarr-side fields the script just set — expected, and it
self-heals within the same run). It deliberately does **not** touch three things that
need your own credentials or judgment: adding actual indexer/tracker accounts to
Prowlarr, Seerr's initial Jellyfin connection (needs Jellyfin admin login), and tagging
specific Cloudflare-protected indexers with the FlareSolverr proxy it creates. Read
sections 4 and 9 below regardless if you want to understand *what* it's doing and why —
the script is that same wiring encoded, not a black box.

---

## 3. Tuning Sonarr/Radarr's indexer behavior

**Minimum Seeders:** if grabs keep landing on releases with almost no seeders, the fix
isn't in the download client — it's a per-indexer setting in Sonarr/Radarr. Settings >
Indexers > edit an indexer > toggle "Show Advanced" (top right) > **Minimum Seeders**
(default `1`, i.e. no real filtering). Raising it to `3`–`5` rejects thin swarms before
they're ever grabbed. Has to be set per indexer.

Tracker-specific client settings (auto-add-trackers lists, DHT/PEX/LSD, ratio/seed-time
policy) live on the download client itself, not here — section 9 for `seedbox` mode, the
Local mode section (after section 9) for `local` mode.

---

## 4. Wire the *arr apps together

1. **Prowlarr** (`:9696`): add indexers under **Indexers > +**. Built-in list includes
   hundreds of public and private options — public need no account, private need an
   invite/signup but are generally higher quality.

   **FlareSolverr:** Settings > Indexers > Indexer Proxies, add one with URL
   `http://flaresolverr:8191`, and give it a **tag** (e.g. `flaresolverr`) — a proxy with
   no tag shows as "Disabled" even if it tests successfully, since tags are what actually
   activate it. Then edit each Cloudflare-protected indexer and add that same tag to its
   own Tags field. Note: a small number of indexers (1337x, kickasstorrents.to/.ws, and
   a few others) have known, currently-unresolved compatibility issues with Prowlarr +
   FlareSolverr even with correct setup — if one keeps 403ing after correct tagging, just
   swap to a different indexer rather than fighting it.

2. Still in Prowlarr, **Settings > Apps > +**, add:
   - Sonarr: `http://sonarr:8989` + API key from Sonarr's Settings > General
   - Radarr: `http://radarr:7878` + API key from Radarr's Settings > General

   This pushes Prowlarr's indexer list into both apps automatically. Force a sync via
   **System > Tasks > App Indexer Sync** if it doesn't appear right away.

3. **Sonarr** (`:8989`) / **Radarr** (`:7878`):
   - Settings > Media Management > Root Folders > add `/media/tv` (Sonarr) or
     `/media/movies` (Radarr) — these map to your host `MEDIA_ROOT/tv`/`MEDIA_ROOT/movies`.
     This step also has to be done before Seerr's root folder dropdown will show anything.
   - Settings > Profiles — edit your quality profile to uncheck qualities you don't
     want (e.g. Bluray/Remux) and reorder the rest by preference.
   - **Download Clients**: set up in section 9 (`seedbox` mode) or the Local mode
     section (`local` mode), not here — every indexer needs an explicit
     `downloadClientId` pointing at the right client, which `provision.py` handles for
     you either way.

4. **Bazarr** (`:6767`): connect to Sonarr and Radarr the same way (hostname + API key),
   set subtitle languages/providers.

5. **Seerr** (`:5055`):
   - Jellyfin URL: `http://host.docker.internal:8096` (Docker Desktop's special DNS name
     for reaching the host machine from inside a container)
   - External URL: your machine's actual LAN IP, e.g. `http://192.168.x.x:8096` — this is
     just what gets displayed/linked to users, not used for the internal connection
   - Forgot Password URL: optional, safe to leave blank
   - Add Sonarr (`sonarr:8989`) and Radarr (`radarr:7878`) as request targets, API keys
     again, root folders `/media/tv` and `/media/movies`

None of this wiring happens automatically just because the containers are networked
together — every connection above needs its API key pasted in manually, once.

---

## 5. LAN-wide access at clean hostnames (`*.<domain>`)

The `Caddyfile` in this repo has a route for every service, each on its own subdomain of
whatever you set `DOMAIN` to in `.env` — this stack's own reference deployment uses
`correll.tv` (a domain already owned, repurposed for LAN-only names — these records are
never published publicly), but any real domain you own works the same way, and so does
the free `<lan-ip>.nip.io` fallback from section 2 if you don't have one:

| Hostname | Routes to |
|---|---|
| `jellyseerr.<domain>` | Seerr |
| `watch.<domain>` | your host Jellyfin |
| `prowlarr.<domain>` | Prowlarr |
| `sonarr.<domain>` | Sonarr |
| `radarr.<domain>` | Radarr |
| `bazarr.<domain>` | Bazarr |
| `qbittorrent.<domain>` | qBittorrent (`local` mode only) |

(**`seedbox` mode:** qBittorrent/Transmission WebUIs live on the seedbox, not on a local
hostname — reach them at the seedbox's own URL directly, e.g.
`https://your-seedbox/qbittorrent/`.)

Using a real, publicly-registered domain (or nip.io, which is also a real registered
domain under the hood) instead of a made-up one matters here: browsers decide whether a
typed address is a URL or a search query based on whether the suffix is a recognized
domain — a fake TLD often gets treated as a search term instead of navigated to. A real
one is always recognized, so these load as pages, not search results, with no extra
configuration needed.

This is plain HTTP, deliberately — see the note below on why HTTPS isn't in play here.

**If you're using nip.io**, it already resolves to your LAN IP from anywhere with normal
internet DNS — skip straight to step 4. **If you own a real domain and want it to
resolve LAN-wide** (not just on this one PC), you need Pi-hole (or another local DNS
resolver) as your network's DNS:

1. **Point your router at Pi-hole.** On Google Wifi: Google Home app > Wifi > Settings
   (gear) > Advanced networking > DNS > Custom > set to your machine's LAN IP.
2. **Add a local DNS record in Pi-hole for each hostname above** — admin UI > Local DNS >
   DNS Records — every one points at the same IP, your machine's LAN IP. This only
   affects devices using your Pi-hole for DNS; it doesn't touch what your domain
   resolves to for anyone outside your network.
3. **Free up port 80 for Caddy.** Pi-hole's own admin UI often also defaults to port 80 —
   if so, remap it (e.g. `8081:80` instead of `80:80`) in Pi-hole's compose file and
   recreate it. Pi-hole's DNS function (port 53) is unaffected either way.
4. **Bring up / reload Caddy:**
   ```bash
   docker compose up -d caddy
   ```
   or, if it's already running and you just changed the `Caddyfile`/`.env`:
   ```bash
   docker compose restart caddy
   ```
5. **Reserve your PC's LAN IP** (in your router's admin UI or the Google Wifi app:
   Devices > your PC > reserve IP) so the DNS records — or a nip.io hostname baked
   around that IP — don't silently break if the router hands out a different address
   later.

**On the machine actually running the stack**, reaching its own LAN IP can be
unreliable due to how Docker Desktop's Windows networking loops traffic back to itself.
If any of these hostnames don't resolve on that PC specifically (but work fine on every
other device), add hosts-file entries instead of relying on DNS for that one machine:

1. Open Notepad **as Administrator**
2. Open `C:\Windows\System32\drivers\etc\hosts`
3. Add one line per hostname (substituting your actual `DOMAIN`), all pointing at
   loopback:
   ```
   127.0.0.1 jellyseerr.<domain>
   127.0.0.1 watch.<domain>
   127.0.0.1 prowlarr.<domain>
   127.0.0.1 sonarr.<domain>
   127.0.0.1 radarr.<domain>
   127.0.0.1 bazarr.<domain>
   ```
4. Save, then `ipconfig /flushdns`

### Why plain HTTP, not HTTPS (for the LAN-only routes)

A real domain (or nip.io) is being used here, but these subdomains often only resolve on
your LAN — Let's Encrypt can't issue a normal certificate for a name it can't reach, and
a self-signed cert would just bring back the "not secure" warning until every device
trusted a custom root CA. Since the whole point here was zero extra setup per device,
the `Caddyfile`'s global `auto_https disable_redirects` option keeps Caddy from ever
synthesizing an HTTP→HTTPS redirect on port 80 — every LAN-only `*.{$DOMAIN}` route
(Sonarr, Radarr, Prowlarr, Bazarr, qBittorrent) stays plain HTTP with no certificate
involved. If a browser's "HTTPS-first" mode tries `https://` before `http://` for one of
these, it gets connection-refused (nothing is listening on 443 for them) rather than a
certificate warning, and falls back to plain HTTP automatically.

`watch.{$DOMAIN}`, `{$DOMAIN}` (apex), and `jellyseerr.{$DOMAIN}` are the exceptions,
covered next — each has both an `http://` block (LAN, unchanged) and a real `https://`
block with a genuine Let's Encrypt cert (public).

---

## 6. Access outside your home network

Two options, different risk profiles. This stack uses the second one for Jellyfin
and Seerr specifically; the *arr apps stay LAN-only either way.

**Tailscale.** Private mesh VPN between your devices — nothing exposed to the public
internet. Install on your PC and phone, same account on both, and your phone reaches the
stack as if it were on your home WiFi from anywhere. None of these apps (Sonarr, Radarr,
Prowlarr) are built with "hostile public internet" as a threat model — several have had
real CVEs over the years — so keeping them reachable only via a private mesh instead of
an open port is the safer default. Downside: every device that wants access needs the
Tailscale client installed, which rules out most TVs.

**Port forward + real domain + Caddy TLS (what `watch.{$DOMAIN}` and `{$DOMAIN}` /
`jellyseerr.{$DOMAIN}` use).** Only Jellyfin and Seerr are exposed this way — the *arr apps
are never forwarded, and the `lanonly` snippet in the `Caddyfile` blocks them at the app
layer too as defense in depth (see below). Both public apps sit behind their own login
(Jellyfin's own auth; Seerr's Jellyfin-backed or local login, see section 4's Seerr step),
so exposing Seerr's request UI doesn't hand out any access Jellyfin itself wouldn't already
gate. The seedbox's own torrent clients are already reached over the internet directly
(that's the nature of a seedbox) and have their own auth in front — not something this
stack's network exposure affects either way.

**Why DNS-01, not the more common HTTP-01 challenge:** HTTP-01 needs port 80 reachable
from the internet for Let's Encrypt to hit `/.well-known/acme-challenge/...`. Forwarding
80 would put every plain-HTTP LAN route (`sonarr.{$DOMAIN}`, `radarr.{$DOMAIN}`, etc.) on
the public internet the moment the router forwards it — nothing in Caddy's config would
stop that on its own. DNS-01 instead proves domain ownership by creating a TXT record via
the DNS provider's API, so **only port 443 ever needs forwarding**, and port 80 can stay
unforwarded permanently — every LAN-only route is then unreachable from outside by
construction, not just by convention.

Setup:

1. **Register a real domain** if you don't already own one — this stack's own reference
   deployment uses `correll.tv`. A domain you already use for the LAN-only hostnames
   (section 5) works fine; the public routes are additional hostnames on it
   (`watch.{$DOMAIN}`, and optionally `{$DOMAIN}` / `jellyseerr.{$DOMAIN}` for Seerr),
   not a second domain.
2. **Add the domain to Cloudflare** (free plan) and point the registrar's nameservers at
   Cloudflare's. This is what makes DNS-01 possible — Caddy's `acme_dns cloudflare`
   directive needs the zone to actually live there.
3. **Create one `A` record per public hostname** you want (`watch`, and optionally
   `{$DOMAIN}`/apex plus `jellyseerr` for Seerr) → your current WAN IP (find it at
   `https://api.ipify.org`). Each can be either DNS-only (grey cloud) or **Proxied**
   (orange cloud, what this stack's reference deployment uses for all three) — DNS-01
   only ever touches the separate `_acme-challenge` TXT record per hostname, so a
   record's proxy status has no effect on cert issuance or renewal either way. Proxied
   does mean routing traffic through Cloudflare's CDN, which is against their free/Pro
   tier's terms for sustained video-streaming use specifically; low-risk for a single
   person's personal remote access, but worth knowing going in (Seerr's own traffic —
   metadata/API calls, no video — isn't affected by that particular ToS concern). In
   exchange you get: your real origin IP hidden from internet-wide scanning, plus free
   WAF/bot-fight-mode. If proxied, Caddy needs to trust Cloudflare's IP ranges to recover
   the real visitor IP from the `CF-Connecting-IP` header — see the `trusted_proxies`
   block in the Caddyfile's global options (ranges from
   `https://www.cloudflare.com/ips/`, ***not*** something `caddy fmt` or Cloudflare keeps
   in sync automatically — re-check that page occasionally). Optional but recommended if
   proxied: Cloudflare dashboard → SSL/TLS → Edge Certificates → **Always Use HTTPS**, so
   a client that tries plain `http://` first gets redirected to `https://` at Cloudflare's
   edge instead of timing out against the router's unforwarded port 80.
4. **Create a scoped API token**: Cloudflare dashboard → My Profile → API Tokens →
   Create Token → permissions `Zone:DNS:Edit`, resource restricted to this one zone. Put
   it in `.env` as `CF_API_TOKEN`, along with `ACME_EMAIL`, `CF_ZONE`, and
   `DDNS_RECORDS=watch.{$DOMAIN},{$DOMAIN},jellyseerr.{$DOMAIN}` (comma-separated list —
   include only the hostnames you actually created records for; see `.env.example` for
   the full description of each var).
5. **Free port 443 on the host for Caddy** if something else already owns it — this
   stack's Pi-hole did (its HTTPS block-page listener), remapped to `8443:443` in its own
   `docker-compose.yml`. Check `docker ps` for anything else publishing `443`.
6. **Bring Caddy up with the new build**: `docker compose up -d --build caddy`. It now
   builds from `Dockerfile.caddy` (adds the Cloudflare DNS module and the rate-limit
   plugin via `xcaddy`) instead of the stock `caddy:latest` image, and persists certs in
   the `caddy_data` named volume —
   check `docker logs caddy` for `certificate obtained successfully`. That volume is what
   prevents a routine `docker compose up --force-recreate` from burning through Let's
   Encrypt's 5-duplicate-certs-per-week rate limit by re-issuing every time.
7. **Reserve this PC's LAN IP** in your router (same step as section 5) and **forward TCP
   443 only** to it. Leave 80 unforwarded.
8. **Keep the DNS record current** — residential WAN IPs aren't guaranteed static.
   `scripts/ddns-update.py` checks the current IP against Cloudflare's record every run
   and PATCHes only on a mismatch; best-effort ntfy notification on change, same pattern
   as `scripts/rclone-sync.py`. Schedule it every few minutes the same way as the other
   scheduled scripts in this repo (Task Scheduler, `pythonw.exe "C:\path\to\repo\scripts\
   ddns-update.py"` — see section 9's rclone step for why `pythonw.exe`, not `python.exe`
   or a raw `.exe`, is the action to use).
9. **Point Jellyfin's own Known Proxies / Published Server URLs at Caddy** — Dashboard →
   Networking, native Jellyfin install. Without **Known proxies** set to the address
   Caddy's requests actually arrive from, every remote session shows Caddy's IP instead of
   the real client's. **Published Server URLs** can stay per-subnet
   (`192.168.x.0/24=http://192.168.x.x:8096`, `all=https://watch.{$DOMAIN}`) so LAN clients
   keep using the LAN URL unchanged. Leave `EnableHttps`/`RequireHttps` off — TLS
   terminates at Caddy, Jellyfin stays plain HTTP behind it. Seerr needs no equivalent
   step — it calls `server.enable('trust proxy')` unconditionally in its own code, so it
   already sees the real client IP via the `X-Forwarded-*`/`X-Real-IP` headers Caddy sets.

**Rate limiting.** `Dockerfile.caddy` also builds in `mholt/caddy-ratelimit`, and the
`https://watch.{$DOMAIN}` and `https://{$DOMAIN}, https://jellyseerr.{$DOMAIN}` blocks in
the Caddyfile each apply it per-visitor: a generous general ceiling (300 req/min) that
shouldn't affect real browsing/streaming, plus a tighter zone (10 req/min) scoped to each
app's own login endpoint specifically (Jellyfin's `/Users/AuthenticateByName`; Seerr's
`/api/v1/auth/*`, covering its Jellyfin-backed, local, and Plex login routes), to blunt
credential-stuffing. Both zones on both blocks are keyed on `{client_ip}`, not
`{remote_host}` — with Cloudflare proxying in front, every request's TCP peer is a
Cloudflare edge IP, so keying on the raw peer would rate-limit everyone as if they were
one visitor.

Neither Jellyfin nor Seerr allows anonymous access — every request that isn't already
authenticated gets redirected to that app's own login page (verified with `curl`: an
unauthenticated request to `{$DOMAIN}` gets a `307` to `/login`). Seerr's login itself
accepts either an existing Jellyfin account (`mediaServerLogin`, validated live against
Jellyfin's own auth API — Seerr never creates Jellyfin accounts, so a user has to already
exist in Jellyfin's Dashboard → Users first) or a pre-existing local Seerr account
(`localLogin`); a Jellyfin login auto-provisions a matching Seerr account on first
success, with basic request-only permissions unless that Jellyfin account is an admin
and it's the very first Seerr user. There's no separate self-serve Seerr signup.

No IP allowlist is applied yet — each app's own login plus the TLS connection and the
rate limiter above are the whole access-control story for now. Worth doing before going
live: update Jellyfin and Seerr, confirm admin passwords are strong and not reused, turn
off Jellyfin Quick Connect if unused, and confirm Jellyfin's own login-attempt lockout is
on. An IP-based allowlist (Caddy `remote_ip` matcher, or a Windows Firewall rule if Docker
Desktop's networking turns out to mangle source IPs — check `docker logs caddy` for the
real client IP on an external request before relying on either) is a planned fast-follow,
not yet implemented.

---

## 7. Start on machine boot

Docker Desktop > Settings > General > enable **"Start Docker Desktop when you log in."**
Every service in this compose file has `restart: unless-stopped`, so containers come
back up automatically once Docker Desktop launches — no compose changes needed.
`unless-stopped` means "always restart unless a human explicitly stopped it," as opposed
to `always`, which would restart even a deliberate stop. Also re-add the rclone sync
scheduled task if it was ever removed — `Register-ScheduledTask` persists across
reboots on its own, so this is normally a one-time setup, not something tied to Docker.

---

## 8. Using it

Search a title in Seerr, hit **Request**. Track status on the **Requests** tab:
Pending (not yet approved) → Processing (grabbed, downloading) → Partially Available
→ Available. Once something's fully downloaded, Jellyfin is the better place to actually
browse your library — Seerr's list is more useful for tracking things still in
progress.

---

## 9. The seedbox: every tracker's actual downloader

**`seedbox` mode only** (`DOWNLOAD_MODE=seedbox` in `.env`) — if you're running
`local` mode instead, skip to the Local mode section right after this one; none of the
setup below applies to you.

**Every indexer downloads and seeds on a remote seedbox, not locally.** Two separate
client instances there split the work by tracker class:

- **qBittorrent** — the private Hit & Run tracker. DHT/PEX/LSD off (tracker rule), ratio
  1.0 or the tracker's own seed-time requirement, whichever's sooner.
- **Transmission** — every other (public) indexer. DHT/PEX/LPD **on** (public releases
  often ship with few/weak tracker URLs and lean on these for peer discovery), its own
  independent ratio + idle-seed-time policy.

This started as a single-tracker setup (just qBittorrent, mirroring a local download for
ratio) and evolved twice: first to make the seedbox the sole downloader instead of a
mirror, then to add Transmission as a genuinely separate client instance once it became
clear that qBittorrent's global DHT/PEX/LSD settings can't be split two ways on one
instance — a single qBittorrent can't simultaneously satisfy "off for this tracker" and
"on for peer discovery on everything else." Two real client processes, each with their
own global settings, resolves that cleanly; a second Sonarr/Radarr *download client
entry* pointing at the same instance would not have (qBittorrent's globals are
per-instance, not per download-client-entry).

Finished files sync down locally afterward via a scheduled rclone job; Sonarr/Radarr
pick them up via Remote Path Mapping and import normally. Each seedbox client keeps
seeding independently the whole time, unaffected by whether or when the sync happens.

### Why a Caddy shim, specifically

The seedbox's clients sit behind a reverse proxy requiring HTTP Basic Auth (confirmed:
`curl -u user:pass https://seedbox/qbittorrent/api/v2/app/version` succeeds with no
separate client-native login at all — same for Transmission's RPC). Neither
Sonarr/Radarr's built-in qBittorrent nor Transmission download-client types have a field
for a Basic Auth layer distinct from the client's own username/password (sent as a login
body, not an `Authorization` header) — pointing Sonarr directly at the seedbox fails with
a 401 before the client is ever reached. Rather than gamble on undocumented
URL-embedded-credential behavior, Caddy reverse-proxies the *entire* seedbox host and
injects the header itself, so Sonarr/Radarr just talk plain HTTP to `caddy:8090` — no
credentials on the Sonarr/Radarr side at all. Because the shim proxies by host, not by
path, **the same `:8090` shim works for both clients** — no per-client shim needed, it
doesn't care whether the request is for `/qbittorrent/...` or Transmission's `/rpc`.

### Setup

1. **`.env`**: fill in `SEEDBOX_URL`/`USER`/`PASS` (reference values, not read by any
   container directly) and `SEEDBOX_BASIC_AUTH` — `base64("user:pass")`, computed with
   `echo -n "user:pass" | base64`. This is the value Caddy actually injects.
2. **Caddyfile**: one internal-only site, not published to the host, shared by both
   clients —
   ```
   :8090 {
   	reverse_proxy https://your-seedbox-host {
   		header_up Host your-seedbox-host
   		header_up Authorization "Basic {$SEEDBOX_BASIC_AUTH}"
   	}
   }
   ```
   Add `environment: - SEEDBOX_BASIC_AUTH=${SEEDBOX_BASIC_AUTH}` to the `caddy` service
   in `compose.yaml` so Caddy can read it, then `docker compose up -d caddy`. Verify from
   another container on the same network:
   ```bash
   docker exec sonarr curl http://caddy:8090/qbittorrent/api/v2/app/version   # qBittorrent
   docker exec sonarr curl -X POST http://caddy:8090/rpc -d '{"method":"session-get"}'  # Transmission
   ```
   Both should succeed with **no credentials** on the client side (Transmission's RPC
   correctly returns `409` + a session-id header on the first call — that's its normal
   handshake, not a failure; retry with `X-Transmission-Session-Id` set to see real data).
3. **New download client per tracker class** in Sonarr/Radarr (Settings > Download
   Clients):
   - qBittorrent type: Host=`caddy`, Port=`8090`, UseSsl=off, UrlBase=`/qbittorrent`,
     username/password dummy (auth already happened at the shim), Category `ratio`.
   - Transmission type: Host=`caddy`, Port=`8090`, UseSsl=off, **UrlBase=`""` (empty)**
     — Transmission's RPC lives at the site root (`/rpc`), *not* under `/transmission/`
     the way the field's own default and help text suggest; leaving the default in place
     causes a silent `405`, not an auth error, since Sonarr never actually reaches the
     right path — username/password dummy, Category `public`.
   - **Priority worse than whichever client ends up as the "default"** (e.g. `50`) on
     every seedbox client — see warning below, this isn't optional, and applies
     regardless of how many clients you have.
4. **Route every indexer** to the matching client via `downloadClientId` — **not tags**
   (see warning below):
   ```bash
   curl -X PUT "http://localhost:8989/api/v3/indexer/<INDEXER_ID>" \
     -H "X-Api-Key: <SONARR_API_KEY>" -H "Content-Type: application/json" \
     --data-binary @- <<'EOF'
   { ...full indexer object from GET, with "downloadClientId": <CLIENT_ID>... }
   EOF
   ```
   (Radarr is identical, port `7878`.) Do this for **every** indexer, not just the
   private tracker — an indexer with no override falls through to round-robin logic
   (see warning below).
5. **Remote Path Mapping per client** (Settings > Download Clients > Remote Path
   Mappings): Host=`caddy` for both. Remote Path=that client's actual download directory
   **including any category subfolder** the client appends on its own (check a real
   torrent via the client's own API — don't guess it; qBittorrent nests categorized
   downloads under `.../qbittorrent/<category>/`, confirmed via `GET /api/v2/torrents/info`;
   Transmission's `download-dir` from `session-get` was the flat base directory with no
   observed per-category nesting, but verify against a real grab rather than assume).
   Local Path=a distinct staging folder per client (e.g. `/media/downloads/seedbox/` for
   qBittorrent, `/media/downloads/seedbox-transmission/` for Transmission — both covered
   by the `${MEDIA_ROOT}:/media` mount, create both folders first, Sonarr/Radarr validate
   they exist before accepting the mapping). **Keep each mapping in sync with whatever
   the rclone sync actually targets for that client** — mismatching the two means
   Sonarr/Radarr look for the file one directory level away from where it lands.
6. **Seeding policy, per client — this is the actual point of running two clients**:
   - qBittorrent (private tracker): ratio `PRIVATE_TRACKER_SEED_RATIO` **or**
     `PRIVATE_TRACKER_SEED_TIME_MINUTES`, whichever's sooner — check the tracker's
     *actual* rule rather than assume the round-number defaults (`1` / `14400` minutes).
     `POST /qbittorrent/api/v2/app/setPreferences`: `max_ratio_enabled=true`,
     `max_ratio=<PRIVATE_TRACKER_SEED_RATIO>`, `max_seeding_time_enabled=true`,
     `max_seeding_time=<PRIVATE_TRACKER_SEED_TIME_MINUTES>`.
     **Action must be `max_ratio_act=0` (Pause) — not "Remove + delete files."** Sonarr
     actively refuses to add a download client configured to auto-delete on its ratio
     limit ("qBittorrent is configured to remove torrents when they reach their Share
     Ratio Limit"), and this is a real safety catch: once Sonarr genuinely tracks a
     client, a delete-on-limit action races the rclone sync — the client could delete a
     file before rclone ever copies it down, losing it permanently. Pausing leaves the
     file in place; Sonarr's own "Remove completed downloads" (already on) cleans up the
     torrent *after* confirming a successful import, not on the client's own timeline.
   - Transmission (public trackers, no real tracker obligation): a modest ratio + a
     disk-hygiene time cap is reasonable, since there's nothing to comply with. Driven by
     its own `PUBLIC_SEED_RATIO` / `PUBLIC_SEED_TIME_MINUTES` — deliberately **separate**
     env vars from the private tracker's above, not shared, since the two clients have no
     obligation to match (an earlier version of `provision.py` reused
     `PRIVATE_TRACKER_SEED_RATIO` for both, which silently pinned Transmission's ratio to
     whatever the private tracker's Hit & Run rule required). Via the JSON-RPC endpoint
     (`session-set`): `"seedRatioLimit": <PUBLIC_SEED_RATIO>, "seedRatioLimited": true`
     plus `"idle-seeding-limit": <PUBLIC_SEED_TIME_MINUTES>, "idle-seeding-limit-enabled":
     true` as a backstop. **Important semantic gap**: Transmission's (and Deluge's) idle-seed-time
     limit means "stop after N minutes of *no* peer activity," not "stop after N minutes
     total, active or not" the way qBittorrent's `max_seeding_time` works — a torrent
     with any occasional trickle of activity never hits an idle limit no matter how long
     it's been seeding in total. In practice this matters little here since ratio
     resolves first for anything with real demand; it only means there's no hard ceiling
     on a slow-but-not-dead public swarm the way there is on the private tracker.
   - Enabling privacy settings on Transmission is a **JSON body**, not qBittorrent-style
     query params:
     ```bash
     curl -u seedit4me:pass -X POST "https://seedbox/rpc" \
       -H "X-Transmission-Session-Id: <id from the 409 handshake>" \
       -d '{"method":"session-set","arguments":{"dht-enabled":true,"pex-enabled":true,"lpd-enabled":true}}'
     ```
7. **rclone**, over the seedbox's SFTP subsystem — works without SSH shell access, since
   SFTP is a distinct SSH subsystem that only does file operations, never arbitrary
   command execution (exactly why providers commonly disable shell access while leaving
   SFTP enabled). One-time setup:
   ```bash
   rclone config create seedbox sftp host=<seedbox-host> port=<sftp-port> user=<user> pass=<pass> --obscure
   pip install -r scripts/requirements.txt
   ```
   (`requests` and `python-dotenv` — used by `scripts/seedbox-cleanup.py`; install into
   whichever Python environment `pythonw.exe`/the scheduled tasks actually run under.)
   Scheduled (Windows Task Scheduler, every few minutes) via `scripts/rclone-sync.py`
   (one `subprocess.run` call per client, sequential) — **don't point the task directly
   at `rclone.exe`**: a Task Scheduler action running under an Interactive logon (the
   default for a task created under your own user) flashes a visible console window
   every time it fires, since rclone is a console app. Task action:
   `pythonw.exe "C:\path\to\repo\scripts\rclone-sync.py"` — `pythonw.exe` has no console
   of its own, so nothing flashes, and `subprocess.run(..., creationflags=CREATE_NO_WINDOW)`
   keeps the child `rclone.exe` hidden too. All scheduled automation in this repo standardizes
   on Python for this reason (see `scripts/seedbox-cleanup.py`) rather than mixing in
   VBScript/PowerShell wrappers per script.
   The sync destinations must live under the same `MEDIA_ROOT` as the movies/TV
   folders — see the hardlink warning below for why.
   `--min-age 30s` skips files still being written remotely; rclone also writes to a
   `.partial` temp name and renames atomically on completion, which independently
   guards against Sonarr/Radarr importing a half-copied file.
   Optional: set `NTFY_TOPIC` (and `NTFY_SERVER` if self-hosting) to get a
   [ntfy.sh](https://ntfy.sh) push notification as *video* files finish syncing down —
   sidecars (nfo/srt/sfv) and anything under a `Sample` path/filename are logged but not
   notified, since notifying on every sidecar/sample copy burns through ntfy.sh's
   free-tier daily quota before the real files even get a chance. Notifications are also
   grouped by top-level synced folder (a season pack's episodes all land in one torrent
   folder, so they're one group) and batched to send at most one push per group per sync
   run rather than one per file — a 9-episode season pack finishing in one run is one
   notification, not nine (a day with 404 raw file-completion events came out to 33
   grouped notifications in practice). A heavy catch-up day can still hit the quota even
   with grouping — self-host `NTFY_SERVER` or a paid ntfy.sh plan if that becomes a
   recurring problem. A failed POST (network error or non-2xx response — a
   quota-exceeded 429 isn't a network error, so the response status is checked
   explicitly) is logged but never fails the sync.
   rclone runs via `Popen` and gets polled every 10s while alive, tailing whatever new
   bytes it has appended to its own `--log-file` since the last poll — a plain
   `subprocess.run()` would block until the *entire* multi-file sync exits, batching
   every notification up until a run that can take hours finally finishes. Byte offsets
   are tracked in binary mode (text-mode seek/tell doesn't reliably mix with manual
   length math across encodings), and only complete lines are consumed — a trailing
   partial line still being written is left for the next poll. Leave `NTFY_TOPIC` blank
   to disable. Pick a hard-to-guess topic name — anyone who knows it can read your
   notifications on public ntfy.sh, no auth by default.
8. **Cleanup** (`scripts/seedbox-cleanup.py`, on its own 30-minute scheduled task via
   `pythonw.exe`): deletes a
   torrent on the seedbox only once it's both paused/stopped at its own ratio-or-time
   target *and* confirmed already imported by Sonarr/Radarr (checked via history, not
   guessed from local disk state). Deliberately **not** done via the client's own
   "delete on limit" action — Sonarr refuses to add a client configured that way, and
   even if it didn't, the seedbox could delete a file before rclone's next sync cycle
   ever pulled it down, losing it permanently. Checking "already imported" first removes
   that race, since the library's hardlinked copy is independent of whatever happens to
   the seedbox/staging copies afterward. Only ever touches the seedbox itself — the
   local staging mirror is left for the next rclone sync run to clean up on its own,
   since `sync` already removes local files no longer present at the source. Logs to
   `<CONFIG_ROOT>\seedbox-cleanup.log` (silent/no file if nothing currently qualifies); run
   with `--dry-run` to preview without deleting anything.

### Warnings (all hit for real running this setup, not theoretical)

**Do not use indexer Tags for step 4**, even though tag-based download-client selection
exists and looks like the obvious way to do it — an indexer with *any* tag gets its
releases **rejected outright** for any series/movie that doesn't share that tag
(`IndexerTagSpecification` in Sonarr's decision engine). Since Seerr-created requests
carry no tags by default, a tagged indexer silently stops being searched for anything
requested through Seerr — not "deprioritized," genuinely never searched. `downloadClientId`
routes directly to one client with no such side effect. If you do use a tag anywhere in
this setup, the download client's own tag list must also stay empty, or the tag-matching
filter excludes it before the `downloadClientId` check ever runs, throwing
`DownloadClientUnavailableException` on every grab.

**Two same-priority download clients silently round-robin every other grab between
them.** `downloadClientId` on the indexer only short-circuits selection for *that*
indexer; any indexer with no override falls through to Sonarr/Radarr's normal
client-selection logic, which groups all *equal-priority* clients together and
load-balances across them (`DownloadClientProvider.GetDownloadClient` in Sonarr's
source). Since seedbox clients have to be untagged to avoid the tag-exclusion problem
above, they end up untagged *and* same-priority as each other unless you explicitly set
worse priorities — meaning grabs from any indexer without an explicit override randomly
land on whichever untagged client happens to win the round-robin. **Once every single
indexer has an explicit `downloadClientId`, this concern mostly disappears in practice**
(nothing is left to fall through) — but keep the priority difference anyway as a
fail-safe for the next indexer you add and forget to route. If misrouting already
happened before the priority fix went in, recategorize the wrongly-routed torrents —
filter by category on the client that received them, check each one's `private` field
(or tracker URL) to tell genuine grabs apart from the misrouted ones, and bulk-set the
correct category via that client's API (`POST /api/v2/torrents/setCategory` for
qBittorrent; Transmission has no native category concept, only the `tvCategory`/
`movieCategory` Sonarr/Radarr fields, which don't retroactively apply to
already-added torrents — move them by hand if this ever happens there).

**qBittorrent's "Seeding Time Limiting" and ratio limit share one action setting** —
`max_ratio_act` fires for whichever of ratio/seeding-time/inactive-seeding-time is met
first; there's no way to configure a different action per limit type. This is what makes
"ratio 1.0 or a set seed-time, whichever's sooner" a single native settings change (both
limits active, shared action) rather than something needing custom scripting — but it
also means a stray seeding-time cap enabled elsewhere can silently override an intended
ratio target. Worth checking if seeding durations look off from what you configured.
Transmission has no equivalent shared-action constraint (ratio and idle-time are
independent settings there), but see the idle-vs-absolute-time semantic gap above.

**Point the rclone sync at each client's category subfolder specifically, not its whole
torrent directory** — syncing the parent path pulls down *everything* on that client,
including any unrelated content that predates this setup (harmless security-wise, since
it's your own account's SFTP root, but a real waste of local disk space and bandwidth
for content that has nothing to do with what you're actually trying to sync). Scope the
sync source to the exact category subfolder from the start, and make sure it matches
whatever Remote Path Mapping (step 5) actually points at for that same client — the two
have to describe the same remote location or Sonarr/Radarr look for the synced file one
directory level away from where it actually lands.

**Sonarr/Radarr's hardlink import silently degrades to a full copy if `movies`/`tv`/
`downloads` aren't all subfolders of one single Docker volume mount.** `copyUsingHardlinks`
is on by default and does exactly what it promises — *if* the source and destination are
part of the same bind mount. Mount them as three separate `volumes:` entries instead
(even pointing at three folders on the literal same physical drive), and Docker gives the
container three separate mount points; a `link()` syscall can't cross that boundary, so
Sonarr/Radarr catch the failure and quietly fall back to a full copy — no warning, no
error, just double the disk usage on every single import, forever. This is exactly why
this repo mounts one `${MEDIA_ROOT}:/media` volume with `movies/`, `tv/`, and
`downloads/` as real subfolders underneath, rather than `${MOVIES_PATH}:/movies`,
`${TV_PATH}:/tv`, `${DOWNLOADS_PATH}:/downloads` as separate mounts. Verify it's actually
working, don't just trust the setting: `docker exec radarr stat -c '%d:%i' <a file under
/media/downloads/...>` and the same file's path after import under `/media/movies/...` —
matching inode numbers mean it's a real hardlink (one copy of the data, two names for it);
different inodes mean it silently copied.

**Relocating `MEDIA_ROOT` to a new drive breaks every existing hardlink, silently
doubling disk usage.** `robocopy` (and any plain file copy) has no concept of NTFS
hardlinks — it copies each linked path as an independent file with its own data, so a
file that only cost one copy's worth of space under `downloads/` + `movies/`/`tv/` on
the old drive costs two on the new one. This doesn't show up as a copy failure or error
of any kind; the only symptom is the destination using noticeably more space than the
source for the same file count (caught here via `du -sb` disagreeing between drives by
hundreds of GB despite matching file totals). Fix: after the copy, find every group of
files sharing an inode on the old drive (`find <old_root> -type f -printf "%i|%p\n"`,
group by inode) and recreate the same hardlink relationship on the new drive — delete
one member of each duplicated pair at the destination and `os.link()`/`mklink /H` it to
its sibling instead of leaving two independent copies. Verify with the same
matching-inode check as above once done.

**Transmission's stock web UI serves its RPC endpoint at the site root, not under its
own path** — the UI's own JS defines the RPC URL as a *relative* `../rpc`, which resolves
against the page's own URL (`/transmission/`), not the JS file's location, landing at
`/rpc` site-wide rather than `/transmission/rpc`. Confirmed by testing both: `/transmission/rpc`
returns a `405` (some other resource exists there, wrong method), while bare `/rpc`
returns the expected `409` + session-id handshake. This is why Sonarr/Radarr's
Transmission client needs `UrlBase=""` here rather than its documented default — worth
re-checking with a live request rather than trusting the field's own help text if a
different seedbox provider mounts things differently.

---

## 10. The trace dashboard

Seerr's own Requests tab tells you *what* you requested and its top-level status, but
when something's actually stuck it just links out to whichever Sonarr/Radarr page owns
it — no explanation of *why*, and nothing that ties together Seerr's request, the grab,
which of the two seedbox clients it landed on, whether it's synced down locally yet, and
whether it actually imported. `dashboard/` is a small FastAPI app that traces one request
across that whole pipeline and gives plain-language diagnoses for the stall patterns this
stack has actually hit in practice, with buttons to run the already-documented manual
fixes above instead of hunting down the right curl command again.

**The join key**: every grab has a `downloadId` in Sonarr/Radarr's history — the torrent's
own infohash, lowercased. That's what ties one Seerr request's grab to a specific torrent
on a specific client (`scripts/seedbox-cleanup.py`'s `fetch_imported_hashes()` already
relies on the exact same join). One TV request can fan out into many grabs (a season pack
covers many episodes in one grab) — the dashboard shows one card per grab, not one per
episode.

**What it diagnoses** (each maps to a fix button where one exists):
- No seeders on a still-downloading torrent, past a configurable stall threshold.
- *(seedbox mode)* Finished on the seedbox but hasn't synced down to local staging yet —
  escalates to an error if a successful rclone run has completed *since* the torrent
  finished and the file is still missing, since that's a broken sync, not a slow one.
- Import failed or blocked, showing Sonarr/Radarr's own message verbatim rather than a
  generic "something went wrong."
- **Manual import required** — the file is fully downloaded but Sonarr/Radarr won't
  auto-import because the match came from grab history (by internal ID) rather than
  parsing the filename itself. A real safety check, not a real problem — confirmed live
  against a genuinely correct match (*The Departed*) that just needed a human to confirm
  it. The fix button re-fetches Sonarr/Radarr's own best-guess match and only proceeds if
  it's a single, unambiguous, rejection-free candidate; otherwise it tells you to use the
  arr's own Manual Import screen instead of guessing.
- **Never grabbed** — matched fine, but nothing's ever searched or grabbed. This one isn't
  a fix button so much as an *answer*: it runs a live interactive search
  (`GET /api/v3/release`) and reports exactly what it finds — zero releases anywhere (not
  a quality-profile problem, the release likely doesn't exist on any configured indexer),
  releases that exist but were all rejected (with each one's actual rejection reason), or
  releases that exist and *weren't* rejected (should be gettable via a manual/automatic
  search). Confirmed live: a "never grabbed" kids' movie special turned out to have zero
  releases on any indexer at all, definitively ruling out a quality-profile cause.
- **Season-number mismatch** (TV only) — the series matched fine in Sonarr, but zero
  episodes ever resolved from Seerr's requested season numbers. Confirmed live against
  MythBusters' original run: Sonarr groups its seasons by year (2003–2018, matching
  TheTVDB), while Seerr's request used ordinal season numbers (1–16, from TMDB) — neither
  app has a season-remapping feature, so the fix bypasses Seerr's season numbers entirely,
  monitors every season Sonarr itself actually has, and triggers a full series search.
- Seerr's cached root folder path going stale (the exact bug in this README's own
  Troubleshooting section) — checked globally, independent of any one request, so it
  warns before the *next* request fails rather than after.
- The normal healthy end state — paused at its seed target *and* already confirmed
  imported, awaiting `scripts/seedbox-cleanup.py`'s next pass — is explicitly rendered as
  "done," not a stall. Getting this backwards would make the dashboard cry wolf on every
  successful download, since that's the expected terminal state for anything with
  `max_ratio_act=0` (Pause, see section 9's warnings).

**Fix buttons** (each is preview-then-confirm — nothing mutates until you click confirm a
second time): push Seerr's corrected root folder, retry a failed request, force a Prowlarr
indexer resync, raise an indexer's Minimum Seeders, confirm a Manual Import, search live
and show why nothing's grabbed, monitor a series' real seasons and search, or run
`rclone-sync.py`/`seedbox-cleanup.py` immediately instead of waiting for their schedule.
The cleanup button's preview is literally that script's own `--dry-run` output — nothing
to reimplement, it already makes the exact judgment call the preview needs.

### The request list: filters, sorting, and service health

The list page has a search box, type/status filters, and a sort order — all cheap and
instant, computed from whatever the background poller already fetched, no extra API
calls. There's also a **"Stalled only"** checkbox that's a different animal: it runs the
*real* diagnosis engine (a full trace + rule evaluation, same as opening a single
request) concurrently across every matched request, capped at 8 at a time so it doesn't
hammer Sonarr/Radarr/Prowlarr with 100+ simultaneous calls. That's genuinely slow (tens of
seconds to a couple of minutes depending on how busy the *arr apps already are) — it's
doing real work, not reading a cache, and one slow/unreachable source just gets skipped
rather than failing the whole filter.

Above the list, a **service health** section shows at-a-glance stats per app (series
count, missing episodes, queue size, indexer count, pending requests, active torrents,
etc.), split into **Services** (Sonarr/Radarr/Prowlarr/Seerr) and **Torrent clients**
(qBittorrent, and Transmission in seedbox mode) — all from the same already-fetched
snapshot data, no extra cost. Distinct from the smaller health-pill strip next to it,
which is about *poll connectivity* (is the background poller currently able to reach each
source), not service-level stats.

### Access and auth

Runs as a plain host process, not a container — the two script-triggering fix buttons
need real `subprocess` access to Windows-only scripts (`rclone-sync.py` hardcodes an
`rclone.exe` path and `CREATE_NO_WINDOW`), the same reason every other scheduled script in
this repo runs on the host. Caddy fronts it exactly the way it already fronts
host-installed Jellyfin: `reverse_proxy host.docker.internal:8099`.

Reachable two ways, same as Jellyfin/Seerr:
- **LAN**: `http://stats.{$DOMAIN}`, plain HTTP, no TLS involved (same reasoning as every
  other LAN-only route in section 5) — still requires signing in once the page loads.
- **Public**: `https://stats.{$DOMAIN}`, real Let's Encrypt cert via the same DNS-01 setup
  as section 6, Cloudflare-proxied, rate-limited the same way (300 req/min general, 10
  req/min on `/login` specifically) — **including the `not remote_ip private_ranges`
  guard** on both zones. Without it, every LAN device sharing Docker Desktop's collapsed
  `172.18.0.1` identity (see the Troubleshooting entry on this) would share one rate
  budget and trip false 429s from nothing more than normal local use.

**Login is Jellyfin-credential delegation, the same mechanism Seerr itself uses**: the
dashboard's own login form forwards the submitted username/password straight to
Jellyfin's `POST /Users/AuthenticateByName` and trusts Jellyfin's answer — not real SSO,
no shared token, just the same household credentials working for both apps with nothing
extra to manage. Any Jellyfin account can sign in and view traces; only accounts where
Jellyfin reports `Policy.IsAdministrator: true` can see or run fix-action buttons, since
those mutate real Sonarr/Radarr/Seerr config and this is now reachable from the internet.

### Setup

1. `pip install -r scripts/requirements.txt -r dashboard/requirements.txt`
2. Create the DNS record and add it to `DDNS_RECORDS` the same way as section 6's other
   public hostnames — `stats.{$DOMAIN}` needs its own Cloudflare `A` record (proxied) and
   a slot in `.env`'s `DDNS_RECORDS` list.
3. Add a Pi-hole Local DNS record for `stats.{$DOMAIN}` → this machine's LAN IP (section
   5) if you want it reachable by name from inside the house too, not just publicly.
4. `.env`: `DASHBOARD_PORT` (default `8099`), `DASHBOARD_POLL_SECONDS` (default `30`),
   `DASHBOARD_HISTORY_POLL_SECONDS` (default `180`), `DASHBOARD_STALL_MINUTES` (default
   `60`), `DASHBOARD_RETENTION_DAYS` (default `90`) — see `.env.example`. No Jellyfin
   connection vars needed; it reads them from Seerr's own `settings.json`.
5. **Run it**: `python -m dashboard.run` (needs `-m`, not a bare file path — it's a
   package, unlike the standalone `scripts/*.py` files). For it to survive logoff/reboot,
   register it as a Task Scheduler task the same way as the other scheduled scripts, but
   with an **At log on** trigger (not a repeating interval — this is a long-running
   server, not a periodic job) and **no execution time limit** (the default kills anything
   still running after 72 hours, which for a server means "kills it after 3 days"):
   ```powershell
   $action = New-ScheduledTaskAction -Execute "C:\Users\drcor\AppData\Local\Programs\Python\Python39\pythonw.exe" `
     -Argument "-m dashboard.run" -WorkingDirectory "C:\Users\drcor\acquisitions"
   $trigger = New-ScheduledTaskTrigger -AtLogOn
   $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1)
   Register-ScheduledTask -TaskName "acquisitions-dashboard" -Action $action -Trigger $trigger -Settings $settings
   ```
   (This needs to be run from an elevated/admin PowerShell on this machine — registering
   an At-log-on task isn't always permitted from a standard session.)
6. Check `<CONFIG_ROOT>\dashboard\dashboard.log` if it doesn't come up — same
   `RotatingFileHandler` + startup-exception pattern as every other scheduled script here.

### Known limits

History lookups for the request list are capped at one page (250 records) per app, same
horizon `scripts/seedbox-cleanup.py` already accepts — anything that scrolled off before
the dashboard's first run won't show on a request whose grab predates that. Opening a
single request's full trace bypasses this (it queries that item's history directly), so
this only affects the list view's freshness for very old requests. There's no test suite
in this repo (see CLAUDE.md) — this was verified by hand against the live stack during
development, including two real bugs it caught and fixed: a fully-imported movie briefly
showing a false "import blocked" (Sonarr's queue can keep a stale entry for a downloadId
that already completed) and a stage-track showing "unknown" instead of "done" for
completed grabs whose torrent had already been cleaned up from the client.

---

## Local mode: local qBittorrent, no seedbox

**`local` mode only** (`DOWNLOAD_MODE=local` + `COMPOSE_PROFILES=local` in `.env`) —
if you're running `seedbox` mode, this doesn't apply to you; see section 9 instead.

A single `qbittorrent` container (`compose.yaml`, gated behind the `local` Compose
profile) does the actual downloading — no seedbox, no recurring cost, no private-tracker
ratio obligation to manage. The tradeoff for that simplicity: **no VPN by default**, so
public-tracker swarms see your home IP directly, and **no ratio/seed-time management**,
so it's not suitable for most private trackers with a Hit & Run rule.

### One-time first-run step (do this before running `provision.py`)

The `linuxserver/qbittorrent` image **ignores** `QBT_USER`/`QBT_PASS` in `.env` at
container start — it generates a random temporary password on first boot instead, and
you have to set the real one yourself, matching what's in `.env` so `provision.py` can
actually log in as Sonarr/Radarr's download client (real credentials, not an
authentication bypass — qBittorrent's WebUI stays properly secured):

1. `docker compose up -d` (if you haven't already)
2. `docker logs qbittorrent` — find the line `A temporary password is provided for this
   session: <password>`
3. Log into `http://localhost:8080` as `admin` with that password
4. **Set the permanent password to exactly what's in `.env`'s `QBT_PASS`** (whatever
   `scripts/setup.py` had you choose, or whatever you put there by hand): Options > Web
   UI > Authentication
5. **Set Default Save Path to `/media/downloads`**: Options > Downloads — this is not
   optional. The image's own default (`/downloads`) isn't under the `${MEDIA_ROOT}:/media`
   mount, so Sonarr/Radarr couldn't hardlink (or even see) anything downloaded there —
   the exact bug the whole hardlink migration (see the Warnings above) fixed for seedbox
   mode, reproduced locally if this step is skipped.

Then run `scripts/provision.py` as usual (section 2) — it configures one qBittorrent
download client per app (`urlBase=""`, since this qBittorrent serves its WebUI/API at
the root, unlike the seedbox's reverse-proxy-mounted `/qbittorrent` path — get this
wrong and it's the same silent-connection-failure shape as the Transmission `UrlBase`
gotcha in section 9), routes every indexer to it (no private/public split — that
complexity in `seedbox` mode exists specifically for a ratio-obligated tracker sharing a
box with public ones, and doesn't apply here), and still strips indexer tags (the
tag-exclusion trap in section 9's Warnings applies regardless of mode).

### Adding a VPN

If you want the privacy seedbox mode gets "for free" via being remote, `compose.vpn.yml`
routes the local qBittorrent through a WireGuard VPN (Gluetun) instead — the same shape
this stack used before the seedbox migration (Gluetun + qBittorrent sharing its network
namespace + a port-forwarding sidecar so Gluetun's forwarded port reaches qBittorrent's
own settings; see git commit `cabf8d4` for the original three-service version this was
adapted from), updated to route through the single `${MEDIA_ROOT}:/media` mount instead
of the old three-way path split that broke hardlinking, and to use the real
`QBT_USER`/`QBT_PASS` credentials local mode already sets rather than env vars
qBittorrent itself never actually read.

1. Fill in `.env`'s VPN block — `VPN_SERVICE_PROVIDER`, `WIREGUARD_PRIVATE_KEY`,
   `WIREGUARD_ADDRESSES` (see
   [Gluetun's provider list](https://github.com/qdm12/gluetun-wiki/tree/main/setup) for
   your provider's exact values) — `scripts/setup.py` prompts for these directly if you
   answer yes to "route it through a VPN?" during setup.
2. Bring the stack up with the overlay layered on top, instead of plain
   `docker compose up -d`:
   ```bash
   docker compose -f compose.yaml -f compose.vpn.yml up -d
   ```
   This **replaces** the base `qbittorrent` service's networking (no ports of its own —
   they publish via `gluetun` instead, since Docker won't let a container both publish
   its own ports and share another container's network stack) and adds `gluetun` and
   `qbittorrent-port-sync` (syncs Gluetun's forwarded port into qBittorrent's own
   settings, so peers can actually reach you through the tunnel).
3. The rest is unchanged — same first-run credential/save-path step above, same
   `provision.py` run afterward. Every subsequent `docker compose` command (restart,
   logs, down) needs both `-f` flags too, as long as you're using this overlay.

Confirm it's actually tunneling before trusting it: `docker exec gluetun wget -qO-
https://ipinfo.io/ip` should show the VPN's IP, not your real one.

### What's not relevant in this mode

`scripts/rclone-sync.py` and `scripts/seedbox-cleanup.py`'s scheduled tasks are
seedbox-only — nothing in local mode needs syncing down or cleaning up remotely, since
qBittorrent already writes straight into `${MEDIA_ROOT}/downloads`. Don't bother setting
up their scheduled tasks in this mode.

---

## Updating a container in place (e.g. Pi-hole)

Config lives in mounted volumes, not the image, so updates are non-destructive:

```bash
docker pull <image>:latest
docker compose pull <service>
docker compose up -d <service>
```

Back up first if it's something with a lot of manual config (Pi-hole: admin UI >
Settings > Teleporter > download backup). If the container wasn't originally started via
this compose file, `docker inspect <container> --format='{{.Config.Labels}}'` will show
its actual compose project directory if one exists — run the update commands from there
instead.

**Note on `:latest` tags:** pulling `:latest` only resolves to whatever the maintainer
had tagged `latest` *the moment you pull* — Docker never re-checks it on its own.
Restarting or recreating a container reuses the already-pulled image; if it's been a
while, `docker compose pull <service>` first or you may be running something far older
than "latest" implies. `docker image inspect <image> --format '{{.Created}}'` shows when
the image was actually built, not when you downloaded it — a useful gap check.

**Seerr specifically** needed more than a routine image bump when it renamed from
Jellyseerr (`fallenbagel/jellyseerr` → `ghcr.io/seerr-team/seerr`): the container now
runs as non-root UID 1000, so its config folder needs `chown 1000:1000` first, and the
compose service needs `init: true` added. Config/database migrate automatically on
first start otherwise. Worth remembering in case a future rename/breaking-change pattern
shows up again — check the project's own migration guide before assuming a plain image
swap is enough.

---

## Troubleshooting

**Downloads stuck on "Downloading metadata" or "Stalled"**
Check the seedbox client directly (qBittorrent or Transmission, whichever the indexer
routed to — section 9) for a forwarded/open port; most seedbox providers forward ports
automatically and it's rarely misconfigured on this end, but it's the first thing to
rule out. Otherwise, this usually just means a genuinely thin swarm — see "Grabbed
torrents have almost no seeders/peers" below.

**Sonarr/Radarr: "Unable to connect to qBittorrent" or "...to Transmission"**
- Confirm the Caddy shim itself works first — from another container:
  `docker exec sonarr curl http://caddy:8090/qbittorrent/api/v2/app/version` (or `/rpc`
  for Transmission, expect a `409` + session-id, not a connection error). If this fails,
  the problem is Caddy/the seedbox, not Sonarr/Radarr's client config.
- Double check the Transmission client's **UrlBase is empty (`""`)**, not the field's
  documented default (`/transmission/`) — see section 9's setup steps, this is the most
  common cause of a Transmission-specific connection failure.
- Confirm you're using the seedbox's current password, not an old/rotated one.
- Repeated failed logins can trigger a client's own IP ban — restart the client on the
  seedbox to clear it if logins start failing after working previously.

**No indexers showing in Sonarr/Radarr**
Check, in order: (1) Prowlarr actually has indexers added, (2) the Prowlarr → Sonarr/
Radarr "app" connection shows green not red (bad API key or wrong internal address are
the usual culprits — use `http://sonarr:8989`, not `localhost`), (3) manually force
**System > Tasks > App Indexer Sync** in Prowlarr rather than waiting.

**Setting an indexer's Priority to 1 doesn't seem to make it get picked over others**
Priority isn't a "prefer this indexer" setting — it's the *last* tiebreaker in Sonarr's
decision engine, checked only after quality, Custom Format score, protocol preference,
and episode matching are already tied between two releases. In practice those rarely
tie, so priority rarely ends up being what decides anything. To actually prefer one
indexer's releases over another's, use a **Custom Format** with an Indexer condition
and a positive score — Custom Format Score is compared right after quality, so it
reliably outranks competing releases the way priority usually doesn't.

**Forgot Prowlarr's password**
```bash
docker compose stop prowlarr
```
Edit `<CONFIG_ROOT>/prowlarr/config.xml`, find
`<AuthenticationMethod>Forms</AuthenticationMethod>`, change to
`<AuthenticationMethod>None</AuthenticationMethod>` (make sure there's only one such line
in the file). Restart, log in with no password, set new credentials under Settings >
General > Security, then optionally switch `AuthenticationMethod` back to `Forms`
afterward.

**FlareSolverr shows "Disabled"**
Not an error — it only activates when a **tag** links it to specific indexers. A proxy
with no matching indexer tag always shows Disabled, even if it tests successfully.

**Specific indexer keeps returning "blocked by CloudFlare Protection" or 403 even with
FlareSolverr correctly tagged**
Some indexers (1337x, kickasstorrents.to/.ws, and others) have open, unresolved
compatibility bugs with how Prowlarr replays FlareSolverr's solved requests — not a
config problem on your end. Swap to a different indexer.

**A seedbox client's WebUI suddenly returns an empty page or 502**
That's the seedbox's own reverse proxy or the client process itself, not anything in
this stack — check the seedbox provider's own status/panel, or restart the client from
its app catalog. The Caddy shim just forwards whatever the seedbox returns; it has
nothing to fix on this end.

**Seerr's root folder dropdown is empty**
Sonarr/Radarr need a root folder configured first (Settings > Media Management > Root
Folders) — Seerr's dropdown just mirrors whatever exists there.

**New requests fail immediately, Sonarr/Radarr logs show `Root folder '/tv' does not
exist` or `Root folder '/movies' does not exist`**
Seerr keeps its **own separate copy** of which root folder to send new requests to
(Settings > Services > Sonarr/Radarr > root folder) — it does not read this live from
Sonarr/Radarr each time. If you ever change Sonarr/Radarr's root folder path (e.g. the
`MEDIA_ROOT` migration in section 9), Seerr's copy goes stale silently: existing
requests and already-imported media are unaffected, but **every new request fails**
with this exact validation error, since Seerr is still telling Sonarr/Radarr to use a
path that no longer exists. Fix via Seerr's Settings UI, or directly:
```bash
curl -X PUT "http://localhost:5055/api/v1/settings/sonarr/0" -H "X-Api-Key: <SEERR_API_KEY>" \
  -H "Content-Type: application/json" -d '{...full object from GET, "activeDirectory": "/media/tv"...}'
```
(Radarr identical, `/settings/radarr/0`, `/media/movies`.) Any requests that already
failed this way stay failed — retry them individually via
`POST /api/v1/request/<id>/retry` once the setting's fixed, they don't self-heal.

**Grabbed torrents have almost no seeders/peers**
Check, in order: (1) On Transmission specifically, confirm `dht-enabled`/`pex-enabled`/
`lpd-enabled` are actually on (section 9) — public releases often ship with few/weak
tracker URLs and lean on these for peer discovery; if they got toggled off somehow,
public-tracker grabs lose most of their discovery path. (2) the client's own tracker
view (qBittorrent: right-click a torrent > Trackers tab; Transmission: torrent details
> Trackers) — `status: Working` with real leecher counts but 0 seeds usually just means
the swarm for that specific release is genuinely thin, not a config problem; try a
different release/group. (3) Sonarr/Radarr's per-indexer **Minimum Seeders** (see
section 3) — if it's still the default `1`, thin releases are getting grabbed instead of
rejected.

**A hostname works from other devices but not from the PC running the stack**
Known Docker Desktop / Windows networking quirk looping a machine back to its own LAN
IP unreliably. Use the hosts-file workaround in section 5 rather than chasing it further.

**A hostname stopped resolving on all devices, including phones**
Check, in order: (1) Pi-hole's Query Log — does the query even show up when you try to
load the page? If not, the device isn't asking Pi-hole at all — check the router's DNS
setting hasn't reverted to Automatic, and check the device itself doesn't have a manual
DNS override (iOS: WiFi network's DNS field; Android: Private DNS setting; browsers:
Chrome/Edge/Firefox's built-in "Secure DNS" / DNS-over-HTTPS setting, which bypasses
whatever the OS/router provides entirely). (2) If it does show up in the Query Log, check
the record itself in Local DNS > DNS Records for a typo or missing entry.

**A `*arr` app's page looks unfamiliar / seems to "redirect" somewhere unexpected**
Often just that app's own login page, which can look surprising the first time. Confirm
with a direct request bypassing browser cache:
```powershell
Invoke-WebRequest -Uri http://sonarr.<domain> -MaximumRedirection 0
```
Check the `Location` header in the error response — if it points to that same app's own
`/login`, it's working correctly. If it points somewhere else entirely, that's an actual
Caddy routing problem worth digging into.