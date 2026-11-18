"""Step 2 storage: shows, ticket rounds, merging what Claude found, itinerary, runs (scan log + lock)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import fit
from .classify import Extraction, KnownShow, Round
from .db import now

JST = timezone(timedelta(hours=9))
STALE_RUN = timedelta(minutes=20)


def jst_now() -> datetime:
    return datetime.now(JST)


# ---------------------------------------------------------------- times

def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def parse_jst(value: str | None, end_of_day: bool = False) -> datetime | None:
    """'2026-11-15T23:59' or '2026-11-15' (JST) -> aware datetime."""
    if not value:
        return None
    v = value.strip().replace(" ", "T")
    try:
        if len(v) <= 10:
            d = date.fromisoformat(v)
            t = (23, 59) if end_of_day else (0, 0)
            return datetime(d.year, d.month, d.day, *t, tzinfo=JST)
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt.replace(tzinfo=JST) if dt.tzinfo is None else dt.astimezone(JST)


def clean_when(value: str | None) -> str | None:
    """Keep 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' only."""
    if not value:
        return None
    dt = parse_jst(value)
    if not dt:
        return None
    return dt.date().isoformat() if len(value.strip()) <= 10 else dt.strftime("%Y-%m-%dT%H:%M")


def round_state(opens: str | None, closes: str | None, at: datetime) -> str:
    o, c = parse_jst(opens), parse_jst(closes, end_of_day=True)
    if c and c < at:
        return "closed"
    if o and o > at:
        return "upcoming"
    if c or o:
        return "open"
    return "unknown"


def key(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\s・()（）【】「」『』\[\]<>〈〉\-_/:：,，、.。!！?？~～★☆*]+", "", t)


# ---------------------------------------------------------------- itinerary

def itinerary(conn: sqlite3.Connection) -> list[fit.Segment]:
    return [fit.Segment(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]), r["city"])
            for r in conn.execute("SELECT * FROM itinerary ORDER BY start_date, id")]


def save_itinerary(conn: sqlite3.Connection, segments: list[fit.Segment]) -> None:
    with conn:
        conn.execute("DELETE FROM itinerary")
        conn.executemany("INSERT INTO itinerary (start_date, end_date, city) VALUES (?, ?, ?)",
                         [(s.start.isoformat(), s.end.isoformat(), s.city) for s in segments])


# ---------------------------------------------------------------- shows

@dataclass
class RoundView:
    id: int
    name: str
    kind: str
    lottery: bool
    opens_at: str | None
    closes_at: str | None
    results_at: str | None
    url: str
    price: str
    notes: str
    state: str = "unknown"


@dataclass
class ShowView:
    id: int
    artist_id: int
    artist: str
    tier: str
    title: str
    date: date | None
    time_note: str
    venue: str
    city: str
    prefecture: str
    kind: str
    notes: str
    cancelled: bool
    hidden: bool
    first_seen_at: str
    fit: str = fit.TBA
    stay: str | None = None
    rounds: list[RoundView] = field(default_factory=list)
    links: list[tuple[str, str]] = field(default_factory=list)   # (url, label)

    @property
    def open_rounds(self) -> list[RoundView]:
        return [r for r in self.rounds if r.state == "open"]

    @property
    def next_deadline(self) -> datetime | None:
        ds = [parse_jst(r.closes_at, True) for r in self.rounds if r.state in ("open", "upcoming") and r.closes_at]
        return min(ds) if ds else None

    @property
    def place(self) -> str:
        bits = [self.venue, self.city if self.city and self.city not in self.venue else ""]
        return ", ".join(b for b in bits if b) or "Venue TBA"


def known_shows(conn: sqlite3.Connection, artist_id: int) -> list[KnownShow]:
    out = []
    for s in conn.execute("SELECT * FROM shows WHERE artist_id = ? ORDER BY date IS NULL, date, id", (artist_id,)):
        rounds = [f"{r['name']} (closes {r['closes_at'] or '?'})" for r in conn.execute(
            "SELECT name, closes_at FROM ticket_rounds WHERE show_id = ? ORDER BY id", (s["id"],))]
        out.append(KnownShow(s["id"], s["date"], s["venue"], s["city"], s["title"], rounds))
    return out


@dataclass
class Event:
    kind: str          # new_show | date_set | new_round | deadline | cancelled
    artist_id: int
    show_id: int
    round_id: int | None = None
    alert_key: str = ""


def _find_show(conn: sqlite3.Connection, artist_id: int, s, d: date | None) -> sqlite3.Row | None:
    if s.existing_show_id:
        row = conn.execute("SELECT * FROM shows WHERE id = ? AND artist_id = ?",
                           (s.existing_show_id, artist_id)).fetchone()
        if row and not (row["date"] and d and row["date"] != d.isoformat()):
            return row
    if d:
        rows = conn.execute("SELECT * FROM shows WHERE artist_id = ? AND date = ?",
                            (artist_id, d.isoformat())).fetchall()
        if len(rows) == 1:
            return rows[0]
        for r in rows:
            if (s.venue and key(s.venue) == key(r["venue"])) or (s.city and key(s.city) == key(r["city"])):
                return r
        if rows:
            return rows[0]
    tk = key(s.title)        # a TBA show that now has its date, or another mention of a TBA show
    for r in conn.execute("SELECT * FROM shows WHERE artist_id = ? AND date IS NULL", (artist_id,)):
        rk = key(r["title"])
        if tk and rk and (tk == rk or tk in rk or rk in tk):
            return r
    return None


def merge(conn: sqlite3.Connection, artist_id: int, ex: Extraction, item_posts: dict[int, int],
          window: tuple[date, date], at: datetime | None = None) -> tuple[list[Event], dict[int, set[int]]]:
    """Store what Claude found. Returns (events worth an alert, post id -> show ids linked)."""
    at = at or jst_now()
    events: list[Event] = []
    linked: dict[int, set[int]] = {}
    ts = now()
    with conn:
        for s in ex.shows:
            d = parse_date(s.date)
            if not s.in_japan or (d and not (window[0] <= d <= window[1])) or (d and d < at.date()):
                continue
            row = _find_show(conn, artist_id, s, d)
            if row is None:
                cur = conn.execute(
                    "INSERT INTO shows (artist_id, title, date, time_note, venue, city, prefecture, kind, notes, "
                    "cancelled, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (artist_id, s.title.strip()[:300], d.isoformat() if d else None, s.time_note[:100],
                     s.venue[:200], s.city[:80], s.prefecture[:80], s.kind, s.notes[:500], int(s.cancelled), ts, ts))
                show_id = cur.lastrowid
                events.append(Event("new_show", artist_id, show_id, alert_key=f"show:{show_id}"))
            else:
                show_id = row["id"]
                updates: dict[str, object] = {}
                if d and not row["date"]:
                    updates["date"] = d.isoformat()
                    events.append(Event("date_set", artist_id, show_id, alert_key=f"show:{show_id}:date:{d}"))
                for col, val in (("venue", s.venue), ("city", s.city), ("prefecture", s.prefecture),
                                 ("time_note", s.time_note), ("notes", s.notes)):
                    if val and not row[col]:
                        updates[col] = val[:300]
                if s.cancelled and not row["cancelled"]:
                    updates["cancelled"] = 1
                    events.append(Event("cancelled", artist_id, show_id, alert_key=f"show:{show_id}:cancelled"))
                if updates:
                    updates["updated_at"] = ts
                    sets = ", ".join(f"{k} = ?" for k in updates)
                    conn.execute(f"UPDATE shows SET {sets} WHERE id = ?", (*updates.values(), show_id))
            for n in s.items:
                if n in item_posts:
                    conn.execute("INSERT OR IGNORE INTO show_posts (show_id, post_id) VALUES (?, ?)",
                                 (show_id, item_posts[n]))
                    linked.setdefault(item_posts[n], set()).add(show_id)
            for r in s.rounds:
                ev = _merge_round(conn, artist_id, show_id, r, at, ts)
                if ev:
                    events.append(ev)
    return events, linked


def _merge_round(conn: sqlite3.Connection, artist_id: int, show_id: int, r: Round, at: datetime,
                 ts: str) -> Event | None:
    name = " ".join(r.name.split())[:120]
    k = key(name)
    if not k:
        return None
    opens, closes, results = clean_when(r.opens_at), clean_when(r.closes_at), clean_when(r.results_at)
    row = conn.execute("SELECT * FROM ticket_rounds WHERE show_id = ? AND name_key = ?", (show_id, k)).fetchone()
    is_closed = round_state(opens, closes, at) == "closed"
    if row is None:
        cur = conn.execute(
            "INSERT INTO ticket_rounds (show_id, name, name_key, kind, lottery, opens_at, closes_at, results_at, url, "
            "price, notes, first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (show_id, name, k, r.kind, int(r.lottery), opens, closes, results, (r.url or "")[:500],
             (r.price or "")[:200], r.notes[:500], ts, ts))
        rid = cur.lastrowid
        return None if is_closed else Event("new_round", artist_id, show_id, rid, f"round:{rid}")
    updates: dict[str, object] = {}
    for col, val in (("opens_at", opens), ("closes_at", closes), ("results_at", results)):
        if val and val != row[col]:
            updates[col] = val
    for col, val in (("url", r.url or ""), ("price", r.price or ""), ("notes", r.notes)):
        if val and not row[col]:
            updates[col] = val[:500]
    if not updates:
        return None
    updates["updated_at"] = ts
    conn.execute(f"UPDATE ticket_rounds SET {', '.join(f'{c} = ?' for c in updates)} WHERE id = ?",
                 (*updates.values(), row["id"]))
    if "closes_at" in updates and not is_closed:
        return Event("deadline", artist_id, show_id, row["id"], f"round:{row['id']}:closes:{closes}")
    if "opens_at" in updates and not row["opens_at"] and not is_closed:
        return Event("deadline", artist_id, show_id, row["id"], f"round:{row['id']}:opens:{opens}")
    return None


def _round_view(r: sqlite3.Row, at: datetime) -> RoundView:
    return RoundView(r["id"], r["name"], r["kind"], bool(r["lottery"]), r["opens_at"], r["closes_at"],
                     r["results_at"], r["url"], r["price"], r["notes"], round_state(r["opens_at"], r["closes_at"], at))


def load_shows(conn: sqlite3.Connection, *, artist_id: int | None = None, show_ids: list[int] | None = None,
               include_hidden: bool = False, at: datetime | None = None) -> list[ShowView]:
    at = at or jst_now()
    segments = itinerary(conn)
    where, args = ["1 = 1"], []
    if artist_id is not None:
        where.append("s.artist_id = ?")
        args.append(artist_id)
    if show_ids is not None:
        where.append(f"s.id IN ({','.join('?' * len(show_ids)) or 'NULL'})")
        args.extend(show_ids)
    if not include_hidden:
        where.append("s.hidden = 0")
    rows = conn.execute(
        f"SELECT s.*, a.name AS artist, a.tier FROM shows s JOIN artists a ON a.id = s.artist_id "
        f"WHERE {' AND '.join(where)}", args).fetchall()
    shows = {}
    for r in rows:
        d = parse_date(r["date"])
        sv = ShowView(r["id"], r["artist_id"], r["artist"], r["tier"], r["title"], d, r["time_note"], r["venue"],
                      r["city"], r["prefecture"], r["kind"], r["notes"], bool(r["cancelled"]), bool(r["hidden"]),
                      r["first_seen_at"])
        sv.fit = fit.fit_for(segments, d, r["city"], r["prefecture"])
        sv.stay = fit.night_city(segments, d) if d else None
        shows[r["id"]] = sv
    if shows:
        ids = list(shows)
        marks = ",".join("?" * len(ids))
        for r in conn.execute(f"SELECT * FROM ticket_rounds WHERE show_id IN ({marks}) "
                              f"ORDER BY COALESCE(opens_at, closes_at, '9999'), id", ids):
            shows[r["show_id"]].rounds.append(_round_view(r, at))
        for r in conn.execute(
                f"SELECT sp.show_id, p.url, src.kind FROM show_posts sp JOIN posts p ON p.id = sp.post_id "
                f"JOIN sources src ON src.id = p.source_id WHERE sp.show_id IN ({marks}) ORDER BY p.posted_at", ids):
            links = shows[r["show_id"]].links
            if r["url"] not in [u for u, _ in links]:
                label = {"x": "X", "instagram": "Instagram"}.get(r["kind"], _host(r["url"]))
                links.append((r["url"], label))
    return list(shows.values())


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    h = urlsplit(url).hostname or url
    return h[4:] if h.startswith("www.") else h


def sort_shows(shows: list[ShowView]) -> dict[str, list[ShowView]]:
    """Open or upcoming deadlines first (soonest first), then dated shows by date, then TBA."""
    far = datetime(9999, 1, 1, tzinfo=JST)
    act = sorted([s for s in shows if s.next_deadline and not s.cancelled],
                 key=lambda s: (s.next_deadline or far, s.date or date.max))
    acted = {s.id for s in act}
    rest = [s for s in shows if s.id not in acted]
    dated = sorted([s for s in rest if s.date], key=lambda s: (s.date, s.artist))
    tba = sorted([s for s in rest if not s.date], key=lambda s: (s.tier, s.artist))
    return {"deadlines": act, "dated": dated, "tba": tba}


def set_hidden(conn: sqlite3.Connection, show_id: int, hidden: bool) -> None:
    with conn:
        conn.execute("UPDATE shows SET hidden = ? WHERE id = ?", (int(hidden), show_id))


# ---------------------------------------------------------------- runs (scan log + lock)

class Busy(RuntimeError):
    pass


def process_started(pid: int) -> float | None:
    """When process `pid` started (epoch seconds), or None if there's no such process. -1.0 if it exists but its
    start time can't be read (another user's process, or a system without /proc)."""
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        h = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return -1.0 if ctypes.get_last_error() == 5 else None   # 5: access denied, so it exists
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:   # 259: STILL_ACTIVE
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetProcessTimes(h, *[ctypes.byref(t) for t in times]):
                return -1.0
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime     # 100 ns since 1601
            return ticks / 1e7 - 11644473600
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)                                       # signal 0: only checks it exists
    except ProcessLookupError:
        return None
    except PermissionError:
        return -1.0
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        if fields[0] == "Z":                                  # a zombie has finished
            return None
        with open("/proc/stat", encoding="ascii") as f:
            boot = next(int(line.split()[1]) for line in f if line.startswith("btime"))
        return boot + int(fields[19]) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, IndexError, StopIteration):
        return -1.0


def run_alive(run: sqlite3.Row) -> bool:
    """Is this running scan's process still there? Without a pid (older scans), only the heartbeat tells."""
    stale = (datetime.now(timezone.utc) - STALE_RUN).isoformat(timespec="seconds")
    if run["heartbeat_at"] < stale:
        return False
    if not run["pid"]:
        return True
    started = process_started(run["pid"])
    if started is None:
        return False
    run_start = datetime.fromisoformat(run["started_at"]).timestamp()
    return started < 0 or started <= run_start + 5      # a newer process with the same id isn't the scan


OnStopped = Callable[[sqlite3.Connection, int], None]


def reap(conn: sqlite3.Connection, why: str = "", on_stopped: OnStopped | None = None) -> list[int]:
    """Close scans whose process is gone: shown as stopped, unfinished steps skipped. Returns their ids.
    on_stopped(conn, run id) runs once per scan closed here (the "scan stopped" Discord notice); only the one
    that closes it calls it, even when several pages notice at once."""
    dead = [r for r in conn.execute("SELECT * FROM runs WHERE status = 'running'") if not run_alive(r)]
    for r in dead:
        msg = why or ("The scan's process ended without finishing" if r["pid"] else "Stopped without finishing")
        with conn:
            done = conn.execute("UPDATE runs SET status = 'error', finished_at = ?, errors = ?, "
                                "current = 'Stopped: ' || COALESCE(NULLIF(summary, ''), ?) "
                                "WHERE id = ? AND status = 'running'", (now(), msg, msg, r["id"])).rowcount
            if done:
                conn.execute("UPDATE run_steps SET status = 'skipped', finished_at = ?, "
                             "detail = 'not reached: the scan stopped', rev = rev + 100000 "
                             "WHERE run_id = ? AND status IN ('pending', 'running')", (now(), r["id"]))
        if done and on_stopped and not r["dry_run"]:
            try:
                on_stopped(conn, r["id"])
            except Exception:  # noqa: BLE001 - a notice must never break a page or a scan
                pass
    return [r["id"] for r in dead]


def start_run(conn: sqlite3.Connection, trigger: str, scope: str, dry_run: bool, pid: int | None = None,
              resumed_from: int | None = None, on_stopped: OnStopped | None = None) -> int:
    reap(conn, on_stopped=on_stopped)
    conn.execute("BEGIN IMMEDIATE")
    try:
        if not dry_run and conn.execute("SELECT 1 FROM runs WHERE status = 'running' AND dry_run = 0").fetchone():
            raise Busy("A scan is already running.")
        cur = conn.execute("INSERT INTO runs (started_at, heartbeat_at, trigger, scope, dry_run, pid, resumed_from) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (now(), now(), trigger, scope, int(dry_run), pid, resumed_from))
        conn.execute("COMMIT")
        return cur.lastrowid
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def resumable(conn: sqlite3.Connection, run_id: int) -> bool:
    """A scan that stopped partway can be resumed while it's the latest real scan and nothing is running."""
    r = conn.execute("SELECT status, dry_run FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not r or r["status"] != "error" or r["dry_run"]:
        return False
    return not conn.execute("SELECT 1 FROM runs WHERE dry_run = 0 AND (id > ? OR status = 'running')",
                            (run_id,)).fetchone()


def done_before(conn: sqlite3.Connection, run_id: int) -> set[str]:
    """Steps a stopped scan (and the scans it resumed) finished, so a resume doesn't read them again."""
    keys: set[str] = set()
    seen: set[int] = set()
    while run_id and run_id not in seen:
        seen.add(run_id)
        keys |= {r[0] for r in conn.execute("SELECT key FROM run_steps WHERE run_id = ? AND status = 'done' "
                                            "AND key LIKE 'src:%'", (run_id,))}
        row = conn.execute("SELECT resumed_from FROM runs WHERE id = ?", (run_id,)).fetchone()
        run_id = row["resumed_from"] if row else None
    return keys


def heartbeat(conn: sqlite3.Connection, run_id: int, summary: str = "") -> None:
    with conn:
        if summary:
            conn.execute("UPDATE runs SET heartbeat_at = ?, summary = ? WHERE id = ?", (now(), summary, run_id))
        else:
            conn.execute("UPDATE runs SET heartbeat_at = ? WHERE id = ?", (now(), run_id))


def finish_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    fields.setdefault("finished_at", now())
    with conn:
        conn.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                     (*fields.values(), run_id))


def running_run(conn: sqlite3.Connection, on_stopped: OnStopped | None = None) -> sqlite3.Row | None:
    """The real scan that's running now. Scans whose process is gone are closed first, so they show as stopped."""
    reap(conn, on_stopped=on_stopped)
    return conn.execute("SELECT * FROM runs WHERE status = 'running' AND dry_run = 0 ORDER BY id DESC LIMIT 1"
                        ).fetchone()


def recent_runs(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


# ---------------------------------------------------------------- schedule (the Scans page's settings)

PREF_RANGE = {"scan_every_hours": (1, 168), "instagram_cooldown_hours": (0, 168)}
SCHEDULE_SLACK = timedelta(minutes=10)     # cron fires on the hour; a scan that started at 9:00:40 counts as 9:00
IG_SLACK = SCHEDULE_SLACK                  # so a 12 h cooldown with scans every 12 h still reads it every scan


@dataclass
class Prefs:
    scan_every_hours: int
    instagram_cooldown_hours: int          # 0: Instagram is read on every scan, like X


def prefs(conn: sqlite3.Connection, settings) -> Prefs:
    """The schedule as saved in the app; `.env` (Settings) gives the values never saved."""
    saved = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}

    def get(key: str, default: float) -> int:
        lo, hi = PREF_RANGE[key]
        try:
            return int(min(max(float(saved[key]), lo), hi))
        except (KeyError, ValueError):
            return int(min(max(default, lo), hi))
    return Prefs(get("scan_every_hours", settings.scan_every_hours),
                 get("instagram_cooldown_hours", settings.instagram_cooldown_hours))


def save_prefs(conn: sqlite3.Connection, **values: int) -> None:
    with conn:
        for key, value in values.items():
            conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                         "SET value = excluded.value, updated_at = excluded.updated_at", (key, str(value), now()))


def last_scheduled_start(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT started_at FROM runs WHERE trigger = 'cron' AND dry_run = 0 ORDER BY id DESC LIMIT 1"
                       ).fetchone()
    return datetime.fromisoformat(row[0]) if row else None


def scan_due(conn: sqlite3.Connection, p: Prefs, at: datetime) -> bool:
    """Is a scheduled scan due? Every N hours since the last scheduled scan started (the hourly cron asks)."""
    last = last_scheduled_start(conn)
    return last is None or at - last >= timedelta(hours=p.scan_every_hours) - SCHEDULE_SLACK


def next_scheduled(conn: sqlite3.Connection, p: Prefs, at: datetime) -> datetime:
    """When the hourly cron will next start a scan: the first full hour at which one is due."""
    last = last_scheduled_start(conn)
    due = at if last is None else max(at, last + timedelta(hours=p.scan_every_hours) - SCHEDULE_SLACK)
    top = due.replace(minute=0, second=0, microsecond=0)
    return top if top == due and last is not None and top > at else top + timedelta(hours=1)


def ig_wait_hours(cursor: str | None, cooldown_hours: float, at: datetime) -> float | None:
    """Hours left of an Instagram profile's cooldown (read at `cursor`); None if the next scan reads it."""
    if not cooldown_hours or not cursor:
        return None
    try:
        left = datetime.fromisoformat(cursor) + timedelta(hours=cooldown_hours) - IG_SLACK - at
    except ValueError:
        return None
    return left.total_seconds() / 3600 if left.total_seconds() > 0 else None


# ---------------------------------------------------------------- what a scan read (the scan page's data view)

VERDICTS = {"relevant": "p.relevant = 1", "other": "p.relevant = 0", "waiting": "p.relevant IS NULL"}


@dataclass
class ReadItem:
    id: int
    kind: str                 # x | instagram | website | fanclub
    source: str               # @handle or the page's host/path
    artist_id: int
    artist: str
    url: str
    posted_at: str | None
    text: str
    images: int
    relevant: bool | None     # None: Claude hasn't read it yet
    note: str                 # what Claude said it is, and why
    read_here: bool           # read by this scan (else read earlier, decided by this scan)
    shows: list[tuple[int, str]] = field(default_factory=list)   # (show id, "Thu 4 Feb · Yokohama")

    @property
    def excerpt(self) -> str:
        """The start of the text as one short paragraph: lines joined, page links (`<https://…>`) left out."""
        text = re.sub(r"\s*<https?://[^>\s]+>", "", self.text or "")
        lines = [" ".join(line.split()) for line in text.splitlines()]
        short = " / ".join(line for line in lines if line)
        return short if len(short) <= 240 else short[:240].rsplit(" ", 1)[0] + "…"


def run_read_counts(conn: sqlite3.Connection, run_id: int) -> dict:
    """How many posts and page versions a scan read or decided about: by verdict, and by artist."""
    where = "(p.run_id = ? OR p.checked_run_id = ?)"
    row = conn.execute(f"SELECT COUNT(*), SUM(p.relevant = 1), SUM(p.relevant = 0), SUM(p.relevant IS NULL) "
                       f"FROM posts p WHERE {where}", (run_id, run_id)).fetchone()
    artists = conn.execute(
        f"SELECT a.id, a.name, COUNT(*) AS n, SUM(p.relevant = 1) AS rel FROM posts p "
        f"JOIN sources s ON s.id = p.source_id JOIN artists a ON a.id = s.artist_id WHERE {where} "
        f"GROUP BY a.id ORDER BY rel DESC, n DESC, a.name", (run_id, run_id)).fetchall()
    return {"all": row[0] or 0, "relevant": row[1] or 0, "other": row[2] or 0, "waiting": row[3] or 0,
            "artists": [(r["id"], r["name"], r["n"], r["rel"] or 0) for r in artists]}


def run_read_items(conn: sqlite3.Connection, run_id: int, verdict: str = "", artist_id: int | None = None,
                   offset: int = 0, limit: int = 40) -> list[ReadItem]:
    """The posts and page versions a scan read (or that Claude decided about in it), shows first, newest first."""
    from .sources import display_url
    where, args = ["(p.run_id = ? OR p.checked_run_id = ?)"], [run_id, run_id]
    if verdict in VERDICTS:
        where.append(VERDICTS[verdict])
    if artist_id:
        where.append("s.artist_id = ?")
        args.append(artist_id)
    rows = conn.execute(
        f"SELECT p.*, s.kind, s.handle, s.url AS source_url, s.artist_id, a.name AS artist FROM posts p "
        f"JOIN sources s ON s.id = p.source_id JOIN artists a ON a.id = s.artist_id WHERE {' AND '.join(where)} "
        f"ORDER BY p.relevant = 1 DESC, p.relevant IS NULL DESC, COALESCE(p.posted_at, p.read_at) DESC, p.id DESC "
        f"LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
    items = []
    for r in rows:
        try:
            images = len(json.loads(r["media"] or "[]"))
        except ValueError:
            images = 0
        source = f"@{r['handle']}" if r["handle"] else display_url(r["url"] or r["source_url"])
        items.append(ReadItem(r["id"], r["kind"], source, r["artist_id"], r["artist"], r["url"], r["posted_at"],
                              r["text"], images, None if r["relevant"] is None else bool(r["relevant"]), r["note"],
                              r["run_id"] == run_id))
    if items:
        marks = ",".join("?" * len(items))
        by_id = {it.id: it for it in items}
        for r in conn.execute(f"SELECT sp.post_id, sh.id, sh.date, sh.city, sh.venue, sh.title FROM show_posts sp "
                              f"JOIN shows sh ON sh.id = sp.show_id WHERE sp.post_id IN ({marks}) "
                              f"ORDER BY sh.date IS NULL, sh.date", list(by_id)):
            d = parse_date(r["date"])
            when = f"{d:%a} {d.day} {d:%b}" if d else "Date TBA"
            by_id[r["post_id"]].shows.append((r["id"], " · ".join(x for x in (when, r["city"] or r["venue"]) if x)))
    return items


def month_costs(conn: sqlite3.Connection) -> tuple[float, float]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    r = conn.execute("SELECT COALESCE(SUM(apify_usd), 0), COALESCE(SUM(claude_usd), 0) FROM runs "
                     "WHERE substr(started_at, 1, 7) = ?", (month,)).fetchone()
    return float(r[0]), float(r[1])


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)
