"""Live scan progress: planned steps, their states and counts, estimates, incremental snapshots, the log, pages."""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, progress, store
from app.config import Settings
from app.main import create_app
from tests.test_watch import AT, FakeApify, FakeClassifier, OkClient, add, fake_fetch, rnd, show, tweet
from app.watch import Scan


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "p.db",
                    discord_webhook="https://discord.example/api/webhooks/1/x")


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def steps(conn, run_id):
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM run_steps WHERE run_id = ? ORDER BY position",
                                                    (run_id,))}


def run_scan(settings, conn, apify, clf, pages=None, send=True):
    run_id = store.start_run(conn, "ui", "all", False)
    scan = Scan(settings, conn, run_id, apify=apify, classifier=clf, fetch=fake_fetch(pages or {}), at=AT,
                alert_client=OkClient(), alert_pause=0)
    scan.run([a for a in [r[0] for r in conn.execute("SELECT DISTINCT artist_id FROM sources")]])
    if send:
        scan.send_alerts(False)
    store.finish_run(conn, run_id, status="ok")
    return run_id, scan


def test_scan_plans_every_step_and_records_outcomes(settings, conn):
    uver = add(conn, "UVERworld", "x.com/UVERworld_dR2", "https://www.uverworld.jp/feature/2027_live",
               "https://www.uverworld.jp/broken")
    eve = add(conn, "Eve", "x.com/oO0Eve0Oo")
    apify = FakeApify(tweets=[tweet(1, "uverworld_dr2", "TOUR 2027 FC先行"), tweet(2, "uverworld_dr2", "single")])
    clf = FakeClassifier({"TOUR 2027": [show("LIVE HOUSE TOUR 2027", "2027-02-04", "Yokohama", "KT Zepp Yokohama",
                                             [rnd("FC先行", closes="2026-11-20T23:59")])]})
    pages = {"https://www.uverworld.jp/feature/2027_live": "TOUR page", "https://www.uverworld.jp/broken": None}
    run_id, scan = run_scan(settings, conn, apify, clf, pages)
    st = steps(conn, run_id)
    srcs = {s.url: s.id for a in (db.get_artist(conn, uver), db.get_artist(conn, eve)) for s in a.sources}

    # planned in run order: X accounts, websites, one Claude step per artist, alerts
    assert [k.split(":")[0] for k in st] == ["src", "src", "src", "src", "claude", "claude", "alerts"]
    x = st[f"src:{srcs['https://x.com/uverworld_dr2']}"]
    assert x["status"] == "done" and x["posts"] == 2 and x["kind"] == "x" and x["sublabel"] == "UVERworld"
    assert x["started_at"] and x["finished_at"] and x["usd"] == pytest.approx(0.01)
    assert st[f"src:{srcs['https://x.com/oo0eve0oo']}"]["posts"] == 0
    web = st[f"src:{srcs['https://www.uverworld.jp/feature/2027_live']}"]
    assert web["status"] == "done" and web["done"] == web["total"] == 1 and web["posts"] == 1
    broken = st[f"src:{srcs['https://www.uverworld.jp/broken']}"]
    assert broken["status"] == "failed" and "404" in broken["error"]

    claude_u, claude_e = st[f"claude:{uver}"], st[f"claude:{eve}"]
    assert claude_u["status"] == "done" and claude_u["done"] == claude_u["total"] == 3 and claude_u["shows"] == 1
    assert claude_e["status"] == "skipped" and claude_e["detail"] == "nothing new to read"
    assert st["alerts"]["status"] == "done" and st["alerts"]["done"] == st["alerts"]["total"] == 1

    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert run["apify_usd"] == pytest.approx(0.02) and run["claude_usd"] > 0   # 2 first-scan calls x $0.01
    assert run["current"].startswith("Discord: sent 1 of 1")
    assert scan.classified == scan.to_classify == 3


def test_failures_and_missing_keys_show_on_the_steps(settings, conn):
    add(conn, "ZUTOMAYO", "x.com/zutomayo", "instagram.com/zutomayo")
    run_id, _ = run_scan(settings, conn, FakeApify(fail_x=True), None, send=False)
    st = steps(conn, run_id)
    by_kind = {s["kind"]: s for s in st.values()}
    assert by_kind["x"]["status"] == "failed" and "actor exploded" in by_kind["x"]["error"]
    assert by_kind["instagram"]["status"] == "done"
    assert by_kind["claude"]["status"] == "skipped"          # nothing was read, so nothing waits for Claude

    run2 = store.start_run(conn, "ui", "all", False)
    scan = Scan(settings, conn, run2, apify=None, classifier=None, fetch=fake_fetch({}), at=AT)
    scan.run()
    st2 = steps(conn, run2)
    assert all(s["status"] == "failed" and "APIFY_API_TOKEN" in s["error"] for s in st2.values() if s["kind"] == "x")


def test_skip_pending_closes_unfinished_steps(conn):
    run_id = store.start_run(conn, "cli", "all", False)
    p = progress.Progress(conn, run_id)
    p.plan([progress.StepSpec("a", "read", "x", "@a"), progress.StepSpec("b", "read", "x", "@b")])
    p.start("a")
    p.skip_pending(why="not reached")
    st = steps(conn, run_id)
    assert {s["status"] for s in st.values()} == {"skipped"} and st["b"]["detail"] == "not reached"


def test_snapshot_returns_only_changes_and_new_log_lines(conn):
    run_id = store.start_run(conn, "cli", "all", False)
    p = progress.Progress(conn, run_id)
    p.plan([progress.StepSpec("a", "read", "x", "@a"), progress.StepSpec("b", "read", "website", "site")])
    handler = progress.RunLogHandler(conn, run_id)
    lg = logging.getLogger("test.progress")
    lg.addHandler(handler)
    lg.setLevel(logging.INFO)
    lg.info("first line")
    full = progress.snapshot(conn, run_id)
    assert len(full["steps"]) == 2 and [l["message"] for l in full["log"]] == ["first line"]

    p.start("a")
    p.now("Apify: reading X @a (1 of 2 accounts)")
    lg.warning("second line")
    lg.removeHandler(handler)
    inc = progress.snapshot(conn, run_id, since_rev=full["rev"], after_log=full["log"][-1]["id"])
    assert [s["key"] for s in inc["steps"]] == ["a"] and inc["steps"][0]["status"] == "running"
    assert [(l["level"], l["message"]) for l in inc["log"]] == [("WARNING", "second line")]
    assert inc["run"]["current"] == "Apify: reading X @a (1 of 2 accounts)"
    assert inc["counts"] == {"pending": 1, "running": 1, "done": 0, "failed": 0, "skipped": 0}


def _step(i, kind, status, start=None, end=None, done=0, total=0, grp=""):
    t0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    iso = lambda s: (t0 + timedelta(seconds=s)).isoformat() if s is not None else None  # noqa: E731
    return {"id": i, "kind": kind, "status": status, "started_at": iso(start), "finished_at": iso(end),
            "done": done, "total": total, "grp": grp}


def test_estimate_uses_this_runs_timings():
    t0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    steps_ = [_step(1, "x", "done", 0, 40), _step(2, "x", "done", 40, 80), _step(3, "x", "running", 80),
              _step(4, "x", "pending"), _step(5, "claude", "pending", total=10)]
    est = progress.estimate(steps_, t0 + timedelta(seconds=90))
    # spent 90 s; left: running x (40 - 10 = 30) + one x (40) + 10 posts x 1.2 s (default, not measured yet)
    assert est["eta"] == 30 + 40 + 12
    assert est["fraction"] == pytest.approx(90 / (90 + 82), abs=0.001)


def test_estimate_counts_one_apify_call_once_and_waits_before_guessing():
    t0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    grouped = [_step(1, "instagram", "done", 0, 60, grp="ig:0"), _step(2, "instagram", "done", 0, 60, grp="ig:0"),
               _step(3, "instagram", "pending", grp="ig:1"), _step(4, "instagram", "pending", grp="ig:1")]
    est = progress.estimate(grouped, t0 + timedelta(seconds=60))
    assert est["eta"] == 60                           # the second call, timed like the first; not 2 x 60
    early = progress.estimate([_step(1, "x", "running", 0), _step(2, "x", "pending")], t0 + timedelta(seconds=5))
    assert early["eta"] is None and 0 < early["fraction"] < 1
    done = progress.estimate([_step(1, "x", "done", 0, 30)], t0 + timedelta(seconds=30))
    assert done["fraction"] == 1.0 and done["eta"] == 0


def test_prune_keeps_recent_runs_only(conn):
    for _ in range(progress.KEEP_RUNS + 3):
        rid = store.start_run(conn, "cli", "all", True)
        progress.Progress(conn, rid).plan([progress.StepSpec("a", "read", "x", "@a")])
    progress.prune(conn)
    kept = conn.execute("SELECT COUNT(DISTINCT run_id) FROM run_steps").fetchone()[0]
    assert kept == progress.KEEP_RUNS


# ---------------------------------------------------------------- pages

@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.state.launched = []
    app.state.launch_scan = lambda artist_id=None: app.state.launched.append(artist_id)
    c = TestClient(app)
    c.post("/login", data={"password": "x" * 12, "next": "/"})
    return c


def test_progress_endpoint_and_pages(client, settings, conn):
    assert client.get("/scan/progress?after=0").json() == {"waiting": True}
    r = client.post("/scan", follow_redirects=False)
    assert r.headers["location"] == "/scans?started=0"
    page = client.get("/scans?started=0").text
    assert "data-progress" in page and 'data-after="0"' in page and "Starting the scan" in page

    add(conn, "Reol", "x.com/RRReol")
    run_id, _ = run_scan(settings, conn, FakeApify([tweet(5, "rrreol", "MIKIROCK")]),
                         FakeClassifier({"MIKIROCK": [show("MIKIROCK", "2027-02-11", "Tokyo", "Garden Theater")]}))
    snap = client.get("/scan/progress?after=0").json()
    assert snap["run"]["id"] == run_id and snap["run"]["status"] == "ok"
    assert {s["kind"] for s in snap["steps"]} == {"x", "claude", "alerts"}
    assert snap["totals"]["posts"] == 1 and snap["totals"]["shows"] == 1 and snap["estimate"]["fraction"] == 1.0
    again = client.get(f"/scan/progress?run={run_id}&rev={snap['rev']}").json()
    assert again["steps"] == []

    detail = client.get(f"/scans/{run_id}")
    assert detail.status_code == 200 and "data-progress-init" in detail.text and "@RRReol" in detail.text
    assert client.get("/scans/999").status_code == 404
    assert f'href="/scans/{run_id}"' in client.get("/scans").text


def test_scan_page_shows_what_it_read_and_what_claude_decided(client, settings, conn):
    add(conn, "Reol", "x.com/RRReol", "https://reol.jp/live/")
    clf = FakeClassifier({"MIKIROCK": [show("MIKIROCK", "2027-02-11", "Tokyo", "Garden Theater")]})
    run_id, _ = run_scan(settings, conn, FakeApify([tweet(5, "rrreol", "MIKIROCK 出演決定"),
                                                    tweet(6, "rrreol", "new single out")]), clf,
                         {"https://reol.jp/live/": "Reol LIVE " + "x" * 400})
    page = client.get(f"/scans/{run_id}").text
    assert "What this scan read" in page and "3 posts and page versions · 1 about a show" in page
    assert "data-data-body" in page and "MIKIROCK 出演決定" not in page             # collapsed: loaded when opened
    first = client.get(f"/scans/{run_id}/data").text
    assert first.index("MIKIROCK 出演決定") < first.index("new single out")       # shows first
    assert "→ Thu 11 Feb · Tokyo" in first and ">Show<" in first and ">Not a show<" in first
    assert "Show all" not in first and "<details class=\"post-text\">" in first      # the long page is folded
    only = client.get(f"/scans/{run_id}/data?show=relevant").text
    assert "MIKIROCK" in only and "new single" not in only
    assert client.get("/scans/999/data").status_code == 404


def test_scans_page_shows_live_panel_while_running(client, settings, conn):
    run_id = store.start_run(conn, "cron", "all", False)
    progress.Progress(conn, run_id).now("Apify: reading X @yoasobi_staff (3 of 29 accounts)")
    page = client.get("/scans").text
    assert f'data-run="{run_id}"' in page and "@yoasobi_staff (3 of 29 accounts)" in page
    assert "Scan everyone now" not in page
