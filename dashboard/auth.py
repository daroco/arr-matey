"""
Login via Jellyfin credentials -- the same credential-delegation Seerr itself uses
(see clients/jellyfin.py's docstring): the dashboard's own login form forwards
username/password to Jellyfin's AuthenticateByName, trusts Jellyfin's answer, and
keeps its own session afterward. Not SSO -- there's no shared token, each app
validates independently against the same Jellyfin account.

Sessions are opaque random tokens stored in the `session` SQLite table (not JWTs, not
the Jellyfin AccessToken itself) so a session can be revoked server-side by deleting
its row -- itsdangerous only signs the cookie to prevent tampering/forgery of which
token a client claims, it doesn't make the token meaningful on its own.

Any authenticated Jellyfin user can view traces (require_session). Only accounts where
Jellyfin reports Policy.IsAdministrator can execute fix actions (require_admin) --
mirrors the admin/member boundary Jellyfin already enforces rather than inventing a
separate permission system.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeSerializer

from .clients.jellyfin import JellyfinAuthError, authenticate
from .models import utcnow_iso

log = logging.getLogger("dashboard.auth")

COOKIE_NAME = "dashboard_session"
SESSION_LIFETIME_DAYS = 14


class AuthState:
    """Holds the signing secret + db reference; instantiated once in main.py and
    passed to route handlers via FastAPI's dependency system (app.state)."""

    def __init__(self, db, cfg, secret_key):
        self.db = db
        self.cfg = cfg
        self.serializer = URLSafeSerializer(secret_key, salt="dashboard-session")

    def login(self, username, password):
        """Raises JellyfinAuthError on bad credentials (caller renders the login
        page with a generic error). Returns the signed cookie value on success."""
        user, _access_token = authenticate(self.cfg.jellyfin_base, username, password)
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=SESSION_LIFETIME_DAYS)
        is_admin = bool(user.get("Policy", {}).get("IsAdministrator", False))
        with self.db.conn:
            self.db.conn.execute(
                "INSERT INTO session (token, jellyfin_user_id, username, is_admin, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                (token, user["Id"], user["Name"], int(is_admin), now.isoformat(), expires.isoformat()),
            )
        log.info(f"login: {user['Name']} (admin={is_admin})")
        return self.serializer.dumps(token)

    def logout(self, cookie_value):
        token = self._unsign(cookie_value)
        if token:
            with self.db.conn:
                self.db.conn.execute("DELETE FROM session WHERE token = ?", (token,))

    def _unsign(self, cookie_value):
        if not cookie_value:
            return None
        try:
            return self.serializer.loads(cookie_value)
        except BadSignature:
            return None

    def session_for(self, cookie_value):
        token = self._unsign(cookie_value)
        if not token:
            return None
        row = self.db.conn.execute(
            "SELECT * FROM session WHERE token = ? AND expires_at > ?",
            (token, utcnow_iso()),
        ).fetchone()
        return row


def get_auth_state(request: Request) -> AuthState:
    return request.app.state.auth


def require_session(request: Request, dashboard_session: str = Cookie(default=None)):
    auth: AuthState = request.app.state.auth
    session = auth.session_for(dashboard_session)
    if session is None:
        # 303 + Location is how an exception handler can express a redirect here --
        # verified against a real request in main.py's route tests, not just assumed.
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return session


def require_admin(session=Depends(require_session)):
    if not session["is_admin"]:
        raise HTTPException(status_code=403, detail="Jellyfin admin required for this action")
    return session
