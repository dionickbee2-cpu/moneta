"""
Personal login links for opening the mini app outside Telegram (browser / home-screen app).

A token is `<payload>.<signature>`: payload is base64url JSON {u: user id, v: session version,
e: expiry}, signed with HMAC-SHA256. Bumping the user's session version (stored as a setting row)
revokes every link issued before.
"""

import os
import hmac
import json
import time
import base64
import hashlib
import threading

import storage

TTL = 180 * 24 * 3600
VERSION_CACHE_SECONDS = 300

_versions = {}  # uid -> (version, fetched_at)
_lock = threading.Lock()


def _secret() -> bytes:
    base = os.getenv("SESSION_SECRET") or os.getenv("TELEGRAM_TOKEN", "")
    if not base:
        raise RuntimeError("SESSION_SECRET or TELEGRAM_TOKEN must be set")
    return hmac.new(b"moneta-session", base.encode(), hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())


def get_version(uid: int) -> int:
    with _lock:
        cached = _versions.get(uid)
    if cached and time.time() - cached[1] < VERSION_CACHE_SECONDS:
        return cached[0]
    rows = storage.read_rows(uid)
    try:
        version = int(storage.get_setting(rows, "session_version", "0") or 0)
    except ValueError:
        version = 0
    with _lock:
        _versions[uid] = (version, time.time())
    return version


def make_token(uid: int, now: float = None) -> str:
    now = now or time.time()
    payload = {"u": uid, "v": get_version(uid), "e": int(now + TTL)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def verify_token(token: str, now: float = None):
    """User id for a valid, unexpired, unrevoked token; otherwise None."""
    try:
        body, sig = token.strip().split(".")
        if not hmac.compare_digest(_sign(body), sig):
            return None
        payload = json.loads(_unb64(body))
        uid, version, expires = int(payload["u"]), int(payload["v"]), int(payload["e"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now or time.time()) > expires:
        return None
    if version != get_version(uid):
        return None
    return uid


def revoke_all(uid: int) -> None:
    version = get_version(uid) + 1
    storage.set_setting(uid, "session_version", str(version))
    with _lock:
        _versions[uid] = (version, time.time())
