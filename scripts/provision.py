"""
Idempotent bootstrap script: wires up every cross-app connection this stack needs --
Prowlarr -> Sonarr/Radarr, download clients, indexer routing, Remote Path Mappings,
Bazarr -> Sonarr/Radarr, Seerr -> Sonarr/Radarr, and the seedbox's own qBittorrent/
Transmission ratio+privacy settings -- via each app's REST API instead of manually
clicking through every UI. Safe to re-run: every step is create-if-missing or
set-to-desired-state, never destructive to unrelated existing config.

Deliberately NOT automated (left as one-time manual/interactive UI steps):
  - Adding actual indexers/trackers to Prowlarr -- needs your own tracker account
    credentials, which don't belong in an env file or a script.
  - Seerr's initial connection to Jellyfin -- needs Jellyfin admin credentials and a
    stateful discovery handshake (server ID, library IDs); this script only verifies
    it's already connected and warns if it isn't.
  - Tagging specific Cloudflare-protected indexers with the FlareSolverr proxy tag --
    depends on which indexers you've actually added; the proxy itself IS configured
    here (see README section 4), just not which indexers use it.

Run once after `docker compose up -d` on a fresh stack (once Sonarr/Radarr/Prowlarr/
Bazarr/Seerr have started at least once, so their own API keys exist on disk), or any
time after to confirm/repair the wiring -- e.g. after recreating a container wipes a
download client, or after rotating the seedbox password.
"""

import json
import logging
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from functools import partial
from pathlib import Path

import requests
import yaml
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# Fixed container-side paths -- these come from compose.yaml's single ${MEDIA_ROOT}:/media
# mount (see README section 9's hardlink warning for why it has to be one mount), not from
# .env's MEDIA_ROOT (that's the HOST path; the container always sees it at /media).
TV_ROOT = "/media/tv"
MOVIES_ROOT = "/media/movies"
QBT_LOCAL_STAGING = "/media/downloads/seedbox"
TRANSMISSION_LOCAL_STAGING = "/media/downloads/seedbox-transmission"

log = logging.getLogger("provision")


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)


def read_xml_api_key(path):
    return ET.parse(path).getroot().find("ApiKey").text


class Config:
    def __init__(self):
        env = dotenv_values(ENV_PATH)
        config_root = Path(env["CONFIG_ROOT"])

        self.ratio_category = env.get("RATIO_CATEGORY", "ratio")
        self.public_category = env.get("PUBLIC_CATEGORY", "public")
        self.private_tracker_name = env.get("PRIVATE_TRACKER_NAME", "TorrentLeech")
        self.private_seed_ratio = float(env.get("PRIVATE_TRACKER_SEED_RATIO", 1))
        self.private_seed_time = int(env.get("PRIVATE_TRACKER_SEED_TIME_MINUTES", 14400))
        self.seedbox_host = urllib.parse.urlparse(env["SEEDBOX_URL"]).netloc
        self.seedbox_basic_auth = env["SEEDBOX_BASIC_AUTH"]

        self.sonarr_key = read_xml_api_key(config_root / "sonarr" / "config.xml")
        self.radarr_key = read_xml_api_key(config_root / "radarr" / "config.xml")
        self.prowlarr_key = read_xml_api_key(config_root / "prowlarr" / "config.xml")

        bazarr_yaml = yaml.safe_load(
            (config_root / "bazarr" / "config" / "config.yaml").read_text(encoding="utf-8")
        )
        self.bazarr_key = bazarr_yaml["auth"]["apikey"]

        seerr_settings = json.loads(
            (config_root / "jellyseerr" / "settings.json").read_text(encoding="utf-8")
        )
        self.seerr_key = seerr_settings["main"]["apiKey"]

        self.sonarr_base = "http://localhost:8989"
        self.radarr_base = "http://localhost:7878"
        self.prowlarr_base = "http://localhost:9696"
        self.bazarr_base = "http://localhost:6767"
        self.seerr_base = "http://localhost:5055"


# ---------------------------------------------------------------------------
# Generic REST helpers
# ---------------------------------------------------------------------------

def api(base, key, method, path, **kwargs):
    r = requests.request(method, f"{base}{path}", headers={"X-Api-Key": key}, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json() if r.content else None


def set_field(fields, name, value):
    """Sonarr/Radarr/Prowlarr's dynamic settings forms all use this {name, value, ...}
    fields-list shape for their implementation-specific config."""
    for f in fields:
        if f["name"] == name:
            f["value"] = value
            return
    fields.append({"name": name, "value": value})


def get_field(fields, name, default=None):
    return next((f.get("value", default) for f in fields if f["name"] == name), default)


# ---------------------------------------------------------------------------
# Prowlarr -> Sonarr/Radarr, FlareSolverr
# ---------------------------------------------------------------------------

def configure_prowlarr_application(prowlarr, name, base_url, api_key):
    apps = prowlarr("GET", "/api/v1/applications")
    existing = next((a for a in apps if a["name"] == name), None)
    if existing:
        body = existing
    else:
        schemas = prowlarr("GET", "/api/v1/applications/schema")
        body = next(s for s in schemas if s["implementation"] == name)
        body["name"] = name
    set_field(body["fields"], "prowlarrUrl", "http://prowlarr:9696")
    set_field(body["fields"], "baseUrl", base_url)
    set_field(body["fields"], "apiKey", api_key)
    body["syncLevel"] = "fullSync"
    if existing:
        prowlarr("PUT", f"/api/v1/applications/{existing['id']}", json=body)
        log.info(f"[Prowlarr] {name} application connection already existed, refreshed it")
    else:
        prowlarr("POST", "/api/v1/applications", json=body)
        log.info(f"[Prowlarr] created {name} application connection")


def wait_for_prowlarr_sync(prowlarr, timeout=30):
    """Trigger Prowlarr's Application Indexer Sync and wait for it to actually finish,
    rather than guessing at a delay. This sync pushes Prowlarr's indexer definitions into
    Sonarr/Radarr and can overwrite Sonarr-side-only fields (downloadClientId,
    seedCriteria) that this script sets afterward -- waiting for it to genuinely settle
    here (instead of racing it) is what makes the later indexer-routing step actually
    idempotent from one run to the next."""
    cmd = prowlarr("POST", "/api/v1/command", json={"name": "ApplicationIndexerSync"})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = prowlarr("GET", f"/api/v1/command/{cmd['id']}")
        if status["status"] in ("completed", "failed"):
            log.info(f"[Prowlarr] Application Indexer Sync {status['status']}")
            return
        time.sleep(1)
    log.warning("[Prowlarr] Application Indexer Sync didn't report completion within "
                f"{timeout}s -- continuing anyway, indexer routing below may need a second run")


def configure_prowlarr_flaresolverr(prowlarr):
    proxies = prowlarr("GET", "/api/v1/indexerproxy")
    if any(p["name"] == "FlareSolverr" for p in proxies):
        log.info("[Prowlarr] FlareSolverr proxy already configured")
        return
    schemas = prowlarr("GET", "/api/v1/indexerproxy/schema")
    body = next(s for s in schemas if s["implementation"] == "FlareSolverr")
    body["name"] = "FlareSolverr"
    set_field(body["fields"], "host", "http://flaresolverr:8191")
    tags = prowlarr("GET", "/api/v1/tag")
    tag = next((t for t in tags if t["label"] == "flaresolverr"), None)
    if not tag:
        tag = prowlarr("POST", "/api/v1/tag", json={"label": "flaresolverr"})
    body["tags"] = [tag["id"]]
    prowlarr("POST", "/api/v1/indexerproxy", json=body)
    log.info("[Prowlarr] created FlareSolverr proxy -- tag it onto Cloudflare-protected "
             "indexers yourself (Indexers > edit > Tags), that part depends on which "
             "indexers you've added")


# ---------------------------------------------------------------------------
# Sonarr/Radarr: root folders, download clients, remote path mappings, indexer routing
# ---------------------------------------------------------------------------

def configure_root_folder(app, path):
    folders = app("GET", "/api/v3/rootfolder")
    if any(f["path"] == path for f in folders):
        log.info(f"root folder {path} already exists")
        return
    app("POST", "/api/v3/rootfolder", json={"path": path})
    log.info(f"created root folder {path}")


def configure_download_client(app, implementation, name, category_field, category_value, overrides, priority=50):
    clients = app("GET", "/api/v3/downloadclient")
    existing = next((c for c in clients if c["name"] == name), None)
    if existing:
        body = existing
    else:
        schemas = app("GET", "/api/v3/downloadclient/schema")
        body = next(s for s in schemas if s["implementation"] == implementation)
        body["name"] = name
    body["enable"] = True
    body["priority"] = priority
    for k, v in overrides.items():
        set_field(body["fields"], k, v)
    set_field(body["fields"], category_field, category_value)
    if existing:
        app("PUT", f"/api/v3/downloadclient/{existing['id']}", json=body)
        log.info(f"download client '{name}' already existed, refreshed it (id {existing['id']})")
        return existing["id"]
    created = app("POST", "/api/v3/downloadclient", json=body)
    log.info(f"created download client '{name}' (id {created['id']})")
    return created["id"]


def configure_remote_path_mapping(app, remote_path, local_path):
    mappings = app("GET", "/api/v3/remotepathmapping")
    existing = next((m for m in mappings if m["host"] == "caddy" and m["remotePath"] == remote_path), None)
    body = {"host": "caddy", "remotePath": remote_path, "localPath": local_path}
    if existing:
        body["id"] = existing["id"]
        app("PUT", f"/api/v3/remotepathmapping/{existing['id']}", json=body)
        log.info(f"remote path mapping {remote_path} -> {local_path} already existed, refreshed it")
    else:
        app("POST", "/api/v3/remotepathmapping", json=body)
        log.info(f"created remote path mapping {remote_path} -> {local_path}")


def configure_indexer_routing(cfg, app, private_client_id, public_client_id, attempts=3):
    """Prowlarr's own background Application Indexer Sync (triggered by touching its
    Applications connection, or on its own schedule) pushes indexer definitions into
    Sonarr/Radarr asynchronously -- it can still be settling even after Prowlarr's own
    sync *command* reports complete, since that only means Prowlarr finished sending,
    not that Sonarr/Radarr finished applying it. That push doesn't know about (and can
    silently clobber) the downloadClientId/seedCriteria fields this function sets. Rather
    than guess a delay that's long enough, re-check after a short pause and only stop
    once a pass finds nothing left to change -- self-verifying instead of hoping."""
    for attempt in range(1, attempts + 1):
        any_changed = _route_indexers_once(cfg, app, private_client_id, public_client_id)
        if not any_changed:
            return
        if attempt < attempts:
            time.sleep(2)
    log.warning("indexer routing still saw changes after multiple passes -- Prowlarr's "
                "sync may still be settling, a follow-up run should confirm it's stable")


def _route_indexers_once(cfg, app, private_client_id, public_client_id):
    indexers = app("GET", "/api/v3/indexer")
    if not indexers:
        log.info("no indexers found yet -- add trackers in Prowlarr first, then re-run this script")
        return False
    any_changed = False
    for idx in indexers:
        is_private = cfg.private_tracker_name.lower() in idx["name"].lower()
        target = private_client_id if is_private else public_client_id
        changed = idx.get("downloadClientId") != target
        idx["downloadClientId"] = target

        if is_private:
            for field_name, desired in (
                ("seedCriteria.seedRatio", cfg.private_seed_ratio),
                ("seedCriteria.seedTime", cfg.private_seed_time),
            ):
                if get_field(idx["fields"], field_name) != desired:
                    set_field(idx["fields"], field_name, desired)
                    changed = True

        # Tags on an indexer silently exclude untagged requests (IndexerTagSpecification --
        # a tagged indexer rejects releases for anything that doesn't share the tag, and
        # Seerr-created requests carry no tags by default). This stack routes purely via
        # downloadClientId, so any tag here is a trap, not a feature -- strip it.
        if idx.get("tags"):
            idx["tags"] = []
            changed = True

        label = "private/qBittorrent" if is_private else "public/Transmission"
        if changed:
            app("PUT", f"/api/v3/indexer/{idx['id']}", json=idx)
            log.info(f"indexer '{idx['name']}' -> routed to {label} client")
            any_changed = True
        else:
            log.info(f"indexer '{idx['name']}' already routed to {label} client")
    return any_changed


# ---------------------------------------------------------------------------
# Bazarr
# ---------------------------------------------------------------------------

def bazarr_set_settings(cfg, fields):
    r = requests.post(
        f"{cfg.bazarr_base}/api/system/settings",
        headers={"X-Api-Key": cfg.bazarr_key},
        files={k: (None, str(v)) for k, v in fields.items()},
        timeout=30,
    )
    r.raise_for_status()


def configure_bazarr(cfg):
    bazarr_set_settings(cfg, {
        "settings-sonarr-ip": "sonarr",
        "settings-sonarr-port": 8989,
        "settings-sonarr-apikey": cfg.sonarr_key,
        "settings-sonarr-base_url": "",
        "settings-sonarr-ssl": "false",
    })
    bazarr_set_settings(cfg, {
        "settings-radarr-ip": "radarr",
        "settings-radarr-port": 7878,
        "settings-radarr-apikey": cfg.radarr_key,
        "settings-radarr-base_url": "",
        "settings-radarr-ssl": "false",
    })
    log.info("[Bazarr] Sonarr/Radarr connections configured")


# ---------------------------------------------------------------------------
# Seerr
# ---------------------------------------------------------------------------

def configure_seerr_service(cfg, seerr, service, active_directory):
    existing_list = seerr("GET", f"/api/v1/settings/{service}")
    existing = existing_list[0] if existing_list else None
    api_key = cfg.sonarr_key if service == "sonarr" else cfg.radarr_key
    port = 8989 if service == "sonarr" else 7878

    body = dict(existing) if existing else {}
    body.update({
        "name": service,
        "hostname": service,
        "port": port,
        "apiKey": api_key,
        "useSsl": False,
        "baseUrl": "",
        "activeProfileId": body.get("activeProfileId", 1),
        "activeProfileName": body.get("activeProfileName", "Any"),
        "activeDirectory": active_directory,
        "is4k": False,
        "isDefault": True,
        "syncEnabled": body.get("syncEnabled", False),
        "preventSearch": body.get("preventSearch", False),
        "tagRequests": body.get("tagRequests", False),
        "tags": body.get("tags", []),
    })
    if service == "sonarr":
        body.setdefault("animeTags", [])
        body.setdefault("enableSeasonFolders", False)
    else:
        body.setdefault("minimumAvailability", "announced")

    seerr_id = existing["id"] if existing else 0
    body.pop("id", None)  # id is read-only in the body -- it's only valid in the URL path
    seerr("PUT", f"/api/v1/settings/{service}/{seerr_id}", json=body)
    log.info(f"[Seerr] {service} connection -> {active_directory}")


def check_seerr_jellyfin(seerr):
    try:
        jf = seerr("GET", "/api/v1/settings/jellyfin")
    except requests.HTTPError:
        jf = None
    if jf and jf.get("serverId"):
        log.info(f"[Seerr] already connected to Jellyfin (server '{jf.get('name')}') -- leaving as-is")
    else:
        log.warning("[Seerr] NOT connected to Jellyfin -- this needs your Jellyfin admin "
                    "credentials and a one-time interactive handshake (server ID + library "
                    "discovery), so it's not scripted here. Do it via Seerr's setup wizard "
                    "or Settings > Services.")


# ---------------------------------------------------------------------------
# Seedbox: qBittorrent + Transmission's own ratio/privacy settings
# ---------------------------------------------------------------------------

def qbt_get(cfg, path):
    r = requests.get(f"https://{cfg.seedbox_host}/qbittorrent{path}",
                      headers={"Authorization": f"Basic {cfg.seedbox_basic_auth}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def qbt_set_preferences(cfg, prefs):
    r = requests.post(f"https://{cfg.seedbox_host}/qbittorrent/api/v2/app/setPreferences",
                       headers={"Authorization": f"Basic {cfg.seedbox_basic_auth}"},
                       data={"json": json.dumps(prefs)}, timeout=30)
    r.raise_for_status()


def configure_seedbox_qbittorrent(cfg):
    qbt_set_preferences(cfg, {
        "dht": False, "pex": False, "lsd": False,
        "max_ratio_enabled": True, "max_ratio": cfg.private_seed_ratio,
        "max_seeding_time_enabled": True, "max_seeding_time": cfg.private_seed_time,
        "max_ratio_act": 0,  # Pause, never Remove -- see README section 9 warnings for why
    })
    log.info("[seedbox/qBittorrent] DHT/PEX/LSD off, ratio/seed-time limits set, action=Pause")


def transmission_request(cfg, payload, session=None):
    session = session or requests.Session()
    headers = {"Authorization": f"Basic {cfg.seedbox_basic_auth}"}
    url = f"https://{cfg.seedbox_host}/rpc"
    for _ in range(3):
        r = session.post(url, json=payload, headers=headers, timeout=30)
        if r.status_code == 409:
            headers["X-Transmission-Session-Id"] = r.headers["X-Transmission-Session-Id"]
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("Transmission RPC handshake didn't settle after 3 attempts")


def configure_seedbox_transmission(cfg):
    transmission_request(cfg, {
        "method": "session-set",
        "arguments": {
            "dht-enabled": True, "pex-enabled": True, "lpd-enabled": True,
            "seedRatioLimit": cfg.private_seed_ratio, "seedRatioLimited": True,
            "idle-seeding-limit": cfg.private_seed_time, "idle-seeding-limit-enabled": True,
        },
    })
    log.info("[seedbox/Transmission] DHT/PEX/LPD on, ratio + idle-seed backstop set")


def derive_qbt_remote_path(cfg):
    base = qbt_get(cfg, "/api/v2/app/preferences")["save_path"].rstrip("/")
    return f"{base}/{cfg.ratio_category}/"


def derive_transmission_remote_path(cfg):
    resp = transmission_request(cfg, {"method": "session-get"})
    return resp["arguments"]["download-dir"].rstrip("/") + "/"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    cfg = Config()
    sonarr = partial(api, cfg.sonarr_base, cfg.sonarr_key)
    radarr = partial(api, cfg.radarr_base, cfg.radarr_key)
    prowlarr = partial(api, cfg.prowlarr_base, cfg.prowlarr_key)
    seerr = partial(api, cfg.seerr_base, cfg.seerr_key)

    log.info("=== Prowlarr -> Sonarr/Radarr, FlareSolverr ===")
    configure_prowlarr_application(prowlarr, "Sonarr", "http://sonarr:8989", cfg.sonarr_key)
    configure_prowlarr_application(prowlarr, "Radarr", "http://radarr:7878", cfg.radarr_key)
    configure_prowlarr_flaresolverr(prowlarr)
    wait_for_prowlarr_sync(prowlarr)

    log.info("=== Root folders ===")
    configure_root_folder(sonarr, TV_ROOT)
    configure_root_folder(radarr, MOVIES_ROOT)

    log.info("=== Seedbox settings (qBittorrent private-tracker ratio, Transmission public) ===")
    configure_seedbox_qbittorrent(cfg)
    configure_seedbox_transmission(cfg)
    qbt_remote_path = derive_qbt_remote_path(cfg)
    transmission_remote_path = derive_transmission_remote_path(cfg)

    log.info("=== Download clients (Sonarr) ===")
    sonarr_qbt_id = configure_download_client(
        sonarr, "QBittorrent", "qBittorrent - Seedbox", "tvCategory", cfg.ratio_category,
        {"host": "caddy", "port": 8090, "useSsl": False, "urlBase": "/qbittorrent",
         "username": "dummy", "password": "dummy"},
    )
    sonarr_transmission_id = configure_download_client(
        sonarr, "Transmission", "Transmission - Seedbox", "tvCategory", cfg.public_category,
        {"host": "caddy", "port": 8090, "useSsl": False, "urlBase": "", "username": "", "password": ""},
    )

    log.info("=== Download clients (Radarr) ===")
    radarr_qbt_id = configure_download_client(
        radarr, "QBittorrent", "qBittorrent - Seedbox", "movieCategory", cfg.ratio_category,
        {"host": "caddy", "port": 8090, "useSsl": False, "urlBase": "/qbittorrent",
         "username": "dummy", "password": "dummy"},
    )
    radarr_transmission_id = configure_download_client(
        radarr, "Transmission", "Transmission - Seedbox", "movieCategory", cfg.public_category,
        {"host": "caddy", "port": 8090, "useSsl": False, "urlBase": "", "username": "", "password": ""},
    )

    log.info("=== Remote Path Mappings ===")
    configure_remote_path_mapping(sonarr, qbt_remote_path, QBT_LOCAL_STAGING + "/")
    configure_remote_path_mapping(sonarr, transmission_remote_path, TRANSMISSION_LOCAL_STAGING + "/")
    configure_remote_path_mapping(radarr, qbt_remote_path, QBT_LOCAL_STAGING + "/")
    configure_remote_path_mapping(radarr, transmission_remote_path, TRANSMISSION_LOCAL_STAGING + "/")

    log.info("=== Indexer routing (Sonarr) ===")
    configure_indexer_routing(cfg, sonarr, sonarr_qbt_id, sonarr_transmission_id)
    log.info("=== Indexer routing (Radarr) ===")
    configure_indexer_routing(cfg, radarr, radarr_qbt_id, radarr_transmission_id)

    log.info("=== Bazarr ===")
    configure_bazarr(cfg)

    log.info("=== Seerr ===")
    configure_seerr_service(cfg, seerr, "sonarr", TV_ROOT)
    configure_seerr_service(cfg, seerr, "radarr", MOVIES_ROOT)
    check_seerr_jellyfin(seerr)

    log.info("=== Done ===")


if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        log.exception("provisioning failed")
        sys.exit(1)
