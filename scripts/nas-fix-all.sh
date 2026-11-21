#!/bin/bash
# One-shot catch-up for the Synology NAS. Idempotent; safe to re-run.
#   cd /volume1/docker/acquisitions && git pull && sudo bash scripts/nas-fix-all.sh
# Flags:
#   --force-jellyfin   recreate Jellyfin even if someone is watching
#   --lean             also stop satisfactory + flaresolverr to free RAM (4 GB box)
#   --skip-pihole      don't bring Pi-hole up
set -u
cd "$(dirname "$0")/.." || exit 1
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
FORCE=0; LEAN=0; PIHOLE=1
for a in "$@"; do case "$a" in
  --force-jellyfin) FORCE=1;; --lean) LEAN=1;; --skip-pihole) PIHOLE=0;;
  *) echo "unknown flag $a"; exit 1;; esac; done
envval() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '\r"'; }
RESULTS=()
note() { RESULTS+=("$1"); echo; echo ">>> $1"; }

echo "=== 1. inotify watch limit (Jellyfin/RomM realtime monitoring) ==="
sysctl -w fs.inotify.max_user_watches=524288 fs.inotify.max_user_instances=512
RC=/usr/local/etc/rc.d/acq-inotify.sh
cat > "$RC" <<'RCEOF'
#!/bin/sh
# Installed by acquisitions/scripts/nas-fix-all.sh -- raises inotify limits at boot.
case "$1" in start)
  sysctl -w fs.inotify.max_user_watches=524288 fs.inotify.max_user_instances=512 ;;
esac
exit 0
RCEOF
chmod 755 "$RC"
note "inotify: now $(sysctl -n fs.inotify.max_user_watches), boot script at $RC"

echo "=== 2. Jellyfin 10.11.11 (login 500 fix) ==="
WANT=$(grep -oE 'linuxserver/jellyfin:[0-9.]+' compose.yaml | head -1)
HAVE=$(docker inspect -f '{{.Config.Image}}' jellyfin 2>/dev/null)
if echo "$HAVE" | grep -q "$WANT"; then
  note "jellyfin: already on $WANT"
else
  KEY=$(envval JELLYFIN_API_KEY)
  PLAYING=$(curl -s -m 15 -H "X-Emby-Token: $KEY" http://localhost:8096/Sessions | grep -o '"NowPlayingItem"' | wc -l)
  if [ "$PLAYING" -gt 0 ] && [ "$FORCE" = 0 ]; then
    note "jellyfin: SKIPPED, $PLAYING active playback session(s). Re-run later or pass --force-jellyfin"
  else
    docker compose pull jellyfin && docker compose up -d jellyfin \
      && note "jellyfin: recreated on $WANT (several minutes of 503s while it boots is normal)" \
      || note "jellyfin: FAILED to pull/recreate, see output above"
  fi
fi

echo "=== 3. Dashboard restart (notify-once + qBittorrent 5 state fix) ==="
docker compose restart dashboard && note "dashboard: restarted" || note "dashboard: restart FAILED"
sleep 8

echo "=== 4. Seedbox cleanup dry run (was silently skipping every qBittorrent torrent) ==="
OUT=$(docker exec dashboard python scripts/seedbox-cleanup.py --dry-run 2>&1)
echo "$OUT" | tail -15 | cut -c1-160
note "seedbox-cleanup dry run: $(echo "$OUT" | grep -c DELETE) would be deleted, $(echo "$OUT" | grep -c 'skip') skipped. The scheduled task does the real run; check DSM Task Scheduler that it is enabled."

if [ "$PIHOLE" = 1 ]; then
  echo "=== 5. Pi-hole on the NAS ==="
  if docker compose up -d pihole; then
    for i in $(seq 1 30); do docker exec pihole pihole status >/dev/null 2>&1 && break; sleep 2; done
    ZIP=/volume1/docker/pihole-teleporter.zip
    if [ -f /volume1/docker/appdata/.pihole-teleporter-imported ]; then
      note "pihole: up, Teleporter archive already imported earlier"
    elif [ -f "$ZIP" ]; then
      docker cp "$ZIP" pihole:/tmp/teleporter.zip \
        && docker exec pihole pihole-FTL --teleporter /tmp/teleporter.zip \
        && touch /volume1/docker/appdata/.pihole-teleporter-imported \
        && note "pihole: up at $(envval PIHOLE_LAN_IP), Teleporter archive imported" \
        || note "pihole: up, but Teleporter import FAILED. Import $ZIP by hand at http://$(envval PIHOLE_LAN_IP)/admin (Settings > Teleporter)"
    else
      note "pihole: up, no Teleporter archive at $ZIP to import"
    fi
  else
    note "pihole: FAILED to start, see output above"
  fi
fi

if [ "$LEAN" = 1 ]; then
  echo "=== 6. Lean mode ==="
  docker stop satisfactory flaresolverr >/dev/null 2>&1
  note "lean: stopped satisfactory + flaresolverr (start again with: sudo docker start satisfactory flaresolverr)"
fi

echo; echo "=== Memory: this is what the lag is ==="
free -m
docker stats --no-stream --format 'table {{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}' | sort -k2 -h -r | head -15

echo; echo "================ SUMMARY ================"
for r in "${RESULTS[@]}"; do echo " - $r"; done
cat <<TXT

Still manual (a script cannot do these):
 - Router: after checking  nslookup watch.$(envval DOMAIN) $(envval PIHOLE_LAN_IP)  from a PC
   answers $(envval CADDY_LAN_IP), point the router's DNS at $(envval PIHOLE_LAN_IP) and stop the old Pi-hole.
 - DSM > Control Panel > Network: DNS must be the router, never Pi-hole.
 - DSM Task Scheduler: confirm the seedbox-cleanup task is enabled.
TXT
