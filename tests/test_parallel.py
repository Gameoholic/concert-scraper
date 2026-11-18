"""Claude calls in parallel: rate-limit headers, the throttle, and a scan with a slow stub classifier."""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import db, progress, store
from app.classify import CallResult, Extraction, RateLimited, Throttle, parse_limits
from app.config import Settings
from app.scan_web import Page
from app.watch import Scan
from tests.test_watch import AT, FakeApify, FakeClassifier, add, fake_fetch, show, tweet

T0 = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def headers(req=(4000, 3990), tin=(2_000_000, 1_900_000), tout=(400_000, 390_000), reset_s=5):
    reset = (T0 + timedelta(seconds=reset_s)).isoformat().replace("+00:00", "Z")
    h = {}
    for k, (lim, left) in {"requests": req, "input-tokens": tin, "output-tokens": tout}.items():
        h[f"anthropic-ratelimit-{k}-limit"] = str(lim)
        h[f"anthropic-ratelimit-{k}-remaining"] = str(left)
        h[f"anthropic-ratelimit-{k}-reset"] = reset
    return h


def result(secs=20.0, tin=20_000, tout=3_000):
    return CallResult(Extraction(items=[], shows=[], follow_urls=[]), 0.003, secs, tin, tout)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- headers and throttle

def test_parse_limits_reads_the_headers():
    lim = parse_limits(headers(), at=T0)
    assert lim.limit == {"requests": 4000, "input-tokens": 2_000_000, "output-tokens": 400_000}
    assert lim.remaining["input-tokens"] == 1_900_000 and lim.reset["requests"] == pytest.approx(5)
    assert lim.describe() == "4,000 requests, 2,000,000 input tokens, 400,000 output tokens per minute"
    assert parse_limits({"content-type": "application/json"}) is None


def test_throttle_starts_small_then_fits_the_account_limits():
    t = Throttle(15, clock=Clock())
    assert t.allowed() == 2                                   # before any headers
    t.done(result(), parse_limits(headers(), at=T0))
    # per call: 20k in / 3k out over 20 s -> 60k in, 9k out per minute per slot; 2M x 0.8 / 60k = 26 -> capped at 15
    assert t.allowed() == 15

    tight = Throttle(15, clock=Clock())
    tight.done(result(), parse_limits(headers(tin=(450_000, 400_000)), at=T0))
    assert tight.allowed() == 6                               # 450k x 0.8 / 60k


def test_throttle_holds_when_a_limit_is_nearly_used_and_halves_on_429():
    clock = Clock()
    t = Throttle(8, clock=clock)
    t.done(result(), parse_limits(headers(tin=(2_000_000, 30_000), reset_s=12), at=T0))
    assert t.allowed() == 0 and t.held() == pytest.approx(12) and "input tokens" in t.why
    clock.t += 13
    assert t.allowed() == 8

    t.rate_limited(retry_after=4)
    assert t.limit == 4 and t.allowed() == 0 and "slow down" in t.why
    clock.t += 5
    for _ in range(8):                                        # a run of good calls grows it back one at a time
        t.done(result(), None)
    assert t.limit == 5 and t.slowdowns == 1


def test_throttle_without_headers_trusts_the_setting():
    t = Throttle(6, clock=Clock())
    t.done(result(), None)
    assert t.allowed() == 6


def test_estimate_times_parallel_claude_steps_by_the_wall_clock():
    iso = lambda s: (T0 + timedelta(seconds=s)).isoformat()  # noqa: E731
    steps = [{"id": i, "kind": "claude", "status": "running", "started_at": iso(0), "finished_at": None,
              "done": 30, "total": 60, "grp": ""} for i in (1, 2)]
    est = progress.estimate(steps, T0 + timedelta(seconds=60))
    # 60 posts read in 60 s of wall time by both together -> 1 s per post; 60 left
    assert est["eta"] == 60 and est["fraction"] == pytest.approx(0.5)


# ---------------------------------------------------------------- a scan with a slow stub

class SlowClassifier(FakeClassifier):
    """Takes `delay` seconds per call, counts how many calls overlap, and says "slow down" once per marked text."""

    def __init__(self, table, delay=0.05, slow_down_on=()):
        super().__init__(table)
        self.delay, self.slow_down_on = delay, set(slow_down_on)
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.threads: set[int] = set()

    def extract(self, artist, today, window, known, items):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.get_ident())
            limited = items[0].text in self.slow_down_on
            self.slow_down_on.discard(items[0].text)
        try:
            time.sleep(self.delay)
            logging.getLogger("app.classify").info("worker read %d item(s) for %s", len(items), artist)
            if limited:
                raise RateLimited("Claude API 429: rate limited", retry_after=0.01)
            return super().extract(artist, today, window, known, items)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "par.db",
                    claude_concurrency=4)


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def seed(conn):
    handles = {"UVERworld": "UVERworld_dR2", "Eve": "oO0Eve0Oo", "Reol": "RRReol", "ZUTOMAYO": "zutomayo"}
    tweets, tid, ids = [], 0, []
    for name, h in handles.items():
        ids.append(add(conn, name, f"x.com/{h}"))
        for n in range(30):                                   # 30 posts -> 3 batches per artist
            tid += 1
            text = "UVER TOUR 2027 FC先行" if (name == "UVERworld" and n == 0) else f"{name} post {n}"
            tweets.append(tweet(tid, h.lower(), text))
    return FakeApify(tweets), ids


def run(settings, conn, clf, seeded):
    apify, ids = seeded
    run_id = store.start_run(conn, "ui", "all", False)
    handler = progress.RunLogHandler(conn, run_id)
    root = logging.getLogger()
    old_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        scan = Scan(settings, conn, run_id, apify=apify, classifier=clf, fetch=fake_fetch({}), at=AT)
        report = scan.run(ids)
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    lines = [r[0] for r in conn.execute("SELECT message FROM run_log WHERE run_id = ? ORDER BY id", (run_id,))]
    steps = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM run_steps WHERE run_id = ?", (run_id,))}
    return report, scan, lines, steps


def test_scan_reads_batches_in_parallel_and_writes_only_from_the_main_thread(settings, conn):
    seeded = seed(conn)
    clf = SlowClassifier({"UVER TOUR": [show("LIVE HOUSE TOUR 2027", "2027-02-04", "Yokohama", "KT Zepp Yokohama")]})
    report, scan, lines, steps = run(settings, conn, clf, seeded)

    assert 2 < clf.peak <= 4 and threading.get_ident() not in clf.threads
    assert store_unread(conn) == 0 and scan.classified == scan.to_classify == 120
    assert only(conn, "SELECT COUNT(*) FROM shows") == 1 and report.errors == []
    claude = [s for k, s in steps.items() if k.startswith("claude:")]
    assert len(claude) == 4 and all(s["status"] == "done" and s["done"] == s["total"] == 30 for s in claude)
    assert sum(s["shows"] for s in claude) == 1
    assert any("batches in flight" in line for line in lines)                 # the "now" line, also logged
    assert sum("worker read" in line for line in lines) == len(clf.calls) == 12   # worker-thread lines reach the log


def test_slow_down_sends_the_batch_again_instead_of_failing(settings, conn):
    seeded = seed(conn)
    clf = SlowClassifier({}, delay=0.01, slow_down_on={"Eve post 0"})
    report, scan, lines, steps = run(settings, conn, clf, seeded)
    assert report.errors == [] and store_unread(conn) == 0
    assert len(clf.calls) == 12                                # the refused call never reached the table
    assert any("Slowing down" in line for line in lines) and any("slowed down 1 time" in line for line in lines)
    assert all(s["status"] == "done" for k, s in steps.items() if k.startswith("claude:"))


def test_a_dead_scan_is_closed_when_the_next_one_starts(conn):
    old = store.start_run(conn, "ui", "all", False)
    p = progress.Progress(conn, old)
    p.plan([progress.StepSpec("claude:1", "claude", "claude", "Eve"), progress.StepSpec("alerts", "alerts", "alerts", "x")])
    p.start("claude:1")
    p.now("Claude: 3 batches in flight")
    with conn:
        conn.execute("UPDATE runs SET heartbeat_at = '2026-01-01T00:00:00+00:00' WHERE id = ?", (old,))
    assert store.running_run(conn) is None                     # the button is back
    store.start_run(conn, "ui", "all", False)
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (old,)).fetchone()
    assert run["status"] == "error" and run["current"].startswith("Stopped: ")
    assert {r[0] for r in conn.execute("SELECT status FROM run_steps WHERE run_id = ?", (old,))} == {"skipped"}


class SlowApify(FakeApify):
    """Each run takes `delay` s; counts how many overlap. limits() says what the 'account' allows."""

    def __init__(self, tweets, delay=0.1, runs=3, memory=16.0):
        super().__init__(tweets)
        self.delay, self.allowed = delay, (runs, memory)
        self.lock, self.active, self.peak, self.threads = threading.Lock(), 0, 0, set()

    def limits(self):
        return self.allowed

    def run(self, actor, run_input, max_items):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.add(threading.get_ident())
        try:
            time.sleep(self.delay)
            return super().run(actor, run_input, max_items)
        finally:
            with self.lock:
                self.active -= 1


def test_apify_runs_side_by_side_within_the_account_limit_while_websites_are_read(settings, conn):
    handles = ["UVERworld_dR2", "oO0Eve0Oo", "RRReol", "zutomayo", "LiSA_OLiVE", "Ado1024imokenp"]
    names = ["UVERworld", "Eve", "Reol", "ZUTOMAYO", "LiSA", "Ado"]
    for name, h in zip(names, handles):
        add(conn, name, f"x.com/{h}")
    add(conn, "Eve", "https://eveofficial.com/live/")
    apify = SlowApify([tweet(i + 1, h.lower(), f"post {i}") for i, h in enumerate(handles)], runs=3)
    fetched = []

    def fetch(url, rendered_before=False, browser=None, probe=False):
        fetched.append(apify.active)                         # Apify runs still going while the page is read
        return Page(url, "Eve live", False)

    run_id = store.start_run(conn, "ui", "all", False)
    scan = Scan(settings, conn, run_id, apify=apify, classifier=FakeClassifier({}), fetch=fetch, at=AT)
    scan.run()
    assert len(apify.calls) == 6 and apify.peak == 3 and threading.get_ident() not in apify.threads
    assert fetched and fetched[0] > 0
    steps = {r["label"]: dict(r) for r in conn.execute("SELECT * FROM run_steps WHERE run_id = ? AND kind = 'x'",
                                                       (run_id,))}
    assert all(s["status"] == "done" and s["posts"] == 1 for s in steps.values())
    assert only(conn, "SELECT COUNT(*) FROM posts") == 7


def test_apify_concurrency_setting_is_the_ceiling(tmp_path):
    from dataclasses import replace
    s = Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "c.db")
    scan = Scan.__new__(Scan)
    scan.s, scan.apify = replace(s, apify_concurrency=15), SlowApify([], runs=5, memory=16.0)
    assert scan.apify_slots() == 5                            # free plan: 5 runs at once
    scan.apify = SlowApify([], runs=25, memory=4.0)
    assert scan.apify_slots() == 4                            # 4 GB / 1 GB per Instagram run
    scan.s = replace(s, apify_concurrency=2)
    assert scan.apify_slots() == 2


def store_unread(conn):
    return only(conn, "SELECT COUNT(*) FROM posts WHERE relevant IS NULL")


def only(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]
