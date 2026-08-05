"""
Transmission JSON-RPC client -- the 409/X-Transmission-Session-Id handshake lifted
directly from scripts/seedbox-cleanup.py rather than reimplemented (3-attempt retry,
same as there). Seedbox-mode only; local mode has no Transmission instance.

status is an int (Transmission's own enum): 0=stopped, 1=queued-to-verify,
2=verifying, 3=queued-to-download, 4=downloading, 5=queued-to-seed, 6=seeding.
isFinished (bool) disambiguates status==0 meaning "done and stopped at its seed
target" vs. "stopped before completion" -- confirmed live during Phase 0 against a
real completed-but-import-blocked torrent (status 0, isFinished true, percentDone 1).
"""

import requests

from .base import SourceError

FIELDS = [
    "id", "hashString", "name", "status", "uploadRatio", "seedRatioLimit",
    "percentDone", "peersConnected", "peersSendingToUs", "isFinished",
    "downloadDir", "doneDate", "leftUntilDone", "sizeWhenDone",
]


def normalize_state(status, is_finished):
    if status == 6:
        return "seeding"
    if status == 0:
        return "complete_paused" if is_finished else "unknown"
    if status in (1, 2, 3, 4, 5):
        return "downloading"
    return "unknown"


class TransmissionClient:
    def __init__(self, seedbox_host, seedbox_basic_auth):
        self.url = f"https://{seedbox_host}/rpc"
        self.headers = {"Authorization": f"Basic {seedbox_basic_auth}"}
        self._session_id = None

    def _rpc(self, method, arguments=None, attempts=3):
        body = {"method": method}
        if arguments:
            body["arguments"] = arguments
        headers = dict(self.headers)
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id
        try:
            for attempt in range(attempts):
                r = requests.post(self.url, headers=headers, json=body, timeout=20)
                if r.status_code == 409:
                    self._session_id = r.headers.get("X-Transmission-Session-Id")
                    headers["X-Transmission-Session-Id"] = self._session_id
                    continue
                r.raise_for_status()
                return r.json()
            raise SourceError("transmission", "409 handshake did not resolve after 3 attempts")
        except requests.RequestException as e:
            raise SourceError("transmission", e) from e

    def torrents(self):
        data = self._rpc("torrent-get", {"fields": FIELDS})
        return data.get("arguments", {}).get("torrents", [])
