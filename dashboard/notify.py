"""
Push notifications via ntfy.sh -- same best-effort pattern scripts/rclone-sync.py and
scripts/ddns-update.py already use (and the same NTFY_SERVER/NTFY_TOPIC .env vars, so
whatever topic is already subscribed to on your phone just starts receiving these
too, no new setup). A failed POST is logged but never raises -- a notification
failing should never take down the poller or a request.
"""

import logging

import requests

log = logging.getLogger("dashboard.notify")


def notify_ntfy(server, topic, title, message):
    if not topic:
        return
    try:
        r = requests.post(server, json={"topic": topic, "title": title, "message": message}, timeout=10)
        if not r.ok:
            log.warning(f"ntfy notification rejected ({r.status_code}): {title} -- {r.text[:200]}")
    except requests.RequestException:
        log.exception(f"ntfy notification failed: {title}")
