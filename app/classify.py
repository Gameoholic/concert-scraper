"""Claude reads posts and pages (text and flyer images) and pulls out shows and ticket rounds."""

from __future__ import annotations

import base64
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable, Literal, Mapping, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# Claude Haiku 5.5, prompts up to 100K tokens (https://claude.com/pricing, checked 2026-11-14).
PRICE_IN, PRICE_OUT = 0.10 / 1e6, 0.50 / 1e6
PRICE_IN_LONG, PRICE_OUT_LONG = 0.50 / 1e6, 2.50 / 1e6

MAX_ITEMS_PER_CALL = 12
MAX_IMAGES_PER_CALL = 16          # more than 20 images per request tightens the size limit
MAX_CHARS_PER_CALL = 180_000
MAX_IMAGE_BYTES = 4_500_000       # the API takes up to 5 MB per image; bigger ones are shrunk first
MAX_IMAGE_EDGE = 1568             # Claude scales larger images down to this long edge anyway
IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_TOKENS = 16000
LIMIT_KINDS = ("requests", "input-tokens", "output-tokens")


# ---------------------------------------------------------------- output schema

class Round(BaseModel):
    name: str = Field(description="Round name as written, e.g. 'FC先行', 'オフィシャル最速先行', 'e+ プレオーダー', '一般発売'")
    kind: Literal["fanclub", "official", "playguide", "general", "other"]
    lottery: bool = Field(description="true for 抽選 (lottery), false for 先着 (first come) or unknown")
    opens_at: Optional[str] = Field(description="Start, JST, 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD'; null if not stated")
    closes_at: Optional[str] = Field(description="Deadline, JST, 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD'; null if not stated")
    results_at: Optional[str] = Field(description="Lottery results announcement, same format; null if not stated")
    url: Optional[str] = Field(description="Where to apply or read about this round, if a link is given")
    price: Optional[str] = Field(description="Ticket price(s) if stated, short")
    notes: str = Field(description="Anything Daniel must know to apply (membership needed, limits), short; '' if none")


class Show(BaseModel):
    existing_show_id: Optional[int] = Field(description="Id of the known show this is, or null if it's new")
    title: str = Field(description="Show or tour name as announced (Japanese is fine)")
    date: Optional[str] = Field(description="Performance date 'YYYY-MM-DD'; null if TBA")
    time_note: str = Field(description="Doors/start times, e.g. '17:30 / 18:30'; '' if unknown")
    venue: str
    city: str = Field(description="City in English, e.g. 'Yokohama', 'Tokyo', 'Kobe'; '' if TBA")
    prefecture: str = Field(description="Prefecture in English, e.g. 'Kanagawa'; '' if unknown")
    in_japan: bool
    kind: Literal["solo", "tour", "festival", "event", "other"]
    cancelled: bool
    rounds: list[Round]
    items: list[int] = Field(description="Numbers of the items this show was read from")
    notes: str = Field(description="One short line of useful context (guests, 'Reol plays Day 2 only'); '' if none")


class ItemVerdict(BaseModel):
    item: int
    relevant: bool
    note: str = Field(description="What the item is and why it is or isn't relevant, in a few words, e.g. "
                                  "'FC lottery for Feb 4 Yokohama', 'new single, not a show', "
                                  "'tour dates in May: outside the window', 'show in Seoul: abroad'")


class Extraction(BaseModel):
    items: list[ItemVerdict]
    shows: list[Show]
    follow_urls: list[str] = Field(description="Up to 5 links found in the items that likely hold missing details "
                                               "(dates, venues, ticket rounds) of a relevant show; [] if none")


SYSTEM = """You read announcements from Japanese artists' official websites, X and Instagram, for a fan travelling \
in Japan. Your job is to find live shows and ticket rounds and return them as structured data.

What counts as a show:
- A live performance by the artist in Japan: one-man live, tour date, festival or event appearance (the artist \
must perform; a model walk, talk-only appearance, release, TV/radio, streaming or merch is not a show).
- It must be dated inside the watch window given below, or have its date not announced yet (TBA) in a way that \
could still fall inside the window (e.g. "2027年初春", "2027年開催決定" with no month). If a stated month or date is \
outside the window, leave it out. Shows abroad: leave out.
- A tour is one show per performance date. Return every date of the tour that is inside the window, each as its \
own show, with the tour's ticket rounds copied onto each date they apply to. Leave out tour dates outside the \
window.

Ticket rounds: FC先行 / ファンクラブ先行 (kind fanclub), オフィシャル先行 / HP先行 / 最速先行 by the artist or organiser \
(official), プレイガイド先行 / e+ / ぴあ / ローチケ presales (playguide), 一般発売 (general). Copy dates exactly; \
times are JST. When the year is not written, infer it from today's date (announcements are about the coming months). \
Include past rounds too if the page lists them: the app decides what is still open.

Matching:
- Known shows are listed with an id. If an item is about one of them (same artist, same date and place, or the \
same TBA event), set existing_show_id to that id instead of creating a duplicate. When a known show had no date \
and the item now gives one, still use its id.
- When a round is the same as a known round, use exactly the known round's name.

Items: for every item, say whether it is relevant (it announces or updates a show or ticket round as defined \
above) and what it is in a few words. Read flyer images: the dates and ticket details are often only in the image.
Return nothing you are not told by the items. Never invent dates, venues or links."""


@dataclass
class Item:
    number: int
    label: str                      # "X post by @foo, 2026-11-06 12:00 JST" / "Web page"
    url: str
    text: str
    media: list[str] = field(default_factory=list)


@dataclass
class KnownShow:
    id: int
    date: str | None
    venue: str
    city: str
    title: str
    rounds: list[str]


@dataclass
class CallResult:
    extraction: Extraction
    usd: float
    seconds: float = 0.0            # wall time of the call, image downloads included
    in_tokens: int = 0
    out_tokens: int = 0


class ClassifyError(Exception):
    pass


class RateLimited(ClassifyError):
    """429 (or 529 overloaded) even after the SDK's own retries: slow down and send the batch again."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ---------------------------------------------------------------- rate limits and concurrency

@dataclass
class RateLimits:
    """The account's per-minute Claude limits and what's left, from a response's anthropic-ratelimit-* headers."""
    limit: dict[str, int]
    remaining: dict[str, int]
    reset: dict[str, float]          # seconds until that limit is fully replenished

    def describe(self) -> str:
        return ", ".join(f"{self.limit[k]:,} {k.replace('-', ' ')}" for k in LIMIT_KINDS if k in self.limit) \
            + " per minute"


def parse_limits(headers: Mapping[str, str], at: datetime | None = None) -> RateLimits | None:
    at = at or datetime.now(timezone.utc)
    limit: dict[str, int] = {}
    remaining: dict[str, int] = {}
    reset: dict[str, float] = {}
    for k in LIMIT_KINDS:
        try:
            limit[k] = int(headers[f"anthropic-ratelimit-{k}-limit"])
            remaining[k] = int(headers[f"anthropic-ratelimit-{k}-remaining"])
        except (KeyError, ValueError, TypeError):
            limit.pop(k, None)
            continue
        try:
            when = datetime.fromisoformat(headers[f"anthropic-ratelimit-{k}-reset"].replace("Z", "+00:00"))
            reset[k] = max(0.0, (when - at).total_seconds())
        except (KeyError, ValueError, AttributeError):
            reset[k] = 60.0
    return RateLimits(limit, remaining, reset) if limit else None


class Throttle:
    """How many Claude calls may be in flight at once.

    Starts with 2 calls until the first answer brings the account's limits in its headers, then allows as many as
    fit under 80% of every per-minute limit (requests, input and output tokens), using this scan's own average
    call (tokens and seconds), never more than CLAUDE_CONCURRENCY. Holds new calls while the headers say a limit is
    nearly used up, halves on a 429/529 that outlived the SDK's retries, and grows back by one after a run of good
    calls. Only the scan's main thread uses it.
    """
    SAFETY = 0.8
    START = 2

    def __init__(self, cap: int, clock: Callable[[], float] = time.monotonic) -> None:
        self.cap = max(1, cap)
        self.limit = min(self.cap, self.START)
        self.ceiling = self.cap
        self.fitted = False
        self.clock = clock
        self.hold_until = 0.0
        self.why = ""
        self.calls, self.secs, self.tin, self.tout = 0, 0.0, 0, 0
        self.good = 0
        self.slowdowns = 0

    def allowed(self) -> int:
        return 0 if self.held() else self.limit

    def held(self) -> float:
        """Seconds until new calls may start again (0 = not held)."""
        return max(0.0, self.hold_until - self.clock())

    def hold(self, seconds: float, why: str) -> None:
        until = self.clock() + min(max(seconds, 1.0), 60.0)
        if until > self.hold_until:
            self.hold_until, self.why = until, why

    def done(self, res: CallResult, limits: RateLimits | None) -> None:
        self.calls += 1
        self.secs += res.seconds
        self.tin += res.in_tokens
        self.tout += res.out_tokens
        if limits:
            self.fit(limits)
            self.check(limits)
        elif not self.fitted:            # no headers (a stub, a proxy): trust the setting
            self.fitted, self.limit = True, self.cap
        self.good += 1
        if self.good >= 2 * self.limit and self.limit < self.ceiling:
            self.limit += 1
            self.good = 0

    def per_call(self) -> dict[str, float]:
        n = max(self.calls, 1)
        return {"requests": 1.0, "input-tokens": self.tin / n, "output-tokens": self.tout / n}

    def fit(self, limits: RateLimits) -> None:
        secs = max(self.secs / max(self.calls, 1), 1.0)
        per_call = self.per_call()
        slots = [limits.limit[k] * self.SAFETY / (per_call[k] * 60 / secs)
                 for k in limits.limit if per_call.get(k)]
        self.ceiling = max(1, min(self.cap, int(min(slots)))) if slots else self.cap
        if not self.fitted:
            self.fitted, self.limit = True, self.ceiling
        self.limit = min(self.limit, self.ceiling)

    def check(self, limits: RateLimits) -> None:
        per_call = self.per_call()
        for k, left in limits.remaining.items():
            if left < 2 * per_call.get(k, 0) + 1:
                self.hold(limits.reset.get(k, 10.0), f"{k.replace('-', ' ')} for this minute nearly used up")

    def rate_limited(self, retry_after: float | None) -> None:
        self.slowdowns += 1
        self.limit = max(1, self.limit // 2)
        self.good = 0
        self.hold(retry_after or 15.0, "Claude said slow down (rate limit)")


def known_block(shows: list[KnownShow]) -> str:
    if not shows:
        return "Known shows for this artist: none yet."
    lines = ["Known shows for this artist:"]
    for s in shows:
        rounds = "; ".join(s.rounds) or "none"
        lines.append(f"#{s.id} | {s.date or 'date TBA'} | {s.venue or '?'} ({s.city or '?'}) | {s.title} | rounds: {rounds}")
    return "\n".join(lines)


def batches(items: list[Item]) -> list[list[Item]]:
    out, cur, imgs, chars = [], [], 0, 0
    for it in items:
        n = len(it.media)
        if cur and (len(cur) >= MAX_ITEMS_PER_CALL or imgs + n > MAX_IMAGES_PER_CALL
                    or chars + len(it.text) > MAX_CHARS_PER_CALL):
            out.append(cur)
            cur, imgs, chars = [], 0, 0
        cur.append(it)
        imgs += n
        chars += len(it.text)
    if cur:
        out.append(cur)
    return out


def shrink_image(data: bytes) -> bytes | None:
    """A JPEG under MAX_IMAGE_BYTES, at most MAX_IMAGE_EDGE px on the long side. Claude scales images down to that
    size anyway, so the flyer text it reads is the same. None if the bytes aren't an image Pillow can open."""
    import io

    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.seek(0)                                      # first frame of an animated GIF
            im = im.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    im.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
    for quality in (90, 80, 70, 60, 50):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality, optimize=True)
        if buf.tell() <= MAX_IMAGE_BYTES:
            return buf.getvalue()
    return None


def download_image(url: str, client: httpx.Client, waits: tuple[float, ...] = (2.0,)) -> dict | None:
    from .retry import call

    def get() -> httpx.Response:
        r = client.get(url)
        if r.status_code in (429, 500, 502, 503, 504):
            r.raise_for_status()                    # an HTTPStatusError, tried again
        return r
    try:
        r = call(get, waits, lambda n, e, wait: log.info("image %s: %s; trying again", url[:80], e))
    except httpx.HTTPError as e:
        log.warning("image %s: %s", url[:80], e)
        return None
    ctype = r.headers.get("content-type", "").split(";")[0].strip()
    if r.status_code != 200 or ctype not in IMAGE_TYPES:
        log.warning("image %s skipped (%s, %s, %d bytes)", url[:80], r.status_code, ctype, len(r.content))
        return None
    data = r.content
    if len(data) > MAX_IMAGE_BYTES:
        small = shrink_image(data)
        if small is None:
            log.warning("image %s skipped: %d bytes and couldn't be shrunk", url[:80], len(data))
            return None
        log.info("image %s shrunk from %.1f MB to %.1f MB", url[:80], len(data) / 1e6, len(small) / 1e6)
        data, ctype = small, "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": ctype,
                                        "data": base64.standard_b64encode(data).decode()}}


class Classifier:
    """Thread-safe: the scan calls extract() from several worker threads at once."""

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic
        self.latest: RateLimits | None = None        # from the most recent API response
        self._lock = threading.Lock()
        self._local = threading.local()
        self._https: list[httpx.Client] = []
        http = anthropic.DefaultHttpxClient(event_hooks={"response": [self._seen]})
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=4, timeout=180, http_client=http)
        self.model = model

    def _seen(self, response) -> None:
        """Every Claude API response, retries included: keep the latest rate-limit headers."""
        limits = parse_limits(response.headers)
        if limits:
            with self._lock:
                self.latest = limits

    @property
    def http(self) -> httpx.Client:
        """One image-download client per worker thread."""
        client = getattr(self._local, "http", None)
        if client is None:
            client = httpx.Client(follow_redirects=True, timeout=30,
                                  headers={"User-Agent": "Mozilla/5.0 (concert-watcher)"})
            self._local.http = client
            with self._lock:
                self._https.append(client)
        return client

    def close(self) -> None:
        for client in self._https:
            client.close()
        self.client.close()

    def probe_limits(self) -> RateLimits | None:
        """One tiny call (a fraction of a cent) just to read the account's rate limits."""
        self.client.messages.create(model=self.model, max_tokens=1,
                                    messages=[{"role": "user", "content": "Say OK."}])
        return self.latest

    def content(self, artist: str, today: date, window: tuple[date, date], known: list[KnownShow],
                items: list[Item]) -> list[dict]:
        head = (f"Artist: {artist}\nToday: {today.isoformat()} ({today:%a}), Japan time.\n"
                f"Watch window: shows dated {window[0].isoformat()} to {window[1].isoformat()}, or TBA as described.\n\n"
                f"{known_block(known)}\n\nItems ({len(items)}):")
        blocks: list[dict] = [{"type": "text", "text": head}]
        for it in items:
            blocks.append({"type": "text", "text": f"[{it.number}] {it.label}\n{it.url}\n{it.text or '(no text)'}"})
            for url in it.media:
                img = download_image(url, self.http)
                if img:
                    blocks.append(img)
                    blocks.append({"type": "text", "text": f"(image above belongs to item [{it.number}])"})
        return blocks

    def extract(self, artist: str, today: date, window: tuple[date, date], known: list[KnownShow],
                items: list[Item]) -> CallResult:
        import anthropic
        started = time.monotonic()
        try:
            resp = self.client.messages.parse(
                model=self.model, max_tokens=MAX_TOKENS, system=SYSTEM,
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": self.content(artist, today, window, known, items)}],
                output_format=Extraction,
            )
        except anthropic.APIStatusError as e:
            if e.status_code in (429, 529):
                try:
                    retry_after = float(e.response.headers.get("retry-after") or 0) or None
                except ValueError:
                    retry_after = None
                raise RateLimited(f"Claude API {e.status_code}: {e.message}"[:300], retry_after) from e
            raise ClassifyError(f"Claude API {e.status_code}: {e.message}"[:300]) from e
        except anthropic.APIConnectionError as e:
            raise ClassifyError(f"Claude API unreachable: {e}"[:300]) from e
        u = resp.usage
        long = u.input_tokens + (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0) > 100_000
        usd = u.input_tokens * (PRICE_IN_LONG if long else PRICE_IN) + \
            u.output_tokens * (PRICE_OUT_LONG if long else PRICE_OUT)
        if resp.stop_reason == "refusal":
            raise ClassifyError("Claude declined to read these items")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise ClassifyError(f"Claude's answer was cut off or unreadable ({resp.stop_reason})")
        log.info("Claude %s: %d items, %d in / %d out tokens, $%.4f", artist, len(items), u.input_tokens,
                 u.output_tokens, usd)
        return CallResult(resp.parsed_output, usd, time.monotonic() - started, u.input_tokens, u.output_tokens)
