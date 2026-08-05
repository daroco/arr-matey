"""
qBittorrent client, mode-aware:
  - seedbox mode: Basic Auth straight to the seedbox's own reverse proxy
    (https://{SEEDBOX_HOST}/qbittorrent/...), same as scripts/seedbox-cleanup.py and
    scripts/provision.py already do -- the Caddy :8090 shim exists only because
    Sonarr/Radarr's download-client *types* have no Basic Auth field; this dashboard's
    own Python code can set the header itself, so the shim isn't needed here.
  - local mode: the linuxserver/qbittorrent container needs a real cookie login
    (POST /api/v2/auth/login), reachable directly at localhost:8080 from the host.

state_raw values and their meaning, confirmed against this stack's live seedbox during
Phase 0 (not assumed from generic docs): "stalledUP" = complete, seeding, currently no
peer demand (idle, NOT a stall in the "something's wrong" sense); "pausedUP" = complete
AND stopped because a ratio/time limit was hit (or a manual pause -- indistinguishable
via this field alone, same caveat scripts/seedbox-cleanup.py already documents);
"uploading" = complete, actively seeding to a connected peer. rules.py's suppressor
rule must only fire on pausedUP, not stalledUP/uploading, or it will flag ordinary
healthy seeding as a stall.
"""

import requests

from .base import SourceError

STATE_MAP = {
    "uploading": "seeding",
    "stalledUP": "seeding",
    "pausedUP": "complete_paused",
    "queuedUP": "seeding",
    "checkingUP": "seeding",
    "forcedUP": "seeding",
    "downloading": "downloading",
    "stalledDL": "downloading",
    "metaDL": "downloading",
    "queuedDL": "downloading",
    "checkingDL": "downloading",
    "forcedDL": "downloading",
    "allocating": "downloading",
    "error": "error",
    "missingFiles": "error",
    "pausedDL": "complete_paused",
}


def normalize_state(state_raw):
    return STATE_MAP.get(state_raw, "unknown")


class QbittorrentClient:
    def __init__(self, mode, seedbox_host=None, seedbox_basic_auth=None, local_base=None, local_user=None, local_pass=None):
        self.mode = mode
        self._session = requests.Session()
        if mode == "seedbox":
            self.base = f"https://{seedbox_host}/qbittorrent"
            self._session.headers["Authorization"] = f"Basic {seedbox_basic_auth}"
        else:
            self.base = local_base
            self._local_user = local_user
            self._local_pass = local_pass
            self._logged_in = False

    def _ensure_local_login(self):
        if self.mode != "local" or self._logged_in:
            return
        r = self._session.post(
            f"{self.base}/api/v2/auth/login",
            data={"username": self._local_user, "password": self._local_pass},
            timeout=15,
        )
        r.raise_for_status()
        if r.text.strip() != "Ok.":
            raise SourceError("qbittorrent", f"login rejected: {r.text[:100]}")
        self._logged_in = True

    def torrents(self, category=None):
        try:
            self._ensure_local_login()
            params = {}
            if category:
                params["category"] = category
            r = self._session.get(f"{self.base}/api/v2/torrents/info", params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise SourceError("qbittorrent", e) from e
