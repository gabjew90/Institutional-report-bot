"""A week slate answer covers every day still to come (2026-10-26 audit A4)."""
from datetime import date

from discord_bot import slate_week as W


def _day(d, bmo, amc):
    row = lambda s, n: {"symbol": s, "name": n}
    return {"date": d, "status": "ok", "before_open": [row(*x) for x in bmo],
            "after_close": [row(*x) for x in amc]}


DAYS = [_day("2026-10-19", [], [("NFLX", "Netflix")]),
        _day("2026-10-20", [("GE", "GE Aerospace")], [("ISRG", "Intuitive")]),
        _day("2026-10-21", [], [("IBM", "IBM"), ("TXN", "Texas Instruments")]),
        _day("2026-10-22", [("UNP", "Union Pacific")], [("WDC", "Western Digital")]),
        {"date": "2026-10-23", "status": "empty"}]


def test_outline_lines():
    o = W.week_outline(DAYS)
    assert [d["date"] for d in o] == ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"]
    assert o[3]["line"] == ("→ **Thu Oct 22:** before the open Union Pacific UNP; "
                            "after the close Western Digital WDC")


def test_skipped_days_are_added_but_past_days_are_not():
    o = W.week_outline(DAYS)
    ans = "→ **Tue:** GE before the open"
    out, added = W.fill_missing_days(ans, o, date(2026, 10, 20))
    assert added == ["2026-10-21", "2026-10-22"] and "IBM" in out and "NFLX" not in out
    full = ans + " · IBM · $WDC"
    assert W.fill_missing_days(full, o, date(2026, 10, 20)) == (full, [])
