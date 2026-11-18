"""Show alerts ping Daniel; silent "scan started / finished / stopped" notices. Stubbed webhook only."""

import logging
import os
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import alerts, db, progress, store
from app.config import Settings
from app.main import create_app
from app.watch import Scan
from tests.test_resume import dead_pid
from tests.test_watch import AT, FakeApify, FakeClassifier, OkClient, add, fake_fetch, rnd, show, tweet

HOOK = "https://discord.example/api/webhooks/1/x"


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "n.db",
                    discord_webhook=HOOK, discord_user_id="123456789012345678", discord_scan_notices=True,
                    app_url="https://concerts.example.com")


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def scan(settings, conn, client, apify, clf):
    run_id = store.start_run(conn, "cron", "all", False)
    s = Scan(settings, conn, run_id, apify=apify, classifier=clf, fetch=fake_fetch({}), at=AT,
             alert_client=client, alert_pause=0, notices=True)
    rep = s.run()
    s.send_alerts(False)
    store.finish_run(conn, run_id, status="partial" if rep.errors else "ok", posts_read=rep.posts_read,
                     shows_new=rep.shows_new, alerts_sent=rep.alerts_sent, apify_usd=0.0123, claude_usd=0.0046,
                     errors="\n".join(rep.errors))
    return run_id, rep


def test_show_alerts_ping_and_scan_notices_are_silent(settings, conn):
    add(conn, "UVERworld", "x.com/UVERworld_dR2")
    client = OkClient()
    clf = FakeClassifier({"TOUR": [show("LIVE HOUSE TOUR 2027", "2027-02-04", "Yokohama", "KT Zepp Yokohama",
                                        [rnd("FC先行")])]})
    run_id, rep = scan(settings, conn, client, FakeApify([tweet(1, "uverworld_dr2", "TOUR 2027")]), clf)

    started, alert = client.sent[0][1], client.sent[1][1]
    assert started["content"].startswith("▶️ Scan started · scheduled · every artist · 1 source (1 X)")
    assert started["flags"] == 4 | 4096 and started["allowed_mentions"] == {"parse": []}     # silent, no ping
    assert f"(<https://concerts.example.com/scans/{run_id}>)" in started["content"]

    assert alert["content"].startswith("🆕 [B] UVERworld · Thu 4 Feb")                      # first line untouched
    assert alert["content"].endswith("\n<@123456789012345678>") and alert["flags"] == 4      # pings, buzzes
    assert alert["allowed_mentions"] == {"parse": [], "users": ["123456789012345678"]}      # only him

    alerts.notify(settings, alerts.finished_text(conn, run_id, settings.app_url), client)
    done = client.sent[-1][1]
    assert done["flags"] == 4100 and "<@" not in done["content"]
    first = done["content"].split("\n")[0]
    assert first.startswith("✅ Scan done · ") and "1 new show · 0 updates · 1 alert sent" in first
    assert "1 new post and changed pages · $0.01 Apify · $0.005 Claude" in done["content"]


def test_no_user_id_means_no_ping(settings, conn):
    client = OkClient()
    alerts.send_discord(HOOK, "🆕 [A] Ado · Sun 31 Jan", client)
    body = client.sent[0][1]
    assert body["content"] == "🆕 [A] Ado · Sun 31 Jan" and body["allowed_mentions"] == {"parse": []}


def test_finished_notice_lists_errors_and_failed_status(settings, conn):
    run_id = store.start_run(conn, "ui", "artist:1,artist:2", False)
    errors = [f"Artist {i}: 3 post(s) waiting; ANTHROPIC_API_KEY is not set" for i in range(15)]
    store.finish_run(conn, run_id, status="partial", errors="\n".join(errors))
    text = alerts.finished_text(conn, run_id, "")
    assert text.startswith("⚠️ Scan done, with errors") and "\n15 errors: 15 other\n" in text
    assert text.count("\n• ") == alerts.MAX_ERROR_LINES + 1
    assert "• +3 more on the scan page" in text and text.endswith(f"scan #{run_id}")
    store.finish_run(conn, run_id, status="error")
    assert alerts.finished_text(conn, run_id).startswith("❌ Scan failed")


def test_a_dead_scan_sends_one_stopped_notice(settings, conn):
    run_id = store.start_run(conn, "ui", "all", False, pid=os.getpid())
    progress.Progress(conn, run_id).now("Website 7 of 36: lxixsxa.com/live")
    with conn:
        conn.execute("UPDATE runs SET pid = ? WHERE id = ?", (dead_pid(), run_id))
    seen = []
    store.running_run(conn, lambda c, rid: seen.append(alerts.finished_text(c, rid, stopped=True)))
    store.running_run(conn, lambda c, rid: seen.append("again"))          # already closed: no second notice
    assert len(seen) == 1 and seen[0].startswith("⛔ Scan stopped partway")
    assert "Last thing it did: Website 7 of 36: lxixsxa.com/live" in seen[0]
    assert "The scan's process ended without finishing" in seen[0]


def test_the_web_app_posts_the_stopped_notice_when_it_notices(settings, conn):
    app = create_app(settings)
    posted = []
    app.state.notify = lambda s, text, client=None: posted.append(text)
    c = TestClient(app)
    c.post("/login", data={"password": "x" * 12, "next": "/"})
    run_id = store.start_run(conn, "ui", "all", False, pid=dead_pid())
    c.get(f"/scan/progress?run={run_id}")
    for _ in range(50):
        if posted:
            break
        time.sleep(0.02)
    assert len(posted) == 1 and posted[0].startswith("⛔ Scan stopped partway")
    c.get("/scans")
    time.sleep(0.05)
    assert len(posted) == 1


def test_dry_run_and_switched_off_post_nothing(settings, conn, monkeypatch):
    from app import watch
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", HOOK)
    calls = []
    monkeypatch.setattr(alerts, "notify", lambda *a, **k: calls.append(a))
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        assert watch.main(["--dry-run"]) == 0
    finally:
        for h in root.handlers:
            if h not in handlers:
                h.close()
        root.handlers[:] = handlers
        root.setLevel(level)
    assert calls == []
    off = replace(settings, discord_scan_notices=False)
    client = OkClient()
    assert alerts.notify(off, "x", client) is None and client.sent == []
