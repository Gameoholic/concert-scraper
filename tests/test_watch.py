"""The scan pipeline end to end with fake Apify, Claude and web: dedup, merge, alerts, failures."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app import alerts, db, store
from app.classify import CallResult, ClassifyError, Extraction, ItemVerdict, Round, Show
from app.config import Settings
from app.scan_web import FetchError, Page
from app.sources import parse_paste
from app.watch import Scan

AT = datetime(2026, 11, 14, 3, 0, tzinfo=timezone.utc)   # 12:00 JST


@pytest.fixture
def settings(tmp_path):
    return Settings(app_password="x" * 12, secret_key="s" * 40, database_path=tmp_path / "w.db",
                    discord_webhook="https://discord.example/api/webhooks/1/x")


@pytest.fixture
def conn(settings):
    db.init_db(settings.database_path)
    c = db.connect(settings.database_path)
    yield c
    c.close()


def add(conn, name, *lines, picks=None):
    aid = conn.execute("SELECT id FROM artists WHERE name = ?", (name,)).fetchone()[0]
    cands = parse_paste("\n".join(lines))
    for i, c in enumerate(cands):
        if c.status == "ambiguous":
            c.kind = (picks or {}).get(i, "x")
            c.url = f"https://x.com/{c.handle.lower()}"
    db.add_sources(conn, aid, cands)
    return aid


def rnd(name, kind="fanclub", closes="2026-11-15T23:59", opens="2026-11-06T12:00", url="https://fc.example/apply"):
    return Round(name=name, kind=kind, lottery=True, opens_at=opens, closes_at=closes, results_at=None, url=url,
                 price="¥9,800", notes="")


def show(title, d, city, venue, rounds=(), items=(1,), existing=None, japan=True, pref=""):
    return Show(existing_show_id=existing, title=title, date=d, time_note="17:30 / 18:30", venue=venue, city=city,
                prefecture=pref, in_japan=japan, kind="tour", cancelled=False, rounds=list(rounds), items=list(items),
                notes="")


class FakeApify:
    def __init__(self, tweets=(), ig=(), fail_x=False):
        self.tweets, self.ig, self.fail_x = list(tweets), list(ig), fail_x
        self.calls = []

    def run(self, actor, run_input, max_items):
        self.calls.append((actor, run_input))
        if "twitter" in actor:
            if self.fail_x:
                raise RuntimeError("actor exploded")
            return self.tweets, 0.01
        return self.ig, 0.02


class FakeClassifier:
    """Answers from a table: text substring -> list of shows."""

    def __init__(self, table, fail_on=None):
        self.table, self.fail_on, self.calls = table, fail_on, []

    def extract(self, artist, today, window, known, items):
        self.calls.append((artist, [i.text for i in items], known))
        if self.fail_on and any(self.fail_on in i.text for i in items):
            raise ClassifyError("Claude API 529: overloaded")
        shows, verdicts = [], []
        for it in items:
            found = [s.model_copy(update={"items": [it.number]}) for key, ss in self.table.items()
                     if key in it.text for s in ss]
            shows += found
            verdicts.append(ItemVerdict(item=it.number, relevant=bool(found), note="show" if found else "other"))
        return CallResult(Extraction(items=verdicts, shows=shows, follow_urls=[]), 0.001)


def fake_fetch(pages):
    def fetch(url, rendered_before=False, browser=None, probe=False):
        v = pages.get(url)
        if isinstance(v, Exception):
            raise v
        if v is None:
            raise FetchError("HTTP 404")
        return Page(url, v, False)
    return fetch


def tweet(tid, user, text, when="Fri Nov 06 03:00:00 +0000 2026", media=()):
    return {"id": str(tid), "author": {"userName": user}, "text": text, "createdAt": when,
            "url": f"https://x.com/{user}/status/{tid}",
            "extendedEntities": {"media": [{"media_url_https": m} for m in media]}}


def scan(settings, conn, apify=None, classifier=None, pages=None, artist_ids=None, at=AT):
    run_id = store.start_run(conn, "cli", "all", False)
    s = Scan(settings, conn, run_id, apify=apify, classifier=classifier, fetch=fake_fetch(pages or {}), at=at)
    rep = s.run(artist_ids)
    store.finish_run(conn, run_id, status="ok")
    return rep


UVER_TOUR = [
    show("LIVE HOUSE TOUR 2027", "2027-02-04", "Yokohama", "KT Zepp Yokohama", [rnd("FC先行")], pref="Kanagawa"),
    show("LIVE HOUSE TOUR 2027", "2027-02-09", "Osaka", "Zepp Osaka Bayside", [rnd("FC先行")]),
    show("LIVE HOUSE TOUR 2027", "2027-03-03", "Nagoya", "Zepp Nagoya", [rnd("FC先行")]),   # outside window
    show("Asia tour", "2027-02-10", "Seoul", "?", japan=False),                             # abroad
]


def only(conn, sql, *args):
    return conn.execute(sql, args).fetchone()[0]


def test_full_scan_finds_merges_and_alerts_once(settings, conn):
    uver = add(conn, "UVERworld", "x.com/UVERworld_dR2", "https://www.uverworld.jp/feature/2027_live")
    apify = FakeApify(tweets=[tweet(1, "uverworld_dr2", "TOUR 2027 決定! FC先行 https://t.co/x"),
                              tweet(2, "uverworld_dr2", "new single out"),
                              {"id": -1, "type": "mock_tweet"}])
    clf = FakeClassifier({"TOUR 2027": UVER_TOUR, "ツアー": UVER_TOUR})
    pages = {"https://www.uverworld.jp/feature/2027_live": "UVERworld ツアー 2.4 KT Zepp Yokohama ..."}
    rep = scan(settings, conn, apify, clf, pages)

    # two in-window Japan dates, merged across the tweet and the page
    assert only(conn, "SELECT COUNT(*) FROM shows") == 2
    assert only(conn, "SELECT COUNT(*) FROM ticket_rounds") == 2
    assert only(conn, "SELECT COUNT(*) FROM show_posts") == 4
    assert rep.shows_new == 2
    assert only(conn, "SELECT relevant FROM posts WHERE external_id = '2'") == 0

    # X was asked for a look-back from:… query
    query = apify.calls[0][1]["searchTerms"][0]
    assert "from:uverworld_dr2" in query and "since_time:" in query

    # one message per show; the first line is the phone notification: artist, date, city, fit, deadline
    assert [m.first_line for m in rep.messages] == [
        "🆕 [B] UVERworld · Thu 4 Feb · Yokohama · ✅ fits · ⏰ FC先行 until Sun 15 Nov 23:59",
        "🆕 [B] UVERworld · Tue 9 Feb · Osaka · ↪️ detour · ⏰ FC先行 until Sun 15 Nov 23:59",
    ]
    m = rep.messages[0]
    assert "KT Zepp Yokohama" in m.text and "OPEN NOW" in m.text
    assert "[apply / info](<https://fc.example/apply>)" in m.text and "[X](<https://x.com/uverworld_dr2/status/1>)" in m.text

    client = OkClient()
    sent, errors = alerts.deliver(conn, settings, rep.messages, client=client, pause=0)
    assert sent == 2 and not errors
    url, body = client.sent[0]
    assert url == settings.discord_webhook and body["content"] == m.text and body["flags"] == 4

    # source status
    src = {s.kind: s for s in db.get_artist(conn, uver).sources}
    assert src["x"].last_status == "ok" and src["x"].last_posts == 2 and src["x"].last_found == 2
    assert src["x"].cursor and src["website"].last_status == "ok"

    # second scan: same posts and unchanged page -> nothing new read, nothing alerted again
    rep2 = scan(settings, conn, apify, clf, pages)
    assert rep2.posts_read == 0 and rep2.messages == []
    assert "since_time:" in apify.calls[-1][1]["searchTerms"][0]


def test_new_round_and_deadline_alert_on_known_show(settings, conn):
    add(conn, "Reol", "x.com/RRReol")
    first = show("MIKIROCK PARTY Day 2", "2027-02-11", "Tokyo", "Tokyo Garden Theater",
                 [rnd("e+ 最速先行", "playguide")])
    clf = FakeClassifier({"MIKIROCK": [first]})
    rep = scan(settings, conn, FakeApify([tweet(10, "rrreol", "MIKIROCK 出演")]), clf)
    alerts.deliver(conn, settings, rep.messages, client=OkClient(), pause=0)

    sid = only(conn, "SELECT id FROM shows")
    later = show("MIKIROCK PARTY Day2", "2027-02-11", "Tokyo", "Tokyo Garden Theater",
                 [rnd("e+最速先行", "playguide", closes="2026-11-16T23:59"),          # deadline moved
                  rnd("ぴあ先行", "playguide", opens="2026-11-20T12:00", closes="2026-11-30T23:59"),
                  rnd("FC先行 (closed)", closes="2026-10-01")],                        # already past: stored quietly
                 existing=sid)
    clf.table = {"追加": [later]}
    rep = scan(settings, conn, FakeApify([tweet(11, "rrreol", "MIKIROCK 追加先行")]), clf)
    kinds = sorted(e.kind for e in rep.events)
    assert kinds == ["deadline", "new_round"]
    assert only(conn, "SELECT COUNT(*) FROM shows") == 1
    assert only(conn, "SELECT COUNT(*) FROM ticket_rounds") == 3
    text = rep.messages[0].text
    assert "ぴあ先行" in text and "⏰" in text and "FC先行 (closed)" not in text
    # known shows were given to Claude for matching
    assert clf.calls[-1][2][0].id == sid


def test_tba_show_gets_its_date_later(settings, conn):
    add(conn, "Hiroyuki Sawano", "x.com/sawano_nZk")
    clf = FakeClassifier({"010": [show("LIVE [nZk]010", None, "", "")]})
    rep = scan(settings, conn, FakeApify([tweet(20, "sawano_nzk", "LIVE [nZk]010 開催決定")]), clf)
    assert [e.kind for e in rep.events] == ["new_show"]
    assert "❔ date TBA" in rep.messages[0].text
    alerts.deliver(conn, settings, rep.messages, client=OkClient(), pause=0)
    clf.table = {"日程": [show("LIVE [nZk]010", "2027-02-14", "Sapporo", "Zepp Sapporo")]}
    rep = scan(settings, conn, FakeApify([tweet(21, "sawano_nzk", "[nZk]010 日程発表")]), clf)
    assert [e.kind for e in rep.events] == ["date_set"]
    assert only(conn, "SELECT date FROM shows") == "2027-02-14"


def test_one_source_failing_does_not_stop_the_others(settings, conn):
    zut = add(conn, "ZUTOMAYO", "x.com/zutomayo", "https://zutomayo.net/apicula/", "https://zutomayo.net/broken")
    clf = FakeClassifier({"アピクラ": [show("アピクラ電機", "2027-02-15", "Kobe", "神戸国際会館", pref="Hyogo")]})
    pages = {"https://zutomayo.net/apicula/": "HALL TOUR 2027 アピクラ電機 2/15 神戸",
             "https://zutomayo.net/broken": FetchError("HTTP 500")}
    rep = scan(settings, conn, FakeApify(fail_x=True), clf, pages)
    assert only(conn, "SELECT COUNT(*) FROM shows") == 1
    srcs = {s.url: s for s in db.get_artist(conn, zut).sources}
    assert srcs["https://x.com/zutomayo"].last_status == "error"
    assert "actor exploded" in srcs["https://x.com/zutomayo"].last_error
    assert srcs["https://zutomayo.net/broken"].last_status == "error"
    assert srcs["https://zutomayo.net/apicula/"].last_status == "ok"
    assert len(rep.errors) == 2


def test_classifier_failure_keeps_posts_for_next_scan(settings, conn):
    add(conn, "TRUE", "x.com/true_singer")
    clf = FakeClassifier({"ユーフォ": [show("吹奏楽コンサート", "2027-02-20", "Kyoto", "ROHM Theatre Kyoto")]},
                         fail_on="ユーフォ")
    rep = scan(settings, conn, FakeApify([tweet(30, "true_singer", "ユーフォ 定期演奏会")]), clf)
    assert rep.errors and only(conn, "SELECT COUNT(*) FROM posts WHERE relevant IS NULL") == 1
    clf.fail_on = None
    rep = scan(settings, conn, FakeApify([]), clf)
    assert only(conn, "SELECT COUNT(*) FROM shows") == 1 and rep.shows_new == 1


def test_missing_keys_are_reported_not_fatal(settings, conn):
    add(conn, "Eve", "x.com/oO0Eve0Oo", "https://eveofficial.com/live/")
    rep = scan(settings, conn, None, None, {"https://eveofficial.com/live/": "Eve live: 廻帰 tour from April"})
    assert any("APIFY_API_TOKEN" in e for e in rep.errors)
    assert any("ANTHROPIC_API_KEY" in e for e in rep.errors)
    assert only(conn, "SELECT COUNT(*) FROM posts") == 1        # the page was still read


def test_hidden_show_gets_no_alerts(settings, conn):
    add(conn, "LiSA", "x.com/LiSA_OLiVE")
    clf = FakeClassifier({"LACE": [show("LACE UP", "2027-01-28", "Nagoya", "Nagoya")]})
    scan(settings, conn, FakeApify([tweet(40, "lisa_olive", "LACE UP tour")]), clf)
    store.set_hidden(conn, only(conn, "SELECT id FROM shows"), True)
    clf.table = {"追加": [show("LACE UP", "2027-01-28", "Nagoya", "Nagoya", [rnd("一般", "general")])]}
    rep = scan(settings, conn, FakeApify([tweet(41, "lisa_olive", "LACE UP 一般 追加")]), clf)
    assert rep.events and rep.messages == []


def test_no_alert_channel_keeps_alerts(settings, conn):
    add(conn, "Ado", "x.com/ado1024imokenp")
    clf = FakeClassifier({"ツアー": [show("歌ってみたツアー", "2027-01-31", "Osaka", "Asueアリーナ大阪")]})
    rep = scan(settings, conn, FakeApify([tweet(50, "ado1024imokenp", "ツアー")]), clf)
    bare = Settings(app_password="x" * 12, secret_key="s" * 40, database_path=settings.database_path)
    sent, errors = alerts.deliver(conn, bare, rep.messages)
    assert sent == 0 and "DISCORD_WEBHOOK_URL is not set" in errors[0]
    assert only(conn, "SELECT COUNT(*) FROM alerts") == 0


def test_sort_puts_open_deadlines_first(settings, conn):
    add(conn, "UVERworld", "x.com/UVERworld_dR2")
    add(conn, "MYTH & ROID", "x.com/MYTH_and_ROID")
    clf = FakeClassifier({"TOUR": UVER_TOUR[:1],
                          "FINAL": [show("FINAL", "2027-02-06", "Yokohama", "KT Zepp Yokohama",
                                         [rnd("一般", "general", opens="2026-10-01", closes="2027-02-05T18:00")])],
                          "TBA": [show("Something 2027", None, "", "")]})
    scan(settings, conn, FakeApify([tweet(60, "uverworld_dr2", "TOUR"), tweet(61, "myth_and_roid", "FINAL TBA")]), clf)
    groups = store.sort_shows(store.load_shows(conn, at=AT.astimezone(store.JST)))
    assert [s.title for s in groups["deadlines"]] == ["LIVE HOUSE TOUR 2027", "FINAL"]
    assert [s.title for s in groups["tba"]] == ["Something 2027"]


class OkClient:
    def __init__(self):
        self.sent = []

    def post(self, url, json):
        self.sent.append((url, json))

        class R:
            status_code, text = 200, "ok"
        return R()


def test_window_from_itinerary(conn):
    assert store.itinerary(conn)[0].start == date(2027, 1, 29)


def test_source_shows_posts_waiting_for_claude(settings, conn):
    eve = add(conn, "Eve", "https://eveofficial.com/live/")
    scan(settings, conn, None, None, {"https://eveofficial.com/live/": "Eve live: 廻帰 tour"})
    src = db.get_artist(conn, eve).sources[0]
    assert src.last_status == "error" and "not checked by Claude yet (ANTHROPIC_API_KEY" in src.last_error


def test_discord_rate_limit_is_retried(settings, conn):
    add(conn, "Ado", "x.com/ado1024imokenp")
    clf = FakeClassifier({"ツアー": [show("歌ってみたツアー", "2027-01-31", "Osaka", "Asueアリーナ大阪",
                                         [rnd("3次先行", "official", closes="2026-11-30T23:59")])]})
    rep = scan(settings, conn, FakeApify([tweet(70, "ado1024imokenp", "ツアー")]), clf)
    assert rep.messages[0].first_line == "🆕 [A] Ado · Sun 31 Jan · Osaka · ↪️ detour · ⏰ 3次先行 until Mon 30 Nov 23:59"

    class Limited(OkClient):
        def post(self, url, json):
            self.sent.append((url, json))

            class R:
                status_code = 429 if len(self.sent) == 1 else 204
                text = ""

                @staticmethod
                def json():
                    return {"retry_after": 0.01}
            return R()

    c = Limited()
    sent, errors = alerts.deliver(conn, settings, rep.messages, client=c, pause=0)
    assert sent == 1 and not errors and len(c.sent) == 2
    again = alerts.build(conn, at=AT.astimezone(store.JST))
    assert alerts.deliver(conn, settings, again, client=c) == (0, [])   # never twice


def test_shows_from_a_killed_scan_alert_on_the_next_one(settings, conn):
    add(conn, "Eve", "x.com/oO0Eve0Oo")
    clf = FakeClassifier({"廻帰": [show("廻帰 TOUR", "2027-02-03", "Tokyo", "Zepp Haneda", [rnd("FC先行")])]})
    scan(settings, conn, FakeApify([tweet(80, "oo0eve0oo", "廻帰 tour")]), clf)   # killed before its alerts
    rep = scan(settings, conn, FakeApify([]), clf)                                # nothing new read
    assert rep.events == [] and [m.first_line for m in rep.messages] == [
        "🆕 [B] Eve · Wed 3 Feb · Tokyo · ✅ fits · ⏰ FC先行 until Sun 15 Nov 23:59"]
    alerts.deliver(conn, settings, rep.messages, client=OkClient(), pause=0)
    assert scan(settings, conn, FakeApify([]), clf).messages == []
    # a later deadline change on the same round is still news
    with conn:
        conn.execute("UPDATE ticket_rounds SET closes_at = '2026-11-20T23:59'")
    msgs = scan(settings, conn, FakeApify([]), clf).messages
    assert len(msgs) == 1 and msgs[0].first_line.startswith("⏰ [B] Eve") and "New date for a ticket round" in msgs[0].text


def test_instagram_placeholder_means_nothing_new_and_the_cooldown_skips_reads(settings, conn):
    eve = add(conn, "Eve", "instagram.com/eve_harapeco")
    store.save_prefs(conn, instagram_cooldown_hours=24)      # default 0 reads it every scan; here 24
    holder = {"url": "https://www.instagram.com/eve_harapeco/", "error": "no_items",
              "errorDescription": "Empty or private data for provided input"}
    clf = FakeClassifier({})
    scan(settings, conn, FakeApify(ig=[holder]), clf)
    src = db.get_artist(conn, eve).sources[0]
    assert src.last_status == "error" and "private, empty" in src.last_error      # on a first scan it's a problem

    apify = FakeApify(ig=[{"shortCode": "C1", "ownerUsername": "eve_harapeco", "caption": "廻帰",
                           "timestamp": "2026-11-13T03:00:00Z"}])
    scan(settings, conn, apify, clf)
    assert db.get_artist(conn, eve).sources[0].last_status == "ok"
    apify.ig = [holder]
    run6 = scan(settings, conn, apify, clf, at=AT + timedelta(hours=6))        # read 6 h ago: not read, not billed
    assert len(apify.calls) == 1 and run6.errors == []
    step = conn.execute("SELECT status, detail FROM run_steps WHERE kind = 'instagram' ORDER BY id DESC").fetchone()
    assert step["status"] == "skipped" and "cooldown is 24 h" in step["detail"]
    rep = scan(settings, conn, apify, clf, at=AT + timedelta(hours=24))
    assert len(apify.calls) == 2 and rep.errors == []
    src = db.get_artist(conn, eve).sources[0]
    assert src.last_status == "empty" and not src.last_error                  # "nothing new", not a failure
    store.save_prefs(conn, instagram_cooldown_hours=0)                         # the default: every scan, like X
    scan(settings, conn, apify, clf, at=AT + timedelta(hours=25))
    assert len(apify.calls) == 3


def test_the_cli_logs_next_to_its_own_database(tmp_path):
    import logging

    from app import watch
    from app.config import ROOT
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    real = ROOT / "data" / "logs" / "watch.log"
    before = real.stat().st_size if real.exists() else None
    try:
        assert watch.main(["--dry-run"]) == 0
    finally:
        for h in root.handlers:
            if h not in handlers:
                h.close()
        root.handlers[:] = handlers
        root.setLevel(level)
    assert "Done (ok)" in (tmp_path / "env" / "logs" / "watch.log").read_text(encoding="utf-8")
    assert (real.stat().st_size if real.exists() else None) == before


def test_migration_marks_what_old_alerts_already_said(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    for sql in db.MIGRATIONS[:3]:
        c.executescript(sql)
    c.executescript("""
        INSERT INTO artists (id, name, tier, created_at, updated_at) VALUES (1, 'Eve', 'B', 'x', 'x');
        INSERT INTO shows (id, artist_id, title, date, first_seen_at, updated_at) VALUES
            (1, 1, 'A', '2027-02-03', 'x', 'x'), (2, 1, 'B', '2027-02-04', 'x', 'x');
        INSERT INTO ticket_rounds (id, show_id, name, name_key, closes_at, first_seen_at, updated_at)
            VALUES (1, 1, 'FC', 'fc', '2027-01-10T23:59', 'x', 'x');
        INSERT INTO alerts (key, artist_id, show_id, kind, text, sent_at) VALUES
            ('show:1', 1, 1, 'new_show', '🆕 Eve · Wed 3 Feb · Tokyo', 't'), ('round:1', 1, 1, 'new_round', '', 't'),
            ('show:2', 1, 2, 'new_show', '🆕 Eve · Date TBA · Tokyo', 't');
        PRAGMA user_version = 3;
    """)
    c.close()
    db.init_db(path)
    c = db.connect(path)
    keys = {r[0] for r in c.execute("SELECT key FROM alerts")}
    c.close()
    assert {"show:1:date:2027-02-03", "round:1:closes:2027-01-10T23:59"} <= keys
    assert "show:2:date:2027-02-04" not in keys        # alerted while TBA: its date is still news
