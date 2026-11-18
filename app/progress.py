"""Live progress of a scan, stored so the Scans page can show it (and survive a reload or a phone).

A scan plans every step up front (one per X account, Instagram profile, website, one Claude step per artist,
one alerts step), then marks them running / done / failed / skipped with counts as it goes. `runs.current`
is the one-line "what's happening this second". Log lines of the run go to `run_log`.
Every change bumps the step's `rev`, so the page only fetches what changed since its last poll.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

STATUSES = ("pending", "running", "done", "failed", "skipped")
KEEP_RUNS = 30            # progress and logs of older runs are deleted
LOG_LIMIT = 4000          # log lines kept per run

# Seconds a step usually takes, used until this run has measured its own (per unit for claude/alerts).
DEFAULT_SECONDS = {"x": 30.0, "instagram": 90.0, "website": 4.0, "claude": 1.2, "alerts": 1.0}
PER_UNIT = ("claude", "alerts")
POOLED = ("claude",)      # steps that run side by side; timed together by the wall clock
CLAUDE_GUESS_POSTS = 15   # posts per artist assumed before the read phase has counted them


def ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


@dataclass
class StepSpec:
    key: str
    phase: str
    kind: str
    label: str
    sublabel: str = ""
    artist_id: int | None = None
    grp: str = ""
    total: int = 0


class Progress:
    def __init__(self, conn: sqlite3.Connection, run_id: int) -> None:
        self.conn, self.run_id = conn, run_id
        self._rev = conn.execute("SELECT COALESCE(MAX(rev), 0) FROM run_steps WHERE run_id = ?",
                                 (run_id,)).fetchone()[0]

    def _bump(self) -> int:
        self._rev += 1
        return self._rev

    def plan(self, specs: list[StepSpec]) -> None:
        with self.conn:
            for i, s in enumerate(specs):
                self.conn.execute(
                    "INSERT OR IGNORE INTO run_steps (run_id, key, position, phase, kind, grp, label, sublabel, "
                    "artist_id, total, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.run_id, s.key, i, s.phase, s.kind, s.grp, s.label, s.sublabel, s.artist_id, s.total,
                     self._bump()))

    def add(self, spec: StepSpec) -> None:
        """A step found mid-run (appended at the end of the plan)."""
        pos = self.conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM run_steps WHERE run_id = ?",
                                (self.run_id,)).fetchone()[0]
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO run_steps (run_id, key, position, phase, kind, grp, label, sublabel, artist_id, "
                "total, rev) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (self.run_id, spec.key, pos, spec.phase, spec.kind, spec.grp, spec.label, spec.sublabel,
                 spec.artist_id, spec.total, self._bump()))

    def _set(self, key: str, **fields) -> None:
        fields["rev"] = self._bump()
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE run_steps SET {sets} WHERE run_id = ? AND key = ?",
                              (*fields.values(), self.run_id, key))

    def start(self, key: str, detail: str = "", **fields) -> None:
        self._set(key, status="running", started_at=ts(), detail=detail, **fields)

    def update(self, key: str, **fields) -> None:
        self._set(key, **fields)

    def add_to(self, key: str, **deltas: float) -> None:
        """Increment counters (posts, shows, updates, usd, done, total)."""
        sets = ", ".join(f"{k} = {k} + ?" for k in deltas)
        with self.conn:
            self.conn.execute(f"UPDATE run_steps SET {sets}, rev = ? WHERE run_id = ? AND key = ?",
                              (*deltas.values(), self._bump(), self.run_id, key))

    def finish(self, key: str, status: str = "done", error: str = "", detail: str | None = None, **fields) -> None:
        assert status in STATUSES
        if detail is not None:
            fields["detail"] = detail
        row = self.conn.execute("SELECT started_at FROM run_steps WHERE run_id = ? AND key = ?",
                                (self.run_id, key)).fetchone()
        started = {} if (row and row[0]) or status == "skipped" else {"started_at": ts()}
        self._set(key, status=status, error=error[:500], finished_at=ts(), **started, **fields)

    def skip_pending(self, keys: list[str] | None = None, why: str = "") -> None:
        """Mark still-pending steps skipped (e.g. no API key, or the scan stopped early)."""
        rows = self.conn.execute("SELECT key FROM run_steps WHERE run_id = ? AND status IN ('pending', 'running')",
                                 (self.run_id,)).fetchall()
        for (k,) in rows:
            if keys is None or k in keys:
                self.finish(k, "skipped", detail=why)

    def now(self, text: str) -> None:
        """The one line saying what the scan is doing this second. Also the run's heartbeat."""
        with self.conn:
            self.conn.execute("UPDATE runs SET current = ?, current_at = ?, heartbeat_at = ?, summary = ? WHERE id = ?",
                              (text[:300], ts(), datetime.now(timezone.utc).isoformat(timespec="seconds"), text[:300],
                               self.run_id))

    def costs(self, apify_usd: float, claude_usd: float) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET apify_usd = ?, claude_usd = ? WHERE id = ?",
                              (round(apify_usd, 4), round(claude_usd, 4), self.run_id))


class RunLogHandler(logging.Handler):
    """Copies the scan's log lines into run_log so the page can tail them.

    The database connection belongs to the scan's main thread: lines logged by worker threads (the parallel Claude
    calls) wait in a queue and are written by the main thread's next log line or flush().
    """

    def __init__(self, conn: sqlite3.Connection, run_id: int, level: int = logging.INFO) -> None:
        super().__init__(level)
        self.conn, self.run_id, self.count = conn, run_id, 0
        self.owner = threading.get_ident()
        self.waiting: deque[tuple[str, str, str]] = deque()

    def emit(self, record: logging.LogRecord) -> None:
        if self.count + len(self.waiting) >= LOG_LIMIT:
            return
        try:
            msg = record.getMessage()
            if record.exc_info and record.levelno >= logging.ERROR:
                msg += f" ({record.exc_info[1]!r})"[:300]
            self.waiting.append((ts(), record.levelname, msg[:1000]))
            if threading.get_ident() == self.owner:
                self.flush()
        except Exception:  # noqa: BLE001 - logging must never break the scan
            self.handleError(record)

    def flush(self) -> None:
        if threading.get_ident() != self.owner or not self.waiting:
            return
        rows = []
        while self.waiting:
            rows.append(self.waiting.popleft())
        with self.conn:
            self.conn.executemany("INSERT INTO run_log (run_id, at, level, message) VALUES (?, ?, ?, ?)",
                                  [(self.run_id, *r) for r in rows])
        self.count += len(rows)


def prune(conn: sqlite3.Connection) -> None:
    """Keep progress and logs for the last KEEP_RUNS runs only."""
    with conn:
        cut = conn.execute("SELECT id FROM runs ORDER BY id DESC LIMIT 1 OFFSET ?", (KEEP_RUNS - 1,)).fetchone()
        if cut:
            conn.execute("DELETE FROM run_steps WHERE run_id < ?", (cut[0],))
            conn.execute("DELETE FROM run_log WHERE run_id < ?", (cut[0],))


# ---------------------------------------------------------------- reading it back

def step_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in ("id", "key", "position", "phase", "kind", "grp", "label", "sublabel", "artist_id",
                              "status", "started_at", "finished_at", "done", "total", "posts", "shows", "updates",
                              "usd", "detail", "error", "rev", "retries")}


def seconds(a: str | None, b: str | None) -> float:
    da, db_ = parse(a), parse(b)
    return max(0.0, (db_ - da).total_seconds()) if da and db_ else 0.0


def estimate(steps: list[dict], at: datetime) -> dict:
    """Overall fraction done and seconds left, from how long this run's own finished steps took.

    Steps in one group (accounts read by a single Apify call) count once. Claude and alerts are timed per unit
    (post, message). Returns {"fraction", "eta", "measured"}; eta is None until something of the kind still to
    come has been timed (or 20 s have passed), so the page doesn't show a wild guess.
    """
    groups: dict[str, list[dict]] = {}
    for s in steps:
        groups.setdefault(s["grp"] or f"step:{s['id']}", []).append(s)

    measured: dict[str, list[tuple[float, float]]] = {}       # kind -> [(seconds, units)]
    for g in groups.values():
        lead = g[0]
        if all(s["status"] in ("done", "failed") for s in g) and all(s["started_at"] for s in g):
            secs = max(seconds(s["started_at"], s["finished_at"]) for s in g)
            units = sum(max(s["done"], 1) for s in g) if lead["kind"] in PER_UNIT else 1
            measured.setdefault(lead["kind"], []).append((secs, units))

    def rate(kind: str) -> float:
        m = measured.get(kind)
        if not m:
            return DEFAULT_SECONDS.get(kind, 5.0)
        return sum(s for s, _ in m) / max(sum(u for _, u in m), 1)

    live = False
    # Time spent is the wall clock since the first step started: Apify runs, websites and Claude calls overlap.
    started_any = [parse(s["started_at"]) for s in steps if s["started_at"]]
    run_elapsed = (at - min(started_any)).total_seconds() if started_any else 0
    spent = max(run_elapsed, 0.0)

    # Claude steps run side by side (several calls in flight), so they're timed as one pool by the wall clock.
    claude_left = 0.0
    pool = [s for s in steps if s["kind"] in POOLED]
    if pool:
        starts = [parse(s["started_at"]) for s in pool if s["started_at"]]
        active = [s for s in pool if s["status"] in ("pending", "running")]
        busy = any(s["status"] == "running" for s in pool)
        ends = [parse(s["finished_at"]) for s in pool if s["started_at"] and s["finished_at"]]
        wall = ((at if busy else max(ends, default=at)) - min(starts)).total_seconds() if starts else 0.0
        units_done = sum(s["done"] for s in pool)
        left = sum(max(s["total"] - s["done"], 0) if s["total"] else CLAUDE_GUESS_POSTS for s in active)
        if busy and units_done and wall > 0:
            per_unit, live = wall / units_done, True
        else:
            per_unit = rate("claude")
        claude_left = left * per_unit

    # Apify runs go side by side (about as many as are running now), while the websites are read one by one on
    # the scan's own thread at the same time; the read phase ends when the slower of the two does.
    social_running: list[float] = []
    social_pending: list[float] = []
    web_left = other_left = 0.0
    for g in groups.values():
        lead = g[0]
        kind = lead["kind"]
        if kind in POOLED:
            continue
        statuses = {s["status"] for s in g}
        if statuses <= {"done", "failed", "skipped"}:
            continue
        if kind in PER_UNIT:
            total = sum(s["total"] for s in g) or 1
            expected_left = max(total - sum(s["done"] for s in g), 0) * rate(kind)
        else:
            expected_left = rate(kind)
        running_now = "running" in statuses
        if running_now and kind not in PER_UNIT:
            started = min((parse(s["started_at"]) for s in g if s["started_at"]), default=at)
            expected_left = max(expected_left - max((at - started).total_seconds(), 0.0), 1.0)
        if kind in ("x", "instagram"):
            (social_running if running_now else social_pending).append(expected_left)
        elif kind == "website":
            web_left += expected_left
        else:
            other_left += expected_left
    social_left = max(social_running, default=0.0) + sum(social_pending) / max(len(social_running), 1)
    remaining = max(social_left, web_left) + claude_left + other_left
    total = spent + remaining
    fraction = spent / total if total else 1.0
    trustworthy = bool(measured or live) and run_elapsed >= 20
    return {"fraction": round(min(fraction, 0.999 if remaining else 1.0), 4),
            "eta": round(remaining) if trustworthy and remaining else (0 if not remaining else None),
            "measured": sorted(measured)}


def snapshot(conn: sqlite3.Connection, run_id: int, since_rev: int = 0, after_log: int = 0,
             log_limit: int = 300) -> dict | None:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not run:
        return None
    all_steps = [step_dict(r) for r in conn.execute("SELECT * FROM run_steps WHERE run_id = ? ORDER BY position",
                                                    (run_id,))]
    at = datetime.now(timezone.utc)
    est = estimate(all_steps, at) if run["status"] == "running" else {"fraction": 1.0, "eta": 0, "measured": []}
    logs = conn.execute("SELECT id, at, level, message FROM run_log WHERE run_id = ? AND id > ? ORDER BY id DESC "
                        "LIMIT ?", (run_id, after_log, log_limit)).fetchall()
    counts = {st: 0 for st in STATUSES}
    for s in all_steps:
        counts[s["status"]] += 1
    totals = {
        "posts": sum(s["posts"] for s in all_steps if s["phase"] == "read"),
        "classified": sum(s["done"] for s in all_steps if s["kind"] == "claude"),
        "to_classify": sum(s["total"] for s in all_steps if s["kind"] == "claude"),
        "shows": sum(s["shows"] for s in all_steps if s["kind"] == "claude"),
        "updates": sum(s["updates"] for s in all_steps if s["kind"] == "claude"),
        "alerts": sum(s["total"] for s in all_steps if s["kind"] == "alerts"),
        "alerts_sent": sum(s["done"] for s in all_steps if s["kind"] == "alerts"),
        "retries": sum(s["retries"] for s in all_steps),
    }
    from .store import resumable
    return {
        "run": {**{k: run[k] for k in ("id", "started_at", "finished_at", "trigger", "scope", "dry_run", "status",
                                        "current", "current_at", "summary", "errors", "apify_usd", "claude_usd",
                                        "posts_read", "shows_new", "alerts_sent", "resumed_from")},
                "resumable": run["status"] == "error" and resumable(conn, run_id)},
        "server_now": at.isoformat(timespec="milliseconds"),
        "steps": [s for s in all_steps if s["rev"] > since_rev],
        "rev": max((s["rev"] for s in all_steps), default=0),
        "counts": counts,
        "totals": totals,
        "estimate": est,
        "log": [dict(r) for r in reversed(logs)],
    }
