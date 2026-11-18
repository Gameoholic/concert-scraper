"""A scan whose process is gone is noticed at once, shown as stopped, and can be resumed."""

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from app import db, progress, store
from app.config import Settings
from app.main import create_app
from app.watch import Scan
from tests.test_watch import AT, FakeApify, FakeClassifier, add, fake_fetch, tweet


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "res.db")


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_process_check():
    assert store.process_started(os.getpid()) not in (None,)
    assert store.process_started(dead_pid()) is None


def test_a_scan_whose_process_is_gone_is_stopped_at_once(conn):
    run_id = store.start_run(conn, "ui", "all", False, pid=os.getpid())
    progress.Progress(conn, run_id).plan([progress.StepSpec("src:1", "read", "x", "@a")])
    assert store.running_run(conn)["id"] == run_id                 # this process is alive: still running
    with conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (dead_pid(), run_id))
    assert store.running_run(conn) is None                          # noticed on the next look, no 20-minute wait
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "error" and "process ended" in run["errors"]
    assert conn.execute("SELECT status FROM run_steps WHERE run_id = ?", (run_id,)).fetchone()[0] == "skipped"
    assert store.resumable(conn, run_id)


def test_resume_reads_only_what_the_stopped_scan_did_not(settings, conn):
    eve = add(conn, "Eve", "x.com/oO0Eve0Oo", "https://eveofficial.com/live/")
    reol = add(conn, "Reol", "x.com/RRReol")
    srcs = {s.url: s.id for a in (db.get_artist(conn, eve), db.get_artist(conn, reol)) for s in a.sources}
    old = store.start_run(conn, "ui", "all", False, pid=dead_pid())
    p = progress.Progress(conn, old)
    p.plan([progress.StepSpec(f"src:{i}", "read", "x", "?") for i in srcs.values()])
    p.finish(f"src:{srcs['https://x.com/oo0eve0oo']}", "done")
    store.reap(conn)

    apify = FakeApify([tweet(1, "rrreol", "hi")])
    run_id = store.start_run(conn, "ui", "all", False, resumed_from=old)
    Scan(settings, conn, run_id, apify=apify, classifier=FakeClassifier({}),
         fetch=fake_fetch({"https://eveofficial.com/live/": "page"}), at=AT).run(
        None, store.done_before(conn, old), old)
    queried = " ".join(c[1]["searchTerms"][0] for c in apify.calls)
    assert "from:rrreol" in queried and "oo0eve0oo" not in queried
    skipped = conn.execute("SELECT detail FROM run_steps WHERE run_id = ? AND key = ?",
                           (run_id, f"src:{srcs['https://x.com/oo0eve0oo']}")).fetchone()[0]
    assert f"#{old}" in skipped
    assert not store.resumable(conn, old)                           # a newer scan ran


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.state.launched = []
    app.state.launch_scan = lambda artist_id=None, resume=None: app.state.launched.append((artist_id, resume))
    c = TestClient(app)
    c.post("/login", data={"password": "x" * 12, "next": "/"})
    return c


def test_scans_page_offers_resume_and_the_poll_reports_the_stop(client, conn):
    run_id = store.start_run(conn, "ui", "all", False, pid=os.getpid())
    progress.Progress(conn, run_id).now("Apify: reading X @a (1 of 3 accounts)")
    assert client.get(f"/scan/progress?run={run_id}").json()["run"]["status"] == "running"
    with conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (dead_pid(), run_id))
    snap = client.get(f"/scan/progress?run={run_id}").json()
    assert snap["run"]["status"] == "error" and snap["run"]["resumable"]
    page = client.get("/scans").text
    assert "The last scan stopped partway" in page and f'action="/scans/{run_id}/resume"' in page
    assert "Last thing it did: Apify: reading X @a (1 of 3 accounts)" in page
    r = client.post(f"/scans/{run_id}/resume", follow_redirects=False)
    assert r.status_code == 303 and client.app.state.launched == [(None, run_id)]
