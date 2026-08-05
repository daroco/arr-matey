---
name: diagnose-connectivity
description: Systematic checklist for "I can't connect to watch/stats/jellyseerr/correll.tv" issues in this stack. Use before assuming something is actually broken -- most connectivity scares in this stack have turned out to be one of a specific, known set of causes, not a new problem.
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

## 2. Do the DNS records actually match the current WAN IP?

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

## 3. Is Cloudflare itself degraded?

Their API can be down independent of the actual proxied sites (this has happened for
real -- `521`s and read-timeouts on `api.cloudflare.com` calls while the sites
themselves were fine). Check `cloudflarestatus.com` before assuming a local config
problem when only *API calls* are failing, not the actual site.

## 4. Is Caddy actually healthy?

```bash
docker logs caddy --since 5m | grep -iE "error|certificate"
```

Look for `certificate obtained successfully` for every expected hostname and no
repeating error loops (e.g. a crash-loop from a bad Caddyfile edit -- check for
`unrecognized global option` or similar parse errors, which mean a bad reload got
applied; see the `caddy-reload` skill for how to validate before reloading in the
first place).

## 5. Don't trust a same-LAN test

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

**The only trustworthy tests are from a genuinely external vantage point**: a phone
on cellular data (wifi off), or a `WebFetch` call (runs from Anthropic's own
infrastructure, nothing to do with this LAN). Cache-bust with a `?cb=N` query param if
checking right after a DNS/Caddy change, since intermediate caches can mask a fix
that already landed.

## 6. If it was working and just stopped

Check what actually changed, in rough likelihood order: WAN IP rotated (residential
IPs aren't static -- check the ddns log for a recent "updated" line around when it
broke), a Caddyfile edit got reloaded without validation, a VPN got connected (step
1), or a Cloudflare-side change (step 3). Don't assume it's a new class of problem
until 1-5 are all ruled out -- so far, it never has been.
