# Self-Hosted Media Automation Stack

A Docker Compose stack that lets you request a movie or TV show and have it automatically
searched, downloaded, and dropped into your media library for Jellyfin.

See `ARCHITECTURE.md` for diagrams of the pieces below and how they connect.

**Flow:** Seerr (request) → Sonarr/Radarr (grab logic) → Prowlarr (indexer search,
with FlareSolverr for Cloudflare-protected sites) → a remote seedbox does the actual
torrenting (see section 9 — two separate clients there, qBittorrent for a private
Hit & Run tracker and Transmission for everything else, each with independent
ratio/seed-time/DHT-PEX-LSD settings) → finished files sync back locally via a scheduled
rclone job → files land in your movies/TV folders → your existing host Jellyfin serves
them → Bazarr backfills subtitles. Caddy fronts everything with clean local hostnames
instead of `ip:port`.

There's no local torrent client or VPN in this stack — every indexer routes straight to
the seedbox (section 9 covers why and how). If you'd rather download locally through a
VPN'd qBittorrent instead, that's a materially different, simpler starting point than
what this README documents; the historical shape of that setup (Gluetun, qBittorrent,
port-forwarding sync) is still visible in git history if useful as a reference.

Seerr was Jellyseerr until the project merged with Overseerr and renamed itself — same
app, same config/database, just a new image (`ghcr.io/seerr-team/seerr`) and container
still named `jellyseerr` in this compose file for continuity.

Jellyfin itself is **not** part of this stack — it's assumed to already be running on the
host, untouched.

Built for Windows + Docker Desktop, with host paths on a `D:` drive. Adjust paths if your
setup differs.

---

## Stack components

| Service | Purpose | Port | LAN hostname (via Caddy) |
|---|---|---|---|
| Prowlarr | Indexer aggregator — searches configured indexers, pushes results to Sonarr/Radarr | 9696 | `prowlarr.correll.tv` |
| FlareSolverr | Solves Cloudflare challenges on Prowlarr's behalf for protected indexers | 8191 | — |
| Sonarr | TV show search/grab/organize | 8989 | `sonarr.correll.tv` |
| Radarr | Movie search/grab/organize | 7878 | `radarr.correll.tv` |
| Bazarr | Subtitle fetching for Sonarr/Radarr libraries | 6767 | `bazarr.correll.tv` |
| Seerr | Request front-end — search a title, hit request, it flows to Sonarr/Radarr | 5055 | `jellyseerr.correll.tv` |
| Caddy | Reverse proxy — drops port numbers, gives every service above a clean hostname, and (internal-only) injects Basic Auth for the seedbox | 80 | `jellyfin.correll.tv` also routes here to the host install |

The actual torrent clients (qBittorrent and Transmission) run on the remote seedbox, not
in this compose file — see section 9.

Games are intentionally **not** included — there's no mature Sonarr/Radarr-equivalent for
game libraries. Prowlarr's own search UI plus a manual grab on the seedbox works in the
meantime.

---

## 1. Prerequisites

- Docker Desktop, with the drive holding your media enabled under
  **Settings > Resources > File Sharing**
- A seedbox with qBittorrent and Transmission installable from its app catalog, SFTP
  access (SSH shell access is *not* required — see section 9), and enough disk space
  to hold in-flight downloads for every tracker you use
- rclone installed on the host (`winget install Rclone.Rclone`) — used to sync finished
  seedbox downloads down locally (section 9)
- Pi-hole already running as your network's DNS resolver (used for the clean-hostname
  setup in section 5)

---

## 2. Initial setup

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
  usage on every import (this bit us once; see the Warnings in section 9).
- `SEEDBOX_URL`/`USER`/`PASS`/`BASIC_AUTH` — see section 9 for how these get used
  (Caddy shim + rclone remote).

Create the host directories if they don't already exist, and point your existing Jellyfin
install's libraries at `MEDIA_ROOT/movies` and `MEDIA_ROOT/tv` — that's the only link
between this stack and Jellyfin.

```bash
docker compose up -d
```

---

## 3. Tuning Sonarr/Radarr's indexer behavior

**Minimum Seeders:** if grabs keep landing on releases with almost no seeders, the fix
isn't in the download client — it's a per-indexer setting in Sonarr/Radarr. Settings >
Indexers > edit an indexer > toggle "Show Advanced" (top right) > **Minimum Seeders**
(default `1`, i.e. no real filtering). Raising it to `3`–`5` rejects thin swarms before
they're ever grabbed. Has to be set per indexer.

Tracker-specific client settings (auto-add-trackers lists, DHT/PEX/LSD, ratio/seed-time
policy) all live on the seedbox now, covered in depth in section 9 — there's no local
client to configure here anymore.

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
   - **Download Clients**: set up in section 9, not here — every indexer needs an
     explicit `downloadClientId` pointing at one of the two seedbox clients, which
     requires the Caddy shim to exist first.

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

## 5. LAN-wide access at clean hostnames (`*.correll.tv`)

Requires Pi-hole as your network's DNS. The `Caddyfile` in this repo has a route for
every service, each on its own subdomain of `correll.tv` (a domain already owned,
repurposed here for LAN-only names — these records are never published publicly,
they only resolve for devices using your Pi-hole):

| Hostname | Routes to |
|---|---|
| `jellyseerr.correll.tv` | Seerr |
| `jellyfin.correll.tv` | your host Jellyfin |
| `prowlarr.correll.tv` | Prowlarr |
| `sonarr.correll.tv` | Sonarr |
| `radarr.correll.tv` | Radarr |
| `bazarr.correll.tv` | Bazarr |

(qBittorrent/Transmission WebUIs live on the seedbox now, not on a local hostname —
reach them at the seedbox's own URL directly, e.g. `https://your-seedbox/qbittorrent/`.)

Using a real, publicly-registered TLD (`.tv`) instead of a made-up one matters here:
browsers decide whether a typed address is a URL or a search query based on whether
the suffix is a recognized domain — a fake TLD often gets treated as a search term
instead of navigated to. A real TLD is always recognized, so these load as pages, not
search results, with no extra configuration needed.

This is plain HTTP, deliberately — see the note below on why HTTPS isn't in play here.

1. **Point your router at Pi-hole.** On Google Wifi: Google Home app > Wifi > Settings
   (gear) > Advanced networking > DNS > Custom > set to your machine's LAN IP.
2. **Add a local DNS record in Pi-hole for each hostname above** — admin UI > Local DNS >
   DNS Records — every one points at the same IP, your machine's LAN IP. This only
   affects devices using your Pi-hole for DNS; it doesn't touch what `correll.tv`
   resolves to for anyone outside your network.
3. **Free up port 80 for Caddy.** Pi-hole's own admin UI often also defaults to port 80 —
   if so, remap it (e.g. `8081:80` instead of `80:80`) in Pi-hole's compose file and
   recreate it. Pi-hole's DNS function (port 53) is unaffected either way.
4. **Bring up / reload Caddy:**
   ```bash
   docker compose up -d caddy
   ```
   or, if it's already running and you just changed the `Caddyfile`:
   ```bash
   docker compose restart caddy
   ```
5. **Reserve your PC's LAN IP** in the Google Wifi app (Devices > your PC > reserve IP)
   so the DNS records don't silently break if the router hands out a different address
   later.

**On the machine actually running the stack**, reaching its own LAN IP can be
unreliable due to how Docker Desktop's Windows networking loops traffic back to itself.
If any of these hostnames don't resolve on that PC specifically (but work fine on every
other device), add hosts-file entries instead of relying on DNS for that one machine:

1. Open Notepad **as Administrator**
2. Open `C:\Windows\System32\drivers\etc\hosts`
3. Add one line per hostname, all pointing at loopback:
   ```
   127.0.0.1 jellyseerr.correll.tv
   127.0.0.1 jellyfin.correll.tv
   127.0.0.1 prowlarr.correll.tv
   127.0.0.1 sonarr.correll.tv
   127.0.0.1 radarr.correll.tv
   127.0.0.1 bazarr.correll.tv
   ```
4. Save, then `ipconfig /flushdns`

### Why plain HTTP, not HTTPS

`correll.tv` is a real domain, but these subdomains only resolve on your LAN — Let's
Encrypt can't issue a normal certificate for a name it can't reach, and a self-signed
cert would just bring back the "not secure" warning until every device trusted a
custom root CA. Since the whole point here was zero extra setup per device, the
`Caddyfile`'s `auto_https off` global option keeps Caddy from touching port 443 or
attempting any TLS at all. If a browser's "HTTPS-first" mode tries `https://` before
`http://`, it gets connection-refused (nothing is listening on 443) rather than a
certificate warning, and falls back to plain HTTP automatically.

---

## 6. Access outside your home network

Two options, different risk profiles:

**Tailscale (recommended).** Private mesh VPN between your devices — nothing exposed to
the public internet. Install on your PC and phone, same account on both, and your phone
reaches the stack as if it were on your home WiFi from anywhere. None of these apps
(Sonarr, Radarr, Prowlarr) are built with "hostile public internet" as a threat model —
several have had real CVEs over the years — so keeping them reachable only via a private
mesh instead of an open port is the safer default.

**Port forward + real domain + Caddy TLS.** Forward port 443 on your router to Caddy,
get a domain, Caddy auto-issues certs via Let's Encrypt. More convenient, meaningfully
riskier — only worth doing for Jellyfin/Seerr specifically, keep the *arr apps reachable
only from inside the network either way. The seedbox's own torrent clients are already
reached over the internet directly (that's the nature of a seedbox) and have their own
auth in front — not something this stack's network exposure affects either way.

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
   - qBittorrent (private tracker): ratio 1.0 **or** the tracker's Hit & Run seed-time
     requirement, whichever's sooner — check the tracker's *actual* rule rather than
     assume a round number (240 hours / `14400` minutes for this tracker's rule).
     `POST /qbittorrent/api/v2/app/setPreferences`: `max_ratio_enabled=true`,
     `max_ratio=1`, `max_seeding_time_enabled=true`, `max_seeding_time=14400`.
     **Action must be `max_ratio_act=0` (Pause) — not "Remove + delete files."** Sonarr
     actively refuses to add a download client configured to auto-delete on its ratio
     limit ("qBittorrent is configured to remove torrents when they reach their Share
     Ratio Limit"), and this is a real safety catch: once Sonarr genuinely tracks a
     client, a delete-on-limit action races the rclone sync — the client could delete a
     file before rclone ever copies it down, losing it permanently. Pausing leaves the
     file in place; Sonarr's own "Remove completed downloads" (already on) cleans up the
     torrent *after* confirming a successful import, not on the client's own timeline.
   - Transmission (public trackers, no real tracker obligation): a modest ratio + a
     disk-hygiene time cap is reasonable, since there's nothing to comply with. Via the
     JSON-RPC endpoint (`session-set`): `"seedRatioLimit": 1, "seedRatioLimited": true`
     plus `"idle-seeding-limit": <minutes>, "idle-seeding-limit-enabled": true` as a
     backstop. **Important semantic gap**: Transmission's (and Deluge's) idle-seed-time
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
   ```
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
   `D:\appdata\seedbox-cleanup.log` (silent/no file if nothing currently qualifies); run
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
Invoke-WebRequest -Uri http://sonarr.correll.tv -MaximumRedirection 0
```
Check the `Location` header in the error response — if it points to that same app's own
`/login`, it's working correctly. If it points somewhere else entirely, that's an actual
Caddy routing problem worth digging into.