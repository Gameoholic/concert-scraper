# Step 1: artist registry + UI

What gets built in step 1 of [PLAN.md](PLAN.md). Preview of the screens: https://claude.ai/artifact/6D6JHMRH8dZDU8o2saBjaf

## Stack (engineer's call)

- **FastAPI + Jinja2, server-rendered, a little vanilla JS.** One Python process, no JS build step, and the scraper/classifier in later steps reuse the same `db.py`. No htmx or other vendored JS: the two dynamic bits (paste preview, autosave) are ~100 lines of `fetch`.
- **SQLite** via the stdlib `sqlite3`, WAL mode, schema versioned with `PRAGMA user_version`. File at `data/concerts.db` (`DATABASE_PATH` overrides).
- **System fonts** (SF + Hiragino on iPhone, Segoe/Yu Gothic on Windows), so no external requests. Light and dark follow the phone's setting.
- Runs the same on Windows (dev) and Linux (VPS): `uvicorn app.main:app`.

## Data

```
artists  id · name (unique) · name_ja · tier S|A|B · notes · enabled · created_at · updated_at
sources  id · artist_id → artists (cascade) · kind · handle · url · label · position · created_at
         last_scanned_at · last_status (ok|empty|error) · last_error · last_found   ← empty until step 2
         unique (artist_id, kind, url)
```

- `kind`: `instagram`, `x`, `website`, `fanclub`, `other`. New kinds are a code change, not a migration.
- `handle` is set for instagram/x; `url` is always the canonical link (what the "open" button opens).
- `label` is free text: "Main", "Staff", "Schedule", "Home".
- Coverage ("missing Instagram / X / website") is computed from `sources`, counted over watching artists only.

## Pasting sources

One box per artist; paste any number of lines (newlines, spaces or commas split them). A live preview shows what each line becomes before anything is saved.

| Pasted | Becomes |
| --- | --- |
| `https://www.instagram.com/foo/?hl=ja` | Instagram `@foo` → `https://www.instagram.com/foo/` |
| `instagram.com/p/…`, `/reel/…` | rejected: "post link, not an account" |
| `https://x.com/foo?s=20`, `twitter.com/foo`, `x.com/foo/status/123` | X `@foo` → `https://x.com/foo` |
| `@foo` or `foo` | **ambiguous**: pick Instagram or X. If the handle can only be Instagram (has a `.` or is over 15 chars), it's pre-picked as Instagram |
| anything with a dot | Website: `https://` added, host lowercased, `#fragment` and `utm_*` dropped, path kept |
| already on this artist | shown as "Already added", skipped |

Each preview row has a type picker (so a site can be marked as **Fan club**), and website pages whose path has `live`, `schedule`, `tour` or `ticket` get the label "Schedule" by default. Labels are editable afterwards.

## Screens

- **Sign in**: one password field. Session cookie lasts 30 days.
- **Artists** (home): "Missing" counts for Instagram / X / Website that filter the list when tapped; search (English or Japanese name); artists grouped S → A → B, each row showing IG / X / Site coverage (filled = has it, with a count when more than one; dashed = missing); paused artists in their own group at the bottom.
- **Artist**: paste box + preview, sources grouped by kind with label, "Not scanned yet" (the slot step 2 fills), open link, and a menu to edit label/kind or remove. Details: name, Japanese name, tier, Watching on/off, notes. Details autosave on change, with a small "Saved". Delete artist asks first.
- **Add artist**: name, Japanese name, tier, then straight to the artist screen to paste sources.
- **Desktop** (≥ 960 px): the list stays on the left, the artist opens on the right.

## Security

- Single password from `.env` (`APP_PASSWORD`), compared in constant time. `SECRET_KEY` signs the session cookie (HttpOnly, SameSite=Lax, Secure when `COOKIE_SECURE=true`, i.e. on the VPS behind HTTPS).
- Every change is a POST; requests whose `Origin` isn't this host are refused (CSRF).
- 5 wrong passwords from one IP → 5-minute lockout (in memory; fine for one user).
- The app refuses to start if `APP_PASSWORD` or `SECRET_KEY` is missing or still the example value.

## Seed

The 29 artists from PLAN.md, as names + tiers, no sources, all watching. Runs automatically when the database is empty; re-running never duplicates.

## Files

```
app/  config.py · db.py · sources.py (parsing, pure) · auth.py · seed.py · main.py
templates/  base, login, artists, artist, artist_new, partials
static/  app.css · app.js
tests/  test_sources.py (parsing table above) · test_app.py (login, CRUD, paste, filters)
requirements.txt · .env.example · README.md · .gitignore
```

## Not in step 1

Scanning, shows, alerts, deploy, the "suggest accounts" button, editing more than one artist at once, an undo for deletes.
