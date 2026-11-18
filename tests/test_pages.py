"""Step 2 pages: shows, trip, scans, scan buttons."""

import pytest
from fastapi.testclient import TestClient

from app import db, store
from app.classify import Extraction, Round, Show
from app.config import Settings
from app.main import create_app

PASSWORD = "test-password-123"


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password=PASSWORD, secret_key="s" * 40, database_path=tmp_path / "p.db")


@pytest.fixture
def client(settings):
    app = create_app(settings)
    app.state.launched = []
    app.state.launch_scan = lambda artist_id=None: app.state.launched.append(artist_id)
    c = TestClient(app)
    c.post("/login", data={"password": PASSWORD, "next": "/"})
    return c


def seed_show(settings, name="UVERworld", d="2027-02-04", city="Yokohama", closes="2027-01-15T23:59"):
    conn = db.connect(settings.database_path)
    try:
        aid = conn.execute("SELECT id FROM artists WHERE name = ?", (name,)).fetchone()[0]
        ex = Extraction(items=[], follow_urls=[], shows=[Show(
            existing_show_id=None, title="LIVE HOUSE TOUR 2027", date=d, time_note="17:30 / 18:30",
            venue="KT Zepp Yokohama", city=city, prefecture="Kanagawa", in_japan=True, kind="tour", cancelled=False,
            items=[], notes="", rounds=[Round(name="FC先行", kind="fanclub", lottery=True, opens_at="2026-11-06T12:00",
                                              closes_at=closes, results_at=None, url="https://fc.example/apply",
                                              price="¥9,800", notes="Neo SOUND WAVE")])])
        store.merge(conn, aid, ex, {}, (store.date(2027, 1, 1), store.date(2027, 2, 28)),
                    at=store.datetime(2026, 11, 14, 12, tzinfo=store.JST))
        return aid, conn.execute("SELECT id FROM shows").fetchone()[0]
    finally:
        conn.close()


def test_pages_need_login(settings):
    c = TestClient(create_app(settings))
    for path in ("/shows", "/trip", "/scans"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")


def test_shows_page_lists_show_with_fit_and_round(client, settings):
    seed_show(settings)
    r = client.get("/shows")
    assert r.status_code == 200
    assert "LIVE HOUSE TOUR 2027" in r.text and "KT Zepp Yokohama" in r.text
    assert "fit-fits" in r.text and "FC先行" in r.text and "https://fc.example/apply" in r.text
    assert "Fits <span>1</span>" in r.text
    assert "No shows found" not in client.get("/shows?fit=fits").text
    assert "Nothing here with this filter" in client.get("/shows?fit=detour").text


def test_empty_shows_page_explains_next_step(client):
    assert "No shows found yet" in client.get("/shows").text


def test_hide_and_unhide_show(client, settings):
    _, sid = seed_show(settings)
    client.post(f"/shows/{sid}/hide", data={"hide": "1", "back": "/shows"})
    assert "LIVE HOUSE TOUR 2027" not in client.get("/shows").text
    assert "LIVE HOUSE TOUR 2027" in client.get("/shows?hidden=1").text
    client.post(f"/shows/{sid}/hide", data={"hide": "0", "back": "//evil.example"})
    assert "LIVE HOUSE TOUR 2027" in client.get("/shows").text


def test_artist_page_shows_its_shows_and_scan_button(client, settings):
    aid, _ = seed_show(settings)
    r = client.get(f"/artists/{aid}")
    assert "LIVE HOUSE TOUR 2027" in r.text and f'action="/artists/{aid}/scan"' in r.text


def test_trip_edit_changes_fit(client, settings):
    seed_show(settings)
    r = client.get("/trip")
    assert r.status_code == 200 and 'value="Nagoya"' in r.text
    # move to Osaka for the whole trip: the Yokohama show becomes a detour
    r = client.post("/trip", data={"start": ["2027-01-29"], "end": ["2027-02-28"], "city": ["Osaka"]})
    assert r.status_code == 200 and "Trip saved" in r.text
    assert "fit-detour" in client.get("/shows").text
    bad = client.post("/trip", data={"start": ["2027-02-05", "2027-02-04"], "end": ["2027-02-10", "2027-02-06"],
                                     "city": ["Tokyo", "Osaka"]})
    assert bad.status_code == 400 and "overlap" in bad.text


def test_scan_buttons_start_a_scan_unless_one_runs(client, settings):
    aid, _ = seed_show(settings)
    client.post("/scan")
    client.post(f"/artists/{aid}/scan")
    assert client.app.state.launched == [None, aid]
    conn = db.connect(settings.database_path)
    store.start_run(conn, "cron", "all", False)
    conn.close()
    r = client.post("/scan")
    assert "already running" in r.text and client.app.state.launched == [None, aid]
    assert client.get("/scan/status").json()["running"] is True


def test_scans_page_shows_setup_and_errors(client, settings):
    conn = db.connect(settings.database_path)
    aid = conn.execute("SELECT id FROM artists WHERE name = 'Eve'").fetchone()[0]
    with conn:
        conn.execute("INSERT INTO sources (artist_id, kind, url, created_at, last_scanned_at, last_status, last_error) "
                     "VALUES (?, 'website', 'https://eveofficial.com/live', 'x', 'x', 'error', 'HTTP 503')", (aid,))
    conn.close()
    r = client.get("/scans")
    assert r.status_code == 200 and "HTTP 503" in r.text and "not set in .env" in r.text
