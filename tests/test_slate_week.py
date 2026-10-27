"""A week slate answer covers every session still to come (2026-10-26
audit A4; per session since the 2026-10-27 harness run)."""
from datetime import date

from discord_bot import slate_week as W


def _day(d, bmo, amc):
    row = lambda s, n: {"symbol": s, "name": n}
    return {"date": d, "status": "ok", "before_open": [row(*x) for x in bmo],
            "after_close": [row(*x) for x in amc]}


DAYS = [_day("2026-10-26", [], [("CDNS", "Cadence Design Systems, Inc.")]),
        _day("2026-10-27", [("KO", "Coca-Cola Company (The)")], [("V", "Visa Inc.")]),
        _day("2026-10-28", [("GEV", "GE Vernova Inc."), ("APH", "Amphenol Corporation")],
             [("GOOG", "Alphabet Inc."), ("GOOGL", "Alphabet Inc."), ("MSFT", "Microsoft Corporation")]),
        _day("2026-10-29", [("LLY", "Eli Lilly and Company"), ("MRK", "Merck & Company, Inc.")], []),
        {"date": "2026-10-30", "status": "empty"}]


def test_short_names():
    assert W.short_name("Merck & Company, Inc.") == "Merck"
    assert W.short_name("Eli Lilly and Company") == "Eli Lilly"
    assert W.short_name("ExxonMobil Holdings Corporation") == "ExxonMobil"
    assert W.short_name("Amazon.com, Inc.") == "Amazon.com"


def test_outline_is_per_session_and_one_line_per_company():
    o = W.week_outline(DAYS)
    wed_pm = next(s for s in o if s["date"] == "2026-10-28" and s["session"] == "after_close")
    assert wed_pm["symbols"] == ["GOOG", "MSFT"]
    assert wed_pm["line"] == "→ **Wed Oct 28, after the close:** Alphabet (GOOG), Microsoft (MSFT)"


def test_a_skipped_session_is_added_even_when_its_day_is_named():
    """The harness answer named Wednesday's morning and left out Alphabet
    and Microsoft after the close."""
    o = W.week_outline(DAYS)
    ans = "→ **Tue:** KO before the open, **V** after the close\n→ **Wed:** GEV and APH pre-market"
    out, added = W.fill_missing_days(ans, o, date(2026, 10, 27), morning_reported=True)
    assert added == ["2026-10-28:after_close", "2026-10-29:before_open"]
    assert "Alphabet (GOOG), Microsoft (MSFT)" in out and "CDNS" not in out


def test_todays_morning_is_skipped_after_the_open():
    o = W.week_outline(DAYS)
    out, added = W.fill_missing_days("→ $V tonight, GEV, Alphabet, MSFT, Lilly", o, date(2026, 10, 27),
                                     morning_reported=True)
    assert added == []
    _, added = W.fill_missing_days("→ $V tonight, GEV, Alphabet, MSFT, Lilly", o, date(2026, 10, 27))
    assert added == ["2026-10-27:before_open"]


def test_one_letter_tickers_need_the_company_name():
    """Code review 2026-10-26: "T" in ordinary text is not AT&T."""
    o = W.week_outline([_day("2026-10-22", [("T", "AT&T Inc")], [("F", "Ford Motor Company")])])
    out, added = W.fill_missing_days("→ Q3 F/X and T-bills", o, date(2026, 10, 20))
    assert added == ["2026-10-22:before_open", "2026-10-22:after_close"]
    assert W.fill_missing_days("→ Ford after the bell, AT&T early", o, date(2026, 10, 20))[1] == []
