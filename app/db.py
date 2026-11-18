"""SQLite storage: schema, connection, and the artist/source queries.

Later steps (sweep, watcher) import this module too. Schema changes go in
MIGRATIONS as a new entry; `PRAGMA user_version` records how far a file is.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .sources import COVERAGE_KINDS, KINDS, Candidate

TIERS = ("S", "A", "B")

MIGRATIONS = [
    # 1: artists + sources. The last_* columns on sources are for step 2's scan status.
    """
    CREATE TABLE artists (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name_ja     TEXT NOT NULL DEFAULT '',
        tier        TEXT NOT NULL CHECK (tier IN ('S', 'A', 'B')),
        notes       TEXT NOT NULL DEFAULT '',
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE sources (
        id              INTEGER PRIMARY KEY,
        artist_id       INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
        kind            TEXT NOT NULL,
        handle          TEXT,
        url             TEXT NOT NULL,
        label           TEXT NOT NULL DEFAULT '',
        position        INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL,
        last_scanned_at TEXT,
        last_status     TEXT CHECK (last_status IN ('ok', 'empty', 'error')),
        last_error      TEXT,
        last_found      INTEGER,
        UNIQUE (artist_id, kind, url)
    );
    CREATE INDEX sources_artist ON sources(artist_id);
    """,
    # 2: scanning, shows, alerts, itinerary (step 2)
    """
    ALTER TABLE sources ADD COLUMN last_posts INTEGER;     -- posts / pages read in the last scan
    ALTER TABLE sources ADD COLUMN cursor TEXT;            -- social: start of the last good scan (ISO)
    ALTER TABLE sources ADD COLUMN first_scan_at TEXT;     -- when the look-back scan ran

    -- Website pages watched: each website source's own page plus detail pages it led to.
    CREATE TABLE pages (
        id              INTEGER PRIMARY KEY,
        source_id       INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        url             TEXT NOT NULL,
        tracked         INTEGER NOT NULL DEFAULT 1,      -- re-fetched every scan (0: read once)
        content_hash    TEXT,
        rendered        INTEGER NOT NULL DEFAULT 0,      -- needed the headless browser
        first_seen_at   TEXT NOT NULL,
        last_fetched_at TEXT,
        last_changed_at TEXT,
        last_error      TEXT,
        UNIQUE (source_id, url)
    );

    -- Everything read: tweets, Instagram posts, page versions. The dedup of "seen".
    CREATE TABLE posts (
        id          INTEGER PRIMARY KEY,
        source_id   INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        external_id TEXT NOT NULL,
        url         TEXT NOT NULL,
        posted_at   TEXT,
        text        TEXT NOT NULL DEFAULT '',
        media       TEXT NOT NULL DEFAULT '[]',
        read_at     TEXT NOT NULL,
        relevant    INTEGER,                              -- NULL until classified
        note        TEXT NOT NULL DEFAULT '',
        UNIQUE (source_id, external_id)
    );
    CREATE INDEX posts_pending ON posts(source_id) WHERE relevant IS NULL;

    CREATE TABLE shows (
        id            INTEGER PRIMARY KEY,
        artist_id     INTEGER NOT NULL REFERENCES artists(id) ON DELETE CASCADE,
        title         TEXT NOT NULL,
        date          TEXT,                               -- YYYY-MM-DD; NULL = TBA
        time_note     TEXT NOT NULL DEFAULT '',
        venue         TEXT NOT NULL DEFAULT '',
        city          TEXT NOT NULL DEFAULT '',
        prefecture    TEXT NOT NULL DEFAULT '',
        kind          TEXT NOT NULL DEFAULT 'live',
        notes         TEXT NOT NULL DEFAULT '',
        cancelled     INTEGER NOT NULL DEFAULT 0,
        hidden        INTEGER NOT NULL DEFAULT 0,         -- dismissed by Daniel in the UI
        first_seen_at TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX shows_artist ON shows(artist_id);

    CREATE TABLE ticket_rounds (
        id            INTEGER PRIMARY KEY,
        show_id       INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
        name          TEXT NOT NULL,
        name_key      TEXT NOT NULL,
        kind          TEXT NOT NULL DEFAULT 'other',      -- fanclub|official|playguide|general|other
        lottery       INTEGER NOT NULL DEFAULT 0,
        opens_at      TEXT,                               -- ISO, JST (+09:00) or a date
        closes_at     TEXT,
        results_at    TEXT,
        url           TEXT NOT NULL DEFAULT '',
        price         TEXT NOT NULL DEFAULT '',
        notes         TEXT NOT NULL DEFAULT '',
        first_seen_at TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        UNIQUE (show_id, name_key)
    );

    CREATE TABLE show_posts (
        show_id INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        PRIMARY KEY (show_id, post_id)
    );

    -- One row per thing alerted, so nothing is sent twice.
    CREATE TABLE alerts (
        id        INTEGER PRIMARY KEY,
        key       TEXT NOT NULL UNIQUE,
        artist_id INTEGER,
        show_id   INTEGER,
        kind      TEXT NOT NULL,
        text      TEXT NOT NULL DEFAULT '',
        sent_at   TEXT NOT NULL
    );

    CREATE TABLE itinerary (
        id         INTEGER PRIMARY KEY,
        start_date TEXT NOT NULL,
        end_date   TEXT NOT NULL,
        city       TEXT NOT NULL
    );
    -- A sample trip, not a real one: set yours on the Trip page.
    INSERT INTO itinerary (start_date, end_date, city) VALUES
        ('2027-01-29', '2027-02-08', 'Tokyo'),
        ('2027-02-08', '2027-02-12', 'Nagoya'),
        ('2027-02-12', '2027-02-18', 'Fukuoka'),
        ('2027-02-18', '2027-02-25', 'Osaka'),
        ('2027-02-25', '2027-02-28', 'Tokyo');

    CREATE TABLE runs (
        id           INTEGER PRIMARY KEY,
        started_at   TEXT NOT NULL,
        finished_at  TEXT,
        heartbeat_at TEXT NOT NULL,
        trigger      TEXT NOT NULL,                       -- cron|ui|cli
        scope        TEXT NOT NULL DEFAULT 'all',         -- all | artist:<id>
        dry_run      INTEGER NOT NULL DEFAULT 0,
        status       TEXT NOT NULL DEFAULT 'running',     -- running|ok|partial|error
        posts_read   INTEGER NOT NULL DEFAULT 0,
        shows_new    INTEGER NOT NULL DEFAULT 0,
        alerts_sent  INTEGER NOT NULL DEFAULT 0,
        apify_usd    REAL NOT NULL DEFAULT 0,
        claude_usd   REAL NOT NULL DEFAULT 0,
        summary      TEXT NOT NULL DEFAULT '',
        errors       TEXT NOT NULL DEFAULT ''
    );
    """,
    # 3: live scan progress (what's running now, every step's state, the run's log)
    """
    ALTER TABLE runs ADD COLUMN current TEXT NOT NULL DEFAULT '';
    ALTER TABLE runs ADD COLUMN current_at TEXT;

    CREATE TABLE run_steps (
        id          INTEGER PRIMARY KEY,
        run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        key         TEXT NOT NULL,                      -- 'x:<source id>', 'claude:<artist id>', 'alerts'
        position    INTEGER NOT NULL,
        phase       TEXT NOT NULL,                      -- read | claude | alerts
        kind        TEXT NOT NULL,                      -- x | instagram | website | claude | alerts
        grp         TEXT NOT NULL DEFAULT '',           -- steps that run as one Apify call share a group
        label       TEXT NOT NULL,
        sublabel    TEXT NOT NULL DEFAULT '',
        artist_id   INTEGER,
        status      TEXT NOT NULL DEFAULT 'pending',    -- pending | running | done | failed | skipped
        started_at  TEXT,
        finished_at TEXT,
        done        INTEGER NOT NULL DEFAULT 0,         -- units finished (pages, posts classified, alerts sent)
        total       INTEGER NOT NULL DEFAULT 0,
        posts       INTEGER NOT NULL DEFAULT 0,
        shows       INTEGER NOT NULL DEFAULT 0,
        updates     INTEGER NOT NULL DEFAULT 0,
        usd         REAL NOT NULL DEFAULT 0,
        detail      TEXT NOT NULL DEFAULT '',
        error       TEXT NOT NULL DEFAULT '',
        rev         INTEGER NOT NULL DEFAULT 0,         -- bumped on every change, so the page fetches only changes
        UNIQUE (run_id, key)
    );
    CREATE INDEX run_steps_run ON run_steps(run_id, rev);

    CREATE TABLE run_log (
        id      INTEGER PRIMARY KEY,
        run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        at      TEXT NOT NULL,
        level   TEXT NOT NULL,
        message TEXT NOT NULL
    );
    CREATE INDEX run_log_run ON run_log(run_id, id);
    """,
    # 4: alerts are built from the stored shows (anything about a show with no row in `alerts` yet), not from one
    # scan's events. Record what alerts sent before this already said, so it isn't sent again. A date counts as
    # said only if the show's alert wasn't "Date TBA"; a cancellation only if the alert had the ❌.
    """
    INSERT OR IGNORE INTO alerts (key, artist_id, show_id, kind, text, sent_at)
        SELECT 'show:' || s.id || ':date:' || s.date, s.artist_id, s.id, 'date_set', '', a.sent_at
        FROM shows s JOIN alerts a ON a.key = 'show:' || s.id
        WHERE s.date IS NOT NULL AND a.text NOT LIKE '%date TBA%';
    INSERT OR IGNORE INTO alerts (key, artist_id, show_id, kind, text, sent_at)
        SELECT 'show:' || s.id || ':cancelled', s.artist_id, s.id, 'cancelled', '', a.sent_at
        FROM shows s JOIN alerts a ON a.key = 'show:' || s.id
        WHERE s.cancelled = 1 AND instr(a.text, '❌') > 0;
    INSERT OR IGNORE INTO alerts (key, artist_id, show_id, kind, text, sent_at)
        SELECT 'round:' || r.id || ':closes:' || r.closes_at, s.artist_id, s.id, 'deadline', '', a.sent_at
        FROM ticket_rounds r JOIN shows s ON s.id = r.show_id JOIN alerts a ON a.key = 'round:' || r.id
        WHERE r.closes_at IS NOT NULL;
    INSERT OR IGNORE INTO alerts (key, artist_id, show_id, kind, text, sent_at)
        SELECT 'round:' || r.id || ':opens:' || r.opens_at, s.artist_id, s.id, 'deadline', '', a.sent_at
        FROM ticket_rounds r JOIN shows s ON s.id = r.show_id JOIN alerts a ON a.key = 'round:' || r.id
        WHERE r.opens_at IS NOT NULL;
    """,
    # 5: pasted links lost their trailing slash, and Sony's sites answer 404 without it (2026-11-18 scan).
    # These four are the only sources that broke; new pastes keep the slash.
    """
    UPDATE OR IGNORE pages SET url = url || '/', last_error = NULL WHERE url IN (
        'https://www.sh-nzk.net/live', 'https://www.sh-nzk.net/info',
        'https://www.sonymusic.co.jp/artist/9Lana/live', 'https://www.sonymusic.co.jp/artist/KocchinoKento/live');
    UPDATE OR IGNORE sources SET url = url || '/', last_status = NULL, last_error = NULL WHERE kind = 'website'
        AND url IN ('https://www.sh-nzk.net/live', 'https://www.sh-nzk.net/info',
                    'https://www.sonymusic.co.jp/artist/9Lana/live',
                    'https://www.sonymusic.co.jp/artist/KocchinoKento/live');
    """,
    # 6: times a step was tried again after a timeout, 5xx, 429 or DNS error (shown in the progress view)
    """
    ALTER TABLE run_steps ADD COLUMN retries INTEGER NOT NULL DEFAULT 0;
    """,
    # 7: the scan's process id, so a scan whose process is gone is noticed at once (not after 20 minutes without a
    # heartbeat), and the scan a "Resume" continues
    """
    ALTER TABLE runs ADD COLUMN pid INTEGER;
    ALTER TABLE runs ADD COLUMN resumed_from INTEGER;
    """,
    # 8: which scan read each post, and in which scan Claude decided about it (the scan page's "what it read").
    # Posts read before this get their scan from the read time.
    """
    ALTER TABLE posts ADD COLUMN run_id INTEGER;
    ALTER TABLE posts ADD COLUMN checked_run_id INTEGER;
    UPDATE posts SET run_id = (
        SELECT r.id FROM runs r WHERE r.dry_run = 0 AND r.started_at <= posts.read_at
        AND (r.finished_at IS NULL OR r.finished_at >= posts.read_at) ORDER BY r.id DESC LIMIT 1);
    UPDATE posts SET checked_run_id = run_id WHERE relevant IS NOT NULL;
    CREATE INDEX posts_run ON posts(run_id);
    CREATE INDEX posts_checked_run ON posts(checked_run_id);
    """,
    # 9: settings changed in the app (the Scans page's Schedule); .env only gives the defaults
    """
    CREATE TABLE settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
]

SEED = {
    "S": ["Phantom Siita", "Hiroyuki Sawano", "Itoki Hana", "9Lana"],
    "A": ["LiSA", "Atarashii Gakko!", "YOASOBI", "Ado", "Raon", "Kenshi Yonezu", "sukekiyo", "Rosu", "Azari"],
    "B": ["Queen Bee", "NIGHTMARE", "KANA-BOON", "Eve", "Kocchi no Kento", "UVERworld", "Haruka Tomatsu",
          "ZUTOMAYO", "MYTH & ROID", "Co shu Nie", "Creepy Nuts", "AKASAKI", "Fujii Kaze", "Ai Higuchi",
          "TRUE", "Reol"],
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(path: Path) -> None:
    """Create or upgrade the schema, then seed the starting artists if there are none."""
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with conn:
                conn.executescript(sql)
                conn.execute(f"PRAGMA user_version = {i}")
        if conn.execute("SELECT COUNT(*) FROM artists").fetchone()[0] == 0:
            seed(conn)
    finally:
        conn.close()


def seed(conn: sqlite3.Connection) -> int:
    """Insert the starting artists (names + tiers). Never duplicates."""
    added = 0
    with conn:
        for tier, names in SEED.items():
            for name in names:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO artists (name, tier, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (name, tier, now(), now()))
                added += cur.rowcount
    return added


# ---------------------------------------------------------------- read models

@dataclass
class Source:
    id: int
    artist_id: int
    kind: str
    handle: str | None
    url: str
    label: str
    position: int
    last_scanned_at: str | None
    last_status: str | None
    last_error: str | None
    last_found: int | None
    last_posts: int | None = None
    cursor: str | None = None
    first_scan_at: str | None = None

    @property
    def scannable(self) -> bool:
        return self.kind in ("instagram", "x", "website", "fanclub")

    @property
    def kind_name(self) -> str:
        return KINDS.get(self.kind, self.kind.title())

    @property
    def display(self) -> str:
        from .sources import display_url
        return f"@{self.handle}" if self.handle else display_url(self.url)


@dataclass
class Artist:
    id: int
    name: str
    name_ja: str
    tier: str
    notes: str
    enabled: bool
    sources: list[Source] = field(default_factory=list)

    def count(self, kind: str) -> int:
        return sum(1 for s in self.sources if s.kind == kind)

    def missing(self, kind: str) -> bool:
        return self.count(kind) == 0

    def search_text(self) -> str:
        return fold(f"{self.name} {self.name_ja}")


def fold(text: str) -> str:
    """Case- and width-insensitive form for search (ＬｉＳＡ == lisa)."""
    return unicodedata.normalize("NFKC", text).casefold()


def _artist(row: sqlite3.Row) -> Artist:
    return Artist(row["id"], row["name"], row["name_ja"], row["tier"], row["notes"], bool(row["enabled"]))


def _source(row: sqlite3.Row) -> Source:
    return Source(**{k: row[k] for k in Source.__dataclass_fields__})


def _sort_sources(sources: list[Source]) -> list[Source]:
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(sources, key=lambda s: (order.get(s.kind, 99), s.position, s.id))


def list_artists(conn: sqlite3.Connection) -> list[Artist]:
    artists = {r["id"]: _artist(r) for r in conn.execute(
        "SELECT * FROM artists ORDER BY CASE tier WHEN 'S' THEN 0 WHEN 'A' THEN 1 ELSE 2 END, id")}
    for r in conn.execute("SELECT * FROM sources"):
        if r["artist_id"] in artists:
            artists[r["artist_id"]].sources.append(_source(r))
    for a in artists.values():
        a.sources = _sort_sources(a.sources)
    return list(artists.values())


def get_artist(conn: sqlite3.Connection, artist_id: int) -> Artist | None:
    row = conn.execute("SELECT * FROM artists WHERE id = ?", (artist_id,)).fetchone()
    if not row:
        return None
    a = _artist(row)
    a.sources = _sort_sources([_source(r) for r in conn.execute(
        "SELECT * FROM sources WHERE artist_id = ?", (artist_id,))])
    return a


def coverage(artists: list[Artist]) -> dict[str, int]:
    """How many watching artists have no source of each coverage kind."""
    watching = [a for a in artists if a.enabled]
    return {k: sum(1 for a in watching if a.missing(k)) for k in COVERAGE_KINDS}


# ---------------------------------------------------------------- writes

class NameTaken(ValueError):
    pass


def create_artist(conn: sqlite3.Connection, name: str, name_ja: str, tier: str) -> int:
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO artists (name, name_ja, tier, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (name, name_ja, tier, now(), now()))
    except sqlite3.IntegrityError as e:
        raise NameTaken(name) from e
    return cur.lastrowid


def update_artist(conn: sqlite3.Connection, artist_id: int, *, name: str, name_ja: str, tier: str,
                  notes: str, enabled: bool) -> None:
    try:
        with conn:
            conn.execute(
                "UPDATE artists SET name = ?, name_ja = ?, tier = ?, notes = ?, enabled = ?, updated_at = ? "
                "WHERE id = ?", (name, name_ja, tier, notes, int(enabled), now(), artist_id))
    except sqlite3.IntegrityError as e:
        raise NameTaken(name) from e


def delete_artist(conn: sqlite3.Connection, artist_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM artists WHERE id = ?", (artist_id,))


def existing_keys(artist: Artist) -> set[tuple[str, str]]:
    return {(s.kind, s.url) for s in artist.sources}


def add_sources(conn: sqlite3.Connection, artist_id: int, candidates: list[Candidate]) -> int:
    with conn:
        pos = conn.execute("SELECT COALESCE(MAX(position), 0) FROM sources WHERE artist_id = ?",
                           (artist_id,)).fetchone()[0]
        added = 0
        for c in candidates:
            pos += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO sources (artist_id, kind, handle, url, label, position, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", (artist_id, c.kind, c.handle, c.url, c.label, pos, now()))
            added += cur.rowcount
        conn.execute("UPDATE artists SET updated_at = ? WHERE id = ?", (now(), artist_id))
    return added


def get_source(conn: sqlite3.Connection, source_id: int) -> Source | None:
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return _source(row) if row else None


def update_source(conn: sqlite3.Connection, source_id: int, *, kind: str, url: str, handle: str | None,
                  label: str) -> bool:
    """False if another source of this artist already has that kind + url."""
    try:
        with conn:
            conn.execute("UPDATE sources SET kind = ?, url = ?, handle = ?, label = ? WHERE id = ?",
                         (kind, url, handle, label, source_id))
    except sqlite3.IntegrityError:
        return False
    return True


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
