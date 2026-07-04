#!/bin/bash
# Invoked by qBittorrent's "Run external program on torrent completion" as:
#   bash /scripts/cross-seed.sh "%I" "%L"
# %I = info hash, %L = category. Only torrents in $RATIO_CATEGORY get pushed
# to the seedbox; everything else is skipped.

HASH="$1"
CATEGORY="$2"
RATIO_CATEGORY="${RATIO_CATEGORY:-ratio}"
LOG="/scripts/cross-seed.log"

log() { echo "$(date -Is) $*" >> "$LOG"; }

if [ "$CATEGORY" != "$RATIO_CATEGORY" ]; then
    exit 0
fi

if [ -z "$SEEDBOX_URL" ] || [ -z "$SEEDBOX_USER" ] || [ -z "$SEEDBOX_PASS" ]; then
    log "ERROR hash=$HASH: SEEDBOX_URL/SEEDBOX_USER/SEEDBOX_PASS not set, skipping"
    exit 1
fi

log "START hash=$HASH category=$CATEGORY"

LOCAL_COOKIE=$(mktemp)
curl -sf -c "$LOCAL_COOKIE" -X POST "http://localhost:8080/api/v2/auth/login" \
    --data-urlencode "username=${QBITTORRENT_USER}" \
    --data-urlencode "password=${QBITTORRENT_PASS}" \
    -H "Referer: http://localhost:8080" > /dev/null
if [ $? -ne 0 ]; then
    log "ERROR hash=$HASH: local qBittorrent login failed"
    rm -f "$LOCAL_COOKIE"
    exit 1
fi

TORRENT_FILE=$(mktemp --suffix=.torrent)
curl -sf -b "$LOCAL_COOKIE" "http://localhost:8080/api/v2/torrents/export?hash=${HASH}" -o "$TORRENT_FILE"
if [ $? -ne 0 ] || [ ! -s "$TORRENT_FILE" ]; then
    log "ERROR hash=$HASH: export failed or empty"
    rm -f "$LOCAL_COOKIE" "$TORRENT_FILE"
    exit 1
fi

# Seedbox sits behind an nginx reverse proxy with HTTP Basic Auth; qBittorrent's
# own login is bypassed entirely once Basic Auth passes, so no separate cookie
# login is needed here (unlike the local instance above).
RESPONSE=$(curl -sf -u "${SEEDBOX_USER}:${SEEDBOX_PASS}" -X POST "${SEEDBOX_URL}/api/v2/torrents/add" \
    -F "torrents=@${TORRENT_FILE}")

if [ "$RESPONSE" = "Ok." ]; then
    log "OK hash=$HASH: added to seedbox"
else
    log "ERROR hash=$HASH: seedbox add returned: $RESPONSE"
fi

rm -f "$LOCAL_COOKIE" "$TORRENT_FILE"
