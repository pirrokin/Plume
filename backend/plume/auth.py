"""Optional password protection, for when Plume is reachable from the internet.

Set PLUME_PASSWORD to enable it. Sessions are stateless signed cookies; the
signing key comes from PLUME_SECRET or is generated once in the data directory.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time

from . import config

PASSWORD = os.environ.get("PLUME_PASSWORD", "")
SESSION_DAYS = float(os.environ.get("PLUME_SESSION_DAYS", "30"))
COOKIE = "plume_session"

# Paths reachable without a session.
PUBLIC_API = {"/api/health", "/api/session", "/api/login", "/api/logout"}

_attempts: dict[str, list[float]] = {}
_attempts_lock = threading.Lock()
MAX_ATTEMPTS = 8
WINDOW = 15 * 60


def enabled() -> bool:
    return bool(PASSWORD)


def _secret() -> bytes:
    env = os.environ.get("PLUME_SECRET")
    if env:
        return env.encode()
    path = config.DATA_DIR / "secret.key"
    try:
        return path.read_bytes()
    except OSError:
        key = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return key


def _sign(payload: str) -> str:
    mac = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def issue() -> str:
    expires = int(time.time() + SESSION_DAYS * 86400)
    # The password fingerprint ties sessions to the current password.
    fp = hashlib.sha256(PASSWORD.encode()).hexdigest()[:8]
    payload = f"{expires}.{fp}"
    return f"{payload}.{_sign(payload)}"


def valid(token: str | None) -> bool:
    if not enabled():
        return True
    if not token or token.count(".") != 2:
        return False
    expires, fp, mac = token.split(".")
    payload = f"{expires}.{fp}"
    if not hmac.compare_digest(mac, _sign(payload)):
        return False
    if fp != hashlib.sha256(PASSWORD.encode()).hexdigest()[:8]:
        return False
    return expires.isdigit() and int(expires) > time.time()


def check_password(candidate: str, client: str) -> bool:
    """Constant-time comparison with a simple per-client attempt limit."""
    now = time.time()
    with _attempts_lock:
        recent = [t for t in _attempts.get(client, []) if now - t < WINDOW]
        if len(recent) >= MAX_ATTEMPTS:
            _attempts[client] = recent
            raise PermissionError("Trop de tentatives, réessayez dans quelques minutes.")
        ok = hmac.compare_digest(candidate.encode(), PASSWORD.encode())
        if not ok:
            recent.append(now)
        _attempts[client] = recent if not ok else []
        return ok
