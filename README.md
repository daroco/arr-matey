# Self-Hosted Media Automation Stack

A Docker Compose stack that lets you request a movie or TV show and have it automatically
searched, downloaded (behind a VPN), and dropped into your media library for Jellyfin.

**Flow:** Seerr (request) → Sonarr/Radarr (grab logic) → Prowlarr (indexer search,
with FlareSolverr for Cloudflare-protected sites) → qBittorrent behind Gluetun (VPN'd
download, port-forwarded, auto-synced) → files land in your movies/TV folders → your
existing host Jellyfin serves them → Bazarr backfills subtitles. Caddy fronts everything
with clean local hostnames instead of `ip:port`.

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
| Gluetun | VPN tunnel (ProtonVPN via WireGuard) with port forwarding | 8080 (shared with qBittorrent) | — |
| qBittorrent | Download client | 8080 (via gluetun) | `qbt.correll.tv` |
| qbittorrent-port-sync | Watches gluetun's forwarded port, pushes changes into qBittorrent automatically | — | — |
| Prowlarr | Indexer aggregator — searches configured indexers, pushes results to Sonarr/Radarr | 9696 | `prowlarr.correll.tv` |
| FlareSolverr | Solves Cloudflare challenges on Prowlarr's behalf for protected indexers | 8191 | — |
| Sonarr | TV show search/grab/organize | 8989 | `sonarr.correll.tv` |
| Radarr | Movie search/grab/organize | 7878 | `radarr.correll.tv` |
| Bazarr | Subtitle fetching for Sonarr/Radarr libraries | 6767 | `bazarr.correll.tv` |
| Seerr | Request front-end — search a title, hit request, it flows to Sonarr/Radarr | 5055 | `jellyseerr.correll.tv` |
| Caddy | Reverse proxy — drops port numbers, gives every service above a clean hostname | 80 | `jellyfin.correll.tv` also routes here to the host install |

Games are intentionally **not** included — there's no mature Sonarr/Radarr-equivalent for
game libraries. Prowlarr's own search UI plus a manual qBittorrent grab works in the
meantime.

---

## 1. Prerequisites

- Docker Desktop, with the drive holding your media enabled under
  **Settings > Resources > File Sharing**
- A VPN subscription with WireGuard support and port forwarding (this guide uses
  ProtonVPN — Proton's port forwarding requires enabling **NAT-PMP** on their
  WireGuard config page, and **Moderate NAT must be unchecked**, they're mutually exclusive)
- Pi-hole already running as your network's DNS resolver (used for the clean-hostname
  setup in section 6)

---

## 2. Initial setup

```bash
cp .env.example .env
```

Fill in:

- `MOVIES_PATH`, `TV_PATH`, `DOWNLOADS_PATH`, `CONFIG_ROOT` — your actual host folders
  (e.g. `D:/movies`, `D:/TV Shows`, `D:/downloads`, `D:/appdata`). Keep `DOWNLOADS_PATH`
  on the **same drive** as your library folders — Sonarr/Radarr import by moving the
  finished file, and same-drive means an instant rename instead of a slow copy+delete.
- `VPN_SERVICE_PROVIDER`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES` — from your VPN
  provider's WireGuard config. For ProtonVPN: generate a WireGuard config from their
  dashboard, open the `.conf` file, and copy just the value after `PrivateKey =` — the
  same key works for any of their servers, you don't need the rest of the file.
- `VPN_SERVER_COUNTRIES` — e.g. `Canada`
- `QBITTORRENT_USER` / `QBITTORRENT_PASS` — the **permanent** WebUI login you set in
  qBittorrent (not the temp startup password), used by `qbittorrent-port-sync`

Create the host directories if they don't already exist, and point your existing Jellyfin
install's libraries at the same `MOVIES_PATH`/`TV_PATH` folders — that's the only link
between this stack and Jellyfin.

```bash
docker compose up -d
```

---

## 3. Verify the VPN tunnel and port forwarding

```bash
docker logs gluetun
```

Confirm it reports a successful connection, then check the actual exit IP and country:

```bash
docker exec gluetun wget -qO- ipinfo.io/json
```

`country` should match what you set, and the IP should not match your real ISP address.

Port forwarding (`VPN_PORT_FORWARDING=on`, `VPN_PORT_FORWARDING_PROVIDER=protonvpn`) is
already set in the `gluetun` service — this matters because without a forwarded, open
port, no peers can connect to you and downloads sit stuck on "Downloading metadata" or
"Stalled." The `qbittorrent-port-sync` container handles keeping qBittorrent's listening
port matched to whatever gluetun gets assigned, automatically, including when it changes
after a reconnect. If a stalled/no-peers issue ever comes back, check
`docker logs qbittorrent-port-sync` first — it logs every port it detects and pushes, so
you can confirm whether it's syncing or silently failing (wrong password in `.env` is the
usual cause of the latter).

Manual check, if you ever need it:
```bash
docker exec gluetun cat /tmp/gluetun/forwarded_port
```
Compare against qBittorrent's **Tools > Options > Connection** listening port — should
always match if the sync sidecar is running correctly.

---

## 4. qBittorrent setup

Open `localhost:8080` (or `http://qbittorrent` once section 6 is done).

**Login:** Since qBittorrent 4.6.1+, there's no static default password. Get the
temporary one from the logs:

```bash
docker logs qbittorrent
```

Look for: `The WebUI administrator password was not set. A temporary password is
provided for this session: <password>`. Username is `admin`. Log in, then immediately
go to **Tools > Options > WebUI** and set a permanent username/password — otherwise a
new random password generates on every restart, and `qbittorrent-port-sync` will fail
to authenticate.

**Do not enable** "Bypass authentication for localhost" or "for subnet" under WebUI
settings — the subnet bypass hands control to anyone on your LAN, and the localhost
bypass is unreliable anyway once traffic is NAT'd through Docker Desktop's networking.

**Host header validation** (same WebUI settings page): set to `*` (wildcard) so any
hostname Caddy or the *arr apps present is accepted, rather than allowlisting each one
individually. Fine for a home LAN; wouldn't recommend it if this were ever exposed
beyond your network.

**Trackers:** Tools > Options > BitTorrent > "Automatically add these trackers to new
downloads" — paste in a maintained list, e.g. the `best` list from
[ngosang/trackerslist](https://github.com/ngosang/trackerslist). Applies to new
downloads only, not retroactively. This list is never applied to private-tracker
torrents — qBittorrent skips it automatically for anything flagged `private` in its
metadata, so it can't conflict with a private tracker's own tracker-only rule.

**Minimum Seeders:** if grabs keep landing on releases with almost no seeders, the fix
isn't in qBittorrent — it's a per-indexer setting in Sonarr/Radarr. Settings > Indexers >
edit an indexer > toggle "Show Advanced" (top right) > **Minimum Seeders** (default `1`,
i.e. no real filtering). Raising it to `3`–`5` rejects thin swarms before they're ever
grabbed. Has to be set per indexer.

**Private tracker rules (e.g. DHT/PEX/LSD):** some private trackers require disabling
DHT, PEX, and LSD (Options > BitTorrent > Peer Discovery) globally, even though
compliant clients already skip all three automatically for any torrent flagged
`private` — the blanket rule is usually about the tracker's own anti-cheat/DHT-crawling
checks, not a real technical gap. It's a global toggle, not per-torrent, so disabling it
also reduces peer discovery on your public-tracker downloads on whichever qBittorrent
instance (home and/or seedbox) you apply it to. Check the specific tracker's rules page.

---

## 5. Wire the *arr apps together

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
   - Settings > Download Clients > add qBittorrent — **host `gluetun`**, not
     `qbittorrent` (qBittorrent shares gluetun's network stack), port `8080`.
   - Settings > Media Management > Root Folders > add `/tv` (Sonarr) or `/movies`
     (Radarr) — these map to your host `TV_PATH`/`MOVIES_PATH`. This step also has to be
     done before Seerr's root folder dropdown will show anything.
   - Settings > Profiles — edit your quality profile to uncheck qualities you don't
     want (e.g. Bluray/Remux) and reorder the rest by preference.

4. **Bazarr** (`:6767`): connect to Sonarr and Radarr the same way (hostname + API key),
   set subtitle languages/providers.

5. **Seerr** (`:5055`):
   - Jellyfin URL: `http://host.docker.internal:8096` (Docker Desktop's special DNS name
     for reaching the host machine from inside a container)
   - External URL: your machine's actual LAN IP, e.g. `http://192.168.x.x:8096` — this is
     just what gets displayed/linked to users, not used for the internal connection
   - Forgot Password URL: optional, safe to leave blank
   - Add Sonarr (`sonarr:8989`) and Radarr (`radarr:7878`) as request targets, API keys
     again, root folders `/tv` and `/movies`

None of this wiring happens automatically just because the containers are networked
together — every connection above needs its API key pasted in manually, once.

---

## 6. LAN-wide access at clean hostnames (`*.correll.tv`)

Requires Pi-hole as your network's DNS. The `Caddyfile` in this repo has a route for
every service, each on its own subdomain of `correll.tv` (a domain already owned,
repurposed here for LAN-only names — these records are never published publicly,
they only resolve for devices using your Pi-hole):

| Hostname | Routes to |
|---|---|
| `jellyseerr.correll.tv` | Seerr |
| `jellyfin.correll.tv` | your host Jellyfin |
| `qbt.correll.tv` | qBittorrent WebUI |
| `prowlarr.correll.tv` | Prowlarr |
| `sonarr.correll.tv` | Sonarr |
| `radarr.correll.tv` | Radarr |
| `bazarr.correll.tv` | Bazarr |

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
   127.0.0.1 qbt.correll.tv
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

## 7. Access outside your home network

Two options, different risk profiles:

**Tailscale (recommended).** Private mesh VPN between your devices — nothing exposed to
the public internet. Install on your PC and phone, same account on both, and your phone
reaches the stack as if it were on your home WiFi from anywhere. None of these apps
(Sonarr, Radarr, qBittorrent, Prowlarr) are built with "hostile public internet" as a
threat model — several have had real CVEs over the years — so keeping them reachable
only via a private mesh instead of an open port is the safer default.

**Port forward + real domain + Caddy TLS.** Forward port 443 on your router to Caddy,
get a domain, Caddy auto-issues certs via Let's Encrypt. More convenient, meaningfully
riskier — only worth doing for Jellyfin/Seerr specifically, keep the *arr apps and
qBittorrent reachable only from inside the network either way.

---

## 8. Start on machine boot

Docker Desktop > Settings > General > enable **"Start Docker Desktop when you log in."**
Every service in this compose file has `restart: unless-stopped`, so containers come
back up automatically once Docker Desktop launches — no compose changes needed.
`unless-stopped` means "always restart unless a human explicitly stopped it," as opposed
to `always`, which would restart even a deliberate stop.

---

## 9. Using it

Search a title in Seerr, hit **Request**. Track status on the **Requests** tab:
Pending (not yet approved) → Processing (grabbed, downloading) → Partially Available
→ Available. Once something's fully downloaded, Jellyfin is the better place to actually
browse your library — Seerr's list is more useful for tracking things still in
progress.

---

## 10. Seedbox as primary downloader for a private tracker (optional)

If a private tracker requires maintaining upload ratio, this stack can have the
**seedbox do the actual downloading and seeding** for that tracker's content — using its
bandwidth instead of home's — with finished files synced back locally afterward for
Sonarr/Radarr to import. This replaced an earlier, simpler version of this feature that
downloaded everything twice (locally *and* on the seedbox, just to mirror a copy for
ratio) — worth knowing that history since some of the gotchas below stem directly from
switching architectures mid-project.

**Architecture:** Sonarr/Radarr's private-tracker indexer routes its grabs directly to a
download client pointing at the seedbox (not local qBittorrent). Since the seedbox's
qBittorrent sits behind an authenticating reverse proxy that Sonarr/Radarr's built-in
qBittorrent client can't talk to directly, Caddy — already in this stack — sits in
between as an auth-injecting shim. Once a file finishes downloading, a scheduled rclone
sync pulls it down from the seedbox over SFTP; Sonarr/Radarr pick it up via Remote Path
Mapping and import it normally. The seedbox keeps seeding independently the whole time,
unaffected by whether or when the sync happens.

### Why a Caddy shim, specifically

The seedbox's qBittorrent WebUI is commonly placed behind a reverse proxy requiring HTTP
Basic Auth, with qBittorrent's own login bypassed once Basic Auth passes (confirmed via
`curl -u user:pass https://seedbox/qbittorrent/api/v2/app/version` succeeding with no
separate qBittorrent-native login at all). Sonarr/Radarr's built-in qBittorrent
download-client type has no field for a Basic Auth layer distinct from qBittorrent's own
username/password (which get sent as a login POST body, not an `Authorization` header)
— pointing Sonarr directly at a Basic-Auth-gated seedbox fails with a 401 before
qBittorrent is ever reached. Rather than gamble on undocumented URL-embedded-credential
behavior, Caddy reverse-proxies to the seedbox and injects the header itself, so
Sonarr/Radarr just talk plain HTTP to `caddy:8090` on the internal Docker network — no
credentials on the Sonarr/Radarr side at all.

### Setup

1. **`.env`**: fill in `SEEDBOX_URL`/`USER`/`PASS` (reference values, not read by any
   container directly) and `SEEDBOX_BASIC_AUTH` — `base64("user:pass")`, computed with
   `echo -n "user:pass" | base64`. This is the value Caddy actually injects.
2. **Caddyfile**: an internal-only site, not published to the host —
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
   another container on the same network — `docker exec sonarr curl http://caddy:8090/qbittorrent/api/v2/app/version`
   should return a version string with **no credentials** on the client side.
3. **New download client** in Sonarr/Radarr (Settings > Download Clients > qBittorrent):
   Host=`caddy`, Port=`8090`, UseSsl=off, UrlBase=`/qbittorrent`, username/password can
   be dummy values (auth already happened at the shim). Category `ratio`. **Priority
   worse than the default client's** (e.g. `50` vs. `1`) — see warning below, this isn't
   optional.
4. **Route the tracker's indexer** to this client via `downloadClientId` — **not tags**
   (see warning below):
   ```bash
   curl -X PUT "http://localhost:8989/api/v3/indexer/<INDEXER_ID>" \
     -H "X-Api-Key: <SONARR_API_KEY>" -H "Content-Type: application/json" \
     --data-binary @- <<'EOF'
   { ...full indexer object from GET, with "downloadClientId": <NEW_CLIENT_ID>... }
   EOF
   ```
   (Radarr is identical, port `7878`.)
5. **Remote Path Mapping** (Settings > Download Clients > Remote Path Mappings): Host=
   `caddy`, Remote Path=the seedbox's actual `save_path` **including the category
   subfolder** qBittorrent appends on its own for categorized downloads (check a real
   torrent via `GET /api/v2/torrents/info` — don't guess it; e.g.
   `/home/user/torrents/qbittorrent/ratio/`, not just `.../qbittorrent/`), Local Path=
   `/downloads/seedbox/` (covered by the existing `${DOWNLOADS_PATH}:/downloads` mount →
   `D:/downloads/seedbox` on the host — create this folder first, Sonarr/Radarr validate
   it exists before accepting the mapping). Keep this in sync with whatever path the
   rclone sync (step 7) actually targets — mismatching the two (e.g. syncing from the
   category subfolder but mapping from its parent) means Sonarr/Radarr look for the file
   one directory level away from where it actually lands locally.
6. **Seedbox seeding policy** — ratio 1.0 **or** 7 days, whichever's sooner, via
   `POST /qbittorrent/api/v2/app/setPreferences`: `max_ratio_enabled=true`,
   `max_ratio=1`, `max_seeding_time_enabled=true`, `max_seeding_time=10080` (minutes).
   **Action must be `max_ratio_act=0` (Pause) — not "Remove + delete files."** Sonarr
   actively refuses to add a download client configured to auto-delete on its ratio
   limit ("qBittorrent is configured to remove torrents when they reach their Share
   Ratio Limit"), and this is a real safety catch: once Sonarr genuinely tracks the
   seedbox as its download client, a delete-on-limit action races against the rclone
   sync — the seedbox could delete a file before rclone ever copies it down, losing it
   permanently. Pausing leaves the file in place; Sonarr's own "Remove completed
   downloads" (already on for this client) cleans up the torrent *after* confirming a
   successful import, not on qBittorrent's independent timeline.
7. **rclone**, over the seedbox's SFTP subsystem — works without SSH shell access, since
   SFTP is a distinct SSH subsystem that only does file operations, never arbitrary
   command execution (exactly why providers commonly disable shell access while leaving
   SFTP enabled). One-time setup:
   ```bash
   rclone config create seedbox sftp host=<seedbox-host> port=<sftp-port> user=<user> pass=<pass> --obscure
   ```
   Scheduled (Windows Task Scheduler, every few minutes):
   ```
   rclone sync "seedbox:<remote save_path>" "D:\downloads\seedbox" --min-age 30s
   ```
   `--min-age 30s` skips files still being written remotely; rclone also writes to a
   `.partial` temp name and renames atomically on completion, which independently
   guards against Sonarr/Radarr importing a half-copied file.

### Warnings (all hit for real running this setup, not theoretical)

**Do not use indexer Tags for step 4**, even though tag-based download-client selection
exists and looks like the obvious way to do it — an indexer with *any* tag gets its
releases **rejected outright** for any series/movie that doesn't share that tag
(`IndexerTagSpecification` in Sonarr's decision engine). Since Seerr-created requests
carry no tags by default, a tagged private-tracker indexer silently stops being searched
for anything requested through Seerr — not "deprioritized," genuinely never searched.
`downloadClientId` routes directly to one client with no such side effect. If you do use
a tag anywhere in this setup, the download client's own tag list must also stay empty,
or the tag-matching filter excludes it before the `downloadClientId` check ever runs,
throwing `DownloadClientUnavailableException` on every grab.

**Two same-priority download clients silently round-robin every other grab between
them.** `downloadClientId` on the indexer only short-circuits selection for *that*
indexer; every other indexer with no override falls through to Sonarr/Radarr's normal
client-selection logic, which groups all *equal-priority* clients together and
load-balances across them (`DownloadClientProvider.GetDownloadClient` in Sonarr's
source). Since the seedbox client has to be untagged to avoid the tag-exclusion problem
above, it ends up untagged *and* same-priority as the default client unless you
explicitly set step 3's Priority worse — meaning roughly half of everything grabbed from
any other indexer randomly lands on the seedbox too, regardless of tracker. If this
already happened before the priority fix went in, recategorize the wrongly-routed
torrents back in qBittorrent — filter by category, check each one's `private` field (or
tracker URL) to tell genuine private-tracker grabs apart from the misrouted ones, and
bulk-set the correct category via `POST /api/v2/torrents/setCategory`.

**qBittorrent's "Seeding Time Limiting" and ratio limit share one action setting** —
`max_ratio_act` fires for whichever of ratio/seeding-time/inactive-seeding-time is met
first; there's no way to configure a different action per limit type. This is what
makes "ratio 1.0 or 7 days, whichever's sooner" a single native settings change (both
limits active, shared action) rather than something needing custom scripting — but it
also means a stray seeding-time cap enabled elsewhere can silently override an intended
ratio target, on both the home instance and the seedbox. Worth checking both if seeding
durations look off from what you configured.

**Point the initial rclone sync at the category subfolder specifically, not the
seedbox's whole torrent directory** — syncing the parent path pulls down *everything*
on the seedbox, including any unrelated content that predates this setup or that a
seedbox provider's other users' torrents happen to share the same box (harmless
security-wise, since it's your own account's SFTP root, but a real waste of local disk
space and bandwidth for content that has nothing to do with this tracker). Scope the
sync source to the exact category subfolder from the start, and make sure it matches
whatever Remote Path Mapping (step 5) actually points at — the two have to describe the
same remote location or Sonarr/Radarr look for the synced file one directory level away
from where it actually lands.

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
Almost always means qBittorrent has no forwarded port, so no peers can connect to you —
see section 3. `qbittorrent-port-sync` handles re-syncing this automatically; check its
logs if this recurs to confirm it's actually keeping up.

**Sonarr/Radarr: "Unable to connect to qBittorrent"**
- Check **Tools > Options > Web UI > "Enable Host header validation"** in qBittorrent —
  set to `*` (see section 4) or add `gluetun` to the allowed domains specifically.
- Confirm you're using the current permanent password, not an old temp one.
- Repeated failed logins can trigger qBittorrent's IP ban — restart the container to
  clear it.

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

**qBittorrent WebUI suddenly returns an empty page**
Check `docker logs gluetun` — if the VPN tunnel dropped, gluetun's kill-switch firewall
blocks everything routing through it, including WebUI access, even though the container
still shows "running." A full restart of both containers usually clears a stuck state
after a settings change.

**Seerr's root folder dropdown is empty**
Sonarr/Radarr need a root folder configured first (Settings > Media Management > Root
Folders) — Seerr's dropdown just mirrors whatever exists there.

**Grabbed torrents have almost no seeders/peers**
Check, in order: (1) `docker logs gluetun` for a `port forwarded is <port>` line, and
that `docker logs qbittorrent-port-sync` shows it syncing that port into qBittorrent —
no forwarded port means no one can connect to you (see section 3). (2) qBittorrent's own
tracker view (right-click a torrent > Trackers tab) — `status: Working` with real
leecher counts but 0 seeds usually just means the swarm for that specific release is
genuinely thin, not a config problem; try a different release/group. (3) Sonarr/Radarr's
per-indexer **Minimum Seeders** (see section 4) — if it's still the default `1`, thin
releases are getting grabbed instead of rejected.

**A hostname works from other devices but not from the PC running the stack**
Known Docker Desktop / Windows networking quirk looping a machine back to its own LAN
IP unreliably. Use the hosts-file workaround in section 6 rather than chasing it further.

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