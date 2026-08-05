"""
Jellyfin client -- login only. This dashboard doesn't need anything else from Jellyfin;
it exists purely so the dashboard's own login form can validate credentials the same
way Seerr does (see Seerr's auth.js: it forwards the submitted username/password
straight to this exact endpoint and trusts Jellyfin's answer -- credential delegation,
not real SSO). Endpoint shape confirmed live during Phase 0: POST /Users/AuthenticateByName
requires an X-Emby-Authorization header identifying the calling client (no pre-shared
API key needed for login itself) and a {"Username", "Pw"} JSON body; a bad login is a
plain 401.
"""

import requests

AUTH_HEADER = 'MediaBrowser Client="acquisitions-dashboard", Device="dashboard", DeviceId="acquisitions-dashboard", Version="1.0.0"'


class JellyfinAuthError(Exception):
    pass


def authenticate(base_url, username, password):
    """Returns the parsed User dict (includes Policy.IsAdministrator) and AccessToken
    on success. Raises JellyfinAuthError on bad credentials or any request failure --
    callers should show a generic "login failed" rather than distinguishing "server
    unreachable" from "bad password" to a public login form, to avoid leaking which
    failure mode occurred to an unauthenticated caller."""
    try:
        r = requests.post(
            f"{base_url}/Users/AuthenticateByName",
            headers={"X-Emby-Authorization": AUTH_HEADER, "Content-Type": "application/json"},
            json={"Username": username, "Pw": password},
            timeout=15,
        )
    except requests.RequestException as e:
        raise JellyfinAuthError(f"could not reach Jellyfin: {e}") from e
    if r.status_code != 200:
        raise JellyfinAuthError("invalid username or password")
    data = r.json()
    return data["User"], data.get("AccessToken", "")
