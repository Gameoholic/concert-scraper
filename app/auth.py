"""Single-user password login, lockout after repeated failures, and a CSRF origin check."""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.responses import PlainTextResponse

MAX_FAILS = 5
LOCK_SECONDS = 5 * 60
SESSION_DAYS = 30


def fingerprint(password: str, secret: str) -> str:
    """Stored in the session, so changing APP_PASSWORD signs every device out."""
    return hmac.new(secret.encode(), password.encode(), hashlib.sha256).hexdigest()[:24]


def password_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode(), expected.encode())


class Lockout:
    """In-memory failure counter per client IP. One user, one process: enough."""

    def __init__(self) -> None:
        self._fails: dict[str, list[float]] = {}

    def seconds_left(self, ip: str) -> int:
        fails = [t for t in self._fails.get(ip, []) if time.monotonic() - t < LOCK_SECONDS]
        self._fails[ip] = fails
        if len(fails) < MAX_FAILS:
            return 0
        return int(LOCK_SECONDS - (time.monotonic() - fails[-MAX_FAILS])) + 1

    def fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.monotonic())

    def clear(self, ip: str) -> None:
        self._fails.pop(ip, None)


class OriginCheck:
    """Refuse state-changing requests whose Origin (or Referer) is another site."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
            source = headers.get("origin") or headers.get("referer")
            if source and source != "null":
                if urlsplit(source).netloc.lower() != headers.get("host", "").lower():
                    await PlainTextResponse("Cross-site request refused", status_code=403)(scope, receive, send)
                    return
            elif source == "null":
                await PlainTextResponse("Cross-site request refused", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
