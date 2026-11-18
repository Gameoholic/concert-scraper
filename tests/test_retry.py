"""Retries with backoff: what counts as worth another try, and how a scan shows it."""

import httpx
import pytest

from app import db, retry, store
from app.scan_social import ApifyRunError
from app.scan_web import FetchError, Page
from app.watch import Scan
from tests.test_watch import AT, FakeApify, FakeClassifier, add, tweet


def test_what_is_worth_another_try():
    assert retry.transient(httpx.ReadTimeout("The read operation timed out"))
    assert retry.transient(httpx.ConnectError("[Errno 11001] getaddrinfo failed"))
    assert retry.transient(OSError("[Errno 11001] getaddrinfo failed"))
    assert retry.transient(FetchError("HTTP 503", True)) and not retry.transient(FetchError("HTTP 404"))
    assert retry.transient(ApifyRunError("run timed-out", transient=True))
    assert not retry.transient(ApifyRunError("run failed"))
    assert not retry.transient(ValueError("bad input"))
    assert retry.delays(3) == (3.0, 9.0, 27.0) and retry.delays(0) == ()


def test_call_retries_with_backoff_then_gives_up():
    waits, calls = [], []

    def flaky():
        calls.append(1)
        raise httpx.ReadTimeout("timed out")

    seen = []
    with pytest.raises(httpx.ReadTimeout):
        retry.call(flaky, (3.0, 9.0), lambda n, e, w: seen.append((n, w)), sleep=waits.append)
    assert len(calls) == 3 and waits == [3.0, 9.0] and seen == [(1, 3.0), (2, 9.0)]

    calls.clear()
    with pytest.raises(FetchError):
        retry.call(lambda: calls.append(1) or (_ for _ in ()).throw(FetchError("HTTP 404")), (3.0,), sleep=waits.append)
    assert len(calls) == 1                                        # a 404 is never tried again


class FlakyApify(FakeApify):
    def __init__(self, tweets, fails):
        super().__init__(tweets)
        self.fails = fails

    def run(self, actor, run_input, max_items):
        if self.fails:
            self.fails -= 1
            self.calls.append((actor, run_input))
            raise ApifyRunError("kaitoeasyapi: run timed-out", transient=True)
        return super().run(actor, run_input, max_items)


@pytest.fixture
def settings(tmp_path):
    from app.config import Settings
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "r.db")


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def test_a_scan_retries_apify_and_websites_and_shows_it(settings, conn):
    add(conn, "Eve", "x.com/oO0Eve0Oo", "https://eveofficial.com/live/", "https://eveofficial.com/gone/")
    tries = {"n": 0}

    def fetch(url, rendered_before=False, browser=None, probe=False):
        if url.endswith("gone/"):
            raise FetchError("HTTP 404")
        tries["n"] += 1
        if tries["n"] == 1:
            raise FetchError("ConnectError: [Errno 11001] getaddrinfo failed", True)
        return Page(url, "Eve live page", False)

    apify = FlakyApify([tweet(1, "oo0eve0oo", "hello")], fails=1)
    run_id = store.start_run(conn, "cli", "all", False)
    rep = Scan(settings, conn, run_id, apify=apify, classifier=FakeClassifier({}), fetch=fetch, at=AT,
               retry_waits=(0.0, 0.0)).run()
    steps = {r["label"]: dict(r) for r in conn.execute("SELECT * FROM run_steps WHERE run_id = ?", (run_id,))}
    assert len(apify.calls) == 2 and steps["@oO0Eve0Oo"]["status"] == "done" and steps["@oO0Eve0Oo"]["retries"] == 1
    site = steps["eveofficial.com/live"]
    assert site["status"] == "done" and site["retries"] == 1 and tries["n"] == 2
    assert steps["eveofficial.com/gone"]["retries"] == 0 and steps["eveofficial.com/gone"]["status"] == "failed"
    assert [e for e in rep.errors if "gone" in e] and not [e for e in rep.errors if "live" in e]
