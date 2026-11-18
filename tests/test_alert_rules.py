"""The Alerts settings: ping / post silently / don't post per fit, which updates alert, what a message includes;
and the "scan finished" notice listing each error with its artist and source, X first."""

import pytest
from fastapi.testclient import TestClient

from app import alerts, db, store
from app.config import Settings
from app.fit import Segment
from app.main import create_app
from app.scan_web import FetchError
from app.watch import Scan
from tests.test_watch import AT, FakeApify, FakeClassifier, OkClient, add, fake_fetch, rnd, show, tweet

HOOK = "https://discord.example/api/webhooks/1/x"
ME = "123456789012345678"


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "rules.db",
                    discord_webhook=HOOK, discord_user_id=ME, discord_scan_notices=True)


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def scan(settings, conn, apify, clf, pages=None, client=None):
    run_id = store.start_run(conn, "cron", "all", False)
    s = Scan(settings, conn, run_id, apify=apify, classifier=clf, fetch=fake_fetch(pages or {}), at=AT,
             alert_client=client or OkClient(), alert_pause=0)
    rep = s.run()
    s.send_alerts(False)
    store.finish_run(conn, run_id, status="partial" if rep.errors else "ok", errors="\n".join(rep.errors))
    return run_id, rep


THREE = FakeClassifier({"TOUR": [
    show("FITS TOUR", "2027-02-04", "Yokohama", "KT Zepp Yokohama", [rnd("FC先行")], pref="Kanagawa"),   # Tokyo night
    show("DETOUR TOUR", "2027-02-09", "Osaka", "Zepp Osaka Bayside", [rnd("FC先行")]),                 # Nagoya night
    show("OUTSIDE TOUR", "2027-01-28", "Gifu", "長良川国際会議場", [rnd("FC先行")]),                      # before the trip
]})


def test_default_rules_ping_only_shows_that_fit(settings, conn):
    add(conn, "UVERworld", "x.com/UVERworld_dR2")
    client = OkClient()
    scan(settings, conn, FakeApify([tweet(1, "uverworld_dr2", "TOUR 2027")]), THREE, client=client)
    by_title = {body["content"].split("\n")[1]: body for _, body in client.sent}
    fits, detour, outside = by_title["**FITS TOUR**"], by_title["**DETOUR TOUR**"], by_title["**OUTSIDE TOUR**"]
    assert fits["content"].endswith(f"<@{ME}>") and fits["flags"] == 4
    for b in (detour, outside):
        assert "<@" not in b["content"] and b["flags"] == 4 | 4096 and b["allowed_mentions"] == {"parse": []}


def test_dont_post_keeps_the_show_unsent_until_it_fits(settings, conn):
    rules = alerts.Rules()
    rules.fit.update(detour=alerts.OFF, outside=alerts.OFF)
    alerts.save_rules(conn, rules)
    add(conn, "UVERworld", "x.com/UVERworld_dR2")
    client = OkClient()
    scan(settings, conn, FakeApify([tweet(1, "uverworld_dr2", "TOUR 2027")]), THREE, client=client)
    assert [b["content"].split("\n")[1] for _, b in client.sent] == ["**FITS TOUR**"]
    # the trip changes: Osaka on Feb 9, so the detour now fits and is sent on the next scan
    segs = [Segment(s.start, s.end, "Osaka" if s.city == "Nagoya" else s.city) for s in store.itinerary(conn)]
    store.save_itinerary(conn, segs)
    client2 = OkClient()
    scan(settings, conn, FakeApify([]), FakeClassifier({}), client=client2)
    assert [b["content"].split("\n")[1] for _, b in client2.sent] == ["**DETOUR TOUR**"]
    assert client2.sent[0][1]["content"].endswith(f"<@{ME}>")


def test_updates_turned_off_are_recorded_not_posted(settings, conn):
    rules = alerts.Rules()
    rules.kinds["new_show"] = False
    alerts.save_rules(conn, rules)
    add(conn, "UVERworld", "x.com/UVERworld_dR2")
    client = OkClient()
    _, rep = scan(settings, conn, FakeApify([tweet(1, "uverworld_dr2", "TOUR 2027")]), THREE, client=client)
    assert client.sent == [] and rep.alerts_sent == 0
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] > 0      # recorded, so they don't come back
    assert [m for m in alerts.build(conn, at=AT.astimezone(store.JST)) if m.postable] == []


def test_message_parts_can_be_left_out(settings, conn):
    add(conn, "UVERworld", "x.com/UVERworld_dR2")
    scan(settings, conn, FakeApify([tweet(1, "uverworld_dr2", "TOUR 2027")]), THREE, client=OkClient())
    s = next(s for s in store.load_shows(conn, at=AT.astimezone(store.JST)) if s.title == "FITS TOUR")
    full = alerts.show_message(s, ["new_show"], s.rounds, "https://c.example")
    assert "• FC先行" in full and "¥9,800" in full and "KT Zepp Yokohama" in full and "[all shows]" in full
    bare = alerts.show_message(s, ["new_show"], s.rounds, "https://c.example",
                               {"place": False, "rounds": False, "links": False})
    assert bare.split("\n") == [full.split("\n")[0], "**FITS TOUR**"]
    no_price = alerts.show_message(s, ["new_show"], s.rounds, "", {"round_extra": False})
    assert "• FC先行" in no_price and "¥9,800" not in no_price


def test_rules_parse_never_breaks_on_bad_values():
    r = alerts.Rules.parse('{"fit": {"fits": "nonsense", "detour": "off", "mars": "ping"}, "kinds": {"x": true}}')
    assert r.fit["fits"] == alerts.PING and r.fit["detour"] == alerts.OFF and "mars" not in r.fit
    assert alerts.Rules.parse("not json").fit == alerts.Rules().fit


def test_finished_notice_lists_each_error_with_artist_and_source_x_first(settings, conn):
    add(conn, "ZUTOMAYO", "x.com/zutomayo", "https://zutomayo.net/live/")
    pages = {"https://zutomayo.net/live/": FetchError("HTTP 403")}
    run_id, _ = scan(settings, conn, FakeApify(fail_x=True), FakeClassifier({}), pages)
    text = alerts.finished_text(conn, run_id)
    lines = text.split("\n")
    assert "❗ 𝕏 X: 1 of 1 account failed" in lines
    assert "2 errors: 1 X account, 1 website" in lines
    x_line = next(i for i, l in enumerate(lines) if l.startswith("• 𝕏 X · ZUTOMAYO · @zutomayo: "))
    web_line = next(i for i, l in enumerate(lines) if l.startswith("• Website · ZUTOMAYO · zutomayo.net/live: HTTP 403"))
    assert x_line < web_line
    assert alerts.finished_pings(conn, run_id)
    rules = alerts.Rules()
    rules.x_fail_ping = False
    alerts.save_rules(conn, rules)
    assert not alerts.finished_pings(conn, run_id)


def test_finished_notice_says_x_is_fine(settings, conn):
    add(conn, "ZUTOMAYO", "x.com/zutomayo", "https://zutomayo.net/live/")
    pages = {"https://zutomayo.net/live/": FetchError("HTTP 403")}
    run_id, _ = scan(settings, conn, FakeApify([]), FakeClassifier({}), pages)
    text = alerts.finished_text(conn, run_id)
    assert "𝕏 X: all 1 account OK ✅" in text and "1 error: 1 website" in text
    assert not alerts.finished_pings(conn, run_id)


def test_notify_pings_when_asked(settings):
    client = OkClient()
    alerts.notify(settings, "❗ X failed", client, ping=True)
    body = client.sent[0][1]
    assert body["content"].endswith(f"<@{ME}>") and body["flags"] == 4


def test_alerts_card_saves_and_previews(settings):
    c = TestClient(create_app(settings))
    c.post("/login", data={"password": "x" * 12, "next": "/"})
    page = c.get("/scans").text
    assert 'id="alerts"' in page and 'name="fit_fits"' in page and "Pings you" in page
    form = {"fit_fits": "ping", "fit_detour": "off", "fit_outside": "off", "fit_tba": "silent",
            "kind_new_show": "1", "kind_cancelled": "1", "line_place": "1", "x_fail_ping": "1"}
    r = c.post("/scans/alerts", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/scans#alerts"
    conn = db.connect(settings.database_path)
    try:
        rules = alerts.load_rules(conn)
    finally:
        conn.close()
    assert rules.fit["detour"] == alerts.OFF and not rules.kinds["date_set"] and not rules.lines["rounds"]
    pv = c.post("/scans/alerts/preview", data={**form, "preview_fit": "detour"}).text
    assert "Not posted" in pv and "discord-msg" not in pv
    pv = c.post("/scans/alerts/preview", data={**form, "preview_fit": "fits"}).text
    assert "Pings you" in pv and "<b>LIVE HOUSE TOUR 2027</b>" in pv and "• FC先行" not in pv
