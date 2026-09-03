"""Authentication for the web UI.

Required once the UI leaves localhost: the database holds platform tokens and
the UI can spend money and post publicly, so an open port is not acceptable.

Password hashing is scrypt from the standard library - memory-hard, and it
avoids adding a native dependency for one function. Sessions are stateless
signed cookies (HMAC-SHA256 over user id + expiry), so restarting the server
does not log everyone out and there is no session table to prune. Signatures
and CSRF tokens are compared with `compare_digest`; a plain `==` on a MAC leaks
timing information.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from ..config import Settings
from ..models import User, utcnow

log = logging.getLogger(__name__)

SESSION_COOKIE = "snsauto_session"
CSRF_COOKIE = "snsauto_csrf"
CSRF_FIELD = "csrf_token"

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1


class AuthError(RuntimeError):
    pass


# ---------------- passwords ----------------


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        candidate = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(digest_hex)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(digest_hex))


# ---------------- signed sessions ----------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: bytes, secret: str) -> str:
    return _b64e(hmac.new(secret.encode(), payload, hashlib.sha256).digest())


def issue_session(user_id: int, secret: str, hours: int = 12) -> str:
    body = json.dumps(
        {"uid": user_id, "exp": int(time.time()) + hours * 3600}, separators=(",", ":")
    ).encode()
    return f"{_b64e(body)}.{_sign(body, secret)}"


def read_session(token: str | None, secret: str) -> int | None:
    """Return the user id, or None when the token is absent, forged or expired."""
    if not token or "." not in token:
        return None
    encoded, signature = token.rsplit(".", 1)
    try:
        body = _b64d(encoded)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_sign(body, secret), signature):
        return None
    try:
        claims = json.loads(body)
    except json.JSONDecodeError:
        return None
    if int(claims.get("exp", 0)) < time.time():
        return None
    uid = claims.get("uid")
    return int(uid) if isinstance(uid, int) else None


# ---------------- CSRF ----------------


def issue_csrf() -> str:
    return secrets.token_urlsafe(32)


def check_csrf(cookie_value: str | None, form_value: str | None) -> bool:
    """Double-submit cookie: the form must echo the cookie exactly."""
    if not cookie_value or not form_value:
        return False
    return hmac.compare_digest(cookie_value, form_value)


# ---------------- users ----------------


def create_user(
    session, email: str, password: str, name: str | None = None, role: str = "editor"
) -> User:
    email = email.strip().lower()
    if not email or "@" not in email:
        raise AuthError("a valid email address is required")
    if role not in ("admin", "editor", "viewer"):
        raise AuthError(f"unknown role {role!r}")
    if session.query(User).filter_by(email=email).one_or_none():
        raise AuthError(f"user {email} already exists")

    user = User(
        email=email, name=name, password_hash=hash_password(password), role=role
    )
    session.add(user)
    session.flush()
    return user


def authenticate(session, email: str, password: str) -> User | None:
    user = session.query(User).filter_by(email=(email or "").strip().lower()).one_or_none()
    # Hash regardless so a missing account and a wrong password take the same
    # time; otherwise the response time enumerates valid addresses.
    reference = user.password_hash if user else hash_password("placeholder-timing")
    ok = verify_password(password or "", reference)
    if not user or not ok or not user.is_active:
        return None
    user.last_login_at = utcnow()
    session.flush()
    return user


def resolve_secret(settings: Settings) -> str:
    """The signing key. Refuses to invent one when auth is actually on."""
    if settings.secret_key:
        return settings.secret_key
    if settings.auth_enabled:
        raise AuthError(
            "SNSAUTO_SECRET_KEY must be set when SNSAUTO_AUTH_ENABLED=true. "
            "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(48))\""
        )
    # Auth is off (localhost); a per-process key is fine and leaks nothing.
    return secrets.token_urlsafe(48)
