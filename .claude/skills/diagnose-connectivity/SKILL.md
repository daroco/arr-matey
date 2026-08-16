---
name: diagnose-connectivity
description: Systematic checklist for "I can't connect to watch/stats/correll.tv" issues in this stack. Use before assuming something is actually broken -- most connectivity scares in this stack have turned out to be one of a specific, known set of causes, not a new problem.
---

# Diagnose "can't connect to X.correll.tv"

This stack has hit the same handful of root causes for connectivity problems
repeatedly. Check them **in this order** before doing anything else -- most of the
time it's one of these, not a genuinely new issue, and later steps depend on having
ruled out earlier ones.

## 1. Is a VPN hijacking outbound traffic on the host?

A system-wide VPN client (ProtonVPN, etc.) with a lower-metric route than the real
adapter silently reroutes *everything*, including `scripts/ddns-update.py`'s "what's
my WAN IP" check -- corrupting the DNS records with the VPN's exit IP instead of the
real one. This has happened for real and cost significant debugging time before the
cause was found.

```powershell
route print -4 | Select-String "0.0.0.0          0.0.0.0"
```

A second `0.0.0.0` default route with **metric `0`** (lower wins) alongside the real
one means a VPN is active and hijacking traffic. Confirm by checking what IP the host
thinks it has:

```bash
curl -s https://api.ipify.org
curl -s "https://ipinfo.io/$(curl -s https://api.ipify.org)/json"   # org field will say a hosting/VPN provider, not an ISP
```

Fix: disconnect the VPN from its own app (not just killing the GUI process -- the
tunnel/service can survive that; disabling the network adapter needs elevation this
environment doesn't have by default, so ask the user to do it via the app if a script
can't).

## 2. Is the ISP's own modem/router actually up?

This has been the real root cause at least once, and it's fast to rule out. A modem
can go into a degraded state -- basic outbound connectivity limps along (DDNS checks,
API calls, etc. still succeed) while inbound forwarded connections fail outright --
that looks *exactly* like a software/DNS/Caddy problem from this side, and none of the
checks below will explain it because none of them are actually broken.

Check the ISP's own status portal/app first (e.g. Spectrum's My Spectrum app/site) for
the modem specifically, separate from the router: confirmed live as showing
"Connection status unavailable for some equipment" / "Modem: Status Unavailable" while
the router itself still showed "Connected" -- an easy thing to miss if you only glance
at overall internet status. If it's flagged, power-cycle the modem (the ISP's own
guided reboot flow if it has one) before spending time on anything else here. On a
double-NAT setup (ISP modem/router -> a second router, e.g. Google WiFi), also
power-cycle the second router afterward if the first restart alone doesn't fix it --
it can be holding a stale WAN-side session from before the modem came back.

## 3. Do the DNS records actually match the current WAN IP?

```bash
set -a && source .env && set +a
WAN_IP=$(curl -s https://api.ipify.org)
curl -s "https://api.cloudflare.com/client/v4/zones/<ZONE_ID>/dns_records?per_page=100" \
  -H "Authorization: Bearer ${CF_API_TOKEN}" | py -c "
import json,sys
for r in json.load(sys.stdin)['result']:
    print(r['type'], r['name'], '->', r['content'])
"
```

Compare every record against `$WAN_IP`. Also check
`D:\appdata\ddns-update.log`'s tail for the last "updated"/"no change" lines -- if the
log shows regular "no change" entries matching the current IP, DNS is healthy and the
problem is elsewhere.

## 4. Is Cloudflare itself degraded?

Their API can be down independent of the actual proxied sites (this has happened for
real -- `521`s and read-timeouts on `api.cloudflare.com` calls while the sites
themselves were fine). Check `cloudflarestatus.com` before assuming a local config
problem when only *API calls* are failing, not the actual site. Their status page can
also lag or miss small/regional edge issues -- a `522` (edge couldn't reach the
origin) on one hostname while others on the identical origin/DNS/Caddy config work
fine, with the status page showing nothing active, still points at a stuck
edge-routing entry for that one record. Fix: toggle that DNS record's proxy status
off then back on (a plain PATCH via the API, using the existing `CF_API_TOKEN`) --
forces Cloudflare to rebuild its edge-to-origin routing for that record.

## 5. Is Caddy actually healthy?

```bash
docker logs caddy --since 5m | grep -iE "error|certificate"
```

Look for `certificate obtained successfully` for every expected hostname and no
repeating error loops (e.g. a crash-loop from a bad Caddyfile edit -- check for
`unrecognized global option` or similar parse errors, which mean a bad reload got
applied; see the `caddy-reload` skill for how to validate before reloading in the
first place).

**Caveat, confirmed live and worth remembering**: no `log` directive is configured
anywhere in this `Caddyfile`, so `docker logs caddy` only ever shows `http.log.error`
entries -- *successful* requests are never logged at all. Zero log lines (even over a
90-minute window) is **not** evidence that nothing reached the origin; it only means
nothing *failed*. This produced a real false "still broken" conclusion in a live
debugging session, after the actual problem (a dead ISP modem, see step 2) had
already been fixed. Don't trust log-absence as a signal here without adding a real
`log` directive first.

## 6. Does Pi-hole's local DNS actually have every public hostname?

Every hostname with a public Cloudflare record also needs a matching entry in
Pi-hole's local DNS (`custom.list`, LAN-only IP) or LAN clients fall through to
public DNS and hit the *public* (Cloudflare) IP instead of the LAN IP -- which then
depends on the router supporting NAT hairpinning correctly, unreliable on this
stack's double-NAT setup. Confirmed live: a hostname added to the public routes
(`stats.{$DOMAIN}`) never got a matching Pi-hole entry, so it failed on WiFi
specifically while every other hostname worked fine.

```bash
MSYS_NO_PATHCONV=1 docker exec pihole cat /etc/pihole/hosts/custom.list
```

Every public hostname should appear here pointed at the LAN IP. If one's missing, add
it via Pi-hole's admin UI (`http://<lan-ip>:8081/admin` -- confirmed via `docker port
pihole`, not the plain default `:80` you might expect) under Local DNS Records, not
by editing the file directly -- it's auto-generated and says as much in its own
header (manual edits get wiped on the next config change).

## 7. Don't trust a same-LAN test, and don't over-trust your testing tool either

Testing a hostname from the same network it also resolves on locally is
**unreliable**, for two independent reasons that have both bitten this stack for
real:

- **NAT hairpinning**: a device on the LAN hitting its own network's public IP/domain
  can loop back unpredictably depending on the router, giving false negatives (or
  even false positives) that don't reflect real external reachability.
- **Docker Desktop's own networking** makes every LAN-sourced connection to a
  published container port on Windows appear to originate from one internal bridge
  gateway address (e.g. `172.18.0.1`), not the real device -- this is *also* why
  rate-limit zones need `not remote_ip private_ranges` (see `expose-service`).

Use a genuinely external vantage point: a phone on cellular data (wifi off), or a
`WebFetch` call. Cache-bust with a `?cb=N` query param if checking right after a
DNS/Caddy change, since intermediate caches can mask a fix that already landed --
**but confirmed live, cache-busting the URL is not enough**: both `WebFetch` and a
real phone's app-level "connect to server" check returned a clean success while the
origin was still genuinely unreachable (Caddy logged zero real traffic the entire
time), because something in the path -- Cloudflare's edge cache, most likely -- was
serving a stale cached response from before the outage without ever touching the
origin. A "connect" ping or a login screen loading is not proof; it can be answered
from cache. The one test that can't be faked by a cache: something that requires a
**sustained, stateful, real backend round-trip** -- actually logging in (a POST,
never cached) or, stronger still, actually streaming real video (Jellyfin: confirmed
live, a genuine active transcode with a freshly-issued access token is airtight proof
the whole chain works, since that cannot be served from any cache).

## 8. If it was working and just stopped

Check what actually changed, in rough likelihood order: the ISP modem (step 2, the
actual cause at least once -- nothing on the software side changed at all that time),
WAN IP rotated (residential IPs aren't static -- check the ddns log for a recent
"updated" line around when it broke), a Caddyfile edit got reloaded without
validation, a VPN got connected (step 1), or a Cloudflare-side change (step 4). Don't
assume it's a new class of problem until 1-7 are all ruled out -- so far, it never
has been.
