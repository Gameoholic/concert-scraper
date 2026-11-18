"""Scan every watched artist's sources, find shows with Claude, store them, send alerts.

    python -m app.watch                 # what cron runs
    python -m app.watch --dry-run       # fetch + classify, print alerts; changes nothing, sends nothing
    python -m app.watch --artist 12     # one artist (repeatable)

One source failing never stops the others; failures are logged and shown in the UI.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import queue
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import alerts, db, fit, retry, store
from . import progress as prog
from .classify import Classifier, ClassifyError, Item, RateLimited, Throttle, batches
from .config import Settings
from .progress import StepSpec
from .scan_social import (ACTOR_MEMORY_GB, Apify, ApifyJob, JobResult, SocialPost, ig_batches, ig_jobs, run_job,
                          x_batches, x_jobs)
from .scan_web import Browser, FetchError, Page, fetch_page, same_site

log = logging.getLogger("watch")

OVERLAP = timedelta(hours=1)          # re-read a little before the last scan so nothing slips between runs
MAX_PAGES_PER_SOURCE = 12             # tracked pages re-read on each scan
MAX_FOLLOW_PER_ARTIST = 8             # detail pages Claude asked to read, per artist per scan
CLASSIFY_CHUNK = 60
MAX_RATE_RETRIES = 6                  # times one batch is sent again after Claude said "slow down"
SOCIAL_HOSTS = ("x.com", "twitter.com", "instagram.com", "t.co", "youtube.com", "youtu.be", "tiktok.com",
                "facebook.com", "line.me", "lin.ee", "spotify.com", "apple.com")
WEB_KINDS = ("website", "fanclub")


@dataclass
class SourceStat:
    posts: int = 0
    error: str | None = None
    scanned: bool = False


@dataclass
class Report:
    posts_read: int = 0
    shows_new: int = 0
    events: list[store.Event] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    apify_usd: float = 0.0
    claude_usd: float = 0.0
    messages: list[alerts.Message] = field(default_factory=list)
    alerts_sent: int = 0


@dataclass
class Job:
    """One batch of an artist's posts for one Claude call."""
    artist: db.Artist
    batch: list[Item]
    by_number: dict[int, sqlite3.Row]
    tries: int = 0                    # times Claude said "slow down" for this batch


@dataclass
class ArtistWork:
    artist: db.Artist
    total: int = 0
    read: int = 0
    queue: deque[Job] = field(default_factory=deque)
    in_flight: int = 0
    seen: set[int] = field(default_factory=set)            # post ids queued this scan
    follow: list[tuple[str, int]] = field(default_factory=list)
    round: int = 0                                          # 1 = reading the detail pages Claude asked for
    started: bool = False
    finished: bool = False


def flush_log() -> None:
    """Write log lines from the Claude worker threads into the run's log."""
    for h in logging.getLogger().handlers:
        if isinstance(h, prog.RunLogHandler):
            h.flush()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class Scan:
    def __init__(self, settings: Settings, conn: sqlite3.Connection, run_id: int, *, apify=None, classifier=None,
                 fetch: Callable[..., Page] = fetch_page, browser=None, at: datetime | None = None,
                 alert_client=None, alert_pause: float = 0.6, retry_waits: tuple[float, ...] | None = None,
                 notices: bool = False) -> None:
        """notices: post the silent "scan started" Discord notice (the CLI does, except for --dry-run)."""
        self.s, self.conn, self.run_id = settings, conn, run_id
        self.notices = notices
        self.waits = retry_waits if retry_waits is not None else retry.delays(settings.retries)
        self.apify = apify
        self.classifier = classifier
        self.fetch = fetch
        self.browser = browser
        self.alert_client, self.alert_pause = alert_client, alert_pause
        self.started = at or utcnow()
        self.today = self.started.astimezone(store.JST).date()
        self.stats: dict[int, SourceStat] = {}
        self.report = Report()
        self.fetched: set[tuple[int, str]] = set()
        self.classify_errors: dict[int, str] = {}
        segments = store.itinerary(conn)
        self.window = fit.watch_window(segments)
        self.ig_hours = store.prefs(conn, settings).instagram_cooldown_hours   # the Scans page's Schedule
        self.progress = prog.Progress(conn, run_id)
        self.classified = 0          # posts Claude has read this scan, across artists
        self.to_classify = 0         # posts waiting for Claude this scan, across artists
        self.limits_logged = False
        self.one_artist = False      # a "Scan now" for one artist reads everything

    # ------------------------------------------------------------ helpers

    def stat(self, source_id: int) -> SourceStat:
        return self.stats.setdefault(source_id, SourceStat())

    def fail(self, source: db.Source, message: str) -> None:
        st = self.stat(source.id)
        st.scanned, st.error = True, message
        self.report.errors.append(f"{source.display}: {message}")

    def add_post(self, source_id: int, external_id: str, url: str, posted_at: str | None, text: str,
                 media: list[str]) -> bool:
        with self.conn:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO posts (source_id, external_id, url, posted_at, text, media, read_at, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (source_id, external_id, url, posted_at, text, store.dumps(media), db.now(), self.run_id))
        if cur.rowcount:
            self.stat(source_id).posts += 1
            self.report.posts_read += 1
        return bool(cur.rowcount)

    def now(self, text: str) -> None:
        """Log it and show it as the scan's "now" line (also the run's heartbeat)."""
        log.info(text)
        self.progress.now(text)

    beat = now

    def costs(self) -> None:
        self.progress.costs(self.report.apify_usd, self.report.claude_usd)

    def retried(self, keys: list[str], what: str, n: int, e: Exception, wait: float) -> None:
        """Log and show a retry: counted on the step(s) and said in the "now" line."""
        why = f"{type(e).__name__}: {e}" if str(e) and not str(e).startswith(type(e).__name__) else str(e)
        why = (why or type(e).__name__).splitlines()[0][:160]
        log.warning("%s: %s. Trying again in %.0f s (retry %d of %d)", what, why, wait, n, len(self.waits))
        for k in keys:
            self.progress.add_to(k, retries=1)
            self.progress.update(k, detail=f"retry {n} of {len(self.waits)} in {wait:.0f} s: {why}")
        self.progress.now(f"{what}: {why}; trying again in {wait:.0f} s (retry {n} of {len(self.waits)})")

    def pending_count(self, artist_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM posts p JOIN sources s ON s.id = p.source_id "
                                 "WHERE s.artist_id = ? AND p.relevant IS NULL", (artist_id,)).fetchone()[0]

    # ------------------------------------------------------------ plan

    def social_targets(self, sources: list[db.Source]) -> tuple[dict[str, datetime], set[str], dict[str, list[db.Source]]]:
        """handle -> read since, handles on their first (look-back) scan, handle -> its sources."""
        handles: dict[str, datetime] = {}
        first: set[str] = set()
        by_handle: dict[str, list[db.Source]] = {}
        for src in sources:
            h = src.handle.lower()
            by_handle.setdefault(h, []).append(src)
            cursor = parse_iso(src.cursor)
            if cursor:
                since = cursor - OVERLAP
            else:
                since = self.started - timedelta(days=self.s.first_scan_days)
                first.add(h)
            handles[h] = min(handles.get(h, since), since)
        first = {h for h in first if all(not s.cursor for s in by_handle[h])}
        return handles, first, by_handle

    def ig_due(self, src: db.Source) -> bool:
        """Instagram is read on every scan, like X, unless the Instagram cooldown (Scans page → Schedule, default 0)
        says it was read less than N hours ago (Apify bills a row even for a profile with nothing new). First scans
        and one-artist scans always read it."""
        if src.kind != "instagram" or self.one_artist:
            return True
        return store.ig_wait_hours(src.cursor, self.ig_hours, self.started) is None

    def social_sources(self, sources: list[db.Source], kind: str) -> list[db.Source]:
        return [s for s in sources if s.kind == kind and s.handle and self.ig_due(s)]

    def plan(self, artists: list[db.Artist], sources: list[db.Source]) -> None:
        """Every step of the scan, in the order it will run, so the page can show what's still to do."""
        names = {a.id: a.name for a in artists}
        specs: list[StepSpec] = []
        for kind, batcher in (("x", x_batches), ("instagram", ig_batches)):
            srcs = self.social_sources(sources, kind)
            handles, first, by_handle = self.social_targets(srcs)
            for i, batch in enumerate(batcher(handles, first)):
                grp = f"{kind}:{i}" if len(batch) > 1 else ""
                for h in batch:
                    for src in by_handle[h]:
                        specs.append(StepSpec(f"src:{src.id}", "read", kind, f"@{src.handle}", names[src.artist_id],
                                              src.artist_id, grp))
        for src in sources:
            if src.kind == "instagram" and src.handle and not self.ig_due(src):
                specs.append(StepSpec(f"src:{src.id}", "read", "instagram", f"@{src.handle}", names[src.artist_id],
                                      src.artist_id))
        for src in sources:
            if src.kind in WEB_KINDS:
                specs.append(StepSpec(f"src:{src.id}", "read", "website", src.display, names[src.artist_id],
                                      src.artist_id))
        for a in artists:
            specs.append(StepSpec(f"claude:{a.id}", "claude", "claude", a.name, "", a.id))
        specs.append(StepSpec("alerts", "alerts", "alerts", "Discord alerts"))
        self.progress.plan(specs)

    # ------------------------------------------------------------ run

    def run(self, artist_ids: list[int] | None = None, done_before: set[str] | None = None,
            resumed_from: int | None = None) -> Report:
        """done_before: step keys a stopped scan already finished (a "Resume"); those sources aren't read again."""
        artists = [a for a in db.list_artists(self.conn)
                   if (a.id in artist_ids if artist_ids else a.enabled)]
        everything = [src for a in artists for src in a.sources if src.scannable]
        done_before = done_before or set()
        sources = [s for s in everything if f"src:{s.id}" not in done_before]
        self.one_artist = bool(artist_ids) and not resumed_from
        self.plan(artists, sources)
        if done_before:
            names = {a.id: a.name for a in artists}
            for s in everything:
                if f"src:{s.id}" in done_before:
                    kind = s.kind if s.kind in ("x", "instagram") else "website"
                    label = f"@{s.handle}" if kind != "website" else s.display
                    self.progress.add(StepSpec(f"src:{s.id}", "read", kind, label, names[s.artist_id], s.artist_id))
                    self.progress.finish(f"src:{s.id}", "skipped", detail=f"read by the scan this resumes (#{resumed_from})")
        self.now(f"Resuming scan #{resumed_from}: {len(sources)} of {len(everything)} source(s) left to read"
                 if resumed_from else f"Starting: {len(artists)} artist(s), {len(sources)} source(s)")
        if self.notices:
            alerts.notify(self.s, alerts.started_text(self.conn, self.run_id, sources, self.s.app_url),
                          self.alert_client)
        for src in sources:
            if src.kind == "instagram" and src.handle and not self.ig_due(src):
                ago = (self.started - parse_iso(src.cursor)).total_seconds() / 3600
                self.progress.finish(f"src:{src.id}", "skipped",
                                     detail=f"read {ago:.0f} h ago; Instagram cooldown is {self.ig_hours} h")
                with self.conn:      # its last error (e.g. an old "Empty or private data") isn't current any more
                    self.conn.execute("UPDATE sources SET last_status = NULL, last_error = NULL "
                                      "WHERE id = ? AND last_status = 'error'", (src.id,))
        social = [s for kind in ("x", "instagram") for s in self.social_sources(sources, kind)]
        try:
            self.social_begin(social)       # Apify runs go on in worker threads while the websites are read here
            web = [s for s in sources if s.kind in WEB_KINDS]
            for i, src in enumerate(web, 1):
                self.social_pump()
                try:
                    self.scan_website(src, i, len(web))
                except Exception as e:  # noqa: BLE001
                    log.exception("website %s failed", src.url)
                    self.fail(src, f"{type(e).__name__}: {e}"[:300])
                    self.close_steps([f"src:{src.id}"], f"{type(e).__name__}: {e}")
            self.social_pump(until_done=True)
        except Exception as e:  # noqa: BLE001
            log.exception("reading X / Instagram failed")
            self.report.errors.append(f"X / Instagram: {e}")
            self.close_steps([f"src:{s.id}" for s in social], f"{type(e).__name__}: {e}")
        finally:
            self.social_end()
        self.classify_all(artists)
        self.save_source_status(sources)
        self.report.shows_new = sum(1 for e in self.report.events if e.kind == "new_show")
        self.report.messages = alerts.build(self.conn, self.s.app_url, self.started.astimezone(store.JST))
        return self.report

    def close_steps(self, keys: list[str], error: str) -> None:
        """Mark steps that never finished because their whole phase broke."""
        for k in keys:
            row = self.conn.execute("SELECT status FROM run_steps WHERE run_id = ? AND key = ?",
                                    (self.run_id, k)).fetchone()
            if row and row[0] in ("pending", "running"):
                self.progress.finish(k, "failed", error=error)

    # ------------------------------------------------------------ social

    def apify_slots(self) -> int:
        """APIFY_CONCURRENCY, kept within the account's limits: runs at once, and memory (the biggest actor's
        default memory per run). The free plan allows 5 runs and 16 GB."""
        want = self.s.apify_concurrency
        runs, memory = self.apify.limits() if hasattr(self.apify, "limits") else (None, None)
        n = want
        if runs:
            n = min(n, runs)
        if memory:
            n = min(n, max(1, int(memory // max(ACTOR_MEMORY_GB.values()))))
        log.info("Apify: up to %d run(s) at once (APIFY_CONCURRENCY=%d%s)", n, want,
                 f"; the account allows {runs} runs and {memory:g} GB at once" if runs and memory else "")
        return max(1, n)

    @staticmethod
    def who(batch: list[str]) -> str:
        return ", ".join(f"@{h}" for h in batch[:3]) + (f" +{len(batch) - 3} more" if len(batch) > 3 else "")

    def social_begin(self, sources: list[db.Source]) -> None:
        """Plan the Apify runs and start the first ones in worker threads. Instagram goes first: its one run for
        all profiles is the longest."""
        self.jobs: deque[ApifyJob] = deque()
        self.flying: dict[Future, ApifyJob] = {}
        self.owners: dict[tuple[str, str], list[db.Source]] = {}
        self.retry_events: queue.SimpleQueue = queue.SimpleQueue()
        self.pool: ThreadPoolExecutor | None = None
        self.accounts_total = self.accounts_done = 0
        self.social_line = ""
        if not sources:
            return
        if self.apify is None:
            for src in sources:
                self.fail(src, "Not scanned: APIFY_API_TOKEN is not set in .env")
                self.progress.finish(f"src:{src.id}", "failed", error="APIFY_API_TOKEN is not set in .env")
            return
        for kind, make in (("instagram", ig_jobs), ("x", x_jobs)):
            handles, first, by_handle = self.social_targets([s for s in sources if s.kind == kind])
            for h, srcs in by_handle.items():
                self.owners[(kind, h)] = srcs
            self.jobs.extend(make(handles, first) if handles else [])
            self.accounts_total += len(handles)
        self.slots = self.apify_slots()
        self.pool = ThreadPoolExecutor(max_workers=self.slots, thread_name_prefix="apify")
        log.info("Apify: %d account(s) in %d run(s)", self.accounts_total, len(self.jobs))
        self.social_pump()

    def social_pump(self, until_done: bool = False) -> None:
        """On the scan's own thread: merge Apify runs that finished, show retries, start queued runs.
        until_done: keep at it until every run is back."""
        if self.pool is None:
            return
        last_beat = time.monotonic()
        while True:
            flush_log()
            while not self.retry_events.empty():
                job, n, e, wait_s = self.retry_events.get()
                self.retried([f"src:{s.id}" for h in job.batch for s in self.owners[(job.kind, h)]],
                             f"Apify {job.name} {self.who(job.batch)}", n, e, wait_s)
            for fut in [f for f in self.flying if f.done()]:
                self.social_done(self.flying.pop(fut), fut.result())
            while self.jobs and len(self.flying) < self.slots:
                job = self.jobs.popleft()
                self.social_start(job)
                self.flying[self.pool.submit(run_job, self.apify, job, self.waits,
                                             lambda n, e, w, job=job: self.retry_events.put((job, n, e, w)))] = job
            if not until_done or not (self.flying or self.jobs):
                return
            line = self.social_now()
            if line != self.social_line:
                self.now(line)
                self.social_line, last_beat = line, time.monotonic()
            elif time.monotonic() - last_beat > 60:
                store.heartbeat(self.conn, self.run_id)
                last_beat = time.monotonic()
            wait(self.flying, timeout=1.0, return_when=FIRST_COMPLETED)

    def social_end(self) -> None:
        if getattr(self, "pool", None) is not None:
            self.pool.shutdown(wait=True, cancel_futures=True)
            self.pool = None
            flush_log()

    def social_now(self) -> str:
        jobs = list(self.flying.values())
        parts = []
        for name in ("Instagram", "X"):
            batch = [h for j in jobs if j.name == name for h in j.batch]
            if batch:
                parts.append(f"{name} {self.who(batch)}")
        n = len(jobs)
        return (f"Apify: {n} run{'s' if n != 1 else ''} at once ({'; '.join(parts)}) · "
                f"{self.accounts_done} of {self.accounts_total} accounts read"
                + (f" · {len(self.jobs)} run{'s' if len(self.jobs) != 1 else ''} queued" if self.jobs else ""))

    def social_start(self, job: ApifyJob) -> None:
        for h in job.batch:
            for src in self.owners[(job.kind, h)]:
                self.progress.start(f"src:{src.id}", detail=f"first scan: reading {self.s.first_scan_days} days back"
                                    if job.first else "reading new posts")
        log.info("Apify: starting %s %s", job.name, self.who(job.batch))

    def social_done(self, job: ApifyJob, res: JobResult) -> None:
        """Store what one Apify run read (on this thread), and finish its accounts' steps."""
        self.accounts_done += len(job.batch)
        self.report.apify_usd += res.usd
        self.costs()
        per_handle: dict[str, list[SocialPost]] = {}
        for post in res.posts:
            per_handle.setdefault(post.handle, []).append(post)
        n_src = sum(len(self.owners[(job.kind, h)]) for h in job.batch) or 1
        for h in job.batch:
            got = per_handle.get(h, [])
            for src in self.owners[(job.kind, h)]:
                new = sum(self.add_post(src.id, p.external_id, p.url, p.posted_at, p.text, p.media) for p in got)
                key = f"src:{src.id}"
                if h in res.errors:
                    self.fail(src, res.errors[h])
                    self.progress.finish(key, "failed", error=res.errors[h], posts=new, usd=res.usd / n_src)
                    continue
                self.stat(src.id).scanned = True
                with self.conn:
                    self.conn.execute(
                        "UPDATE sources SET cursor = ?, first_scan_at = COALESCE(first_scan_at, ?) WHERE id = ?",
                        (self.started.isoformat(timespec="seconds"), db.now(), src.id))
                self.progress.finish(key, "done", posts=new, usd=res.usd / n_src,
                                     detail=f"{len(got)} fetched, {new} new" if got or job.kind == "x"
                                     else "nothing new")

    # ------------------------------------------------------------ websites

    def scan_website(self, src: db.Source, index: int = 1, count: int = 1) -> None:
        key = f"src:{src.id}"
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO pages (source_id, url, first_seen_at) VALUES (?, ?, ?)",
                              (src.id, src.url, db.now()))
        pages = self.conn.execute(
            "SELECT * FROM pages WHERE source_id = ? AND tracked = 1 "
            "ORDER BY url = ? DESC, COALESCE(last_changed_at, first_seen_at) DESC LIMIT ?",
            (src.id, src.url, MAX_PAGES_PER_SOURCE)).fetchall()
        self.progress.start(key, total=len(pages), detail=f"{len(pages)} page(s) to check")
        root_error = None
        before = self.stat(src.id).posts
        for i, page in enumerate(pages, 1):
            self.now(f"Website {index} of {count}: {src.display}"
                     + (f" (page {i} of {len(pages)})" if len(pages) > 1 else ""))
            err = self.read_page(src.id, page["url"], bool(page["rendered"]), key)
            if err and page["url"] == src.url:
                root_error = err
                self.fail(src, err)
            self.progress.update(key, done=i)
        new = self.stat(src.id).posts - before
        self.stat(src.id).scanned = True
        with self.conn:
            self.conn.execute("UPDATE sources SET first_scan_at = COALESCE(first_scan_at, ?) WHERE id = ?",
                              (db.now(), src.id))
        detail = f"{len(pages)} page(s) checked, " + (f"{new} changed" if new else "unchanged")
        self.progress.finish(key, "failed" if root_error else "done", error=root_error or "", posts=new, detail=detail)

    def read_page(self, source_id: int, url: str, rendered: bool, key: str = "") -> str | None:
        """Fetch one page (tried again on a timeout, 5xx, 429 or DNS error); store a new version as a post if its
        text changed. Returns an error or None. `key`: the progress step the retries are counted on."""
        if (source_id, url) in self.fetched:
            return None
        self.fetched.add((source_id, url))
        first = self.conn.execute("SELECT content_hash IS NULL FROM pages WHERE source_id = ? AND url = ?",
                                  (source_id, url)).fetchone()
        try:
            page = retry.call(
                lambda: self.fetch(url, rendered_before=rendered, browser=self.browser, probe=bool(first and first[0])),
                self.waits, lambda n, e, wait: self.retried([key] if key else [], urlsplit(url).hostname or url,
                                                            n, e, wait))
        except FetchError as e:
            log.warning("%s: %s", url, e)
            with self.conn:
                self.conn.execute("UPDATE pages SET last_error = ?, last_fetched_at = ? WHERE source_id = ? AND url = ?",
                                  (str(e), db.now(), source_id, url))
            return str(e)
        row = self.conn.execute("SELECT content_hash FROM pages WHERE source_id = ? AND url = ?",
                                (source_id, url)).fetchone()
        changed = row is None or row["content_hash"] != page.hash
        with self.conn:
            self.conn.execute(
                "UPDATE pages SET content_hash = ?, rendered = ?, last_fetched_at = ?, last_error = NULL, "
                "last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END WHERE source_id = ? AND url = ?",
                (page.hash, int(page.rendered), db.now(), int(changed), db.now(), source_id, url))
        if changed:
            self.add_post(source_id, f"{url}#{page.hash[:16]}", url, None, page.text, [])
        return None

    # ------------------------------------------------------------ classification

    def classify_all(self, artists: list[db.Artist]) -> None:
        waiting = {a.id: self.pending_count(a.id) for a in artists}
        self.to_classify = sum(waiting.values())
        for a in artists:
            self.progress.update(f"claude:{a.id}", total=waiting[a.id])
        if self.to_classify:
            log.info("Claude: %d post(s) / page version(s) to read for %d artist(s)", self.to_classify,
                     sum(1 for n in waiting.values() if n))
        todo = []
        for a in artists:
            key = f"claude:{a.id}"
            if not waiting[a.id]:
                self.progress.finish(key, "skipped", detail="nothing new to read")
                continue
            if self.classifier is None:
                self.report.errors.append(f"{a.name}: {waiting[a.id]} post(s) waiting; ANTHROPIC_API_KEY is not set")
                self.classify_errors[a.id] = "ANTHROPIC_API_KEY is not set"
                self.progress.finish(key, "failed", error="ANTHROPIC_API_KEY is not set in .env")
                continue
            self.progress.update(key, detail=f"{waiting[a.id]} to read")
            todo.append(ArtistWork(a, total=waiting[a.id]))
        if todo:
            self.classify_parallel(todo)

    def classify_parallel(self, works: list[ArtistWork]) -> None:
        """Every artist's batches go through one pool of worker threads that only call Claude. Everything that
        touches the database (known shows before a call, merging after it, progress) stays on this thread."""
        by_artist = {w.artist.id: w for w in works}
        throttle = Throttle(self.s.claude_concurrency)
        flying: dict[Future, Job] = {}
        last_line, last_beat = "", time.monotonic()
        log.info("Claude: up to %d call(s) at once (CLAUDE_CONCURRENCY), fitted to the account's rate limits",
                 throttle.cap)
        for w in works:
            self.queue_unread(w)
            if not w.queue:
                self.round_done(w)
        pool = ThreadPoolExecutor(max_workers=throttle.cap, thread_name_prefix="claude")
        try:
            while True:
                while len(flying) < throttle.allowed():
                    job = self.next_job(works)
                    if job is None:
                        break
                    self.submit(pool, flying, by_artist[job.artist.id], job)
                if not flying and not any(w.queue for w in works):
                    break
                line = self.flight_line(flying, throttle, works)
                if line != last_line:
                    self.now(line)
                    last_line, last_beat = line, time.monotonic()
                elif time.monotonic() - last_beat > 60:
                    store.heartbeat(self.conn, self.run_id)
                    last_beat = time.monotonic()
                if not flying:                       # new calls held by the rate limits
                    time.sleep(min(max(throttle.held(), 0.05), 1.0))
                    continue
                finished, _ = wait(flying, timeout=1.0, return_when=FIRST_COMPLETED)
                flush_log()
                for fut in finished:
                    job = flying.pop(fut)
                    w = by_artist[job.artist.id]
                    w.in_flight -= 1
                    self.collect(fut, job, w, throttle)
                    if not w.queue and not w.in_flight and not w.finished:
                        self.round_done(w)
                    elif not w.finished:
                        self.artist_detail(w)
        finally:
            pool.shutdown(wait=not flying, cancel_futures=True)
            flush_log()
        if throttle.slowdowns:
            log.info("Claude: slowed down %d time(s) for the rate limits; ended at %d call(s) at once",
                     throttle.slowdowns, throttle.limit)

    def next_job(self, works: list[ArtistWork]) -> Job | None:
        """Spread calls over artists (fewest in flight first), the artist with most left first."""
        ready = [w for w in works if w.queue]
        if not ready:
            return None
        w = min(ready, key=lambda w: (w.in_flight, -len(w.queue)))
        return w.queue.popleft()

    def submit(self, pool: ThreadPoolExecutor, flying: dict[Future, Job], w: ArtistWork, job: Job) -> None:
        a = job.artist
        if not w.started:
            self.progress.start(f"claude:{a.id}")
            w.started = True
        name = a.name + (f" ({a.name_ja})" if a.name_ja else "")
        known = store.known_shows(self.conn, a.id)
        flying[pool.submit(self.classifier.extract, name, self.today, self.window, known, job.batch)] = job
        w.in_flight += 1
        self.artist_detail(w)

    def artist_detail(self, w: ArtistWork) -> None:
        queued = len(w.queue)
        parts = ([f"{w.in_flight} batch{'es' if w.in_flight != 1 else ''} in flight"] if w.in_flight else []) \
            + ([f"{queued} queued"] if queued else [])
        self.progress.update(f"claude:{w.artist.id}", detail=" · ".join(parts) or "waiting for Claude")

    def flight_line(self, flying: dict[Future, Job], throttle: Throttle, works: list[ArtistWork]) -> str:
        n = len(flying)
        names = list(dict.fromkeys(j.artist.name for j in flying.values()))
        who = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
        line = (f"Claude: {n} batch{'es' if n != 1 else ''} in flight ({who})" if n
                else "Claude: waiting to send the next batch")
        if throttle.held():
            line += f"; new ones on hold: {throttle.why}"
        queued = sum(len(w.queue) for w in works)
        return (line + f" · {self.classified:,} of {self.to_classify:,} posts read"
                + (f" · {queued} batch{'es' if queued != 1 else ''} queued" if queued else ""))

    def collect(self, fut: Future, job: Job, w: ArtistWork, throttle: Throttle) -> None:
        a = job.artist
        try:
            res = fut.result()
        except RateLimited as e:
            throttle.rate_limited(e.retry_after)
            job.tries += 1
            if job.tries <= MAX_RATE_RETRIES:
                log.warning("%s: %s. Slowing down to %d call(s) at once and sending it again.", a.name, e,
                            throttle.limit)
                w.queue.appendleft(job)
            else:
                self.batch_failed(job, w, str(e))
            return
        except ClassifyError as e:
            self.batch_failed(job, w, str(e))
            return
        except Exception as e:  # noqa: BLE001
            self.batch_failed(job, w, f"{type(e).__name__}: {e}"[:300])
            return
        limits = getattr(self.classifier, "latest", None)
        throttle.done(res, limits)
        if limits and not self.limits_logged:
            self.limits_logged = True
            log.info("Claude rate limits: %s. Running up to %d call(s) at once.", limits.describe(), throttle.limit)
        try:
            w.follow += self.apply(job, res, w)
        except Exception as e:  # noqa: BLE001
            log.exception("saving Claude's answer for %s failed", a.name)
            self.batch_failed(job, w, f"{type(e).__name__}: {e}"[:300])

    def batch_failed(self, job: Job, w: ArtistWork, message: str) -> None:
        a = job.artist
        self.report.errors.append(f"{a.name}: {message}")
        self.classify_errors[a.id] = message
        log.error("%s: %s", a.name, message)
        self.classified += len(job.batch)
        w.read += len(job.batch)
        self.progress.add_to(f"claude:{a.id}", done=len(job.batch))

    def round_done(self, w: ArtistWork) -> None:
        """All of an artist's batches are back: read the detail pages Claude asked for (once), else finish."""
        a, key = w.artist, f"claude:{w.artist.id}"
        if w.round == 0 and w.follow:
            w.round = 1
            before = self.pending_count(a.id)
            for url, source_id in list(dict.fromkeys(w.follow))[:MAX_FOLLOW_PER_ARTIST]:
                self.now(f"{a.name}: reading a linked page Claude asked for ({urlsplit(url).hostname})")
                self.follow(a, url, source_id)
            added = self.pending_count(a.id) - before
            if added > 0:
                self.to_classify += added
                w.total += added
                self.progress.add_to(key, total=added)
            self.queue_unread(w)
            if w.queue:
                self.artist_detail(w)
                return
        w.finished = True
        row = self.conn.execute("SELECT shows, updates, done FROM run_steps WHERE run_id = ? AND key = ?",
                                (self.run_id, key)).fetchone()
        detail = f"{row['done']} read · {row['shows']} new show(s) · {row['updates']} update(s)"
        err = self.classify_errors.get(a.id, "")
        self.progress.finish(key, "failed" if err else "done", error=err, detail=detail)

    @staticmethod
    def label(row: sqlite3.Row) -> str:
        when = ""
        if row["posted_at"]:
            dt = parse_iso(row["posted_at"])
            when = f", posted {dt.astimezone(store.JST):%Y-%m-%d %H:%M} JST" if dt else ""
        if row["kind"] == "x":
            return f"X post by @{row['handle']}{when}"
        if row["kind"] == "instagram":
            return f"Instagram post by @{row['handle']}{when}"
        return ("Fan club page" if row["kind"] == "fanclub" else "Official web page") + " (current content)"

    def unread(self, artist_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.*, s.kind, s.handle FROM posts p JOIN sources s ON s.id = p.source_id "
            "WHERE s.artist_id = ? AND p.relevant IS NULL ORDER BY p.posted_at IS NULL, p.posted_at, p.id",
            (artist_id,)).fetchall()

    def queue_unread(self, w: ArtistWork) -> None:
        """Queue the artist's unread posts (not queued before this scan) as batches for Claude."""
        rows = [r for r in self.unread(w.artist.id) if r["id"] not in w.seen]
        w.seen.update(r["id"] for r in rows)
        for i in range(0, len(rows), CLASSIFY_CHUNK):           # items numbered [1]..[60] per chunk
            chunk = rows[i:i + CLASSIFY_CHUNK]
            items = [Item(n, self.label(r), r["url"], r["text"], store_json(r["media"]))
                     for n, r in enumerate(chunk, 1)]
            by_number = dict(enumerate(chunk, 1))
            for batch in batches(items):
                w.queue.append(Job(w.artist, batch, by_number))

    def apply(self, job: Job, res, w: ArtistWork) -> list[tuple[str, int]]:
        """Merge one answer from Claude into shows and rounds; returns the detail pages it asked to read."""
        artist, batch, by_number = job.artist, job.batch, job.by_number
        key = f"claude:{artist.id}"
        self.report.claude_usd += res.usd
        self.costs()
        ex = res.extraction
        item_posts = {it.number: by_number[it.number]["id"] for it in batch}
        events, _ = store.merge(self.conn, artist.id, ex, item_posts, self.window,
                                self.started.astimezone(store.JST))
        self.report.events += events
        new_ids = {e.show_id for e in events if e.kind == "new_show"}
        new_shows = len(new_ids)
        updates = sum(1 for e in events if e.show_id not in new_ids)    # a new show's own rounds aren't updates
        verdicts = {v.item: v for v in ex.items}
        with self.conn:
            for it in batch:
                v = verdicts.get(it.number)
                self.conn.execute("UPDATE posts SET relevant = ?, note = ?, checked_run_id = ? WHERE id = ?",
                                  (int(bool(v and v.relevant)), (v.note if v else "")[:300], self.run_id,
                                   item_posts[it.number]))
        self.classified += len(batch)
        w.read += len(batch)
        self.progress.add_to(key, done=len(batch), shows=new_shows, updates=updates, usd=res.usd)
        if events:
            log.info("%s: %d new show(s), %d update(s)", artist.name, new_shows, updates)
        # which source each follow-up link belongs to: the item that mentions it, else the batch's first
        out = []
        for url in ex.follow_urls[:5]:
            host = (urlsplit(url).hostname or "").lower()
            if not url.startswith(("http://", "https://")) or any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS):
                continue
            src_post = next((by_number[it.number] for it in batch if url in it.text), by_number[batch[0].number])
            out.append((url, src_post["source_id"]))
        return out

    def follow(self, artist: db.Artist, url: str, source_id: int) -> None:
        """Read a detail page Claude pointed at. Tracked (re-read each scan) when it's on one of the artist's sites."""
        site = next((s for s in artist.sources if s.kind in WEB_KINDS and same_site(s.url, url)), None)
        owner = site.id if site else source_id
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO pages (source_id, url, tracked, first_seen_at) VALUES (?, ?, ?, ?)",
                              (owner, url, int(site is not None), db.now()))
        rendered = self.conn.execute("SELECT rendered FROM pages WHERE source_id = ? AND url = ?",
                                     (owner, url)).fetchone()["rendered"]
        err = self.read_page(owner, url, bool(rendered), f"claude:{artist.id}")
        if err:
            log.warning("follow-up %s: %s", url, err)

    # ------------------------------------------------------------ alerts

    def send_alerts(self, dry_run: bool) -> None:
        msgs = self.report.messages
        key = "alerts"
        if dry_run:
            self.progress.finish(key, "skipped", total=len(msgs),
                                 detail=f"dry run: {len(msgs)} alert(s) printed, not sent")
            return
        if not msgs:
            self.progress.finish(key, "done", detail="nothing new to alert")
            return
        if not self.s.discord_webhook:
            err = f"DISCORD_WEBHOOK_URL is not set; {len(msgs)} alert(s) kept for the next scan"
            self.report.errors.append(err)
            self.progress.finish(key, "failed", total=len(msgs), error=err)
            return
        self.progress.start(key, total=len(msgs), detail=f"{len(msgs)} to send")
        self.now(f"Discord: sending {len(msgs)} alert(s)")

        def on_sent(i: int, m: alerts.Message) -> None:
            self.progress.update(key, done=i)
            self.now(f"Discord: sent {i} of {len(msgs)}: {m.first_line}")

        sent, errors = alerts.deliver(self.conn, self.s, msgs, client=self.alert_client, pause=self.alert_pause,
                                      on_sent=on_sent)
        self.report.alerts_sent = sent
        self.report.errors += errors
        self.progress.finish(key, "failed" if errors else "done", error="; ".join(errors)[:500],
                             detail=f"{sent} of {len(msgs)} sent")

    # ------------------------------------------------------------ status

    def save_source_status(self, sources: list[db.Source]) -> None:
        with self.conn:
            for src in sources:
                st = self.stats.get(src.id)
                if not st or not st.scanned:
                    continue
                found = self.conn.execute(
                    "SELECT COUNT(DISTINCT sp.show_id) FROM show_posts sp JOIN posts p ON p.id = sp.post_id "
                    "JOIN shows sh ON sh.id = sp.show_id WHERE p.source_id = ? AND sh.hidden = 0", (src.id,)).fetchone()[0]
                waiting = self.conn.execute("SELECT COUNT(*) FROM posts WHERE source_id = ? AND relevant IS NULL",
                                            (src.id,)).fetchone()[0]
                error = st.error
                if not error and waiting:
                    why = self.classify_errors.get(src.artist_id, "will retry next scan")
                    error = f"{waiting} post(s) read but not checked by Claude yet ({why})"
                status = "error" if error else ("ok" if st.posts else "empty")
                self.conn.execute(
                    "UPDATE sources SET last_scanned_at = ?, last_status = ?, last_error = ?, last_posts = ?, "
                    "last_found = ? WHERE id = ?", (db.now(), status, error, st.posts, found, src.id))


def store_json(value: str) -> list[str]:
    import json
    try:
        v = json.loads(value or "[]")
        return [x for x in v if isinstance(x, str)]
    except ValueError:
        return []


# ---------------------------------------------------------------- CLI

def setup_logging(verbose: bool, log_file: Path) -> None:
    """Log to `log_file` (Settings.log_file: next to the database, named after it unless it's the real one) and
    stderr."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5,
                                              encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]
    for noisy in ("httpx", "httpx2", "httpcore", "apify_client", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def copy_db(src: Path) -> Path:
    """A private copy for --dry-run, so nothing in the real database changes."""
    tmp = Path(tempfile.mkdtemp(prefix="concert-dry-")) / "dry.db"
    a, b = sqlite3.connect(src), sqlite3.connect(tmp)
    try:
        a.backup(b)
    finally:
        a.close()
        b.close()
    return tmp


def print_limits(settings: Settings) -> int:
    if not settings.anthropic_key:
        print("ANTHROPIC_API_KEY is not set in .env")
        return 1
    clf = Classifier(settings.anthropic_key, settings.claude_model)
    try:
        limits = clf.probe_limits()
    finally:
        clf.close()
    if not limits:
        print("The answer had no rate-limit headers.")
        return 1
    print(f"{settings.claude_model}: {limits.describe()}")
    for k in limits.limit:
        print(f"  {k}: {limits.remaining[k]:,} of {limits.limit[k]:,} left, full again in {limits.reset[k]:.0f}s")
    print(f"CLAUDE_CONCURRENCY is {settings.claude_concurrency}; the scan fits it under these limits as it runs.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.watch", description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="fetch and classify, print alerts; change and send nothing")
    ap.add_argument("--artist", type=int, action="append", help="artist id (repeatable); default: every watched artist")
    ap.add_argument("--trigger", default="cli", choices=["cli", "cron", "ui"])
    ap.add_argument("--resume", type=int, metavar="RUN_ID",
                    help="continue a scan that stopped partway: same artists, sources it finished aren't read again")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--limits", action="store_true",
                    help="print the Anthropic account's rate limits (one tiny Claude call) and exit")
    args = ap.parse_args(argv)

    settings = Settings.from_env()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if args.limits:
        return print_limits(settings)
    db.init_db(settings.database_path)
    if args.trigger == "cron" and not args.dry_run and not args.resume:
        # cron starts this every hour; scan only when the Scans page's schedule says one is due, else say nothing
        c = db.connect(settings.database_path)
        try:
            if not store.scan_due(c, store.prefs(c, settings), utcnow()):
                return 0
        finally:
            c.close()
    setup_logging(args.verbose, settings.log_file)
    path = copy_db(settings.database_path) if args.dry_run else settings.database_path
    conn = db.connect(path)
    done_before: set[str] = set()
    if args.resume:
        old = conn.execute("SELECT scope FROM runs WHERE id = ?", (args.resume,)).fetchone()
        if not old:
            log.warning("There's no scan #%d to resume.", args.resume)
            return 2
        if old["scope"] != "all":
            args.artist = [int(p.split(":")[1]) for p in old["scope"].split(",") if p.startswith("artist:")]
        done_before = store.done_before(conn, args.resume)
    scope = "all" if not args.artist else ",".join(f"artist:{a}" for a in args.artist)
    notices = settings.discord_scan_notices and not args.dry_run      # a dry run never posts anything

    def stopped(c: sqlite3.Connection, old_id: int) -> None:          # an earlier scan found dead on the way in
        alerts.notify(settings, alerts.finished_text(c, old_id, settings.app_url, stopped=True))
    try:
        run_id = store.start_run(conn, args.trigger, scope, args.dry_run, pid=os.getpid(), resumed_from=args.resume,
                                 on_stopped=stopped if notices else None)
    except store.Busy as e:
        log.warning("%s Not starting another.", e)
        return 2

    prog.prune(conn)
    run_log = prog.RunLogHandler(conn, run_id)
    logging.getLogger().addHandler(run_log)
    apify = Apify(settings.apify_token, settings.apify_max_usd_per_run) if settings.apify_token else None
    classifier = Classifier(settings.anthropic_key, settings.claude_model) if settings.anthropic_key else None
    browser = Browser() if settings.browser_fallback else None
    report, status, scan = Report(), "error", None
    try:
        scan = Scan(settings, conn, run_id, apify=apify, classifier=classifier, browser=browser, notices=notices)
        report = scan.run(args.artist, done_before, args.resume)
        scan.send_alerts(args.dry_run)
        if args.dry_run:
            print(f"\n=== DRY RUN: {len(report.messages)} alert(s) would be sent ===\n")
            for m in report.messages:
                print(m.text, "\n" + "-" * 60)
        status = "partial" if report.errors else "ok"
    except Exception as e:  # noqa: BLE001
        log.exception("Scan failed")
        report.errors.append(f"Scan failed: {type(e).__name__}: {e}")
    finally:
        if browser:
            browser.close()
        if classifier:
            classifier.close()
        summary = (f"{report.posts_read} new post(s)/page version(s), {report.shows_new} new show(s), "
                   f"{len(report.events)} update(s), {report.alerts_sent} alert(s) sent")
        log.info("Done (%s): %s. Apify $%.3f, Claude $%.3f", status, summary, report.apify_usd, report.claude_usd)
        for err in report.errors:
            log.warning("  %s", err)
        logging.getLogger().removeHandler(run_log)
        if scan:
            scan.progress.skip_pending(why="not reached: the scan stopped early")
            scan.progress.now(("Done: " if status != "error" else "Stopped: ") + summary)
        store.finish_run(conn, run_id, status=status, posts_read=report.posts_read, shows_new=report.shows_new,
                         alerts_sent=report.alerts_sent, apify_usd=round(report.apify_usd, 4),
                         claude_usd=round(report.claude_usd, 4), summary=summary,
                         errors="\n".join(report.errors)[:8000])
        if notices:
            alerts.notify(settings, alerts.finished_text(conn, run_id, settings.app_url))
        conn.close()
        if args.dry_run:
            shutil.rmtree(path.parent, ignore_errors=True)
    return 0 if status != "error" else 1


if __name__ == "__main__":
    sys.exit(main())
