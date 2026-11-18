"""Turn pasted links and @handles into normalized sources.

Pure functions, no database: `parse_paste` takes the pasted text plus the
artist's existing (kind, url) pairs and says what each piece would become.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Kinds, in display order. Adding one is a code change, not a migration.
KINDS = {
    "instagram": "Instagram",
    "x": "X",
    "website": "Website",
    "fanclub": "Fan club",
    "other": "Other",
}
SOCIAL_KINDS = ("instagram", "x")          # identified by a handle
PAGE_KINDS = ("website", "fanclub", "other")  # identified by a URL
COVERAGE_KINDS = ("instagram", "x", "website")
SHORT = {"instagram": "IG", "x": "X", "website": "Site", "fanclub": "FC", "other": "Other"}

IG_HANDLE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
X_HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")

IG_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
X_HOSTS = {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}
IG_NOT_ACCOUNTS = {"p", "reel", "reels", "tv", "explore", "accounts", "direct", "about", "legal", "developer"}
X_NOT_ACCOUNTS = {"i", "home", "explore", "search", "intent", "share", "hashtag", "settings", "messages",
                  "notifications", "compose", "login", "signup", "tos", "privacy"}
TRACKING_PARAMS = {"fbclid", "gclid", "igshid", "igsh", "si", "s", "t", "ref", "ref_src", "mibextid"}
SCHEDULE_WORDS = re.compile(r"(live|schedule|tour|ticket|concert)", re.I)


@dataclass
class Candidate:
    raw: str
    status: str                 # new | duplicate | invalid | ambiguous
    kind: str | None = None     # None while ambiguous
    handle: str | None = None
    url: str | None = None
    label: str = ""
    message: str = ""
    choices: tuple[str, ...] = field(default_factory=tuple)  # kinds the user may switch between

    @property
    def addable(self) -> bool:
        return self.status in {"new", "ambiguous"}

    @property
    def display(self) -> str:
        return f"@{self.handle}" if self.handle else (display_url(self.url) if self.url else self.raw)


def social_url(kind: str, handle: str) -> str:
    h = handle.lower()
    return f"https://www.instagram.com/{h}/" if kind == "instagram" else f"https://x.com/{h}"


def display_url(url: str) -> str:
    """https://www.Example.jp/live/ -> example.jp/live (for showing, not storing)."""
    parts = urlsplit(url)
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    rest = parts.path.rstrip("/")
    if parts.query:
        rest += "?" + parts.query
    return host + rest


def normalize_page_url(text: str) -> str | None:
    if not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or "." not in parts.hostname:
        return None
    host = parts.hostname.lower()
    if parts.port:
        host += f":{parts.port}"
    path = parts.path or "/"     # a trailing slash is kept: some servers (Sony's) answer 404 without it
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS])
    return urlunsplit((parts.scheme.lower(), host, path, query, ""))


def looks_like_domain(token: str) -> bool:
    if "/" in token or "://" in token:
        return True
    if "." not in token:
        return False
    tld = token.rsplit(".", 1)[1]
    return 2 <= len(tld) <= 6 and tld.isalpha()


def split_paste(text: str) -> list[str]:
    tokens = re.split(r"[\s,、，]+", text or "")
    return [t.strip().strip("<>\"'()[]「」") for t in tokens if t.strip().strip("<>\"'()[]「」")]


def classify(token: str) -> Candidate:
    """What one pasted piece is, ignoring duplicates."""
    if token.startswith("@") or not looks_like_domain(token):
        handle = token.lstrip("@")
        ig_ok, x_ok = bool(IG_HANDLE.match(handle)), bool(X_HANDLE.match(handle))
        if ig_ok and x_ok:
            return Candidate(token, "ambiguous", handle=handle, choices=SOCIAL_KINDS,
                             message="Bare handle: Instagram or X?")
        if ig_ok:
            return Candidate(token, "new", "instagram", handle, social_url("instagram", handle),
                             message="Has a dot or is long, so it can only be Instagram")
        return Candidate(token, "invalid", message="Not a link or a valid handle")

    parts = urlsplit(token if "://" in token else "https://" + token)
    host = (parts.hostname or "").lower()
    segments = [s for s in parts.path.split("/") if s]

    if host in IG_HOSTS:
        if not segments:
            return Candidate(token, "invalid", message="Instagram link without an account")
        first = segments[0].lower()
        if first == "stories":
            if len(segments) < 2:
                return Candidate(token, "invalid", message="Instagram link without an account")
            handle = segments[1]
        elif first in IG_NOT_ACCOUNTS:
            return Candidate(token, "invalid", message="A post link, not an account")
        else:
            handle = segments[0]
        if not IG_HANDLE.match(handle):
            return Candidate(token, "invalid", message="Not a valid Instagram handle")
        return Candidate(token, "new", "instagram", handle.lower(), social_url("instagram", handle),
                         message="From an instagram.com link")

    if host in X_HOSTS:
        if not segments:
            return Candidate(token, "invalid", message="X link without an account")
        handle = segments[0]
        if handle.lower() in X_NOT_ACCOUNTS:
            return Candidate(token, "invalid", message="Not an account link")
        if not X_HANDLE.match(handle):
            return Candidate(token, "invalid", message="Not a valid X handle")
        return Candidate(token, "new", "x", handle, social_url("x", handle),
                         message="From an x.com link" if "x.com" in host else "From a twitter.com link")

    url = normalize_page_url(token)
    if not url:
        return Candidate(token, "invalid", message="Not a link or a valid handle")
    path = urlsplit(url).path
    label = "Schedule" if SCHEDULE_WORDS.search(path) else ""
    return Candidate(token, "new", "website", url=url, label=label, choices=PAGE_KINDS,
                     message="Website page")


def same_key(kind: str, url: str) -> tuple[str, str]:
    """For duplicate checks: a page with or without its trailing slash is the same page."""
    if kind in PAGE_KINDS:
        parts = urlsplit(url)
        if len(parts.path) > 1:
            url = urlunsplit(parts._replace(path=parts.path.rstrip("/")))
    return kind, url


def parse_paste(text: str, existing: set[tuple[str, str]] = frozenset()) -> list[Candidate]:
    """Classify every pasted piece and mark duplicates (on the artist or within the paste).

    `existing` holds (kind, url) pairs. Duplicates of an ambiguous handle are checked
    once a kind is chosen (see `resolve`).
    """
    out: list[Candidate] = []
    existing = {same_key(k, u) for k, u in existing}
    seen = set(existing)
    for token in split_paste(text):
        c = classify(token)
        if c.status == "new":
            key = same_key(c.kind, c.url)
            if key in seen or (c.kind in PAGE_KINDS and any((k, key[1]) in seen for k in PAGE_KINDS)):
                c.status = "duplicate"
                c.message = "Already added" if any((k, key[1]) in existing for k in PAGE_KINDS + (c.kind,)) \
                    else "Pasted twice"
            seen.add(key)
        elif c.status == "ambiguous":
            if all((k, social_url(k, c.handle)) in seen for k in SOCIAL_KINDS):
                c.status, c.message = "duplicate", "Already added as Instagram and X"
        out.append(c)
    return out


def resolve(candidates: list[Candidate], picks: dict[int, str],
            existing: set[tuple[str, str]] = frozenset()) -> tuple[list[Candidate], list[int]]:
    """Apply the user's per-row kind picks. Returns (sources to add, indexes still needing a pick)."""
    ready: list[Candidate] = []
    unresolved: list[int] = []
    seen = {same_key(k, u) for k, u in existing}
    for i, c in enumerate(candidates):
        if not c.addable:
            continue
        pick = picks.get(i)
        if pick not in c.choices:
            pick = None
        if c.status == "ambiguous":
            if not pick:
                unresolved.append(i)
                continue
            c = Candidate(c.raw, "new", pick, c.handle, social_url(pick, c.handle), choices=c.choices)
        elif pick and pick != c.kind:
            if c.kind in SOCIAL_KINDS:
                c = Candidate(c.raw, "new", pick, c.handle, social_url(pick, c.handle), choices=c.choices)
            else:
                c = Candidate(c.raw, "new", pick, url=c.url, label=c.label, choices=c.choices)
        if same_key(c.kind, c.url) in seen:
            continue
        seen.add(same_key(c.kind, c.url))
        ready.append(c)
    return ready, unresolved
