---
name: caddy-reload
description: Validate and safely apply a Caddyfile edit to the running caddy container without downtime. Use whenever the Caddyfile has been changed and needs to go live.
---

# Caddy reload

This repo's `Caddyfile` is bind-mounted into the running `caddy` container
(`compose.yaml`), not baked into the image. That means `docker compose up -d` alone
is a **no-op** after editing it — Compose only restarts a container when the service
*definition* changes, and a bind-mounted file edit doesn't count. You have to
validate and reload explicitly.

## Steps

1. **Validate with `caddy fmt` + `caddy adapt`, offline, before touching the live
   container.** Must use the custom `acquisitions-caddy:latest` image (built from
   `Dockerfile.caddy`), not stock `caddy:latest` — the stock image doesn't have the
   `caddy-dns/cloudflare` or `mholt/caddy-ratelimit` modules registered, and using it
   produces a confusing, unrelated-looking error (`module not registered:
   dns.providers.cloudflare`) instead of a real validation failure.

   On Windows Git Bash, prefix with `MSYS_NO_PATHCONV=1` or the container-side
   absolute path gets silently mangled into a Windows path and the mount fails.

   ```bash
   MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile" \
     --env-file .env acquisitions-caddy:latest caddy fmt --overwrite /etc/caddy/Caddyfile
   MSYS_NO_PATHCONV=1 docker run --rm -v "$(pwd)/Caddyfile:/etc/caddy/Caddyfile" \
     --env-file .env acquisitions-caddy:latest caddy adapt --config /etc/caddy/Caddyfile
   ```

   `caddy adapt` exiting 0 with no output on stderr means the config is valid. If it
   fails on something like `parsing caddyfile tokens for 'email': wrong argument
   count`, check you passed `--env-file .env` — without it, `{$ACME_EMAIL}` and
   friends can't resolve and every directive that uses them looks broken even though
   it isn't.

2. **Reload the live container** (this is what actually applies the change):

   ```bash
   MSYS_NO_PATHCONV=1 docker exec caddy caddy reload --config /etc/caddy/Caddyfile
   ```

   Exit 0 + `"msg":"adapted config to JSON"` in the output means it took. `caddy
   reload` is graceful — it doesn't drop existing connections, unlike restarting the
   container.

3. **Confirm it actually applied**, don't just trust the exit code:

   ```bash
   docker logs caddy --since 10s
   ```

   Look for `"msg":"enabling automatic TLS certificate management","domains":[...]`
   and confirm every public hostname you expect is listed. If you added a new public
   hostname, you should also see `tls.obtain` / `certificate obtained successfully`
   lines for it within ~15 seconds.

## Known gotchas

- **A bare `docker compose up -d caddy` after only editing the Caddyfile does
  nothing** — the container was already running with the old config in memory, and
  Compose sees no service-definition change to act on. Always use `caddy reload`
  (step 2) for a Caddyfile-only change; only use `docker compose up -d --build caddy`
  when `Dockerfile.caddy` itself changed (e.g. adding a new xcaddy plugin).
- If you're testing from the same LAN the public route also resolves on, results can
  be unreliable due to NAT hairpinning and Docker Desktop's own networking quirks —
  see the `diagnose-connectivity` skill before concluding something's actually
  broken based on a local test alone.
