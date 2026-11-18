# concert-scraper

Watches Daniel's favorite Japanese artists for shows and ticket lotteries during their Jan–Feb 2027 trip.

| Doc | What |
| --- | --- |
| [docs/PLAN.md](docs/PLAN.md) | Why, decisions, step order, open questions |
| [docs/STEP1.md](docs/STEP1.md) | Step 1 spec: stack, schema, paste rules, screens |
| [docs/STEP2.md](docs/STEP2.md) | Step 2: how a scan works (sources, Claude, merging, alerts), data, screens |
| [docs/SWEEP.md](docs/SWEEP.md) | Manual sweep of 2026-11-14; the test oracle for the scanner. Never edit. Local only (gitignored: it has the real itinerary) |
| [docs/SOURCES.md](docs/SOURCES.md) | The 89 official sources pre-filled for the 29 artists, and any left out |
| [README.md](README.md) | Running locally, scanning, Discord alerts, cost estimate, step-by-step VPS deploy (Daniel's), tests, code layout |

## VPS (Daniel's)

https://concerts.gameoholic.dev: nginx + certbot in front, the app under pm2 as `concerts` (`--no-treekill`, so a restart doesn't kill a running scan), code in `/opt/concert-scraper` (a clone of the public GitHub repo). To update after a push:

```bash
cd /opt/concert-scraper && git pull && pm2 restart concerts
```

If `requirements.txt` changed, run `.venv/bin/pip install -r requirements.txt` before the restart.
