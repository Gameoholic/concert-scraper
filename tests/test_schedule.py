"""Scan every N hours (set in the app); the hourly cron exits quietly when no scan is due; Instagram interval."""

import logging
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.config import Settings
from app.main import create_app
from app.watch import Scan
from tests.test_watch import AT, FakeApify, FakeClassifier, add, fake_fetch

T = datetime(2026, 11, 18, 0, 0, 30, tzinfo=timezone.utc)     # 09:00:30 JST


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "s.db")


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def cron_run(conn, at):
    run_id = store.start_run(conn, "cron", "all", False)
    with conn:
        conn.execute("UPDATE runs SET started_at = ?, status = 'ok' WHERE id = ?", (at.isoformat(), run_id))


def test_defaults_come_from_env_until_saved(settings, conn):
    assert store.prefs(conn, settings) == store.Prefs(12, 0)             # Instagram: no cooldown, like X
    store.save_prefs(conn, scan_every_hours=6, instagram_cooldown_hours=24)
    assert store.prefs(conn, settings) == store.Prefs(6, 24)


def test_due_every_n_hours_since_the_last_scheduled_scan(settings, conn):
    p = store.Prefs(12, 24)
    assert store.scan_due(conn, p, T)                                       # never ran: due
    assert store.next_scheduled(conn, p, T) == T.replace(minute=0, second=0) + timedelta(hours=1)
    cron_run(conn, T)
    assert not store.scan_due(conn, p, T + timedelta(hours=11))
    assert store.scan_due(conn, p, T + timedelta(hours=11, minutes=59, seconds=30))   # the 21:00 cron tick
    assert store.next_scheduled(conn, p, T + timedelta(hours=1)) == datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    store.start_run(conn, "ui", "all", False)                                # a scan from the app doesn't move it
    assert not store.scan_due(conn, p, T + timedelta(hours=3))
    assert store.scan_due(conn, store.Prefs(3, 24), T + timedelta(hours=3))


def test_hourly_cron_exits_quietly_when_not_due(settings, conn, monkeypatch, tmp_path):
    from app import alerts, watch
    monkeypatch.setenv("DATABASE_PATH", str(settings.database_path))
    calls = []
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: calls.append(a))
    cron_run(conn, datetime.now(timezone.utc) - timedelta(hours=2))
    before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert watch.main(["--trigger", "cron"]) == 0
    finally:
        for h in root.handlers:
            if h not in handlers:
                h.close()
        root.handlers[:] = handlers
        root.setLevel(level)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before
    assert calls == [] and not (settings.database_path.parent / "logs" / "watch-s.log").exists()


def test_skipped_instagram_source_drops_its_stale_error(settings, conn):
    eve = add(conn, "Eve", "instagram.com/eve_harapeco")
    store.save_prefs(conn, instagram_cooldown_hours=24)
    with conn:
        conn.execute("UPDATE sources SET cursor = ?, last_scanned_at = ?, last_status = 'error', "
                     "last_error = 'Instagram: Empty or private data for provided input'",
                     ((AT - timedelta(hours=6)).isoformat(), AT.isoformat()))
    run_id = store.start_run(conn, "cli", "all", False)
    Scan(settings, conn, run_id, apify=FakeApify(), classifier=FakeClassifier({}), fetch=fake_fetch({}), at=AT).run()
    src = db.get_artist(conn, eve).sources[0]
    assert src.last_status is None and src.last_error is None
    assert store.ig_wait_hours(src.cursor, 24, AT) == pytest.approx(18 - 10 / 60)


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.state.launch_scan = lambda artist_id=None, resume=None: None
    c = TestClient(app)
    c.post("/login", data={"password": "x" * 12, "next": "/"})
    return c


def test_scans_page_schedule_card(client, settings, conn):
    page = client.get("/scans").text
    assert "Every 12 hours, or now. Next scheduled scan:" in page and 'name="scan_every_hours"' in page
    assert 'name="instagram_cooldown_hours"' in page and "Instagram cooldown" in page and "about $3.60 a month" in page
    r = client.post("/scans/schedule", data={"scan_every_hours": "6", "instagram_cooldown_hours": "48"},
                    follow_redirects=False)
    assert r.status_code == 303 and store.prefs(conn, settings) == store.Prefs(6, 48)
    page = client.get("/scans").text
    assert "Saved. Next scheduled scan:" in page and "Every 6 hours, or now." in page
    bad = client.post("/scans/schedule", data={"scan_every_hours": "0", "instagram_cooldown_hours": "x"})
    assert store.prefs(conn, settings) == store.Prefs(6, 48)                 # refused, nothing changed
    assert "Scan every: 1 to 168 hours." in bad.text and "Instagram cooldown: enter whole hours." in bad.text


def test_artist_page_says_when_instagram_is_read_next(client, settings, conn):
    eve = add(conn, "Eve", "instagram.com/eve_harapeco")
    store.save_prefs(conn, instagram_cooldown_hours=24)
    now = datetime.now(timezone.utc)
    with conn:
        conn.execute("UPDATE sources SET cursor = ?, last_scanned_at = ?, last_status = 'error', "
                     "last_error = 'Stale placeholder error'", ((now - timedelta(hours=3)).isoformat(), now.isoformat()))
    page = client.get(f"/artists/{eve}").text
    assert "not read this scan, next read in about 21 h (Instagram cooldown 24 h)" in page
    assert "Stale placeholder error" not in page and "Stale placeholder error" not in client.get("/scans").text
