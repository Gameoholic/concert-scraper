"""Try again, with backoff, when a website or Apify fails for a reason that usually passes: a timeout, a 5xx, a 429,
a DNS hiccup (`getaddrinfo failed`), a dropped connection. Never for a 404 or anything else that will fail the same
way again."""

from __future__ import annotations

import socket
import time
from typing import Callable, TypeVar

T = TypeVar("T")
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
TRANSIENT_WORDS = ("timed out", "timeout", "getaddrinfo", "name resolution", "temporarily unavailable",
                   "connection reset", "connection aborted", "connection refused", "remote end closed",
                   "server disconnected", "err_name_not_resolved", "err_connection", "err_timed_out",
                   "err_network_changed", "err_internet_disconnected", "timed-out")
BUSY_WORDS = ("memory limit", "concurrent", "too many requests", "rate limit")
MAX_WAIT = 60.0

OnRetry = Callable[[int, Exception, float], None]      # (retry number, the error, seconds until it)


def delays(retries: int, first: float = 3.0) -> tuple[float, ...]:
    """3 s, 9 s, 27 s, ... for `retries` retries."""
    return tuple(first * 3 ** i for i in range(max(retries, 0)))


def status_of(e: BaseException) -> int | None:
    for attr in ("status", "status_code"):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "response", None)
    v = getattr(resp, "status_code", None)
    return v if isinstance(v, int) else None


def transient(e: BaseException) -> bool:
    """Worth another try? An explicit `transient` attribute wins, then the HTTP status, then the kind of error."""
    flag = getattr(e, "transient", None)
    if isinstance(flag, bool):
        return flag
    if any(w in str(e).lower() for w in BUSY_WORDS):       # Apify: too many runs or too much memory at once
        return True
    status = status_of(e)
    if status is not None:
        return status in TRANSIENT_STATUS
    if isinstance(e, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    try:
        import httpx
        if isinstance(e, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
            return True
    except ImportError:  # pragma: no cover
        pass
    text = f"{type(e).__name__} {e}".lower()
    return any(w in text for w in TRANSIENT_WORDS)


def retry_after(e: BaseException) -> float | None:
    v = getattr(e, "retry_after", None)
    if isinstance(v, (int, float)):
        return float(v)
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def call(fn: Callable[[], T], waits: tuple[float, ...], on_retry: OnRetry | None = None,
         sleep: Callable[[float], None] = time.sleep) -> T:
    """fn(), tried again after each wait in `waits` while it fails with a transient error."""
    for n, wait in enumerate((*waits, None), 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if wait is None or not transient(e):
                raise
            wait = min(max(wait, retry_after(e) or 0.0), MAX_WAIT)
            if on_retry:
                on_retry(n, e, wait)
            if wait > 0:
                sleep(wait)
    raise AssertionError("unreachable")  # pragma: no cover
