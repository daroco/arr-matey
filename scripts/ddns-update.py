"""
Keeps the public DNS record for the remote-Jellyfin hostname (watch.<domain>, on
Cloudflare) pointed at this machine's current WAN IP. Run via pythonw.exe as a
scheduled task action, same pattern as scripts/rclone-sync.py -- pythonw has no
console, so nothing flashes on each run, and every outcome (including failures)
goes to a log file since an uncaught exception would otherwise vanish silently.

Why this exists at all: the WAN IP on a residential connection is not guaranteed
static (see README section 6). Caddy's DNS-01 ACME challenge and the router's
port-443 forward both depend on watch.<domain> resolving to wherever this house
currently is -- if the IP drifts and the A record doesn't follow, the cert still
renews fine (DNS-01 only needs the TXT challenge record, not the A record) but
remote playback silently starts failing to connect.

Cloudflare's API needs the DNS record's own ID to PATCH it -- there's no
"upsert by name" endpoint -- so this looks the record up by name first (GET),
then only PATCHes if the current value actually differs. Comparing before
writing means a no-op run (the common case, IP unchanged) makes one read-only
API call instead of an unconditional write every few minutes.
"""

import logging
import logging.handlers
import sys

import requests
from dotenv import dotenv_values

ENV_PATH = r"C:\Users\drcor\acquisitions\.env"

_env = dotenv_values(ENV_PATH)
CONFIG_ROOT = _env["CONFIG_ROOT"]
CF_API_TOKEN = _env["CF_API_TOKEN"]
CF_ZONE = _env["CF_ZONE"]
DDNS_RECORD = _env["DDNS_RECORD"]
NTFY_SERVER = _env.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = _env.get("NTFY_TOPIC", "")

LOG_PATH = f"{CONFIG_ROOT}\\ddns-update.log"
CF_API = "https://api.cloudflare.com/client/v4"
IP_ECHO_SERVICES = ["https://api.ipify.org?format=json", "https://ifconfig.me/all.json"]

log = logging.getLogger("ddns-update")


def notify_ntfy(title, message):
    # Same best-effort pattern as scripts/rclone-sync.py's notify_ntfy: a failed
    # push is logged but never fails the run -- notifications are a convenience,
    # not something the DDNS update should depend on.
    if not NTFY_TOPIC:
        return
    try:
        r = requests.post(
            NTFY_SERVER,
            json={"topic": NTFY_TOPIC, "title": title, "message": message},
            timeout=10,
        )
        if not r.ok:
            log.warning(f"ntfy notification rejected ({r.status_code}): {title} -- {r.text[:200]}")
    except requests.RequestException:
        log.exception(f"ntfy notification failed: {title}")


def current_wan_ip():
    last_error = None
    for url in IP_ECHO_SERVICES:
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            data = r.json()
            ip = data.get("ip") or data.get("ip_addr")
            if ip:
                return ip
        except (requests.RequestException, ValueError) as e:
            last_error = e
            log.warning(f"IP echo service failed ({url}): {e}")
    raise RuntimeError(f"all IP echo services failed; last error: {last_error}")


def cf_headers():
    return {"Authorization": f"Bearer {CF_API_TOKEN}", "Content-Type": "application/json"}


def get_zone_id():
    r = requests.get(f"{CF_API}/zones", headers=cf_headers(), params={"name": CF_ZONE}, timeout=15)
    r.raise_for_status()
    result = r.json()["result"]
    if not result:
        raise RuntimeError(f"Cloudflare zone not found: {CF_ZONE}")
    return result[0]["id"]


def get_record(zone_id):
    r = requests.get(
        f"{CF_API}/zones/{zone_id}/dns_records",
        headers=cf_headers(),
        params={"type": "A", "name": DDNS_RECORD},
        timeout=15,
    )
    r.raise_for_status()
    result = r.json()["result"]
    if not result:
        raise RuntimeError(f"Cloudflare A record not found: {DDNS_RECORD} (create it once manually first)")
    return result[0]


def update_record(zone_id, record_id, new_ip):
    r = requests.patch(
        f"{CF_API}/zones/{zone_id}/dns_records/{record_id}",
        headers=cf_headers(),
        json={"content": new_ip},
        timeout=15,
    )
    r.raise_for_status()
    if not r.json().get("success"):
        raise RuntimeError(f"Cloudflare update reported failure: {r.text[:300]}")


def main():
    new_ip = current_wan_ip()
    zone_id = get_zone_id()
    record = get_record(zone_id)
    old_ip = record["content"]

    if old_ip == new_ip:
        log.info(f"no change: {DDNS_RECORD} already {new_ip}")
        return

    update_record(zone_id, record["id"], new_ip)
    log.info(f"updated: {DDNS_RECORD} {old_ip} -> {new_ip}")
    notify_ntfy("DDNS updated", f"{DDNS_RECORD}: {old_ip} -> {new_ip}")


def setup_logging():
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.addHandler(logging.StreamHandler(sys.stdout))


if __name__ == "__main__":
    setup_logging()
    try:
        main()
    except Exception:
        log.exception("ddns-update failed")
        notify_ntfy("DDNS update FAILED", "see ddns-update.log")
        sys.exit(1)
