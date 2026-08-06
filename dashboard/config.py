"""
Config discovery for the trace dashboard -- a direct descendant of
scripts/provision.py's Config class, same sources, same fallbacks, so anything already
proven to work there (API-key discovery off disk, DOWNLOAD_MODE branching, base-URL
overrides) doesn't need re-deriving here.

One deliberate difference from provision.py: this never hard-fails on a
DOWNLOAD_MODE/COMPOSE_PROFILES mismatch. provision.py refuses to run in that case because
running it would half-configure the wrong download client. The dashboard's whole job is
to *diagnose* problems with this stack, including that one -- refusing to boot when the
thing it diagnoses is misconfigured would be backwards. See rules.py's
R_download_mode_mismatch.

Jellyfin connection details are deliberately NOT a new .env var -- they're read from
Seerr's own settings.json (<CONFIG_ROOT>/jellyseerr/settings.json, "jellyfin" block),
which already has them because Seerr needed them for its own Jellyfin-backed login. One
adjustment: that file stores "ip": "host.docker.internal" because Seerr runs in a
container. This dashboard runs as a plain host process (see README's dashboard section
for why -- the rclone/cleanup fix buttons need real subprocess access to Windows-only
scripts), so host.docker.internal doesn't resolve for us; translate it to localhost.
"""

import json
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

log = logging.getLogger("dashboard.config")


def read_xml_api_key(path):
    return ET.parse(path).getroot().find("ApiKey").text


class Config:
    def __init__(self):
        self.reload()

    def reload(self):
        """Re-read .env and every app's on-disk config. Called once at startup and
        again by the poller whenever .env's mtime changes, so an edit doesn't require
        restarting the dashboard process the way it would for the *arr containers."""
        env = dotenv_values(ENV_PATH)
        config_root = Path(env["CONFIG_ROOT"])
        self.config_root = config_root
        self.media_root = Path(env["MEDIA_ROOT"])
        self.tz = env.get("TZ", "UTC")

        self.download_mode = env.get("DOWNLOAD_MODE", "seedbox")
        self.compose_profiles = env.get("COMPOSE_PROFILES", "")

        # Both blocks are populated regardless of mode (env.get, never hard-indexed) --
        # a local-mode .env leaves the seedbox vars blank, a seedbox-mode .env leaves
        # QBT_* blank, and the dashboard needs to reason about *both* possibilities
        # (e.g. to render "why are these buttons hidden") rather than just the active one.
        self.ratio_category = env.get("RATIO_CATEGORY", "ratio")
        self.public_category = env.get("PUBLIC_CATEGORY", "public")
        self.seedbox_host = env.get("SEEDBOX_HOST", "")
        self.seedbox_basic_auth = env.get("SEEDBOX_BASIC_AUTH", "")
        self.qbt_category = env.get("QBT_CATEGORY", "downloads")
        self.qbt_user = env.get("QBT_USER", "admin")
        self.qbt_pass = env.get("QBT_PASS", "")

        self.sonarr_key = read_xml_api_key(config_root / "sonarr" / "config.xml")
        self.radarr_key = read_xml_api_key(config_root / "radarr" / "config.xml")
        self.prowlarr_key = read_xml_api_key(config_root / "prowlarr" / "config.xml")

        seerr_settings = json.loads(
            (config_root / "jellyseerr" / "settings.json").read_text(encoding="utf-8")
        )
        self.seerr_key = seerr_settings["main"]["apiKey"]

        jf = seerr_settings.get("jellyfin", {})
        jf_ip = jf.get("ip", "localhost")
        if jf_ip in ("host.docker.internal", "jellyfin"):
            # "host.docker.internal" was the old host-installed-Jellyfin address;
            # "jellyfin" is its containerized replacement's service name. Either way
            # this dashboard runs as a host process, not a container, so it has to
            # reach the *published* port via localhost, not a container-DNS name.
            jf_ip = "localhost"
        scheme = "https" if jf.get("useSsl") else "http"
        self.jellyfin_base = f"{scheme}://{jf_ip}:{jf.get('port', 8096)}{jf.get('urlBase', '')}"

        self.sonarr_base = env.get("SONARR_BASE_URL") or "http://localhost:8989"
        self.radarr_base = env.get("RADARR_BASE_URL") or "http://localhost:7878"
        self.prowlarr_base = env.get("PROWLARR_BASE_URL") or "http://localhost:9696"
        self.seerr_base = env.get("SEERR_BASE_URL") or "http://localhost:5055"
        self.qbittorrent_local_base = "http://localhost:8080"

        self.dashboard_port = int(env.get("DASHBOARD_PORT", 8099))
        self.poll_seconds = int(env.get("DASHBOARD_POLL_SECONDS", 30))
        self.history_poll_seconds = int(env.get("DASHBOARD_HISTORY_POLL_SECONDS", 180))
        self.stall_minutes = int(env.get("DASHBOARD_STALL_MINUTES", 60))
        self.retention_days = int(env.get("DASHBOARD_RETENTION_DAYS", 90))
        # How often the background notification sweep runs the *real* diagnosis
        # engine across every matched request -- this is the same expensive
        # concurrent check the "stalled only" filter runs on demand, just automatic.
        # Deliberately not the fast 30s poll tier: a full sweep hits Sonarr/Radarr/
        # Prowlarr with a real deep-trace per request, same cost profile as the
        # manual filter (tens of seconds to a couple minutes for this stack's ~150
        # requests) -- see dashboard/poller.py.
        self.notify_poll_seconds = int(env.get("DASHBOARD_NOTIFY_POLL_SECONDS", 900))
        # Reused verbatim from scripts/rclone-sync.py/ddns-update.py's own vars --
        # same topic, so whatever's already subscribed on your phone just starts
        # receiving these too.
        self.ntfy_server = env.get("NTFY_SERVER", "https://ntfy.sh")
        self.ntfy_topic = env.get("NTFY_TOPIC", "")

        self.dashboard_config_dir = config_root / "dashboard"
        self.dashboard_config_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dashboard_config_dir / "dashboard.db"
        self.log_path = self.dashboard_config_dir / "dashboard.log"

        # Log paths the rclone-stage / cleanup-stage detectors tail. Filenames match
        # what scripts/rclone-sync.py and scripts/seedbox-cleanup.py actually write --
        # see local_fs.py for the exact tailing logic, which must track these exactly.
        self.rclone_wrapper_log = config_root / "rclone-sync-wrapper.log"
        self.rclone_qbt_log = config_root / "rclone-sync.log"
        self.rclone_transmission_log = config_root / "rclone-sync-transmission.log"
        self.seedbox_cleanup_log = config_root / "seedbox-cleanup.log"

        self.staging_root_qbt = self.media_root / "downloads" / "seedbox"
        self.staging_root_transmission = self.media_root / "downloads" / "seedbox-transmission"

    def env_mtime(self):
        return ENV_PATH.stat().st_mtime

    def download_mode_mismatch(self):
        """Mirrors provision.py's check_download_mode_consistency(), but returns a
        bool instead of raising -- this is a diagnosis to surface on the page, not a
        reason to refuse to start."""
        profiles = [p.strip() for p in self.compose_profiles.split(",") if p.strip()]
        local_running = "local" in profiles
        if self.download_mode == "local" and not local_running:
            return "DOWNLOAD_MODE=local but COMPOSE_PROFILES doesn't include \"local\" -- the qbittorrent container isn't running."
        if self.download_mode == "seedbox" and local_running:
            return "DOWNLOAD_MODE=seedbox but COMPOSE_PROFILES includes \"local\" -- a qbittorrent container is running but won't be used."
        return None
