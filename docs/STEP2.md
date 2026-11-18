# Step 2: scanning, shows, alerts

What step 2 of [PLAN.md](PLAN.md) built, and why it works the way it does. Running, and the step-by-step VPS deploy (Daniel does it himself): [README.md](../README.md).

## One scan

`python -m app.watch` (cron, the "Scan now" buttons, or by hand) does, for every watched artist, the steps below.

**Schedule.** Cron starts `python -m app.watch --trigger cron` every hour. It scans only when the last scheduled scan started at least "scan every N hours" ago (Scans page → Schedule, default 12, 10 minutes' slack so a scan that started a minute past the hour still counts); otherwise it exits at once: no run row, no log line, no Discord notice. Scans from the app or by hand always run and don't move the schedule. The Scans page shows the setting and the next scheduled scan. The schedule and the Instagram cooldown are stored in the `settings` table (migration 9); `.env` (`SCAN_EVERY_HOURS`, `INSTAGRAM_COOLDOWN_HOURS`) only gives the defaults until they're saved in the app.

Each scan:

1. **Read** every source. One failing source is logged and shown on it; the others carry on.
   - **X**: Apify `kaitoeasyapi/…-cheapest`, one search per 8 accounts: `(from:a OR from:b …) since_time:<last scan − 1 h> -filter:replies`. Filler rows the actor returns for empty searches are dropped.
   - **Instagram**: Apify `apify/instagram-post-scraper`, one run for all profiles, `onlyPostsNewerThan` = last scan − 1 h. For a profile with nothing new the actor returns one "Empty or private data" row, which Apify bills: after the first scan that means "nothing new" (status empty, not an error); on the first scan it means private, empty or a wrong handle. Instagram is still read on every scan like X by default (Daniel: "I want you to do Instagram same as Twitter... like a cooldown... but default is zero cooldown"); an Instagram cooldown above 0 on the Scans page skips it when it was read less than that many hours ago (about $0.06 a read, ~$3.60 a month at 2 scans a day; a profile skipped this way shows "not read this scan, next read in N h" rather than its last scan's status). A "Scan now" on one artist always reads it.
   - **First scan of an account** reads 120 days back (`FIRST_SCAN_DAYS`), since Jan–Feb shows were announced from Aug 2026 and their ticket rounds are still running. X reads those one account per search so a busy account can't crowd out a quiet one.
   - **At once**: up to `APIFY_CONCURRENCY` Apify runs (default 5) go side by side in worker threads, never more than the account allows (read from Apify at the start: the free plan allows 5 runs and 16 GB; an Instagram run takes 1 GB, an X run 256 MB). Instagram starts first (its one run is the longest), and the websites are read on the scan's own thread meanwhile. Only that thread touches the database.
   - **Retries**: a timeout, 5xx, 429, a dropped connection or a DNS error (`getaddrinfo failed`) is tried again after 3 s and 9 s (`RETRIES`, default 2); a 404, a failed actor run or bad input isn't. Each retry shows on its step in the progress view ("↻ tried again once") and in the log. Flyer images get one quick retry too.
   - **Websites** (and fan-club pages): httpx + BeautifulSoup. If the page has almost no text or says it needs JavaScript, it's loaded in headless Chromium instead, and remembered as needing it. The text keeps links as `label <url>` so ticket links survive.
2. **Dedup**: every tweet, Instagram post and page version is a row in `posts`, unique per source. A page is only stored (and only sent to Claude) when its text changed.
3. **Read with Claude** (Haiku 5.5, structured JSON output): the artist's new posts in batches of up to 12 items / 16 images, flyer images included, plus the artist's known shows and rounds, so Claude can say "this is show #12" and reuse round names. If a post points at a detail page with the missing dates or rounds, Claude lists it and the scan reads it too (up to 8 per artist per scan); pages on the artist's own site are then re-checked on every scan. Up to `CLAUDE_CONCURRENCY` calls (default 15) run at once in worker threads, kept under the account's per-minute limits from the `anthropic-ratelimit-*` headers; a 429 halves the number and the batch is sent again. Only the scan's main thread touches the database.
4. **Merge** into `shows` and `ticket_rounds`:
   - A show is one performance date. A tour becomes one show per date in the watch window; the tour's rounds are copied onto each date.
   - Kept: in Japan, dated inside the trip's calendar months (Jan 1 – Feb 28, 2027, from the Trip page) or with the date TBA. Past dates, shows abroad, and dates outside the window are dropped.
   - Matching: Claude's show id, else same artist + same date (+ venue or city), else a TBA show with the same title (that's how a TBA show gets its date later).
   - Rounds match by normalized name per show. New dates on a known round update it.
5. **Alerts** (Discord webhook only): one message per show, its first line being what the phone notification shows ([tier] artist · date · city · fit · nearest deadline), for a new show (with its open and coming rounds), a date announced for a TBA show, a new ticket round, a new deadline or opening date, or a cancellation. Each is keyed (`show:12`, `show:12:date:2027-02-04`, `round:40`, `round:40:closes:2026-11-29T23:59`) in `alerts`, so nothing is sent twice. Alerts are built from the stored shows, not from what this scan found: anything about a show with no row in `alerts` yet goes out, so shows found by a scan that was killed before its alerts still alert on the next scan. Rounds that are already closed are stored but not alerted; shows already past and shows Daniel hides in the UI never alert. Every show alert mentions Daniel (`DISCORD_USER_ID`, allowed to ping only him), so it buzzes even with the channel on "Only @mentions". Each scan also posts a silent notice (Discord's no-notification flag, no mention) when it starts and when it finishes, fails or is found stopped; never for `--dry-run`; `DISCORD_SCAN_NOTICES=0` turns them off.
6. **Status**: each source gets last scanned, new posts read, shows found, and the error if any (including "posts read but not checked by Claude yet" when the Claude call failed; those posts are retried next scan).

`--dry-run` runs all of this on a temporary copy of the database and prints the alerts instead of sending them.

## Decisions

| Decision | Why |
| --- | --- |
| Alerts go to Discord only, via a free channel webhook (Daniel: "i dont want telegram") | One message per show so each push notification is about one thing. Discord's ~30 messages/minute per channel is respected (pause between posts, retry on 429). |
| Alert on every relevant show, fits or not (Daniel) | "I will later decide myself as the human what to do with that information." The fit is in every alert and card. |
| Fit from where you sleep each night | Same rules as docs/SWEEP.md; Yokohama/Kawasaki/Saitama/Chiba count as Tokyo, Kobe/Kyoto/Nara as Osaka. The last stay's end day is departure, so it's "outside". |
| One scan at a time | A lock row in `runs`, so cron and "Scan now" can't overlap. The row has the scan's process id: when that process is gone, the next look (any page, or the progress poll every 1.5 s) marks the scan stopped. Scans from before that, or on another machine, still fall back to 20 minutes without a heartbeat. |
| Scans from the UI run in their own process, detached from the web server | The page returns at once and shows the scan's live progress. Restarting the web server doesn't stop the scan: its own session on Linux plus `KillMode=process` in the systemd unit; on Windows no console, and out of the server's job when allowed. |
| "Resume" a scan that stopped partway (2026-11-17: the dev server stopped and took the scan with it) | The Scans page says the last scan stopped, what it was doing, and offers Resume (also on the scan's own page). A resume (`python -m app.watch --resume <id>`) covers the same artists and skips the sources the stopped scan (and any scan it resumed) finished; Claude then reads everything still waiting, and the alerts go out from the stored shows. Only the latest scan can be resumed. |
| What each scan read is kept and shown (Daniel: "make it so I can see the actual data like the posts and all that") | Every post and page version records the scan that read it and the scan in which Claude decided about it. The scan page has "What this scan read", collapsed: shows first, filters (shows / not a show / not read by Claude yet, artist), each item with Claude's note on what it is and why, the shows it led to, its text, and a link. It loads only when opened, 40 at a time. |
| Live progress is stored, not streamed (Daniel: "what's going on in this exact moment and what's been done and what's yet to be done") | Every step is planned up front in `run_steps` (one per X account, Instagram profile, website, Claude per artist, alerts) and marked running / done / failed / skipped with counts; `runs.current` is the "now" line; log lines go to `run_log`. The page polls `/scan/progress` every 1.5 s (5 s in a background tab) for changed steps only, so it survives reloads and works from a phone mid-scan. Time left comes from this run's own timings (`app/progress.py`). Progress and logs of the last 30 runs are kept. |
| Apify cost cap | `APIFY_MAX_USD_PER_RUN` (default $1) is passed to every actor run, so a runaway run can't drain the account. |
| Claude Haiku 5.5, effort medium | Cheap enough to read every post and every flyer image (~$1–2/month); medium effort for reading dates out of Japanese flyers. |

## Data added (migration 2)

```
sources  + last_posts · cursor (start of the last good social scan) · first_scan_at
pages    source_id · url · tracked · content_hash · rendered · last_fetched/changed_at · last_error
posts    source_id · external_id (tweet id / shortcode / url#hash) · url · posted_at · text · media · relevant · note
shows    artist_id · title · date (NULL = TBA) · time_note · venue · city · prefecture · kind · notes · cancelled · hidden
ticket_rounds  show_id · name · name_key · kind · lottery · opens_at · closes_at · results_at · url · price · notes
show_posts     show ↔ post (source links)
alerts   key (unique) · artist_id · show_id · kind · text · sent_at
itinerary  start_date · end_date · city   (seeded with the trip from PLAN.md)
runs     started/finished/heartbeat · trigger · scope · dry_run · status · counts · apify_usd · claude_usd · summary · errors
         + current · current_at   (migration 3: the "now" line)
run_steps  run_id · key · position · phase · kind · grp (one Apify call) · label · sublabel · status · started/finished_at
           · done/total · posts · shows · updates · usd · detail · error · rev (changes since the page's last poll)
run_log    run_id · at · level · message
```

Later migrations (`app/db.py`): 4 records what alerts sent before "alerts from stored shows" already said · 5 adds the trailing slash to four website sources · 6 `run_steps.retries` · 7 `runs.pid`, `runs.resumed_from` · 8 `posts.run_id` (read by), `posts.checked_run_id` (decided by Claude in).

## Screens added

- **Shows** (`/shows`): filter chips by fit; groups "ticket rounds open or coming, soonest deadline first", "by date", "date not announced". Each card: date, artist, title, venue/city, time, fit, rounds (state, window, results date, price, apply link), source links, Hide.
- **Trip** (`/trip`): the stays, add/remove, saved as a whole; validation for overlaps and dates.
- **Scans** (`/scans`): scan everyone now, what's set up in `.env`, this month's cost, sources with errors, sources never scanned, recent scans. While a scan runs: what it's doing right now and for how long, elapsed time, a progress bar with time left, Apify and Claude cost so far, the three phases (read sources → Claude → alerts) with counts, every source and artist as a checklist (pending / running / done / failed / skipped, with posts, shows and errors inline), and a live log tail.
- **Scan** (`/scans/<id>`): the same view for any recent scan, live or finished, plus "What this scan read" (collapsed) and Resume if it stopped partway.
- **Artist**: "Scan now", the artist's shows, and per-source status.
- Phone: a bottom tab bar (Shows, Artists, Trip, Scans). Desktop: the same links in the top bar.
