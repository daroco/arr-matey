"""
Keeps the public DNS records for this stack's internet-facing hostnames (on
Cloudflare -- whatever DDNS_RECORDS in .env lists: watch/apex/stats/games.<domain>
as of this writing) pointed at this machine's current WAN IP. Run via pythonw.exe as a scheduled
task action, same pattern as scripts/rclone-sync.py -- pythonw has no console,
so nothing flashes on each run, and every outcome (including failures) goes to
a log file since an uncaught exception would otherwise vanish silently.

Why this exists at all: the WAN IP on a residential connection is not guaranteed
static (see README section 6). Caddy's DNS-01 ACME challenge and the router's
port-443 forward both depend on each of these hostnames resolving to wherever
this house currently is -- if the IP drifts and a record doesn't follow, the
cert still renews fine (DNS-01 only needs the TXT challenge record, not the A
record) but remote access to that one app silently starts failing to connect.

Cloudflare's API needs the DNS record's own ID to PATCH it -- there's no
"upsert by name" endpoint -- so this looks each record up by name first (GET),
then only PATCHes if the current value actually differs. Comparing before
writing means a no-op run (the common case, IP unchanged) makes one read-only
API call per record instead of an unconditional write every few minutes. One
record's lookup/update failure doesn't stop the others from being checked --
main() collects failures and reports them all at the end.

VPN guard: a system-wide VPN client on this host (ProtonVPN, seen live on
2026-09-16) adds its own default route, so every "what's my IP" echo service
answers with the VPN's exit IP -- and this script would then faithfully point
every public hostname at a server in another city that isn't forwarding 443 to
this house. That happened: all four records were rewritten to Proton's IP
within one 5-minute cycle and every public route returned Cloudflare 522 until
the VPN was disconnected. So before trusting the echoed IP, main() looks at the
IPv4 default routes: more than one, or any on an interface whose name looks
like a VPN adapter, means the answer can't be trusted and the run is skipped
(logged, one ntfy push per VPN session via a marker file, never an error --
the records keep their last good value, which is exactly what we want).
"""

import logging
import logging.handlers
import re
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import dotenv_values

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

_env = dotenv_values(ENV_PATH)
CONFIG_ROOT = _env["CONFIG_ROOT"]
CF_API_TOKEN = _env["CF_API_TOKEN"]
CF_ZONE = _env["CF_ZONE"]
DDNS_RECORDS = [r.strip() for r in _env["DDNS_RECORDS"].split(",") if r.strip()]
NTFY_SERVER = _env.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOPIC = _env.get("NTFY_TOPIC", "")
# Bearer token for the self-hosted ntfy (deny-all access by default); blank for
# a server that allows anonymous publishing, e.g. public ntfy.sh.
NTFY_TOKEN = _env.get("NTFY_TOKEN", "")

LOG_PATH = Path(CONFIG_ROOT) / "ddns-update.log"
# Exists while runs are being skipped because a VPN owns the default route --
# so the "paused"/"resumed" ntfy pushes fire once per VPN session, not every
# 5 minutes for as long as the VPN stays up.
VPN_PAUSE_FLAG = Path(CONFIG_ROOT) / "ddns-vpn-paused.flag"
# Interface names that mean "this default route is a tunnel, not the WAN".
VPN_ALIAS_PATTERN = re.compile(r"vpn|wireguard|openvpn|tailscale|mullvad|nord|tap-|tun", re.IGNORECASE)
PHYSICAL_ALIAS_PATTERN = re.compile(r"^(Ethernet|Wi-Fi|WiFi|Local Area Connection)", re.IGNORECASE)
CF_API = "https://api.cloudflare.com/client/v4"
IP_ECHO_SERVICES = ["https://api.ipify.org?format=json", "https://ifconfig.me/all.json"]

log = logging.getLogger("ddns-update")


def vpn_default_routes():
    """Names of IPv4 default-route interfaces that make the echoed WAN IP untrustworthy.

    Empty list = safe to proceed. Windows-only by construction (Get-NetRoute); on
    anything else, or if the query itself fails, it returns [] with a warning so
    the guard degrades to the old unguarded behaviour rather than blocking DDNS.
    """
    if sys.platform != "win32":
        return []
    try:
        out = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-NetRoute -DestinationPrefix 0.0.0.0/0 -AddressFamily IPv4 "
                "| Select-Object -ExpandProperty InterfaceAlias",
            ],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning(f"could not inspect default routes, proceeding unguarded: {e}")
        return []
    aliases = [line.strip() for line in out.splitlines() if line.strip()]
    suspicious = [a for a in aliases if VPN_ALIAS_PATTERN.search(a)]
    if not suspicious and len(aliases) > 1:
        # A single-WAN box normally has exactly one default route. Two is fine only
        # when both are ordinary physical adapters to the same router (wired +
        # Wi-Fi on a laptop); anything else in there is a tunnel with an
        # unrecognised name.
        suspicious = [a for a in aliases if not PHYSICAL_ALIAS_PATTERN.match(a)]
    return suspicious


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
            headers={"Authorization": f"Bearer {NTFY_TOKEN}"} if NTFY_TOKEN else None,
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


def get_record(zone_id, hostname):
    r = requests.get(
        f"{CF_API}/zones/{zone_id}/dns_records",
        headers=cf_headers(),
        params={"type": "A", "name": hostname},
        timeout=15,
    )
    r.raise_for_status()
    result = r.json()["result"]
    if not result:
        raise RuntimeError(f"Cloudflare A record not found: {hostname} (create it once manually first)")
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
    suspicious = vpn_default_routes()
    if suspicious:
        log.warning(f"skipping: a VPN owns the default route ({', '.join(suspicious)}); "
                    "the echoed WAN IP would be the VPN's exit, not this house")
        if not VPN_PAUSE_FLAG.exists():
            VPN_PAUSE_FLAG.touch()
            notify_ntfy("DDNS paused", f"VPN default route detected ({', '.join(suspicious)}); "
                        "records left as-is until it's disconnected")
        return
    if VPN_PAUSE_FLAG.exists():
        VPN_PAUSE_FLAG.unlink()
        log.info("VPN gone, resuming DDNS updates")
        notify_ntfy("DDNS resumed", "VPN default route gone, checking records again")

    new_ip = current_wan_ip()
    zone_id = get_zone_id()

    failures = []
    for hostname in DDNS_RECORDS:
        try:
            record = get_record(zone_id, hostname)
            old_ip = record["content"]

            if old_ip == new_ip:
                log.info(f"no change: {hostname} already {new_ip}")
                continue

            update_record(zone_id, record["id"], new_ip)
            log.info(f"updated: {hostname} {old_ip} -> {new_ip}")
            notify_ntfy("DDNS updated", f"{hostname}: {old_ip} -> {new_ip}")
        except Exception as e:
            log.exception(f"failed to update {hostname}")
            failures.append(f"{hostname}: {e}")

    if failures:
        raise RuntimeError(f"{len(failures)} of {len(DDNS_RECORDS)} record(s) failed: {'; '.join(failures)}")


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
