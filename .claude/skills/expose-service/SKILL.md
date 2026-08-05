---
name: expose-service
description: Expose a new internal service publicly at a new subdomain (Cloudflare DNS, DDNS, Caddy TLS + rate limiting), following this stack's established pattern. Use when adding a new public hostname like the existing watch/jellyseerr/stats routes.
---

# Expose a new service publicly

This stack already has four public routes (`watch`, apex `{$DOMAIN}` + `jellyseerr`,
`stats`) all built the exact same way. Follow this pattern rather than inventing a
new one — README section 6 is the full narrative version of these steps.

## Prerequisites this assumes already exist

- The service already has a working **LAN-only** `http://` Caddyfile block
  (`import lanonly; reverse_proxy ...`).
- `.env` already has `CF_API_TOKEN`, `CF_ZONE`, `ACME_EMAIL` set up (section 6).

## Steps

1. **Create the Cloudflare DNS record** — proxied `A` record pointed at the current
   WAN IP:
   ```bash
   set -a && source .env && set +a
   WAN_IP=$(curl -s https://api.ipify.org)
   ZONE_ID=<the zone id -- GET /zones?name=$CF_ZONE if you don't have it cached>
   curl -s -X POST "https://api.cloudflare.com/client/v4/zones/${ZONE_ID}/dns_records" \
     -H "Authorization: Bearer ${CF_API_TOKEN}" -H "Content-Type: application/json" \
     --data "{\"type\":\"A\",\"name\":\"NEWHOST.${CF_ZONE}\",\"content\":\"${WAN_IP}\",\"ttl\":1,\"proxied\":true}"
   ```
   Confirm `"success":true` in the response.

2. **Add it to `DDNS_RECORDS`** in both `.env` and `.env.example` (comma-separated
   list) so `scripts/ddns-update.py` keeps it pointed at the current WAN IP going
   forward. Don't forget `.env.example`'s doc comment listing the example hostnames.

3. **Add the Caddyfile block** — copy the exact shape of the `stats.{$DOMAIN}` /
   `watch.{$DOMAIN}` blocks (see `Caddyfile`), not a simplified version:
   - LAN `http://` block stays as-is (already exists per the prerequisite above).
   - New `https://` block with:
     - `rate_limit` with two zones (general ~300 req/min, a tighter one on the
       login/auth path specifically if the service has one, ~10 req/min).
     - **Both zones MUST include `match { not remote_ip private_ranges }`.**
       Without this, every LAN device sharing Docker Desktop's collapsed
       `172.18.0.1` identity shares one rate-limit budget and trips false 429s from
       nothing more than normal local browsing — this is a real bug that happened
       once already (see the Caddyfile's own comments on `watch_general`) and cost a
       debugging session to find. Don't reproduce it.
     - `reverse_proxy` to the target with `header_up X-Real-IP {client_ip}`.
   - `acme_dns cloudflare` (already in the global options block) covers any new site
     block's hostname automatically — no global-option changes needed.

4. **Validate and reload** — use the `caddy-reload` skill for this step exactly, do
   not skip validation.

5. **Confirm the cert issued**:
   ```bash
   docker logs caddy --since 15s | grep -iE "certificate obtained|error"
   ```

6. **Verify from a genuinely external vantage point**, not from the LAN — local
   testing of a hostname that's *also* resolved locally is unreliable (see
   `diagnose-connectivity`). A WebFetch call, or the user's phone on cellular data,
   are the only trustworthy checks.

7. Add a **Pi-hole Local DNS record** for the new hostname → this machine's LAN IP if
   you want it reachable by name from inside the house too (manual step in Pi-hole's
   admin UI, not scriptable — same as every other hostname in this stack).

8. **Document it** — README section 6's hostname list, ARCHITECTURE.md's diagram 5 if
   it changes the public-route picture, and `CLAUDE.md`'s "exactly four/five
   hostnames" line.
