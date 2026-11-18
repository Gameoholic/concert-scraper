"""Compare what the scanner finds with the manual sweep (docs/SWEEP.md) on the sweep's official pages.

    .venv/bin/python -m tools.sweep_check            # needs ANTHROPIC_API_KEY in .env; ~$0.05 of Claude
    .venv/bin/python -m tools.sweep_check --keep     # keep the throwaway database to look at

Runs the real pipeline (web pages + Claude + merge) against a throwaway database, never the real one,
and sends no alerts. Only official websites are used, so it needs no Apify credit.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from app import db, store
from app.classify import Classifier
from app.config import Settings
from app.scan_web import Browser
from app.sources import parse_paste
from app.watch import Scan, setup_logging

# Official pages the sweep read (or couldn't read without JavaScript), per artist.
PAGES = {
    "UVERworld": ["https://www.uverworld.jp/feature/2027_live"],
    "ZUTOMAYO": ["https://zutomayo.net/apicula/"],
    "sukekiyo": ["https://sukekiyo-official.jp/news/2336/"],
    "MYTH & ROID": ["https://mythandroid.com/contents/1109275"],
    "TRUE": ["https://true-singer.com/contents/1115995"],
    "Reol": ["https://mikitop.com/mikirockparty/", "https://reol.jp/feature/otosata_2027"],
    "Phantom Siita": ["https://phantomsiita.com/schedule/list/2027/2/"],
    "Atarashii Gakko!": ["https://atarashiigakko.com/schedule/index.html?cat=all&year=2027&month=2"],
    "Itoki Hana": ["https://110ki.com/live/"],
    "Eve": ["https://eveofficial.com/live/"],
    "AKASAKI": ["https://akasaki.red/live_information/schedule/list"],
    "Co shu Nie": ["https://coshunie.jp/pages/shows"],
    "KANA-BOON": ["https://sp.kanaboon.jp/feature/jackin2027"],
    "Ado": ["https://www.universal-music.co.jp/ado/news/2026-10-19/"],
    "Creepy Nuts": ["https://creepynuts.com/onemantour2027/"],
    "LiSA": ["https://www.sonymusic.co.jp/artist/lisa/info/583485"],
    "Hiroyuki Sawano": ["https://www.sonymusic.co.jp/artist/sawanohiroyukinzk/info/584795"],
    "9Lana": ["https://www.sonymusic.co.jp/artist/9Lana/live/"],
}

# What the sweep found in Japan, Jan–Feb 2027: (artist, date or None for TBA, a word of the venue/city, round that
# must be there or None). Shows the sweep marked "outside" are still expected: the app keeps every date in the window.
EXPECTED = [
    ("UVERworld", "2027-02-04", "Yokohama", "FC"),
    ("UVERworld", "2027-02-09", "Osaka", "FC"),
    ("UVERworld", "2027-02-14", "松山", "FC"),
    ("UVERworld", "2027-02-22", "Fukuoka", "FC"),
    ("ZUTOMAYO", "2027-01-26", "", None),
    ("ZUTOMAYO", "2027-02-06", "Fuji", "先行"),
    ("ZUTOMAYO", "2027-02-15", "Kobe", "先行"),
    ("ZUTOMAYO", "2027-02-16", "Kobe", "先行"),
    ("ZUTOMAYO", "2027-02-21", "Oita", "先行"),
    ("ZUTOMAYO", "2027-02-23", "Kumamoto", "先行"),
    ("ZUTOMAYO", "2027-02-27", "Iwaki", None),
    ("sukekiyo", "2027-02-16", "Kyoto", "京"),
    ("MYTH & ROID", "2027-02-06", "Yokohama", None),
    ("TRUE", "2027-02-20", "Kyoto", "先行"),
    ("Reol", "2027-02-11", "Tokyo", "先行"),
    ("Reol", "2027-01-25", "Billboard", None),
    ("KANA-BOON", "2027-02-27", "Osaka", None),
    ("Ado", "2027-01-31", "Osaka", None),
    ("Ado", "2027-02-27", "Nagoya", None),
    ("Creepy Nuts", "2027-02-27", "Sendai", "4次"),
    ("LiSA", "2027-01-30", "", None),
    ("Hiroyuki Sawano", "2027-02-27", "Tokyo", None),
]
# Sweep: nothing in the window for these (any show found for them is a false positive to look at).
NOTHING = ["Phantom Siita", "Atarashii Gakko!", "Itoki Hana", "Eve", "AKASAKI", "9Lana"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    setup_logging(False)
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    base = Settings.from_env()
    if not base.anthropic_key:
        print("ANTHROPIC_API_KEY is not set in .env")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="sweep-check-")) / "check.db"
    settings = replace(base, database_path=tmp, discord_webhook="")
    db.init_db(tmp)
    conn = db.connect(tmp)
    for name, urls in PAGES.items():
        aid = conn.execute("SELECT id FROM artists WHERE name = ?", (name,)).fetchone()[0]
        db.add_sources(conn, aid, [c for c in parse_paste("\n".join(urls)) if c.status == "new"])
    ids = [r[0] for r in conn.execute("SELECT DISTINCT artist_id FROM sources")]
    run_id = store.start_run(conn, "cli", "sweep-check", True)
    browser = Browser()
    clf = Classifier(settings.anthropic_key, settings.claude_model)
    try:
        rep = Scan(settings, conn, run_id, classifier=clf, browser=browser).run(ids)
    finally:
        browser.close()
        clf.close()

    shows = store.load_shows(conn)
    print(f"\nRead {rep.posts_read} pages, Claude ${rep.claude_usd:.3f}. Found {len(shows)} shows.\n")
    found_ids, misses = set(), []
    for artist, d, where, rnd in EXPECTED:
        cands = [s for s in shows if s.artist == artist and (s.date.isoformat() if s.date else None) == d]
        hit = next((s for s in cands if not where or where.lower() in f"{s.venue} {s.city} {s.title}".lower()),
                   cands[0] if cands else None)
        if not hit:
            misses.append(f"MISSING  {artist} {d} {where}")
            continue
        found_ids.add(hit.id)
        if rnd and not any(rnd.lower() in r.name.lower() for r in hit.rounds):
            misses.append(f"NO ROUND {artist} {d}: wanted '{rnd}', got {[r.name for r in hit.rounds]}")
    extra = [s for s in shows if s.id not in found_ids]
    print(f"Sweep shows found: {len(EXPECTED) - sum(m.startswith('MISSING') for m in misses)}/{len(EXPECTED)}")
    for m in misses:
        print("  " + m)
    print(f"\nFound by the app but not in the sweep's list ({len(extra)}):")
    for s in sorted(extra, key=lambda s: (s.artist, s.date or store.date.max)):
        flag = "  <- sweep says nothing in the window" if s.artist in NOTHING else ""
        print(f"  {s.artist}: {s.date or 'TBA'} {s.place} | {s.title} | {[r.name for r in s.rounds]}{flag}")
    print("\nErrors:", *rep.errors, sep="\n  ") if rep.errors else None
    conn.close()
    if args.keep:
        print(f"\nDatabase kept at {tmp}")
    else:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    return 0


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.INFO)
    sys.exit(main())
