import re

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import ConfigError, Settings
from app.main import create_app

PASSWORD = "test-password-123"
SECRET = "s" * 40


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password=PASSWORD, secret_key=SECRET, database_path=tmp_path / "t.db")


@pytest.fixture
def client(settings):
    c = TestClient(create_app(settings))
    r = c.post("/login", data={"password": PASSWORD, "next": "/"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def artist_id(settings, name):
    conn = db.connect(settings.database_path)
    try:
        return conn.execute("SELECT id FROM artists WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def test_refuses_example_secrets(tmp_path):
    with pytest.raises(ConfigError):
        create_app(Settings(app_password="change-me", secret_key=SECRET, database_path=tmp_path / "x.db"))
    with pytest.raises(ConfigError):
        create_app(Settings(app_password=PASSWORD, secret_key="short", database_path=tmp_path / "x.db"))


def test_login_required_and_wrong_password(settings):
    c = TestClient(create_app(settings))
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.get("/healthz").json() == {"ok": True}


def test_lockout_after_five_failures(settings):
    c = TestClient(create_app(settings))
    for _ in range(5):
        c.post("/login", data={"password": "nope"})
    r = c.post("/login", data={"password": PASSWORD})
    assert r.status_code == 429


def test_open_redirect_blocked(settings):
    c = TestClient(create_app(settings))
    r = c.post("/login", data={"password": PASSWORD, "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_cross_site_post_refused(client):
    r = client.post("/artists", data={"name": "X", "tier": "A"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_seeded_list(client):
    html = client.get("/").text
    for name in ("Phantom Siita", "Hiroyuki Sawano", "MYTH &amp; ROID", "Reol", "TRUE"):
        assert name in html
    assert len(re.findall(r'class="row( paused)?"', html)) == 29
    # all 29 are missing everything
    assert re.findall(r'class="miss-n">(\d+)<', html) == ["29", "29", "29"]


def test_seed_is_idempotent(settings, client):
    conn = db.connect(settings.database_path)
    try:
        assert db.seed(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 29
    finally:
        conn.close()


def test_add_edit_pause_delete_artist(client, settings):
    r = client.post("/artists", data={"name": "  Test   Band ", "name_ja": "テスト", "tier": "b"},
                    follow_redirects=False)
    assert r.status_code == 303
    aid = artist_id(settings, "Test Band")
    assert "テスト" in client.get(f"/artists/{aid}").text

    assert client.post("/artists", data={"name": "test band", "tier": "A"}).status_code == 400

    r = client.post(f"/artists/{aid}", data={"name": "Test Band", "name_ja": "", "tier": "S", "notes": "FC only"},
                    headers={"X-Requested-With": "fetch"})
    assert r.json() == {"ok": True}
    home = client.get("/").text
    assert "1 paused" in home  # enabled not sent -> paused

    r = client.post(f"/artists/{aid}", data={"name": "LiSA", "tier": "S"}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400 and "already" in r.json()["error"]

    client.post(f"/artists/{aid}/delete")
    assert client.get(f"/artists/{aid}").status_code == 404


def test_paste_flow(client, settings):
    aid = artist_id(settings, "Hiroyuki Sawano")
    paste = "https://x.com/sawano_staff?s=20\n@sawano_lab\nsawano.example.jp/live/\ninstagram.com/p/abc"

    preview = client.post(f"/artists/{aid}/sources/preview", data={"paste": paste}).text
    assert "Bare handle" in preview and "Add 3 sources" in preview and "A post link" in preview

    # without a pick for the bare handle, nothing is saved
    r = client.post(f"/artists/{aid}/sources", data={"paste": paste})
    assert r.status_code == 400 and "Pick Instagram or X for @sawano_lab" in r.text

    r = client.post(f"/artists/{aid}/sources", data={"paste": paste, "kind_1": "instagram", "kind_2": "website"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/artists/{aid}").text
    assert "@sawano_staff" in page and "@sawano_lab" in page and "sawano.example.jp/live" in page
    assert "Schedule" in page and page.count("Not scanned yet") == 3

    # pasting again: all duplicates
    preview = client.post(f"/artists/{aid}/sources/preview", data={"paste": "x.com/SAWANO_STAFF"}).text
    assert "Already added" in preview
    assert client.post(f"/artists/{aid}/sources", data={"paste": "x.com/sawano_staff"}).status_code == 400

    home = client.get("/").text
    assert re.findall(r'class="miss-n">(\d+)<', home) == ["28", "28", "28"]
    assert "Sawano" not in client.get("/?missing=x").text.split("Tier S")[-1].split("Tier A")[0]


def test_edit_and_remove_source(client, settings):
    aid = artist_id(settings, "Eve")
    client.post(f"/artists/{aid}/sources", data={"paste": "eve.example.jp\n@eve_x", "kind_1": "x"})
    conn = db.connect(settings.database_path)
    try:
        sid, = conn.execute("SELECT id FROM sources WHERE artist_id = ? AND kind = 'website'", (aid,)).fetchone()
        xid, = conn.execute("SELECT id FROM sources WHERE artist_id = ? AND kind = 'x'", (aid,)).fetchone()
    finally:
        conn.close()

    client.post(f"/sources/{sid}", data={"kind": "fanclub", "label": "FC"})
    client.post(f"/sources/{xid}", data={"kind": "website", "label": ""})  # social can't become a page
    a = db.get_artist(db.connect(settings.database_path), aid)
    assert {(s.kind, s.label) for s in a.sources} == {("fanclub", "FC"), ("x", "")}

    client.post(f"/sources/{sid}/delete")
    a = db.get_artist(db.connect(settings.database_path), aid)
    assert [s.kind for s in a.sources] == ["x"]


def test_search_folds_width_and_case(client):
    html = client.get("/?q=ｙｏａｓｏｂｉ").text
    assert "YOASOBI" in html and "Hiroyuki Sawano" not in html
