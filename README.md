# concert-scraper

Watches Daniel's favorite Japanese artists for shows and ticket lotteries during the Jan–Feb 2027 trip. See [docs/PLAN.md](docs/PLAN.md) for the why and [docs/STEP2.md](docs/STEP2.md) for how scanning works.

- **Artists**: a password-protected, phone-first web UI to manage artists, tiers and their Instagram / X / website sources, with per-source scan status and a "Scan now" button.
- **Shows**: every show found, with ticket rounds and deadlines, fit (fits / detour / outside) against the trip, and source links. Open deadlines first.
- **Trip**: the itinerary, editable; every show's fit follows it.
- **Scans**: "Scan everyone now", the scan log, errors, and this month's cost.
- **Watcher**: `python -m app.watch`, started by cron every hour; it scans every N hours as set on the Scans page (default 12), and posts a Discord alert for every new show and every new ticket round or deadline, never twice for the same thing.

## Run locally

Needs Python 3.11+.

Windows (PowerShell):

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium   # headless browser for JavaScript-built pages
copy .env.example .env      # then fill in .env (see the comments in it)
.venv\Scripts\uvicorn app.main:create_app --factory --reload --port 8000
```

Linux / macOS:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
cp .env.example .env        # then fill in .env
.venv/bin/uvicorn app.main:create_app --factory --reload --port 8000
```

Open http://127.0.0.1:8000 and sign in with `APP_PASSWORD`. The app won't start while `APP_PASSWORD` or `SECRET_KEY` is missing or still `change-me`.

The database is created at `data/concerts.db` on first start, seeded with the 29 starting artists (no sources) and a made-up sample trip: set your real one on the Trip page. Delete that file to start over.

## Scanning and alerts

```bash
.venv/bin/python -m app.watch              # scan every watched artist, send alerts (what cron runs)
.venv/bin/python -m app.watch --dry-run    # fetch + classify, print the alerts; changes nothing, sends nothing
.venv/bin/python -m app.watch --artist 12  # one artist (id from the URL); repeatable
.venv/bin/python -m app.watch --resume 40  # continue scan 40, which stopped partway (the Scans page's Resume)
.venv/bin/python -m app.watch --limits     # the Anthropic account's rate limits (one tiny Claude call)
```

- Claude reads up to `CLAUDE_CONCURRENCY` batches at once (default 15). The scan starts with 2, then fits the number under 80% of the account's per-minute limits (from the `anthropic-ratelimit-*` headers), holds new calls when a limit is nearly used up, and halves on a 429. On 2026-11-18 the account allowed 4,000 requests, 2M input and 400k output tokens per minute.

- Keys go in `.env`: `APIFY_API_TOKEN` (X + Instagram), `ANTHROPIC_API_KEY` (Claude) and `DISCORD_WEBHOOK_URL` (alerts). Anything missing is reported on the Scans page and in the log; the rest still runs.
- `--dry-run` works on a throwaway copy of the database, so it really changes nothing. It still calls Apify and Claude, so it costs the same as a real scan.
- Logs: `data/logs/watch.log` (rotated; always in a `logs` folder next to the database) and, on the VPS, `data/logs/cron.log`.
- A scan that stops partway (server restart, crash) shows as stopped on the Scans page right away, with a **Resume** button that reads only what it didn't get to. Shows it found still alert: alerts come from the stored shows, not from one scan.
- Each scan's page has **What this scan read** (collapsed): every post and page it read, what Claude decided about each and why, and the shows it led to.
- Alerts that couldn't be sent (no webhook set, Discord down) are kept and go out with the next scan.

### Discord alerts

One message per show. The first line is what your phone's notification shows, the artist's tier (S/A/B) in brackets:

```
🆕 [B] UVERworld · Thu 4 Feb · Yokohama · ✅ fits · ⏰ FC先行 until Sun 15 Nov 23:59
**UVERworld LIVE HOUSE TOUR 2027**
KT Zepp Yokohama · 17:30 / 18:30
• FC先行 (FC, lottery): OPEN NOW, until Sun 15 Nov 23:59 · ¥9,800 — apply / info
• 一般発売 (general): opens Sat 19 Dec
uverworld.jp · X · all shows
@Daniel
```

🆕 new show · 📅 date announced · 🎟 new ticket round · ⏰ new deadline or opening date · ❌ cancelled.

Every show alert **pings you** (`@you` on its last line) once `DISCORD_USER_ID` is set; without it they're sent the same way, just without the ping.

Each scan also posts a **silent** message when it starts and when it finishes (Discord's "no notification" flag, no ping, so your phone doesn't buzz). Started: who started it (scheduled, from the app, by hand, or a resume), which artists, how many sources. Finished: done / done with errors / failed / stopped partway, how long it took, new shows, updates, alerts sent, the errors (first five), what Apify and Claude cost, and a link to the scan's page. A scan that dies partway gets its "stopped" message as soon as the app notices. `--dry-run` posts nothing. Turn these off with `DISCORD_SCAN_NOTICES=0`.

Setting up the webhook (free, 2 minutes):

1. In Discord, create a private server for yourself: **+** (Add a Server) → **Create My Own** → **For me and my friends**. Name it, e.g. "Concerts".
2. On its text channel (e.g. `#general`, or make `#alerts`): **Edit Channel** (gear icon) → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
3. Put it in the server's `.env` as `DISCORD_WEBHOOK_URL=…` and restart (`sudo systemctl restart concert-watcher`). The Scans page then shows "Discord alerts: set up".
4. Your Discord user ID, for the pings: Discord **User Settings** → **Advanced** → turn on **Developer Mode**. Then right-click your own name (in the member list or on one of your messages; on the phone: tap your profile → **⋯**) → **Copy User ID**. It's a long number. Put it in `.env` as `DISCORD_USER_ID=…` and restart.
5. On your phone, in the Discord app: long-press the server → **Notification Settings** → **Only @mentions**. Show alerts still buzz (they mention you); nothing else in the channel does. (With **All Messages** the alerts buzz too; the scan started / finished messages stay silent either way.)

Anyone who has the webhook URL can post to that channel, so keep it out of chats and screenshots. If it leaks, delete the webhook in the same menu and make a new one.

## Cost (29 artists, 2 scans a day)

Prices checked 2026-11-14:

| What | Price |
| --- | --- |
| X: Apify [`kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest`](https://apify.com/kaitoeasyapi/twitter-x-data-tweet-scraper-pay-per-result-cheapest) | $0.25 per 1,000 tweets. A query with no new tweets still bills about 20 (filler rows). |
| Instagram: Apify [`apify/instagram-post-scraper`](https://apify.com/apify/instagram-post-scraper) | $2.70 per 1,000 posts on Apify's free plan ($1.70 "post" + $1.00 "post details"). A profile with nothing new still returns one placeholder row, and it's billed: 24 of them cost $0.059 on 2026-11-18. |
| Claude Haiku 5.5 | $0.10 / $0.50 per million input / output tokens (prompts under 100K tokens). A flyer image is at most 4,784 tokens, so at most about $0.0005 per image. |
| Apify free plan | Includes $5 of usage a month. **When it's used up, Apify stops running actors until next month; it doesn't bill extra.** |

The app batches accounts: X is read with one `from:a OR from:b …` search per 8 accounts, Instagram with one run for all profiles, and website pages go to Claude only when their text changed.

| Monthly | Low | High | How |
| --- | --- | --- | --- |
| X | $0.70 | $2.00 | ~2,600 new tweets a month (29 accounts × ~3 a day), plus up to 4 queries × 2 runs × 30 days × ~20 filler rows |
| Instagram | $3.00 | $4.80 | ~450 new posts a month ($1.20), plus a billed placeholder row per profile with nothing new: each read of the 24 profiles costs about $0.06 even when nothing is new, so about $3.60 a month at 2 scans a day (the default: no Instagram cooldown, read every scan like X). A 24 h cooldown on the Scans page halves that. |
| Claude | $1.00 | $2.00 | ~8–13M input tokens: posts, ~1,500 flyer images, changed pages; ~0.5M output |
| **Total** | **~$5** | **~$9** | |
| First scan, once | ~$7 | | 120 days back: ~8,700 tweets ($2.20), up to 1,450 Instagram posts ($3.90), Claude ~$1 |

So the first month is likely to go past Apify's free $5. If Apify stops partway through a month, the Scans page shows the X/Instagram errors and the official websites keep being read. Cheapest fixes, if that happens: Apify Starter ($29/month, includes $29 of usage), or read Instagram only for artists that have no X account (most post the same announcement on both).

Each scan records what Apify and Claude actually charged; the Scans page shows the month's total.

## Deploy to your VPS (step by step)

You do this yourself; nothing in the app touches the server on its own. Commands are for Debian or Ubuntu and are run on the VPS unless they say "on your PC". Replace `concerts.example.com` with your subdomain.

**Before you start**

- The subdomain's DNS **A record** points at the VPS's IP (check with `ping concerts.example.com`).
- Ports 80 and 443 are open, and nothing else is listening on them (`sudo ss -ltnp | grep -E ':(80|443) '` prints nothing). If nginx or another Caddy already serves other sites there, see the note under step 6.
- At least 1 GB of RAM free: headless Chromium needs it during scans.

**1. Copy the code** (on your PC, in Git Bash, from the project folder). This leaves out `.env`, the database and the local venv:

```bash
tar czf ../concert-scraper.tgz --exclude=.venv --exclude=data --exclude=.env --exclude=__pycache__ --exclude=.pytest_cache --exclude=.claude .
scp ../concert-scraper.tgz user@your-vps:/tmp/
```

On the VPS:

```bash
sudo mkdir -p /opt/concert-scraper
sudo tar xzf /tmp/concert-scraper.tgz -C /opt/concert-scraper
```

**2. Install Python and Caddy**

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip cron curl debian-keyring debian-archive-keyring apt-transport-https gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

**3. A user for the app, the venv and headless Chromium**

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin concert
sudo mkdir -p /opt/concert-scraper/data/logs
sudo chown -R concert:concert /opt/concert-scraper
cd /opt/concert-scraper
sudo -u concert python3 -m venv .venv
sudo -u concert .venv/bin/pip install -r requirements.txt
sudo .venv/bin/python -m playwright install-deps chromium      # system libraries Chromium needs
sudo -u concert .venv/bin/python -m playwright install chromium
```

**Bring your local data** (sources, trip, shows, what's been alerted), so the VPS doesn't start from the sample trip, re-run the first scan or re-send every alert. On your PC, stop the local server, then:

```bash
scp data/concerts.db user@your-vps:/tmp/
```

On the VPS:

```bash
sudo install -o concert -g concert -m 600 /tmp/concerts.db /opt/concert-scraper/data/concerts.db
```

Skipping this gives a fresh database: paste the sources and set the trip in the app before the first scan.

**4. The `.env` file** lives at `/opt/concert-scraper/.env`, readable only by the app user:

```bash
sudo -u concert cp .env.example .env
sudo chmod 600 .env
sudo nano .env
```

Set:

| Key | Value |
| --- | --- |
| `APP_PASSWORD` | the password you'll sign in with |
| `SECRET_KEY` | output of `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `COOKIE_SECURE` | `true` (the site is HTTPS) |
| `APP_URL` | `https://concerts.example.com` (used for the "all shows" link in alerts) |
| `APIFY_API_TOKEN` | from https://console.apify.com/settings/integrations |
| `ANTHROPIC_API_KEY` | from https://platform.claude.com/settings/keys |
| `CLAUDE_CONCURRENCY` | optional, default 15: most Claude calls at once |
| `SCAN_EVERY_HOURS` | optional, default 12: scheduled scans. Only the default: change it on the Scans page (Schedule), which wins once saved. |
| `INSTAGRAM_COOLDOWN_HOURS` | optional, default 0 (Instagram read on every scan, like X): skip Instagram if it was read less than this many hours ago. Each read costs about $0.06 even when nothing is new. Only the default, as above. |
| `APIFY_CONCURRENCY` | optional, default 5: most Apify runs at once. Never more than the account allows (the free plan: 5). |
| `RETRIES` | optional, default 2: times a timed-out or failed website / Apify call is tried again (after 3 s, then 9 s) |
| `DISCORD_WEBHOOK_URL` | see [Discord alerts](#discord-alerts) |
| `DISCORD_USER_ID` | your Discord user ID, so every show alert pings you (see [Discord alerts](#discord-alerts), step 4) |
| `DISCORD_SCAN_NOTICES` | optional, default 1: silent "scan started" / "scan finished" messages; 0 turns them off |

Leave `DATABASE_PATH` as it is. Check it all works before going further (prints what a scan would alert; sends nothing, changes nothing):

```bash
sudo -u concert .venv/bin/python -m app.watch --dry-run
```

With no sources pasted yet it just says it scanned 0 sources; any missing key is named in the output.

**5. The web app as a service** (starts on boot, restarts if it crashes):

```bash
sudo cp deploy/concert-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now concert-watcher
systemctl status concert-watcher        # should say "active (running)"
curl -s http://127.0.0.1:8000/healthz   # {"ok":true}
```

It listens only on 127.0.0.1:8000; Caddy puts it on the internet.

**6. HTTPS with Caddy.** Replace the contents of `/etc/caddy/Caddyfile` (`sudo nano /etc/caddy/Caddyfile`) with [deploy/Caddyfile](deploy/Caddyfile), with `{DOMAIN}` changed to your subdomain:

```
concerts.example.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8000
	header {
		Strict-Transport-Security "max-age=31536000"
		X-Content-Type-Options nosniff
		Referrer-Policy same-origin
		X-Robots-Tag "noindex, nofollow"
	}
}
```

```bash
sudo systemctl reload caddy
```

Caddy gets the Let's Encrypt certificate by itself within a minute; open `https://concerts.example.com` and sign in. If it doesn't load, `journalctl -u caddy -n 50` says why (usually DNS not pointing at the VPS yet, or port 80 blocked).

If the VPS already runs Caddy for other sites, add the block above to the existing Caddyfile instead of replacing it. With nginx instead, proxy the subdomain to `http://127.0.0.1:8000` and get a certificate with certbot; nothing in the app depends on Caddy.

**7. Scheduled scans (cron)**

```bash
sudo crontab -u concert -e
```

Add this line, save:

```
0 * * * * cd /opt/concert-scraper && .venv/bin/python -m app.watch --trigger cron >> data/logs/cron.log 2>&1
```

Cron starts the watcher every hour, on the hour. It scans only when a scan is due under **Scans → Schedule** (default every 12 hours since the last scheduled scan started); otherwise it exits at once, with no scan, no log line and no Discord message. Change how often in the app, not here. The server's time zone doesn't matter: the app shows times in Japan time.

**8. First use**

1. Sign in, paste each artist's links on **Artists**.
2. **Scans** → check every line says "set up" → **Scan everyone now**. The first scan reads 120 days back and takes a few minutes; the page updates when it's done.
3. Alerts for everything already announced arrive in Discord; **Shows** lists them.

[deploy/setup.sh](deploy/setup.sh) does steps 2–7 in one go (`sudo bash deploy/setup.sh concerts.example.com`, run from `/opt/concert-scraper`) if you'd rather not type them; it never overwrites an existing `.env`.

**Updating later:** copy the code as in step 1 (it never includes `.env` or the database), then

```bash
cd /opt/concert-scraper && sudo chown -R concert:concert . && sudo -u concert .venv/bin/pip install -r requirements.txt && sudo systemctl restart concert-watcher
```

If `deploy/concert-watcher.service` changed (it did on 2026-11-18: `KillMode=process`, so restarting the web app no longer stops a scan that's running), copy it again first: `sudo cp deploy/concert-watcher.service /etc/systemd/system/ && sudo systemctl daemon-reload`.

**Once, if you set up cron before 2026-11-18:** the crontab line changed from `0 9,21 * * *` to `0 * * * *` (every hour; the app decides whether a scan is due). `sudo crontab -u concert -e`, change the start of the line, save. Until then the old line still scans at 9:00 and 21:00, and the Scans page's schedule only takes effect at those hours.

(or `bash deploy/push.sh user@your-vps` from your PC, which does the copy and this in one go).

**Where things are**

| Piece | Where |
| --- | --- |
| Code, `.env` | `/opt/concert-scraper`, `/opt/concert-scraper/.env` |
| Web app | `concert-watcher` service: `systemctl status concert-watcher`, logs `journalctl -u concert-watcher` |
| HTTPS | Caddy: `/etc/caddy/Caddyfile`, logs `journalctl -u caddy` |
| Scans | `sudo crontab -u concert -l`; logs `data/logs/watch.log` and `data/logs/cron.log` |
| Data | `/opt/concert-scraper/data/concerts.db` (SQLite) |

After changing `.env`: `sudo systemctl restart concert-watcher` (cron scans read it fresh each time).

Backup: `sudo -u concert sqlite3 /opt/concert-scraper/data/concerts.db ".backup /tmp/concerts-backup.db"` (needs `sudo apt install sqlite3`).

## Tests

```bash
.venv/bin/python -m pytest        # Windows: .venv\Scripts\python -m pytest
```

They use fake Apify, Claude and web pages, so they need no keys and cost nothing.

## Layout

| Path | What |
| --- | --- |
| `app/sources.py` | Turns pasted links / @handles into normalized sources (pure, tested) |
| `app/db.py` | SQLite schema, migrations, seed, artist/source queries |
| `app/store.py` | Shows, ticket rounds, merging what Claude found, itinerary, scan runs (log + lock) |
| `app/fit.py` | Itinerary rules: fits / detour / outside (pure, tested against docs/SWEEP.md) |
| `app/scan_web.py` | Official pages: httpx + BeautifulSoup, headless Chromium for JavaScript-built pages |
| `app/scan_social.py` | X and Instagram through Apify, batched into runs that go side by side |
| `app/retry.py` | Tries again with backoff on timeouts, 5xx, 429 and network errors |
| `app/classify.py` | Claude Haiku: posts, pages and flyer images → shows and ticket rounds (structured output) |
| `app/alerts.py` | One Discord message per show, phone-notification first line, never the same thing twice |
| `app/watch.py` | The scan: CLI for cron, `--dry-run`, logging, per-source status |
| `app/main.py` | Web routes |
| `app/auth.py` | Password login, lockout, cross-site POST check |
| `templates/`, `static/` | Server-rendered pages, CSS, a little JS |
| `deploy/` | VPS setup script, systemd unit, Caddyfile, push script |
| `docs/` | Plan, per-step specs, the manual sweep |
