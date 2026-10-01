"""Accounts: password hashing, session cookies and login throttling."""

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request
from fastapi.responses import Response

from . import db

COOKIE = "refeed_session"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
MIN_PASSWORD = 8

# scrypt with n=2**14, r=8: ~16 MB and a few tens of ms per hash.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p)
        )
        return hmac.compare_digest(actual, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def authenticate(username: str, password: str):
    """The user, if the password is right. Takes as long for unknown usernames."""
    user = db.get_user_by_name(username.strip())
    ok = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
    return user if user and ok else None


def check_new_credentials(username: str | None, password: str, confirm: str) -> str | None:
    """An error message for a bad username/password choice, or None."""
    if username is not None and not USERNAME_RE.match(username):
        return "Usernames are 3–32 letters, numbers, dots, dashes or underscores."
    if len(password) < MIN_PASSWORD:
        return f"Use a password of at least {MIN_PASSWORD} characters."
    if password != confirm:
        return "The two passwords don't match."
    return None


# ------------------------------------------------------------------ throttle


class LoginThrottle:
    """At most `limit` failed logins per client address per `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 900):
        self.limit, self.window = limit, window
        self.failures: dict[str, deque] = defaultdict(deque)

    def _recent(self, key: str) -> deque:
        q = self.failures[key]
        while q and q[0] < time.time() - self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.limit

    def fail(self, key: str):
        self._recent(key).append(time.time())

    def reset(self, key: str):
        self.failures.pop(key, None)


throttle = LoginThrottle()


def client_key(request: Request) -> str:
    # Behind a reverse proxy, uvicorn's --proxy-headers puts the visitor's address here.
    return request.client.host if request.client else "unknown"


# ------------------------------------------------------------------- sessions


def start_session(response: Response, request: Request, user_id: int, base_url: str = ""):
    token = db.create_session(user_id)
    secure = request.url.scheme == "https" or base_url.startswith("https://")
    response.set_cookie(
        COOKIE,
        token,
        max_age=db.SESSION_DAYS * 86400,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def end_session(response: Response, request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        db.delete_session(token)
    response.delete_cookie(COOKIE)


def current_user(request: Request):
    """FastAPI dependency: the signed-in user, or a redirect to the login page."""
    token = request.cookies.get(COOKIE)
    user = db.user_for_session(token) if token else None
    if user is None:
        raise HTTPException(303, headers={"Location": "/login"})
    return user


def admin_user(request: Request):
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Admins only")
    return user
