"""Web UI: artist registry, shows, trip itinerary, scans.

Run: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import math
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import date, datetime, timezone
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import alerts, db, fit, progress, store
from .alerts import fmt_when, round_line
from .auth import SESSION_DAYS, Lockout, OriginCheck, fingerprint, password_ok
from .config import ROOT, Settings
from .sources import COVERAGE_KINDS, KINDS, PAGE_KINDS, SHORT, SOCIAL_KINDS, parse_paste, resolve, social_url


def jst(value: str | None) -> str:
    """UTC ISO timestamp -> 'Nov 14 21:00' in Japan time."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value).astimezone(store.JST)
    except ValueError:
        return value
    return f"{dt:%b} {dt.day} {dt:%H:%M}"


def day(d: date | None) -> str:
    return f"{d:%a} {d.day} {d:%b}" if d else "Date TBA"


templates = Jinja2Templates(directory=ROOT / "templates")
templates.env.globals.update(KINDS=KINDS, SHORT=SHORT, COVERAGE_KINDS=COVERAGE_KINDS, TIERS=db.TIERS,
                             FIT_LABEL=fit.FIT_LABEL)
templates.env.filters.update(jst=jst, when=fmt_when, day=day, round_line=round_line)

MISSING_LABEL = {"instagram": "an Instagram account", "x": "an X account", "website": "a website"}
FIT_FILTERS = ("fits", "detour", "outside", "tba")


def launch_scan(artist_id: int | None = None, resume: int | None = None) -> None:
    """Start a scan in its own process so the page returns at once. The scan takes the run lock itself.

    The process is detached from the web server so restarting the server doesn't stop it: its own session on
    Linux (and the systemd unit has KillMode=process, so a restart leaves it running), and on Windows no console
    and out of the server's job when that's allowed (a dev server's tooling may kill the job's whole tree)."""
    cmd = [sys.executable, "-m", "app.watch", "--trigger", "ui"]
    if artist_id:
        cmd += ["--artist", str(artist_id)]
    if resume:
        cmd += ["--resume", str(resume)]
    common = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  close_fds=True)
    if sys.platform != "win32":
        subprocess.Popen(cmd, start_new_session=True, **common)
        return
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common)
    except OSError:                       # the server's job doesn't allow breaking away
        subprocess.Popen(cmd, creationflags=flags, **common)


class LoginRequired(Exception):
    pass


def is_fetch(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.check()
    db.init_db(settings.database_path)

    app = FastAPI(title="Concert Watcher", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.lockout = Lockout()
    app.state.launch_scan = launch_scan
    app.state.notify = alerts.notify          # tests swap in a stub
    session_fp = fingerprint(settings.app_password, settings.secret_key)

    def stopped_notice(conn: sqlite3.Connection, run_id: int) -> None:
        """A scan's process was found gone: the silent "scan stopped partway" Discord notice, posted in the
        background so the page doesn't wait for Discord."""
        if settings.discord_scan_notices and settings.discord_webhook:
            text = alerts.finished_text(conn, run_id, settings.app_url, stopped=True)
            threading.Thread(target=app.state.notify, args=(settings, text), daemon=True).start()

    app.add_middleware(OriginCheck)
    app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="cw_session",
                       max_age=SESSION_DAYS * 86400, same_site="lax", https_only=settings.cookie_secure)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    # ------------------------------------------------------------ plumbing

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.database_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_login(request: Request) -> None:
        if request.session.get("auth") != session_fp:
            raise LoginRequired()

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        if is_fetch(request):
            return JSONResponse({"ok": False, "error": "Signed out. Reload to sign in."}, status_code=401)
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)

    def flash(request: Request, message: str) -> None:
        request.session["flash"] = message

    def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.setdefault("flash", request.session.pop("flash", None))
        ctx.setdefault("nav", "artists")
        if "trip_label" not in ctx:
            conn = db.connect(settings.database_path)
            try:
                bounds = fit.trip_bounds(store.itinerary(conn))
                running = store.running_run(conn, stopped_notice)
            finally:
                conn.close()
            ctx["trip_label"] = (f"{bounds[0]:%b} {bounds[0].day} – {bounds[1]:%b} {bounds[1].day}, {bounds[1].year}"
                                 if bounds else "No trip dates")
            ctx.setdefault("scan_running", running is not None)
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def list_context(conn: sqlite3.Connection, q: str = "", missing: str = "") -> dict:
        artists = db.list_artists(conn)
        missing = missing if missing in COVERAGE_KINDS else ""
        watching = [a for a in artists if a.enabled]
        shown = watching
        if missing:
            shown = [a for a in shown if a.missing(missing)]
        if q.strip():
            needle = db.fold(q.strip())
            shown = [a for a in shown if needle in a.search_text()]
        paused = [] if missing else [a for a in artists if not a.enabled
                                     and (not q.strip() or db.fold(q.strip()) in a.search_text())]
        return {
            "groups": [(t, [a for a in shown if a.tier == t]) for t in db.TIERS],
            "paused": paused,
            "watching_count": len(watching),
            "paused_count": len(artists) - len(watching),
            "coverage": db.coverage(artists),
            "missing": missing,
            "missing_label": MISSING_LABEL.get(missing, ""),
            "shown_count": len(shown),
            "q": q,
        }

    def load_artist(conn: sqlite3.Connection, artist_id: int) -> db.Artist:
        artist = db.get_artist(conn, artist_id)
        if not artist:
            raise HTTPException(404, "No such artist")
        return artist

    def artist_page(request: Request, conn: sqlite3.Connection, artist: db.Artist, status_code: int = 200,
                    **ctx) -> HTMLResponse:
        shows = store.sort_shows(store.load_shows(conn, artist_id=artist.id))
        return render(request, "artist.html", status_code=status_code, artist=artist,
                      selected_id=artist.id, artist_shows=shows["deadlines"] + shows["dated"] + shows["tba"],
                      ig_waits=ig_waits(conn, artist.sources), ig_every=store.prefs(conn, settings).instagram_cooldown_hours,
                      **list_context(conn), **ctx)

    def clean_artist_form(name: str, name_ja: str, tier: str, notes: str = "") -> tuple[dict, str | None]:
        data = {"name": " ".join(name.split()), "name_ja": " ".join(name_ja.split()),
                "tier": tier.strip().upper(), "notes": notes.strip()}
        if not data["name"]:
            return data, "Name can't be empty."
        if len(data["name"]) > 120 or len(data["name_ja"]) > 120:
            return data, "Names are limited to 120 characters."
        if data["tier"] not in db.TIERS:
            return data, "Tier must be S, A or B."
        if len(data["notes"]) > 5000:
            return data, "Notes are limited to 5000 characters."
        return data, None

    # ------------------------------------------------------------ login

    @app.get("/login")
    def login_form(request: Request, next: str = "/"):
        if request.session.get("auth") == session_fp:
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", next=next)

    @app.post("/login")
    def login(request: Request, password: str = Form(""), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        lockout: Lockout = app.state.lockout
        wait = lockout.seconds_left(ip)
        if wait:
            return render(request, "login.html", status_code=429, next=next,
                          error=f"Too many tries. Wait {max(1, round(wait / 60))} min and try again.")
        if not password_ok(password, settings.app_password):
            lockout.fail(ip)
            return render(request, "login.html", status_code=401, next=next, error="That's not the password.")
        lockout.clear(ip)
        request.session.clear()
        request.session["auth"] = session_fp
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        return RedirectResponse(safe_next, status_code=303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    # ------------------------------------------------------------ artists

    @app.get("/", dependencies=[Depends(require_login)])
    def home(request: Request, q: str = "", missing: str = "", conn=Depends(get_conn)):
        return render(request, "home.html", selected_id=None, **list_context(conn, q, missing))

    @app.get("/artists/new", dependencies=[Depends(require_login)])
    def new_artist_form(request: Request, conn=Depends(get_conn)):
        return render(request, "artist_new.html", selected_id=None, form={"tier": "A"}, **list_context(conn))

    @app.post("/artists", dependencies=[Depends(require_login)])
    def create_artist(request: Request, name: str = Form(""), name_ja: str = Form(""), tier: str = Form("A"),
                      conn=Depends(get_conn)):
        data, error = clean_artist_form(name, name_ja, tier)
        if not error:
            try:
                artist_id = db.create_artist(conn, data["name"], data["name_ja"], data["tier"])
            except db.NameTaken:
                error = f"There's already an artist called {data['name']}."
        if error:
            return render(request, "artist_new.html", status_code=400, selected_id=None, form=data, error=error,
                          **list_context(conn))
        flash(request, f"Added {data['name']}. Paste their links below.")
        return RedirectResponse(f"/artists/{artist_id}", status_code=303)

    @app.get("/artists/{artist_id}", dependencies=[Depends(require_login)])
    def show_artist(request: Request, artist_id: int, conn=Depends(get_conn)):
        return artist_page(request, conn, load_artist(conn, artist_id))

    @app.post("/artists/{artist_id}", dependencies=[Depends(require_login)])
    def update_artist(request: Request, artist_id: int, name: str = Form(""), name_ja: str = Form(""),
                      tier: str = Form(""), notes: str = Form(""), enabled: str = Form(""),
                      conn=Depends(get_conn)):
        artist = load_artist(conn, artist_id)
        data, error = clean_artist_form(name, name_ja, tier, notes)
        if not error:
            try:
                db.update_artist(conn, artist_id, enabled=enabled in {"on", "1", "true"}, **data)
            except db.NameTaken:
                error = f"There's already an artist called {data['name']}."
        if is_fetch(request):
            if error:
                return JSONResponse({"ok": False, "error": error}, status_code=400)
            return JSONResponse({"ok": True})
        if error:
            return artist_page(request, conn, artist, status_code=400, details_error=error)
        flash(request, "Saved.")
        return RedirectResponse(f"/artists/{artist_id}", status_code=303)

    @app.post("/artists/{artist_id}/delete", dependencies=[Depends(require_login)])
    def delete_artist(request: Request, artist_id: int, conn=Depends(get_conn)):
        artist = load_artist(conn, artist_id)
        db.delete_artist(conn, artist_id)
        flash(request, f"Deleted {artist.name}.")
        return RedirectResponse("/", status_code=303)

    # ------------------------------------------------------------ sources

    @app.post("/artists/{artist_id}/sources/preview", dependencies=[Depends(require_login)])
    def preview_sources(request: Request, artist_id: int, paste: str = Form(""), conn=Depends(get_conn)):
        artist = load_artist(conn, artist_id)
        candidates = parse_paste(paste, db.existing_keys(artist))
        return templates.TemplateResponse(request, "_preview.html", {"artist": artist, "candidates": candidates})

    @app.post("/artists/{artist_id}/sources", dependencies=[Depends(require_login)])
    async def add_sources(request: Request, artist_id: int, conn=Depends(get_conn)):
        form = await request.form()
        paste = str(form.get("paste", ""))
        picks = {int(k[5:]): str(v) for k, v in form.items() if k.startswith("kind_") and k[5:].isdigit()}
        artist = load_artist(conn, artist_id)
        existing = db.existing_keys(artist)
        candidates = parse_paste(paste, existing)
        ready, unresolved = resolve(candidates, picks, existing)
        error = None
        if unresolved:
            names = ", ".join(candidates[i].display for i in unresolved)
            error = f"Pick Instagram or X for {names}."
        elif not ready:
            error = "Nothing new to add." if candidates else "Paste a link or an @handle first."
        if error:
            return artist_page(request, conn, artist, status_code=400, paste=paste, candidates=candidates,
                               picks=picks, paste_error=error)
        added = db.add_sources(conn, artist_id, ready)
        flash(request, f"Added {added} source{'s' if added != 1 else ''}.")
        return RedirectResponse(f"/artists/{artist_id}#sources", status_code=303)

    @app.post("/sources/{source_id}", dependencies=[Depends(require_login)])
    def update_source(request: Request, source_id: int, kind: str = Form(""), label: str = Form(""),
                      conn=Depends(get_conn)):
        source = db.get_source(conn, source_id)
        if not source:
            raise HTTPException(404, "No such source")
        kind = kind if kind in KINDS else source.kind
        url, handle = source.url, source.handle
        if source.kind in SOCIAL_KINDS:
            kind = kind if kind in SOCIAL_KINDS else source.kind
            url = social_url(kind, handle)
        else:
            kind = kind if kind in PAGE_KINDS else source.kind
        label = " ".join(label.split())[:40]
        if db.update_source(conn, source_id, kind=kind, url=url, handle=handle, label=label):
            flash(request, "Saved.")
        else:
            flash(request, f"{source.display} is already listed as {KINDS[kind]}.")
        return RedirectResponse(f"/artists/{source.artist_id}#source-{source_id}", status_code=303)

    @app.post("/sources/{source_id}/delete", dependencies=[Depends(require_login)])
    def delete_source(request: Request, source_id: int, conn=Depends(get_conn)):
        source = db.get_source(conn, source_id)
        if not source:
            raise HTTPException(404, "No such source")
        db.delete_source(conn, source_id)
        flash(request, f"Removed {source.display}.")
        return RedirectResponse(f"/artists/{source.artist_id}#sources", status_code=303)

    # ------------------------------------------------------------ shows

    @app.get("/shows", dependencies=[Depends(require_login)])
    def shows_page(request: Request, fit: str = "", hidden: str = "", conn=Depends(get_conn)):
        fit_filter = fit if fit in FIT_FILTERS else ""
        all_shows = store.load_shows(conn, include_hidden=bool(hidden))
        counts = {f: sum(1 for s in all_shows if s.fit == f and not s.hidden) for f in FIT_FILTERS}
        shown = [s for s in all_shows if (not fit_filter or s.fit == fit_filter) and (s.hidden if hidden else True)]
        hidden_count = conn.execute("SELECT COUNT(*) FROM shows WHERE hidden = 1").fetchone()[0]
        return render(request, "shows.html", groups=store.sort_shows(shown), fit_filter=fit_filter,
                      counts=counts, total=sum(counts.values()), hidden=bool(hidden), hidden_count=hidden_count,
                      last_run=next(iter(store.recent_runs(conn, 1)), None), nav="shows")

    @app.post("/shows/{show_id}/hide", dependencies=[Depends(require_login)])
    def hide_show(request: Request, show_id: int, hide: str = Form("1"), back: str = Form("/shows"),
                  conn=Depends(get_conn)):
        store.set_hidden(conn, show_id, hide == "1")
        flash(request, "Hidden. It won't alert again." if hide == "1" else "Shown again.")
        return RedirectResponse(back if back.startswith("/") and not back.startswith("//") else "/shows",
                                status_code=303)

    # ------------------------------------------------------------ trip

    @app.get("/trip", dependencies=[Depends(require_login)])
    def trip_page(request: Request, conn=Depends(get_conn)):
        segments = store.itinerary(conn)
        rows = [(s.start.isoformat(), s.end.isoformat(), s.city) for s in segments]
        return render(request, "trip.html", rows=rows, segments=segments, nav="trip",
                      window=fit.watch_window(segments))

    @app.post("/trip", dependencies=[Depends(require_login)])
    async def save_trip(request: Request, conn=Depends(get_conn)):
        form = await request.form()
        starts, ends, cities = form.getlist("start"), form.getlist("end"), form.getlist("city")
        rows = [(str(s), str(e), str(c)) for s, e, c in zip(starts, ends, cities)]
        segments, error = fit.check_segments(rows)
        if not error and not segments:
            error = "Add at least one stay."
        if error:
            return render(request, "trip.html", status_code=400, rows=rows, segments=store.itinerary(conn),
                          error=error, nav="trip", window=fit.watch_window(store.itinerary(conn)))
        store.save_itinerary(conn, segments)
        flash(request, "Trip saved. Fits are updated everywhere.")
        return RedirectResponse("/trip", status_code=303)

    # ------------------------------------------------------------ scans

    def ig_waits(conn: sqlite3.Connection, sources) -> dict[int, int]:
        """Instagram sources the next scan won't read (the Schedule's interval): source id -> hours until it does."""
        p, at = store.prefs(conn, settings), datetime.now(timezone.utc)
        out = {}
        for s in sources:
            if s.kind == "instagram":
                h = store.ig_wait_hours(s.cursor, p.instagram_cooldown_hours, at)
                if h is not None:
                    out[s.id] = max(1, math.ceil(h))
        return out

    def schedule_context(conn: sqlite3.Connection, **extra) -> dict:
        p = store.prefs(conn, settings)
        nxt = store.next_scheduled(conn, p, datetime.now(timezone.utc)).astimezone(store.JST)
        return {"prefs": p, "next_scan": f"{nxt:%a} {nxt.day} {nxt:%b} {nxt:%H:%M}",
                "never_scheduled": store.last_scheduled_start(conn) is None, **extra}

    @app.post("/scans/schedule", dependencies=[Depends(require_login)])
    def save_schedule(request: Request, scan_every_hours: str = Form(""), instagram_cooldown_hours: str = Form(""),
                      conn=Depends(get_conn)):
        values, errors = {}, []
        for key, raw, label in (("scan_every_hours", scan_every_hours, "Scan every"),
                                ("instagram_cooldown_hours", instagram_cooldown_hours, "Instagram cooldown")):
            lo, hi = store.PREF_RANGE[key]
            try:
                n = int(raw.strip())
            except ValueError:
                errors.append(f"{label}: enter whole hours.")
                continue
            if not lo <= n <= hi:
                errors.append(f"{label}: {lo} to {hi} hours.")
                continue
            values[key] = n
        if errors:
            flash(request, " ".join(errors))
            return RedirectResponse("/scans#schedule", status_code=303)
        store.save_prefs(conn, **values)
        ctx = schedule_context(conn)
        flash(request, f"Saved. Next scheduled scan: {ctx['next_scan']} Japan time.")
        return RedirectResponse("/scans#schedule", status_code=303)

    @app.get("/scans", dependencies=[Depends(require_login)])
    def scans_page(request: Request, started: str = "", conn=Depends(get_conn)):
        artists = db.list_artists(conn)
        waits = ig_waits(conn, [s for a in artists for s in a.sources])
        problems = [(a, s) for a in artists if a.enabled for s in a.sources
                    if s.last_status == "error" and s.id not in waits]
        never = [(a, s) for a in artists if a.enabled for s in a.sources if s.scannable and not s.last_scanned_at]
        apify_usd, claude_usd = store.month_costs(conn)
        keys = {"Apify (X, Instagram)": bool(settings.apify_token), "Claude": bool(settings.anthropic_key),
                "Discord alerts": bool(settings.discord_webhook),
                "Discord pings (DISCORD_USER_ID)": bool(settings.discord_user_id)}
        running = store.running_run(conn, stopped_notice)
        snap = progress.snapshot(conn, running["id"]) if running else None
        after = int(started) if started.isdigit() else 0
        starting = started.isdigit() and not running
        last = conn.execute("SELECT * FROM runs WHERE dry_run = 0 ORDER BY id DESC LIMIT 1").fetchone()
        stopped = last if last and not running and not starting and store.resumable(conn, last["id"]) else None
        return render(request, "scans.html", runs=store.recent_runs(conn, 20), problems=problems, never=never,
                      apify_usd=apify_usd, claude_usd=claude_usd, keys=keys, running=running, snap=snap,
                      starting=starting, after_run=after, stopped=stopped, nav="scans", **schedule_context(conn))

    @app.get("/scans/{run_id}", dependencies=[Depends(require_login)])
    def run_page(request: Request, run_id: int, conn=Depends(get_conn)):
        store.reap(conn, on_stopped=stopped_notice)
        snap = progress.snapshot(conn, run_id, log_limit=progress.LOG_LIMIT)
        if not snap:
            raise HTTPException(404, "No such scan")
        return render(request, "run.html", snap=snap, run=snap["run"], read=store.run_read_counts(conn, run_id),
                      nav="scans")

    @app.get("/scans/{run_id}/data", dependencies=[Depends(require_login)])
    def run_data(request: Request, run_id: int, show: str = "", artist: int = 0, offset: int = 0,
                 conn=Depends(get_conn)):
        """What a scan read and what Claude decided: a page of it, as HTML for the scan page's data view."""
        if not conn.execute("SELECT 1 FROM runs WHERE id = ?", (run_id,)).fetchone():
            raise HTTPException(404, "No such scan")
        verdict = show if show in store.VERDICTS else ""
        page = 40
        items = store.run_read_items(conn, run_id, verdict, artist or None, max(offset, 0), page + 1)
        return templates.TemplateResponse(request, "_data.html", {
            "items": items[:page], "more": len(items) > page, "next": max(offset, 0) + page, "run_id": run_id,
            "verdict": verdict, "artist": artist, "counts": store.run_read_counts(conn, run_id) if not offset else None,
            "offset": offset})

    @app.post("/scans/{run_id}/resume", dependencies=[Depends(require_login)])
    def resume_scan(request: Request, run_id: int, conn=Depends(get_conn)):
        if store.running_run(conn, stopped_notice):
            flash(request, "A scan is already running; here's its progress.")
            return RedirectResponse("/scans", status_code=303)
        if not store.resumable(conn, run_id):
            flash(request, "That scan can't be resumed: a newer scan has run since. Start a new one instead.")
            return RedirectResponse("/scans", status_code=303)
        last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]
        app.state.launch_scan(None, resume=run_id)
        return RedirectResponse(f"/scans?started={last}", status_code=303)

    @app.get("/scan/progress", dependencies=[Depends(require_login)])
    def scan_progress(run: int = 0, rev: int = 0, log: int = 0, after: int = 0, conn=Depends(get_conn)):
        """Changes since the page's last poll. run=0: the running scan, or the first one started after `after`.
        A scan whose process is gone is closed here, so the page shows it stopped within one poll."""
        store.reap(conn, on_stopped=stopped_notice)
        if not run:
            row = store.running_run(conn, stopped_notice) or conn.execute(
                "SELECT * FROM runs WHERE id > ? AND dry_run = 0 ORDER BY id LIMIT 1", (after,)).fetchone()
            if not row:
                return {"waiting": True}
            run, rev, log = row["id"], 0, 0
        snap = progress.snapshot(conn, run, since_rev=rev, after_log=log)
        if not snap:
            raise HTTPException(404, "No such scan")
        return snap

    def start_scan(request: Request, conn: sqlite3.Connection, artist_id: int | None, back: str) -> Response:
        if store.running_run(conn, stopped_notice):
            flash(request, "A scan is already running; here's its progress.")
            return RedirectResponse("/scans", status_code=303)
        last = conn.execute("SELECT COALESCE(MAX(id), 0) FROM runs").fetchone()[0]
        app.state.launch_scan(artist_id)
        return RedirectResponse(f"/scans?started={last}", status_code=303)

    @app.post("/scan", dependencies=[Depends(require_login)])
    def scan_all(request: Request, conn=Depends(get_conn)):
        return start_scan(request, conn, None, "/scans")

    @app.post("/artists/{artist_id}/scan", dependencies=[Depends(require_login)])
    def scan_artist(request: Request, artist_id: int, conn=Depends(get_conn)):
        load_artist(conn, artist_id)
        return start_scan(request, conn, artist_id, f"/artists/{artist_id}")

    @app.get("/scan/status", dependencies=[Depends(require_login)])
    def scan_status(conn=Depends(get_conn)):
        run = store.running_run(conn, stopped_notice)
        return {"running": run is not None, "summary": run["summary"] if run else ""}

    return app
