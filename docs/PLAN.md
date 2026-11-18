# Japan Concert Watcher: plan

## Why (Daniel's words, not concrete)

- "instant mobile alerts whenever any of my favorite Japanese artists announce a live show, tour, presale (先行), or ticket lottery (抽選) taking place in Jan/Feb 2027 (or if dates are TBA)"
- "a good UI to enter all the information about them so it can scrape instagram, twitter and official website, and also so I the user know what it already scanned what it didn't"

## Settled

- **Daniel:** trip is Jan–Feb 2027; 29 artists in S/A/B tiers (list in the original spec).
- **Daniel:** the itinerary is kept out of the public repo: it lives only in the app's database (Trip page). The database starts with a made-up sample trip.
- **Daniel:** a one-off manual sweep runs in its own session alongside the build; output `docs/SWEEP.md`, later used to check the app's sweep.
- **Daniel:** they can enter Japanese lotteries, so that isn't a concern.
- **Daniel:** a one-time sweep **per artist** of what's already announced, plus a live watcher for new posts. (Reason: Jan/Feb shows were mostly announced Aug–Nov 2026, and their ticket rounds — FC → 先行 → playguide → general — are still running.)
- **Daniel:** a UI to enter each artist's sources (Instagram, X, official site) and to see what's been scanned and what hasn't.
- **Daniel:** the app is built properly, not rushed ("I want this to be well done").
- **Daniel:** everything runs on the VPS, UI included, on a subdomain of one of their own domains. Proposal: HTTPS + a password login, designed phone-first since it's used in Japan.
- **Daniel:** handles are pasted in by hand in the UI; the app doesn't look them up. Proposal: the starting 29 are pre-filled once from the links on each official site.
- **Daniel:** alert on every relevant show, whatever the fit ("I will later decide myself as the human what to do with that information"). Proposal (accepted): itinerary editable in the app, each show marked fits / detour / outside in the UI and in alerts.
- **Proposal:** flyer images are read by Claude too, since announcements are often images with a short caption.
- **Daniel (spec):** Python, Apify for IG + X, SQLite, Claude classification, Telegram/Discord alerts, cron on a Linux VPS, `--dry-run`.
- **Daniel:** deploys to the VPS himself ("dont try to deploy! i will do all that myself"); the app ships with a step-by-step deploy guide in the README.
- **Daniel:** alerts go to Discord only (free channel webhook), not Telegram ("i dont want telegram, i want discord if possible if free").

## The order

- [x] 1. **Artist registry + UI**: add/edit artists, tier, IG/X/official-site sources pasted by Daniel; the 29 seeded as names + tiers. Spec: [STEP1.md](STEP1.md).
- [x] 2. **Everything else, in one go** (Daniel: "do everything so that I can start using the app immediately, no more steps"): per-artist first scan + scan status, shows view with fit marking, live watcher + Discord alerts + `--dry-run`. Built and tested locally; spec: [STEP2.md](STEP2.md). Still open: the check against [SWEEP.md](SWEEP.md) with Claude (`python -m tools.sweep_check`), which runs once `ANTHROPIC_API_KEY` is in the local `.env`.
- [ ] 3. **Deploy to the VPS: Daniel's** ("dont try to deploy! i will do all that myself"). Step-by-step guide in [README.md](../README.md#deploy-to-your-vps-step-by-step).
- [x] Fixes from the first real scan (2026-11-18, Daniel: "Let's fix all this"): alerts come from the stored shows, so shows found by a killed scan still alert (the next scan sends the ones that were missed); the four 404 website sources had lost their trailing slash ([SOURCES.md](SOURCES.md#changes)); Instagram's "Empty or private data" row means "nothing new" after a profile's first scan, and Apify bills it (~$0.06 a read), so there's an Instagram cooldown on the Scans page (default 0: read every scan like X, Daniel's call); flyer images over the size limit are shrunk, not skipped; tests and preview databases log next to their own database, never into `data/logs/watch.log`.
- [x] See the actual data, i.e. the posts and pages each scan read, what Claude decided about each, and why (Daniel: "make it so I can see the actual data like the posts and all that"). Collapsed by default: it's for reassurance and debugging, not daily use. Built 2026-11-18: "What this scan read" on each scan's page.
- [x] Detect a dead scan right away (check the scan process, not a 20-minute heartbeat timeout), show it as stopped, and offer "Resume". Also run UI scans independently of the web server so a server restart doesn't kill them. (2026-11-17: the dev server stopped, the scan died with it, and the page showed "Scanning" with no button for 20 minutes.) Built 2026-11-18; on the VPS the systemd unit needs copying again (README, "Updating later").
- [x] Run Apify reads in parallel too, with a concurrency setting (Daniel: "have it configurable... like max 15"). Apify's free plan allows 5 runs at once and 16 GB (an Instagram run takes 1 GB, X 256 MB), so `APIFY_CONCURRENCY` defaults to 5 and is never more than the account allows; a "too many runs" refusal is retried with backoff. Claude calls already run in parallel (2026-11-18, Daniel: "JUST FOR NOW do the anthropic one"): `CLAUDE_CONCURRENCY`, default 15, fitted to the account's limits (4,000 requests, 2M input and 400k output tokens per minute). Still to do properly: batches of the same artist can run at once, so each sees the known shows from before the others merged. That's rarely a problem because shows also match on date and venue, but run an artist's batches one after another if duplicate shows turn up.
- [x] Retries for Apify and website fetches (a few attempts with backoff on timeouts, 5xx and 429; not on 404 or bad input), shown in the progress view (Daniel: "we should have retry for apify and websites"). Built 2026-11-18: 3 s then 9 s (`RETRIES`), DNS errors included.
- [x] Show the artist's tier (S/A/B) in each Discord alert, on the first line so it's in the phone notification (Daniel, 2026-11-18): `🆕 [B] UVERworld · Thu 4 Feb · …`.
- [x] Every show alert pings Daniel; a silent message when each scan starts and finishes (Daniel, 2026-11-18: "I'd like each discord message to PING me. And each time the scan starts AND finishes, send a message WITHOUT pinging me."). `DISCORD_USER_ID`, `DISCORD_SCAN_NOTICES`; README, Discord alerts.
- [x] Scan frequency set in the app (Daniel, 2026-11-18: "can you make it so I can modify the frequency of the scan (every X hours)"): Scans page → Schedule, "scan every N hours" and the Instagram cooldown (default 0) in one card. Cron runs every hour and the watcher decides; fixed times of day left out (every N hours since the last scheduled scan is simpler and does the job). The VPS crontab line changes once (README, "Updating later").
- Later: a "suggest accounts" button for new artists; anything not needed before the trip.

## Open (proposed defaults)


- Nothing open. (Settled while building: official sites are scanned in v1; classification uses the current Haiku model.)
