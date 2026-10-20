"""Signed, expiring login tokens for the app and the dashboard. Login is by phone + PIN (app/mobile/accounts.py).

Token = "<user_id>.<version>.<expiry unix>.<hmac>" with HMAC-SHA256 over the first three parts and DASHBOARD_SECRET.
The version must equal users.token_version, so "logout" (bumping it) kills every link already sent.
Without a secret the dashboard is off (fail closed).
"""

import hashlib
import hmac
import time

from app.config import get_settings


MIN_SECRET = 32  # shorter secrets are treated as unset: the dashboard stays off rather than guessable


def _secret() -> str:
    secret = get_settings().dashboard_secret
    return secret if len(secret) >= MIN_SECRET else ""


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(user_id: int, version: int = 0, now: float | None = None, hours: int | None = None) -> str:
    s = get_settings()
    if not _secret():
        raise RuntimeError(f"DASHBOARD_SECRET is not set or shorter than {MIN_SECRET} characters")
    expires = int((now or time.time()) + (hours or s.dashboard_link_hours) * 3600)
    payload = f"{user_id}.{version}.{expires}"
    return f"{payload}.{_sign(payload, _secret())}"


def read_token(token: str | None, now: float | None = None) -> tuple[int, int] | None:
    """(user id, token version) if the token is genuine and not expired, else None.
    The caller must still compare the version with the user's current token_version."""
    secret = _secret()
    if not secret or not token or token.count(".") != 3:
        return None
    user_id, version, expires, signature = token.split(".")
    # bytes, not str: compare_digest raises TypeError on non-ASCII str, which would be a 500 instead of a 401
    if not hmac.compare_digest(signature.encode(), _sign(f"{user_id}.{version}.{expires}", secret).encode()):
        return None
    parts = (user_id, version, expires)
    if not all(p.isascii() and p.isdigit() for p in parts) or int(expires) < (now or time.time()):
        return None
    return int(user_id), int(version)
