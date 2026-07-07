"""
Interactive wizard: asks the questions this stack actually needs answered and writes
.env for you, instead of hand-editing .env.example. Stdlib only, no dependencies --
this runs before `pip install -r scripts/requirements.txt` even makes sense to ask for.

Deliberately doesn't touch anything beyond writing .env -- it doesn't run `docker
compose up`, doesn't install anything, doesn't call provision.py. Print the next steps
and let you actually run them yourself.
"""

import base64
import re
import socket
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_PATH = REPO_ROOT / ".env"


def ask(prompt, default=None):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        answer = input(f"{prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default is not None:
            return default


def ask_yes_no(prompt, default_yes):
    default = "Y/n" if default_yes else "y/N"
    while True:
        answer = input(f"{prompt} [{default}]: ").strip().lower()
        if not answer:
            return default_yes
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer y or n.")


def ask_choice(prompt, choices, default):
    choice_str = "/".join(c if c != default else c.upper() for c in choices)
    while True:
        answer = input(f"{prompt} [{choice_str}]: ").strip().lower()
        if not answer:
            return default
        if answer in choices:
            return answer
        print(f"Please answer one of: {', '.join(choices)}")


def detect_lan_ip():
    """No packets actually sent -- connecting a UDP socket just makes the OS pick
    which local interface/IP would be used, which is all this needs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def ask_domain():
    print()
    print("Every service gets a clean hostname (sonarr.<domain>, radarr.<domain>, ...)")
    print("instead of ip:port, via the Caddy reverse proxy already in this stack.")
    lan_ip = detect_lan_ip()
    nip_default = f"{lan_ip}.nip.io" if lan_ip else None
    if nip_default:
        print(f"No domain of your own? nip.io is a free wildcard DNS service that needs")
        print(f"no account and no local DNS server -- {nip_default} would work right now.")
    print("Have a real domain and Pi-hole already? Use that instead (e.g. example.com),")
    print("pointed at your LAN via Pi-hole's Local DNS records.")
    return ask("Domain to use", default=nip_default)


def compute_basic_auth(user, password):
    return base64.b64encode(f"{user}:{password}".encode()).decode()


def write_env(values):
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(keepends=True)
    key_pattern = re.compile(r"^([A-Z_][A-Z0-9_]*)=")
    out = []
    for line in lines:
        m = key_pattern.match(line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}\n")
        else:
            out.append(line)
    ENV_PATH.write_text("".join(out), encoding="utf-8")


def main():
    if ENV_PATH.exists():
        if not ask_yes_no(f"{ENV_PATH} already exists -- overwrite it?", default_yes=False):
            print("Leaving the existing .env untouched.")
            return

    print("=== Media stack setup ===")
    values = {}

    values["MEDIA_ROOT"] = ask("Where should movies/tv/downloads live (one folder,\n"
                                "  see README section 9 for why it must be one)", default="D:/media")
    values["CONFIG_ROOT"] = ask("Where should each app's config live", default="D:/appdata")
    values["DOMAIN"] = ask_domain()

    print()
    print("seedbox: everything downloads on a remote seedbox you pay for -- needed for")
    print("  ratio/Hit&Run management on a private tracker. See README section 9.")
    print("local: a qBittorrent container in this stack does the downloading, no")
    print("  seedbox needed. No cost, but no VPN by default and no ratio management.")
    mode = ask_choice("Download mode", ["seedbox", "local"], default="local")
    values["DOWNLOAD_MODE"] = mode
    values["COMPOSE_PROFILES"] = "local" if mode == "local" else ""

    if mode == "seedbox":
        print()
        print("--- Seedbox ---")
        seedbox_url = ask("Seedbox qBittorrent WebUI URL (e.g. https://your-seedbox:port)")
        values["SEEDBOX_URL"] = seedbox_url
        values["SEEDBOX_HOST"] = re.sub(r"^https?://", "", seedbox_url).split("/")[0]
        values["SEEDBOX_USER"] = ask("Seedbox login username")
        values["SEEDBOX_PASS"] = ask("Seedbox login password")
        values["SEEDBOX_BASIC_AUTH"] = compute_basic_auth(values["SEEDBOX_USER"], values["SEEDBOX_PASS"])
        values["RATIO_CATEGORY"] = ask("qBittorrent category for the ratio-obligated tracker", default="ratio")
        values["PUBLIC_CATEGORY"] = ask("Transmission category for everything else", default="public")

        has_private = ask_yes_no("Do you have a private tracker with a ratio/Hit&Run rule to satisfy?", default_yes=True)
        if has_private:
            values["PRIVATE_TRACKER_NAME"] = ask("That tracker's name (must match how Prowlarr names it)", default="TorrentLeech")
            values["PRIVATE_TRACKER_SEED_RATIO"] = ask("Required seed ratio", default="1")
            values["PRIVATE_TRACKER_SEED_TIME_MINUTES"] = ask("Required seed time in minutes", default="14400")
        else:
            # Deliberately blank, not a placeholder name -- provision.py treats an
            # empty PRIVATE_TRACKER_NAME as "nothing is private," routing every
            # indexer to the public/Transmission client.
            values["PRIVATE_TRACKER_NAME"] = ""
    else:
        print()
        print("--- Local qBittorrent ---")
        values["QBT_CATEGORY"] = ask("Download category", default="downloads")
        print("Choose a permanent WebUI password now -- you'll set this exact password")
        print("in qBittorrent's own UI during the one-time first-run step below (the")
        print("linuxserver/qbittorrent image ignores env-supplied credentials at")
        print("container start, so this doesn't set it automatically, but provision.py")
        print("uses it as Sonarr/Radarr's real download-client login).")
        values["QBT_USER"] = "admin"
        values["QBT_PASS"] = ask("qBittorrent WebUI password to set")

    write_env(values)
    print()
    print(f"Wrote {ENV_PATH}")
    print()
    print("Next steps:")
    print("  1. docker compose up -d")
    if mode == "local":
        print("  2. docker logs qbittorrent   # find 'A temporary password is provided...'")
        print("     Log into http://localhost:8080 as admin with that password, then:")
        print(f"       - set the permanent password to exactly what you entered above")
        print("         (Options > WebUI)")
        print("       - set Default Save Path to /media/downloads (Options > Downloads)")
        print("  3. pip install -r scripts/requirements.txt && python scripts/provision.py")
    else:
        print("  2. pip install -r scripts/requirements.txt && python scripts/provision.py")
    print()
    print("See README.md for what provision.py does and doesn't automate.")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print()
        print("Cancelled, nothing written.")
        sys.exit(1)
