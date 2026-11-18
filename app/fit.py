"""Trip itinerary and show fit (fits / detour / outside). Pure functions, no database.

A segment "Feb 1 – Feb 8 Tokyo" means you sleep in Tokyo the nights of Feb 1 … Feb 7;
Feb 8 is the travel day and its night belongs to the next segment. The last segment's end
day is the departure day, so a show that evening is "outside" (same rule as docs/SWEEP.md).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta

FITS, DETOUR, OUTSIDE, TBA = "fits", "detour", "outside", "tba"
FIT_LABEL = {FITS: "Fits", DETOUR: "Detour", OUTSIDE: "Outside trip", TBA: "Date TBA"}

# Places that count as a stay city (docs/SWEEP.md: Yokohama/Saitama/Chiba are Tokyo,
# Kobe/Kyoto are Osaka). Whole prefectures count for Tokyo and Osaka.
AREAS: dict[str, dict[str, set[str]]] = {
    "tokyo": {
        "prefectures": {"tokyo"},
        "cities": {"tokyo", "yokohama", "kawasaki", "saitama", "chiba", "makuhari", "funabashi", "urayasu",
                   "tokorozawa", "sagamihara", "kawaguchi", "maihama", "ariake", "shibuya", "shinjuku",
                   "tachikawa", "chofu", "hachioji", "machida", "kashiwa", "ichikawa", "ageo", "koshigaya",
                   "東京", "横浜", "川崎", "さいたま", "埼玉", "千葉", "幕張"},
    },
    "osaka": {
        "prefectures": {"osaka"},
        "cities": {"osaka", "kobe", "kyoto", "nishinomiya", "amagasaki", "sakai", "suita", "toyonaka",
                   "uji", "nara", "大阪", "神戸", "京都", "堺", "奈良"},
    },
}

JA_CITY = {"東京": "tokyo", "大阪": "osaka", "札幌": "sapporo", "仙台": "sendai", "盛岡": "morioka",
           "福島": "fukushima", "名古屋": "nagoya", "福岡": "fukuoka", "横浜": "yokohama", "神戸": "kobe",
           "京都": "kyoto"}


@dataclass
class Segment:
    start: date
    end: date
    city: str

    @property
    def nights(self) -> int:
        return (self.end - self.start).days


def norm_place(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").strip().lower()
    for ja, en in JA_CITY.items():
        if t.startswith(ja):
            return en
    t = re.sub(r"[-\s]+(city|shi|prefecture|pref\.?|ken|fu|to)$", "", t)
    t = re.sub(r"[市県府都]$", "", t)
    return t.strip(" ,.")


def area_of(city: str, prefecture: str = "") -> set[str]:
    """Stay cities a show place counts as (the place itself always counts)."""
    c, p = norm_place(city), norm_place(prefecture)
    out = {c} if c else set()
    for area, rule in AREAS.items():
        if c in rule["cities"] or c == area or p in rule["prefectures"]:
            out.add(area)
    return out


def night_city(segments: list[Segment], day: date) -> str | None:
    """Where you sleep that night, or None if it's not a night of the trip."""
    for s in segments:
        if s.start <= day < s.end:
            return s.city
    return None


def fit_for(segments: list[Segment], day: date | None, city: str, prefecture: str = "") -> str:
    if day is None:
        return TBA
    stay = night_city(segments, day)
    if stay is None:
        return OUTSIDE
    return FITS if norm_place(stay) in area_of(city, prefecture) else DETOUR


def trip_bounds(segments: list[Segment]) -> tuple[date, date] | None:
    if not segments:
        return None
    return min(s.start for s in segments), max(s.end for s in segments)


def watch_window(segments: list[Segment]) -> tuple[date, date]:
    """Shows dated in these calendar months count ("taking place in Jan/Feb 2027")."""
    bounds = trip_bounds(segments)
    if not bounds:
        return date(2027, 1, 1), date(2027, 2, 28)
    start, end = bounds
    first = start.replace(day=1)
    nxt = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first, nxt - timedelta(days=1)


def check_segments(rows: list[tuple[str, str, str]]) -> tuple[list[Segment], str | None]:
    """Validate (start, end, city) rows from the form. Returns segments sorted, or an error."""
    segments = []
    for i, (s, e, c) in enumerate(rows, 1):
        s, e, c = s.strip(), e.strip(), " ".join(c.split())
        if not (s or e or c):
            continue
        try:
            sd, ed = date.fromisoformat(s), date.fromisoformat(e)
        except ValueError:
            return [], f"Row {i}: dates must be filled in."
        if not c:
            return [], f"Row {i}: city can't be empty."
        if ed <= sd:
            return [], f"Row {i}: the end date must be after the start date."
        segments.append(Segment(sd, ed, c[:60]))
    segments.sort(key=lambda x: x.start)
    for a, b in zip(segments, segments[1:]):
        if b.start < a.end:
            return [], f"{a.city} and {b.city} overlap: {b.city} starts before {a.city} ends."
    return segments, None
