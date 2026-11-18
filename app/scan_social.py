"""X and Instagram through Apify actors, batched so one run covers many accounts.

The scan plans the Apify runs as jobs (x_jobs, ig_jobs) and runs several at once (run_job, in worker threads that
touch no database); it merges each job's result on its own main thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from . import retry

log = logging.getLogger(__name__)

X_ACTOR = "kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest"
IG_ACTOR = "apify/instagram-post-scraper"
# Each actor's default memory (from the Apify API, 2026-11-18); all runs at once must fit the plan's memory.
ACTOR_MEMORY_GB = {X_ACTOR: 0.25, IG_ACTOR: 1.0}
X_HANDLES_PER_QUERY = 8      # keeps each search query well under X's length limit
X_ITEMS_PER_HANDLE = 40      # cap for a 12-hour window
X_ITEMS_FIRST_SCAN = 300     # cap per account for the look-back scan
IG_POSTS_PER_PROFILE = 12
IG_POSTS_FIRST_SCAN = 50


@dataclass
class SocialPost:
    kind: str                 # x | instagram
    handle: str               # lowercase
    external_id: str
    url: str
    posted_at: str | None     # ISO, UTC
    text: str
    media: list[str] = field(default_factory=list)


@dataclass
class ApifyJob:
    """One Apify run: an X search for up to 8 accounts, or one Instagram run for many profiles."""
    kind: str                 # x | instagram
    batch: list[str]          # lowercase handles
    first: bool               # the look-back first scan
    actor: str
    run_input: dict
    max_items: int

    @property
    def name(self) -> str:
        return "X" if self.kind == "x" else "Instagram"


@dataclass
class JobResult:
    posts: list[SocialPost] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)   # handle -> message
    usd: float = 0.0
    items: int = 0
    empty: int = 0            # Instagram profiles with nothing new


def x_jobs(handles: dict[str, datetime], first_scan: set[str]) -> list[ApifyJob]:
    """handles: handle -> read posts newer than this. first_scan: handles getting the look-back."""
    jobs = []
    for batch in x_batches(handles, first_scan):
        since = min(handles[h] for h in batch)
        first = batch[0] in first_scan
        cap = (X_ITEMS_FIRST_SCAN if first else X_ITEMS_PER_HANDLE) * len(batch)
        jobs.append(ApifyJob("x", batch, first, X_ACTOR,
                             {"searchTerms": [x_query(batch, since)], "maxItems": cap, "queryType": "Latest"}, cap))
    return jobs


def ig_jobs(handles: dict[str, datetime], first_scan: set[str]) -> list[ApifyJob]:
    jobs = []
    for group in ig_batches(handles, first_scan):
        first = group[0] in first_scan
        since = min(handles[h] for h in group)
        limit = IG_POSTS_FIRST_SCAN if first else IG_POSTS_PER_PROFILE
        jobs.append(ApifyJob("instagram", group, first, IG_ACTOR,
                             {"username": group, "resultsLimit": limit, "skipPinnedPosts": not first,
                              "onlyPostsNewerThan": since.strftime("%Y-%m-%dT%H:%M:%S")}, limit * len(group)))
    return jobs


def run_job(apify: "Apify", job: ApifyJob, waits: tuple[float, ...],
            on_retry: Callable[[int, Exception, float], None] | None = None) -> JobResult:
    """Run one job, tried again on a timeout / 5xx / 429 / network error. Safe in a worker thread: it only calls
    Apify and parses. A failure is returned as errors for every account of the job, never raised."""
    try:
        items, usd = retry.call(lambda: apify.run(job.actor, job.run_input, job.max_items), waits, on_retry)
    except Exception as e:  # noqa: BLE001 - one job failing must not stop the others
        log.exception("%s batch %s failed", job.name, job.batch)
        err = f"Apify {job.name} scraper: {e}"[:300]
        return JobResult(errors={h: err for h in job.batch})
    return (parse_x_items if job.kind == "x" else parse_ig_items)(job, items, usd)


def x_batches(handles: Iterable[str], first_scan: set[str]) -> list[list[str]]:
    """How scan_x groups accounts into searches: one per account on its first scan, else 8 per search."""
    hs = list(handles)
    first = sorted(h for h in hs if h in first_scan)
    rest = sorted(h for h in hs if h not in first_scan)
    return [[h] for h in first] + list(chunks(rest, X_HANDLES_PER_QUERY))


def ig_batches(handles: Iterable[str], first_scan: set[str]) -> list[list[str]]:
    """How scan_instagram groups profiles: one run for first scans, one for the rest."""
    hs = list(handles)
    return [g for g in (sorted(h for h in hs if h in first_scan), sorted(h for h in hs if h not in first_scan)) if g]


class Apify:
    """Thin wrapper so tests can swap in a fake."""

    def __init__(self, token: str, max_usd_per_run: float) -> None:
        from apify_client import ApifyClient
        self.client = ApifyClient(token)
        self.max_usd = max_usd_per_run

    def limits(self) -> tuple[int | None, float | None]:
        """The account's (most actor runs at once, actor memory in GB): 5 and 16 GB on the free plan
        (apify.com/pricing, 2026-11-18). (None, None) if Apify won't say."""
        try:
            lim = self.client.user().limits().limits
            return int(lim.max_concurrent_actor_jobs), float(lim.max_actor_memory_gbytes)
        except Exception as e:  # noqa: BLE001
            log.info("Apify account limits not available (%s); using APIFY_CONCURRENCY as it is", e)
            return None, None

    def run(self, actor: str, run_input: dict, max_items: int) -> tuple[list[dict], float]:
        from decimal import Decimal
        run = self.client.actor(actor).call(run_input=run_input, max_items=max_items,
                                            max_total_charge_usd=Decimal(str(self.max_usd)), logger=None)
        if run is None:
            raise RuntimeError(f"{actor}: run did not finish")
        usd = float(run.usage_total_usd or 0) + float(getattr(run, "external_charged_total_usd", 0) or 0)
        events = getattr(run, "charged_event_counts", None)
        if events:
            log.info("%s: billed events %s", actor.split("/")[-1], events)
        status = str(getattr(run.status, "value", run.status))
        items = list(self.client.dataset(run.default_dataset_id).iterate_items())
        if status not in ("SUCCEEDED",) and not items:
            raise ApifyRunError(f"{actor}: run {status.lower()} {run.status_message or ''}".strip(),
                                transient=status.upper().replace("_", "-") == "TIMED-OUT")
        return items, usd


class ApifyRunError(RuntimeError):
    """An actor run that ended without results. A timed-out run is worth another try; a failed one isn't."""

    def __init__(self, message: str, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


# ---------------------------------------------------------------- X

def chunks(items: list, n: int) -> Iterable[list]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def x_query(handles: list[str], since: datetime) -> str:
    who = " OR ".join(f"from:{h}" for h in handles)
    return f"({who}) since_time:{int(since.timestamp())} -filter:replies"


def parse_x_time(value: Any) -> str | None:
    if not value:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(str(value).replace("Z", "+0000"), fmt).astimezone(timezone.utc) \
                .isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _x_media(tweet: dict) -> list[str]:
    urls = []
    for m in (tweet.get("extendedEntities") or {}).get("media") or []:
        u = m.get("media_url_https") or m.get("media_url")
        if u:
            urls.append(u)
    for m in tweet.get("media") or []:
        u = m if isinstance(m, str) else (m.get("media_url_https") or m.get("url") if isinstance(m, dict) else None)
        if u and u not in urls and "pbs.twimg.com" in u:
            urls.append(u)
    return urls


def _x_text(tweet: dict) -> str:
    text = tweet.get("text") or tweet.get("fullText") or tweet.get("full_text") or ""
    for u in ((tweet.get("entities") or {}).get("urls") or []):
        if u.get("url") and u.get("expanded_url"):
            text = text.replace(u["url"], u["expanded_url"])
    return text


def parse_tweet(item: dict, wanted: set[str]) -> SocialPost | None:
    author = ((item.get("author") or {}).get("userName") or "").lower()
    tid = str(item.get("id") or "")
    if not tid.isdigit() or author not in wanted:   # mock rows / other accounts
        return None
    text = _x_text(item)
    media = _x_media(item)
    rt = item.get("retweeted_tweet") or item.get("retweetedTweet")
    if isinstance(rt, dict) and rt.get("text"):
        rt_author = (rt.get("author") or {}).get("userName", "")
        text = f"RT @{rt_author}: {_x_text(rt)}"
        media = media or _x_media(rt)
    qt = item.get("quoted_tweet") or item.get("quotedTweet")
    if isinstance(qt, dict) and qt.get("text"):
        text += f"\n[Quoting @{(qt.get('author') or {}).get('userName', '')}]: {_x_text(qt)}"
        media = media or _x_media(qt)
    url = item.get("url") or item.get("twitterUrl") or f"https://x.com/{author}/status/{tid}"
    return SocialPost("x", author, tid, url.replace("twitter.com", "x.com"), parse_x_time(item.get("createdAt")),
                      text, media[:4])


def parse_x_items(job: ApifyJob, items: list[dict], usd: float) -> JobResult:
    wanted = {h.lower() for h in job.batch}
    seen, posts = set(), []
    for item in items:
        post = parse_tweet(item, wanted)
        if post and post.external_id not in seen:
            seen.add(post.external_id)
            posts.append(post)
    log.info("X %s: %d items, %d tweets, $%.4f", ",".join(job.batch), len(items), len(seen), usd)
    return JobResult(posts, {}, usd, len(items))


# ---------------------------------------------------------------- Instagram

def parse_ig(item: dict, wanted: set[str]) -> SocialPost | None:
    owner = (item.get("ownerUsername") or "").lower()
    code = item.get("shortCode") or item.get("id")
    if not code or owner not in wanted:
        return None
    media = [u for u in (item.get("images") or []) if isinstance(u, str)]
    for child in item.get("childPosts") or []:
        if isinstance(child, dict) and child.get("displayUrl") and child["displayUrl"] not in media:
            media.append(child["displayUrl"])
    if not media and item.get("displayUrl"):
        media = [item["displayUrl"]]
    ts = item.get("timestamp")
    posted = parse_x_time(ts) if ts else None
    url = item.get("url") or f"https://www.instagram.com/p/{code}/"
    return SocialPost("instagram", owner, str(code), url, posted, item.get("caption") or "", media[:4])


def _ig_error_handle(item: dict, wanted: set[str]) -> str | None:
    for key in ("username", "inputUrl", "url"):
        v = str(item.get(key) or "").lower().rstrip("/")
        h = v.rsplit("/", 1)[-1]
        if h in wanted:
            return h
    return None


def is_placeholder(item: dict) -> bool:
    """The row the actor writes for a profile with no posts newer than `onlyPostsNewerThan`. Apify bills it like a
    post (24 of them cost $0.059 on 2026-11-18). After a profile's first scan it means "nothing new"; on the first
    scan it means the account is private, empty or misspelled."""
    text = f"{item.get('error') or ''} {item.get('errorDescription') or ''}".lower()
    return "empty or private" in text or "no_items" in text


def parse_ig_items(job: ApifyJob, items: list[dict], usd: float) -> JobResult:
    res = JobResult(usd=usd, items=len(items))
    wanted = set(job.batch)
    for item in items:
        if item.get("error"):
            h = _ig_error_handle(item, wanted)
            if h and not job.first and is_placeholder(item):
                res.empty += 1                 # nothing new since the last scan: not an error
                continue
            msg = f"Instagram: {item.get('errorDescription') or item['error']}"[:300]
            if h and is_placeholder(item):
                msg = "Instagram returned no posts: the account is private, empty, or the handle is wrong"
            if h:
                res.errors[h] = msg
            continue
        post = parse_ig(item, wanted)
        if post:
            res.posts.append(post)
    log.info("Instagram %d profiles: %d items, $%.4f%s", len(job.batch), len(items), usd,
             f" ({res.empty} with nothing new; Apify bills a row for each)" if res.empty else "")
    return res
