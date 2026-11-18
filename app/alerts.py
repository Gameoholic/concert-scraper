"""Turn scan events into Discord messages, one per show, and post them to a channel webhook.

Each message's first line is what a phone push notification shows (the artist's tier in brackets):
    🆕 [B] UVERworld · Thu 4 Feb · Yokohama · ✅ fits · ⏰ FC先行 until Sun 15 Nov 23:59
Details and links follow on the next lines.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

import httpx

from . import fit
from .config import Settings
from .db import now
from .store import JST, RoundView, ShowView, load_shows, parse_jst

log = logging.getLogger(__name__)

FIT_MARK = {fit.FITS: "✅ fits", fit.DETOUR: "↪️ detour", fit.OUTSIDE: "⛔ outside trip", fit.TBA: "❔ date TBA"}
EMOJI = {"new_show": "🆕", "date_set": "📅", "new_round": "🎟", "deadline": "⏰", "cancelled": "❌"}
WHAT = {"new_show": "New show", "date_set": "Date announced", "new_round": "New ticket round",
        "deadline": "New date for a ticket round", "cancelled": "Cancelled"}
ORDER = ["cancelled", "new_show", "date_set", "new_round", "deadline"]   # most important first
ROUND_KIND = {"fanclub": "FC", "official": "official", "playguide": "playguide", "general": "general", "other": ""}
DISCORD_LIMIT = 1900


# ---------------------------------------------------------------- alert rules (the Scans page's Alerts card)

PING, SILENT_POST, OFF = "ping", "silent", "off"
MODES = {PING: "Ping me", SILENT_POST: "Post, no ping", OFF: "Don't post"}
FITS_IN_ORDER = [fit.FITS, fit.DETOUR, fit.OUTSIDE, fit.TBA]
KIND_LABEL = {"new_show": "New show", "date_set": "Date announced", "new_round": "New ticket round",
              "deadline": "New opening or closing date of a ticket round", "cancelled": "Cancelled"}
LINES = {"place": "Venue and time", "stay": "Where you sleep that night (detours)", "rounds": "Ticket rounds",
         "round_extra": "Prices and notes of ticket rounds", "notes": "Show notes", "links": "Links (posts, all shows)"}
RULES_KEY = "alert_rules"


@dataclass
class Rules:
    """What gets posted, what pings, and what a message includes. Saved in `settings` as JSON."""
    fit: dict[str, str] = field(default_factory=lambda: {fit.FITS: PING, fit.DETOUR: SILENT_POST,
                                                         fit.OUTSIDE: SILENT_POST, fit.TBA: SILENT_POST})
    kinds: dict[str, bool] = field(default_factory=lambda: {k: True for k in KIND_LABEL})
    lines: dict[str, bool] = field(default_factory=lambda: {k: True for k in LINES})
    x_fail_ping: bool = True            # the "scan finished" notice pings you when an X account failed

    def mode(self, show_fit: str) -> str:
        return self.fit.get(show_fit, SILENT_POST)

    def to_json(self) -> str:
        return json.dumps({"fit": self.fit, "kinds": self.kinds, "lines": self.lines, "x_fail_ping": self.x_fail_ping})

    @classmethod
    def parse(cls, raw: str | dict | None) -> "Rules":
        """Anything missing or unknown falls back to the default, so an old or broken value never breaks alerts."""
        r = cls()
        try:
            data = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            data = {}
        for f, m in (data.get("fit") or {}).items():
            if f in r.fit and m in MODES:
                r.fit[f] = m
        for name, target in (("kinds", r.kinds), ("lines", r.lines)):
            for k, v in (data.get(name) or {}).items():
                if k in target:
                    target[k] = bool(v)
        if "x_fail_ping" in data:
            r.x_fail_ping = bool(data["x_fail_ping"])
        return r

    @classmethod
    def from_form(cls, form) -> "Rules":
        """From the Alerts card's form (unchecked boxes are simply absent)."""
        return cls.parse({"fit": {f: form.get(f"fit_{f}", "") for f in FITS_IN_ORDER},
                          "kinds": {k: bool(form.get(f"kind_{k}")) for k in KIND_LABEL},
                          "lines": {k: bool(form.get(f"line_{k}")) for k in LINES},
                          "x_fail_ping": bool(form.get("x_fail_ping"))})


def load_rules(conn: sqlite3.Connection) -> Rules:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (RULES_KEY,)).fetchone()
    return Rules.parse(row[0] if row else None)


def save_rules(conn: sqlite3.Connection, rules: Rules) -> None:
    with conn:
        conn.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE "
                     "SET value = excluded.value, updated_at = excluded.updated_at", (RULES_KEY, rules.to_json(), now()))


@dataclass
class Message:
    artist_id: int
    artist: str
    show_id: int
    text: str
    keys: list[tuple[str, str, int]] = field(default_factory=list)   # (alert key, kind, show id)
    mode: str = PING        # ping | silent | record (nothing to post: every update in it is turned off; just record it)

    @property
    def first_line(self) -> str:
        return self.text.split("\n", 1)[0]

    @property
    def postable(self) -> bool:
        return self.mode != "record"


def fmt_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def fmt_when(value: str | None) -> str:
    dt = parse_jst(value)
    if not dt:
        return "?"
    s = f"{dt:%a} {dt.day} {dt:%b}"
    if dt.year != datetime.now(JST).year:
        s += f" {dt.year}"
    if value and len(value.strip()) > 10:
        s += f" {dt:%H:%M}"
    return s


def round_line(r: RoundView, extra: bool = True) -> str:
    bits = [r.name]
    tag = ", ".join(x for x in (ROUND_KIND.get(r.kind, ""), "lottery" if r.lottery else "") if x)
    if tag:
        bits.append(f"({tag})")
    when = []
    if r.state == "open":
        when.append("OPEN NOW")
    if r.opens_at and r.state != "open":
        when.append(f"opens {fmt_when(r.opens_at)}")
    if r.closes_at:
        when.append(f"until {fmt_when(r.closes_at)}")
    if r.results_at:
        when.append(f"results {fmt_when(r.results_at)}")
    if r.state == "closed":
        when.append("closed")
    line = " ".join(bits) + (": " + ", ".join(when) if when else "")
    if extra and r.price:
        line += f" · {r.price}"
    if extra and r.notes:
        line += f" · {r.notes}"
    return line


def deadline_bit(s: ShowView, focus: list[RoundView]) -> str:
    """'⏰ FC先行 until Sun 15 Nov 23:59' for the round this alert is about, else the show's soonest one."""
    pool = [r for r in (focus or s.rounds) if r.state in ("open", "upcoming")]
    with_close = sorted([r for r in pool if r.closes_at], key=lambda r: parse_jst(r.closes_at, True))
    if with_close:
        r = with_close[0]
        return f"⏰ {r.name} until {fmt_when(r.closes_at)}"
    if pool and pool[0].opens_at:
        return f"⏰ {pool[0].name} opens {fmt_when(pool[0].opens_at)}"
    if not s.rounds:
        return "no ticket round yet"
    return ""


def show_message(s: ShowView, kinds: list[str], focus: list[RoundView], app_url: str = "",
                  include: dict[str, bool] | None = None) -> str:
    """include: which optional parts to show (alerts.LINES; all by default). The first line and title always are."""
    on = {k: True for k in LINES} | (include or {})
    lead = min(kinds, key=ORDER.index)
    where = s.city or s.venue or "venue TBA"
    first = " · ".join(b for b in (f"{EMOJI[lead]} [{s.tier}] {s.artist}", fmt_day(s.date) if s.date else "Date TBA",
                                   where,
                                   FIT_MARK[s.fit], deadline_bit(s, focus)) if b)
    lines = [first, f"**{s.title}**"]
    place = []
    if on["place"]:
        place.append(s.place + (f" · {s.time_note}" if s.time_note else ""))
    if on["stay"] and s.fit == fit.DETOUR and s.stay:
        place.append(f"you're in {s.stay} that night")
    if place:
        lines.append(" · ".join(place))
    if lead != "new_show":
        lines.append(f"*{', '.join(WHAT[k] for k in sorted(set(kinds), key=ORDER.index))}*")
    if on["rounds"]:
        shown = [r for r in s.rounds if r.state != "closed"] if lead in ("new_show", "date_set") else focus
        for r in shown:
            link = f" — [apply / info](<{r.url}>)" if r.url and on["links"] else ""
            lines.append(f"• {round_line(r, on['round_extra'])}{link}")
    if on["notes"] and s.notes:
        lines.append(f"_{s.notes}_")
    if on["links"]:
        links = [f"[{label}](<{url}>)" for url, label in s.links[-3:]]
        if app_url:
            links.append(f"[all shows](<{app_url.rstrip('/')}/shows#show-{s.id}>)")
        if links:
            lines.append(" · ".join(links))
    return "\n".join(lines)


def facts(s: ShowView) -> list[tuple[str, str, int | None]]:
    """Everything about a show worth an alert, as (alert key, kind, round id). A key with no row in `alerts`
    hasn't been sent yet. Rounds already closed are never alerted."""
    out: list[tuple[str, str, int | None]] = [(f"show:{s.id}", "new_show", None)]
    if s.date:
        out.append((f"show:{s.id}:date:{s.date.isoformat()}", "date_set", None))
    if s.cancelled:
        out.append((f"show:{s.id}:cancelled", "cancelled", None))
    for r in s.rounds:
        if r.state == "closed":
            continue
        out.append((f"round:{r.id}", "new_round", r.id))
        if r.closes_at:
            out.append((f"round:{r.id}:closes:{r.closes_at}", "deadline", r.id))
        if r.opens_at:
            out.append((f"round:{r.id}:opens:{r.opens_at}", "deadline", r.id))
    return out


def build(conn: sqlite3.Connection, app_url: str = "", at: datetime | None = None,
          rules: Rules | None = None) -> list[Message]:
    """One message per show that has anything not alerted yet, read from the stored shows (not from one scan's
    events), so shows found by a scan that was killed before its alerts still alert on the next one.
    Hidden shows and shows already past are left out.

    rules (default: the saved Alerts settings): a show whose fit is set to "Don't post" is left out and stays
    unsent, so it's sent if a trip or date change makes it fit later. A message whose updates are all turned off
    is only recorded (mode "record"), so it doesn't come back."""
    at = at or datetime.now(JST)
    rules = rules or load_rules(conn)
    sent = {r[0] for r in conn.execute("SELECT key FROM alerts")}
    messages, shows = [], {}
    for s in load_shows(conn, at=at):
        if s.date and s.date < at.date():
            continue
        mode = rules.mode(s.fit)
        if mode == OFF:
            continue
        fs = facts(s)
        due = [f for f in fs if f[0] not in sent]
        if not due:
            continue
        shows[s.id] = s
        if f"show:{s.id}" not in sent:
            kinds = ["new_show"] + (["cancelled"] if s.cancelled else [])
            round_ids = {rid for _, _, rid in due if rid}
        else:
            new_rounds = {rid for k, kind, rid in due if kind == "new_round"}
            due = [f for f in due if rules.kinds.get(f[1], True)]
            kinds = [kind for k, kind, rid in due if not (kind == "deadline" and rid in new_rounds)]
            round_ids = {rid for _, _, rid in due if rid}
        kinds = [k for k in dict.fromkeys(kinds) if rules.kinds.get(k, True)]
        keys = [(k, kind, s.id) for k, kind, _ in fs]
        if not kinds:
            messages.append(Message(s.artist_id, s.artist, s.id, "", keys, "record"))
            continue
        focus = [r for r in s.rounds if r.id in round_ids]
        text = show_message(s, kinds, focus, app_url, rules.lines)
        # every current fact is recorded once sent, so what this message already says isn't sent again
        messages.append(Message(s.artist_id, s.artist, s.id, text, keys, mode))
    far = datetime(9999, 1, 1, tzinfo=JST)
    messages.sort(key=lambda m: (shows[m.show_id].next_deadline or far, shows[m.show_id].date or date.max))
    return messages


def sample_show(show_fit: str) -> ShowView:
    """A made-up show for the Alerts card's preview, placed so it has the fit asked for."""
    day = {fit.OUTSIDE: date(2027, 3, 3), fit.TBA: None}.get(show_fit, date(2027, 2, 4))
    s = ShowView(0, 0, "UVERworld", "B", "LIVE HOUSE TOUR 2027", day, "17:30 / 18:30", "KT Zepp Yokohama",
                 "Yokohama", "Kanagawa", "tour", "FC members only", False, False, "", fit=show_fit,
                 stay="Nagoya" if show_fit == fit.DETOUR else None)
    s.rounds = [RoundView(1, "FC先行", "fanclub", True, "2026-11-06T12:00", "2026-11-15T23:59", None,
                          "https://example.com/apply", "¥9,800", "2 per person", "open"),
                RoundView(2, "一般発売", "general", False, "2026-12-19T10:00", None, None, "", "", "", "upcoming")]
    s.links = [("https://x.com/uverworld_dr2", "X")]
    return s


def preview(rules: Rules, show_fit: str, app_url: str = "") -> tuple[str, str]:
    """(message text, mode) of a new show with this fit under these rules; text is "" when it isn't posted."""
    mode = rules.mode(show_fit)
    if mode == OFF or not rules.kinds.get("new_show", True):
        return "", OFF
    s = sample_show(show_fit)
    return show_message(s, ["new_show"], s.rounds, app_url or "https://concerts.example.com", rules.lines), mode


def split(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
        while len(cur) > limit:
            parts.append(cur[:limit])
            cur = cur[limit:]
    if cur:
        parts.append(cur)
    return parts


NO_PREVIEWS = 4                 # Discord message flag: no link previews
SILENT = 4096                   # Discord message flag: no push notification


def send_discord(webhook: str, text: str, client: httpx.Client, ping: str = "", silent: bool = False) -> None:
    """Post `text` (split if long). ping: a Discord user id, mentioned on the last line so it pings only them;
    nothing else in a post can ping (not even an @everyone copied from a tweet). silent: no phone notification."""
    parts = split(text, DISCORD_LIMIT - 30)
    if ping:
        parts[-1] += f"\n<@{ping}>"
    for part in parts:
        for attempt in range(4):
            mentions = {"parse": [], "users": [ping]} if ping else {"parse": []}
            r = client.post(webhook, json={"content": part, "flags": NO_PREVIEWS | (SILENT if silent else 0),
                                           "allowed_mentions": mentions})
            if r.status_code == 429 and attempt < 3:       # rate limited: Discord says how long to wait
                try:
                    wait = float(r.json().get("retry_after", 2))
                except Exception:  # noqa: BLE001
                    wait = 2.0
                time.sleep(min(wait, 30) + 0.2)
                continue
            if r.status_code not in (200, 204):
                raise RuntimeError(f"Discord {r.status_code}: {r.text[:200]}")
            break


# ---------------------------------------------------------------- silent "scan started / finished" notices

TRIGGER = {"cron": "scheduled", "ui": "from the app", "cli": "by hand"}
FINISHED = {"ok": "✅ Scan done", "partial": "⚠️ Scan done, with errors", "error": "❌ Scan failed",
            "stopped": "⛔ Scan stopped partway", "running": "Scan"}
SOURCE_KINDS = (("x", "X"), ("instagram", "Instagram"), ("website", "website"), ("fanclub", "fan club"))


def plural(n: int, word: str, many: str = "") -> str:
    return f"{n:,} {word if n == 1 else (many or word + 's')}"


def fmt_duration(seconds: float) -> str:
    s = max(0, round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    return f"{m} min {s:02d} s" if m < 60 else f"{m // 60} h {m % 60:02d} min"


def who_text(conn: sqlite3.Connection, scope: str) -> str:
    ids = [int(p.split(":")[1]) for p in scope.split(",") if p.startswith("artist:")]
    if not ids:
        return "every artist"
    names = [r[0] for r in conn.execute(f"SELECT name FROM artists WHERE id IN ({','.join('?' * len(ids))})", ids)]
    return ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "") or "selected artists"


def scan_link(app_url: str, run_id: int) -> str:
    return f"[scan page](<{app_url.rstrip('/')}/scans/{run_id}>)" if app_url else f"scan #{run_id}"


def started_text(conn: sqlite3.Connection, run_id: int, sources: list, app_url: str = "") -> str:
    """'▶️ Scan started · scheduled · every artist · 89 sources (29 X, 24 Instagram, 36 websites)'."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    how = f"resuming scan #{run['resumed_from']}" if run["resumed_from"] else TRIGGER.get(run["trigger"], run["trigger"])
    counts = [plural(sum(1 for s in sources if s.kind == k), label) for k, label in SOURCE_KINDS
              if any(s.kind == k for s in sources)]
    what = plural(len(sources), "source") + (f" ({', '.join(counts)})" if counts else "")
    return f"▶️ Scan started · {how} · {who_text(conn, run['scope'])} · {what}\n{scan_link(app_url, run_id)}"


def finished_text(conn: sqlite3.Connection, run_id: int, app_url: str = "", stopped: bool = False) -> str:
    """Status, time taken, what it found and sent, the errors (first five), the cost, and a link."""
    from .progress import parse
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    t = conn.execute("SELECT COALESCE(SUM(CASE WHEN phase = 'read' THEN posts END), 0), "
                     "COALESCE(SUM(CASE WHEN kind = 'claude' THEN shows END), 0), "
                     "COALESCE(SUM(CASE WHEN kind = 'claude' THEN updates END), 0), "
                     "COALESCE(SUM(CASE WHEN kind = 'alerts' THEN done END), 0) FROM run_steps WHERE run_id = ?",
                     (run_id,)).fetchone()
    posts, shows, updates, sent = (int(x) for x in t)
    if not stopped:                                  # the run's own totals once it finished normally
        posts, shows, sent = run["posts_read"] or posts, run["shows_new"] or shows, run["alerts_sent"] or sent
    start, end = parse(run["started_at"]), parse(run["finished_at"])
    took = fmt_duration((end - start).total_seconds()) if start and end else "?"
    status = "stopped" if stopped else run["status"]
    lines = [" · ".join((FINISHED.get(status, status), took, plural(shows, "new show"), plural(updates, "update"),
                        plural(sent, "alert") + " sent")),
             f"{plural(posts, 'new post')} and changed pages · ${run['apify_usd'] or 0:.2f} Apify · "
             f"${run['claude_usd'] or 0:.3f} Claude"]
    if stopped and run["current"]:
        lines.append(f"Last thing it did: {run['current'].removeprefix('Stopped: ')}"[:300])
    lines += error_lines(conn, run_id, run["errors"] or "")
    lines.append(scan_link(app_url, run_id))
    return "\n".join(lines)


SOURCE_NAME = {"x": "𝕏 X", "instagram": "Instagram", "website": "Website", "fanclub": "Fan club"}
SOURCE_ORDER = ["x", "instagram", "website", "fanclub"]
COUNT_WORDS = {"x": ("X account", "X accounts"), "instagram": ("Instagram profile", "Instagram profiles"),
               "website": ("website", "websites"), "fanclub": ("fan club page", "fan club pages"),
               "other": ("other", "other")}
MAX_ERROR_LINES = 12          # X errors are always all listed; the rest up to this many lines


@dataclass
class SourceError:
    kind: str                 # x | instagram | website | fanclub | other
    artist: str
    source: str               # @handle or page
    error: str


def source_errors(conn: sqlite3.Connection, run_id: int, run_errors: str) -> tuple[list[SourceError], int]:
    """Every error of a scan with its artist and source (X first), and how many X accounts it read."""
    steps = conn.execute("SELECT kind, label, sublabel, status, error FROM run_steps WHERE run_id = ? AND phase = 'read' "
                         "ORDER BY position", (run_id,)).fetchall()
    out, labels = [], set()
    for s in steps:
        if s["status"] == "failed" or s["error"]:
            out.append(SourceError(s["kind"], s["sublabel"], s["label"], s["error"] or "failed"))
            labels.add(s["label"])
    for line in (e.strip() for e in run_errors.splitlines()):
        if not line or line.split(": ", 1)[0] in labels:
            continue                # already listed from its step
        if line.startswith("X / Instagram:") and any(e.kind in ("x", "instagram") for e in out):
            continue                # the whole Apify phase broke: each account is listed already
        out.append(SourceError("other", "", "", line))
    order = {k: i for i, k in enumerate(SOURCE_ORDER)}
    out.sort(key=lambda e: order.get(e.kind, 99))
    return out, sum(1 for s in steps if s["kind"] == "x")


def error_lines(conn: sqlite3.Connection, run_id: int, run_errors: str) -> list[str]:
    """'𝕏 X: all 29 accounts OK' (or how many failed), then each error with its source and artist."""
    errs, x_total = source_errors(conn, run_id, run_errors)
    x_bad = [e for e in errs if e.kind == "x"]
    lines = []
    if x_total:
        lines.append(f"❗ 𝕏 X: {len(x_bad)} of {plural(x_total, 'account')} failed" if x_bad
                     else f"𝕏 X: all {plural(x_total, 'account')} OK ✅")
    if not errs:
        return lines
    by_kind = [plural(sum(1 for e in errs if e.kind == k), *COUNT_WORDS[k])
               for k in SOURCE_ORDER + ["other"] if any(e.kind == k for e in errs)]
    lines.append(f"{plural(len(errs), 'error')}: {', '.join(by_kind)}")
    room = max(MAX_ERROR_LINES, len(x_bad))
    for e in errs[:room]:
        if e.kind == "other":
            lines.append(f"• {e.error[:200]}")
        else:
            who = " · ".join(b for b in (SOURCE_NAME.get(e.kind, e.kind), e.artist, e.source) if b)
            lines.append(f"• {who}: {(e.error.splitlines() or [''])[0][:160]}")
    if len(errs) > room:
        lines.append(f"• +{len(errs) - room} more on the scan page")
    return lines


def finished_pings(conn: sqlite3.Connection, run_id: int) -> bool:
    """The "scan finished" notice pings you when an X account failed (Alerts settings, on by default)."""
    if not load_rules(conn).x_fail_ping:
        return False
    run = conn.execute("SELECT errors FROM runs WHERE id = ?", (run_id,)).fetchone()
    errs, _ = source_errors(conn, run_id, (run["errors"] if run else "") or "")
    return any(e.kind == "x" for e in errs)


def notify(settings: Settings, text: str, client: httpx.Client | None = None, ping: bool = False) -> str | None:
    """Post a notice if DISCORD_SCAN_NOTICES is on: silent (no ping, no phone notification), or with ping=True a
    normal message that pings you (an X account failed). Never raises; returns the error, if any."""
    if not (settings.discord_scan_notices and settings.discord_webhook):
        return None
    own = client is None
    client = client or httpx.Client(timeout=15)
    try:
        if ping and settings.discord_user_id:
            send_discord(settings.discord_webhook, text, client, ping=settings.discord_user_id)
        else:
            send_discord(settings.discord_webhook, text, client, silent=True)
        return None
    except Exception as e:  # noqa: BLE001 - a notice must never break a scan or a page
        log.warning("Discord scan notice failed: %s", e)
        return str(e)
    finally:
        if own:
            client.close()


def deliver(conn: sqlite3.Connection, settings: Settings, messages: list[Message],
            client: httpx.Client | None = None, pause: float = 0.6,
            on_sent: Callable[[int, Message], None] | None = None) -> tuple[int, list[str]]:
    """Post each message; record its keys once Discord took it, so it's never sent twice.

    on_sent(count so far, message) is called after each message Discord accepted (for live progress).
    """
    def record(m: Message) -> None:
        with conn:
            conn.executemany(
                "INSERT OR IGNORE INTO alerts (key, artist_id, show_id, kind, text, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                [(k, m.artist_id, sid, kind, m.text[:4000], now()) for k, kind, sid in m.keys])

    for m in messages:
        if not m.postable:              # every update in it is turned off in the Alerts settings
            record(m)
    messages = [m for m in messages if m.postable]
    if not messages:
        return 0, []
    if not settings.discord_webhook:
        return 0, [f"DISCORD_WEBHOOK_URL is not set; {len(messages)} alert(s) kept for the next scan"]
    own = client is None
    client = client or httpx.Client(timeout=20)
    sent, errors = 0, []
    try:
        for i, m in enumerate(messages):
            if i and pause:
                time.sleep(pause)          # stay under Discord's ~30 messages a minute per channel
            try:
                if m.mode == PING:
                    send_discord(settings.discord_webhook, m.text, client, ping=settings.discord_user_id)
                else:
                    send_discord(settings.discord_webhook, m.text, client, silent=True)
            except Exception as e:  # noqa: BLE001 - keep the alert for the next scan
                log.error("Discord alert for %s failed: %s", m.artist, e)
                errors.append(f"Discord: {e}"[:300])
                continue
            sent += 1
            record(m)
            if on_sent:
                on_sent(sent, m)
    finally:
        if own:
            client.close()
    return sent, errors
