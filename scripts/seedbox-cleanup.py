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
cycle ever pulled it down, losing it permanently. Checking "already imported" first
removes that race entirely, since the library's hardlinked copy is independent of
whatever happens to the seedbox/staging copies afterward.

Run via pythonw.exe as the scheduled task action -- pythonw has no console, so nothing
printed to stdout/stderr is ever seen and an uncaught exception vanishes silently.
Everything that matters is logged to LOG_PATH instead, and main() is wrapped so a
failure still exits non-zero (visible in Task Scheduler's Last Run Result) rather than
silently reporting success.
"""

import logging
import logging.handlers
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import dotenv_values

ENV_PATH = Path(r"C:\Users\drcor\acquisitions\.env")
SONARR_CONFIG = Path(r"D:\appdata\sonarr\config.xml")
RADARR_CONFIG = Path(r"D:\appdata\radarr\config.xml")
LOG_PATH = Path(r"D:\appdata\seedbox-cleanup.log")
SONARR_BASE = "http://localhost:8989"
RADARR_BASE = "http://localhost:7878"
HISTORY_PAGE_SIZE = 250
REQUEST_TIMEOUT = 60
TRANSMISSION_HANDSHAKE_ATTEMPTS = 3

DRY_RUN = "--dry-run" in sys.argv

log = logging.getLogger("seedbox-cleanup")


def setup_logging():
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stdout))


def read_api_key(config_path):
    return ET.parse(config_path).getroot().find("ApiKey").text


class Config:
    def __init__(self):
        env = dotenv_values(ENV_PATH)
        self.seedbox_host = urllib.parse.urlparse(env["SEEDBOX_URL"]).netloc
        self.basic_auth = env["SEEDBOX_BASIC_AUTH"]
        self.ratio_category = env.get("RATIO_CATEGORY", "ratio")
        self.sonarr_key = read_api_key(SONARR_CONFIG)
        self.radarr_key = read_api_key(RADARR_CONFIG)


def qbt_get_torrents(cfg, category):
    r = requests.get(
        f"https://{cfg.seedbox_host}/qbittorrent/api/v2/torrents/info",
        params={"category": category},
        headers={"Authorization": f"Basic {cfg.basic_auth}"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def qbt_delete_torrent(cfg, hash_):
    r = requests.post(
        f"https://{cfg.seedbox_host}/qbittorrent/api/v2/torrents/delete",
        params={"hashes": hash_, "deleteFiles": "true"},
        headers={"Authorization": f"Basic {cfg.basic_auth}"},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()


def transmission_request(cfg, payload, session=None):
    session = session or requests.Session()
    headers = {"Authorization": f"Basic {cfg.basic_auth}"}
    url = f"https://{cfg.seedbox_host}/rpc"
    for attempt in range(TRANSMISSION_HANDSHAKE_ATTEMPTS):
        r = session.post(url, json=payload, headers=headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 409:
            headers["X-Transmission-Session-Id"] = r.headers["X-Transmission-Session-Id"]
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Transmission RPC handshake didn't settle after {TRANSMISSION_HANDSHAKE_ATTEMPTS} attempts")


def transmission_get_torrents(cfg):
    resp = transmission_request(cfg, {
        "method": "torrent-get",
        "arguments": {"fields": ["id", "hashString", "name", "status", "uploadRatio", "seedRatioLimit"]},
    })
    return resp["arguments"]["torrents"]


def transmission_delete_torrent(cfg, id_):
    transmission_request(cfg, {
        "method": "torrent-remove",
        "arguments": {"ids": [id_], "delete-local-data": True},
    })


def fetch_imported_hashes(base_url, api_key):
    r = requests.get(
        f"{base_url}/api/v3/history",
        params={"pageSize": HISTORY_PAGE_SIZE, "sortKey": "date", "sortDirection": "descending"},
        headers={"X-Api-Key": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return {
        rec["downloadId"].lower()
        for rec in r.json().get("records", [])
        if rec.get("eventType") == "downloadFolderImported" and rec.get("downloadId")
    }


def cleanup_qbittorrent(cfg, imported_hashes):
    for t in qbt_get_torrents(cfg, cfg.ratio_category):
        if t["state"] != "pausedUP":
            continue
        h = t["hash"].lower()
        name = t["name"]
        if h in imported_hashes:
            log.info(f"qBittorrent: DELETE '{name}' (hash {h}) -- paused at seed target, confirmed imported")
            if not DRY_RUN:
                qbt_delete_torrent(cfg, t["hash"])
        else:
            log.info(f"qBittorrent: skip '{name}' (hash {h}) -- paused at seed target but not yet confirmed imported")


def cleanup_transmission(cfg, imported_hashes):
    for t in transmission_get_torrents(cfg):
        if t["status"] != 0:  # 0 = stopped; assumes nothing here is manually paused by hand
            continue
        h = t["hashString"].lower()
        name = t["name"]
        if h in imported_hashes:
            log.info(f"Transmission: DELETE '{name}' (hash {h}) -- stopped at seed target, confirmed imported")
            if not DRY_RUN:
                transmission_delete_torrent(cfg, t["id"])
        else:
            log.info(f"Transmission: skip '{name}' (hash {h}) -- stopped but not yet confirmed imported")


def main():
    cfg = Config()
    imported = fetch_imported_hashes(SONARR_BASE, cfg.sonarr_key) | fetch_imported_hashes(RADARR_BASE, cfg.radarr_key)
    cleanup_qbittorrent(cfg, imported)
    cleanup_transmission(cfg, imported)


if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        log.exception("seedbox-cleanup failed")
        sys.exit(1)
