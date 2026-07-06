"""
Deletes torrents on the seedbox once they're BOTH (a) done seeding -- paused/stopped
at their configured ratio or seed-time target, meaning the client itself decided the
obligation is met, not a manual pause -- and (b) already imported into the local
library by Sonarr/Radarr (confirmed via history, not by guessing from local disk
state). Only ever deletes on the seedbox itself; the local downloads/seedbox* staging
mirror is left to the existing rclone sync task to clean up on its own next run,
since `rclone sync` already removes local files no longer present at the source.

This is deliberately NOT done via qBittorrent's/Transmission's own "delete on limit"
action -- Sonarr refuses to add a download client configured that way, and even if it
didn't, there's a real race: the seedbox could delete a file before rclone's next sync
cycle ever pulls it down, losing it permanently. Checking "already imported" first
removes that race entirely, since the library's hardlinked copy is independent of
whatever happens to the seedbox/staging copies afterward.
"""

import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = r"C:\Users\drcor\acquisitions\.env"
SONARR_CONFIG = r"D:\appdata\sonarr\config.xml"
RADARR_CONFIG = r"D:\appdata\radarr\config.xml"
LOG_PATH = r"D:\appdata\seedbox-cleanup.log"
SONARR_BASE = "http://localhost:8989"
RADARR_BASE = "http://localhost:7878"
HISTORY_PAGE_SIZE = 250

DRY_RUN = "--dry-run" in sys.argv


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {'[DRY RUN] ' if DRY_RUN else ''}{msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def read_env():
    env = {}
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def read_api_key(config_path):
    with open(config_path, encoding="utf-8") as f:
        content = f.read()
    return re.search(r"<ApiKey>([^<]+)</ApiKey>", content).group(1)


env = read_env()
SEEDBOX_HOST = urllib.parse.urlparse(env["SEEDBOX_URL"]).netloc
BASIC_AUTH = env["SEEDBOX_BASIC_AUTH"]
SONARR_KEY = read_api_key(SONARR_CONFIG)
RADARR_KEY = read_api_key(RADARR_CONFIG)


def http_json(url, method="GET", headers=None, data=None):
    headers = dict(headers or {})
    req = urllib.request.Request(url, method=method, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
        return json.loads(body) if body else None


def qbt_get_torrents(category):
    url = f"https://{SEEDBOX_HOST}/qbittorrent/api/v2/torrents/info?category={category}"
    return http_json(url, headers={"Authorization": f"Basic {BASIC_AUTH}"})


def qbt_delete_torrent(hash_):
    url = (f"https://{SEEDBOX_HOST}/qbittorrent/api/v2/torrents/delete"
           f"?hashes={hash_}&deleteFiles=true")
    http_json(url, method="POST", headers={"Authorization": f"Basic {BASIC_AUTH}"})


def transmission_request(payload, session_id=None):
    headers = {"Authorization": f"Basic {BASIC_AUTH}", "Content-Type": "application/json"}
    if session_id:
        headers["X-Transmission-Session-Id"] = session_id
    req = urllib.request.Request(f"https://{SEEDBOX_HOST}/rpc", method="POST",
                                  headers=headers, data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 409:
            return transmission_request(payload, session_id=e.headers.get("X-Transmission-Session-Id"))
        raise


def transmission_get_torrents():
    resp = transmission_request({
        "method": "torrent-get",
        "arguments": {"fields": ["id", "hashString", "name", "status", "uploadRatio", "seedRatioLimit"]},
    })
    return resp["arguments"]["torrents"]


def transmission_delete_torrent(id_):
    transmission_request({
        "method": "torrent-remove",
        "arguments": {"ids": [id_], "delete-local-data": True},
    })


def fetch_imported_hashes(base_url, api_key):
    url = f"{base_url}/api/v3/history?pageSize={HISTORY_PAGE_SIZE}&sortKey=date&sortDirection=descending"
    data = http_json(url, headers={"X-Api-Key": api_key})
    return {
        rec["downloadId"].lower()
        for rec in data.get("records", [])
        if rec.get("eventType") == "downloadFolderImported" and rec.get("downloadId")
    }


def cleanup_qbittorrent(imported_hashes):
    category = env.get("RATIO_CATEGORY", "ratio")
    for t in qbt_get_torrents(category):
        if t["state"] != "pausedUP":
            continue
        h = t["hash"].lower()
        name = t["name"]
        if h in imported_hashes:
            log(f"qBittorrent: DELETE '{name}' (hash {h}) -- paused at seed target, confirmed imported")
            if not DRY_RUN:
                qbt_delete_torrent(t["hash"])
        else:
            log(f"qBittorrent: skip '{name}' (hash {h}) -- paused at seed target but not yet confirmed imported")


def cleanup_transmission(imported_hashes):
    for t in transmission_get_torrents():
        if t["status"] != 0:  # 0 = stopped; assumes nothing here is manually paused by hand
            continue
        h = t["hashString"].lower()
        name = t["name"]
        if h in imported_hashes:
            log(f"Transmission: DELETE '{name}' (hash {h}) -- stopped at seed target, confirmed imported")
            if not DRY_RUN:
                transmission_delete_torrent(t["id"])
        else:
            log(f"Transmission: skip '{name}' (hash {h}) -- stopped but not yet confirmed imported")


def main():
    imported = fetch_imported_hashes(SONARR_BASE, SONARR_KEY) | fetch_imported_hashes(RADARR_BASE, RADARR_KEY)
    cleanup_qbittorrent(imported)
    cleanup_transmission(imported)


if __name__ == "__main__":
    main()
