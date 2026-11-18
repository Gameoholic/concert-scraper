from datetime import date

import pytest

from app import fit
from app.fit import Segment

# The sample trip the database starts with (app/db.py), not a real one.
TRIP = [
    Segment(date(2027, 1, 29), date(2027, 2, 8), "Tokyo"),
    Segment(date(2027, 2, 8), date(2027, 2, 12), "Nagoya"),
    Segment(date(2027, 2, 12), date(2027, 2, 18), "Fukuoka"),
    Segment(date(2027, 2, 18), date(2027, 2, 25), "Osaka"),
    Segment(date(2027, 2, 25), date(2027, 2, 28), "Tokyo"),
]


@pytest.mark.parametrize("day,city,pref,expected", [
    (date(2027, 2, 4), "Yokohama", "Kanagawa", fit.FITS),         # Yokohama counts as Tokyo
    (date(2027, 2, 6), "Saitama", "Saitama", fit.FITS),           # so does Saitama
    (date(2027, 2, 5), "Koto", "Tokyo", fit.FITS),                # a Tokyo ward
    (date(2027, 2, 20), "Kyoto", "Kyoto", fit.FITS),              # Kyoto counts as Osaka
    (date(2027, 2, 10), "Nagoya", "Aichi", fit.FITS),
    (date(2027, 2, 14), "Fukuoka", "Fukuoka", fit.FITS),
    (date(2027, 2, 9), "Osaka", "Osaka", fit.DETOUR),             # a Nagoya night
    (date(2027, 2, 15), "Kobe", "Hyogo", fit.DETOUR),             # a Fukuoka night
    (date(2027, 2, 6), "Fuji", "Shizuoka", fit.DETOUR),
    (date(2027, 1, 31), "Osaka", "Osaka", fit.DETOUR),
    (date(2027, 2, 8), "Tokyo", "Tokyo", fit.DETOUR),             # travel day: that night is in Nagoya
    (date(2027, 2, 28), "Tokyo", "Tokyo", fit.OUTSIDE),           # departure day
    (date(2027, 1, 28), "Gifu", "Gifu", fit.OUTSIDE),             # before the trip
])
def test_fit_verdicts(day, city, pref, expected):
    assert fit.fit_for(TRIP, day, city, pref) == expected


def test_tba_and_japanese_city_names():
    assert fit.fit_for(TRIP, None, "", "") == fit.TBA
    assert fit.fit_for(TRIP, date(2027, 2, 18), "神戸市", "") == fit.FITS
    assert fit.fit_for(TRIP, date(2027, 2, 2), "Kyoto", "Kyoto") == fit.DETOUR   # "kyoto" must not become "kyo"


def test_watch_window_is_the_trip_months():
    assert fit.watch_window(TRIP) == (date(2027, 1, 1), date(2027, 2, 28))


def test_check_segments():
    segs, err = fit.check_segments([("2027-02-01", "2027-02-05", " Tokyo "), ("", "", "")])
    assert err is None and segs[0].city == "Tokyo" and len(segs) == 1
    assert fit.check_segments([("2027-02-05", "2027-02-01", "Tokyo")])[1]
    assert fit.check_segments([("2027-02-01", "2027-02-05", "Tokyo"), ("2027-02-04", "2027-02-06", "Osaka")])[1]
    assert fit.check_segments([("2027-02-01", "", "Tokyo")])[1]
