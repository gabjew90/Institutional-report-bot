"""A week slate's body is rendered in code, one line per session from
today on (2026-10-26 audit A4; posted as written since 2026-10-27)."""
from datetime import date

from discord_bot import slate_week as W


def _day(d, bmo, amc):
    row = lambda s, n, c=50_000: {"symbol": s, "name": n, "market_cap_musd": c}
    return {"date": d, "status": "ok", "before_open": [row(*x) for x in bmo],
            "after_close": [row(*x) for x in amc]}


DAYS = [_day("2026-10-26", [], [("CDNS", "Cadence Design Systems, Inc.")]),
        _day("2026-10-27", [("KO", "Coca-Cola Company (The)")], [("V", "Visa Inc.")]),
        _day("2026-10-28", [("GEV", "GE Vernova Inc."), ("APH", "Amphenol Corporation")],
             [("GOOG", "Alphabet Inc."), ("GOOGL", "Alphabet Inc."), ("MSFT", "Microsoft Corporation")]),
        _day("2026-10-29", [("LLY", "Eli Lilly and Company"), ("MRK", "Merck & Company, Inc.")], []),
        _day("2026-10-30", [], [("CVE", "Cenovus Energy Inc", 30_000), ("FUBO", "fuboTV Inc.", 900)]),
        _day("2026-10-31", [], [("XYZ", "Tiny Corp", 2_000)]),
        {"date": "2026-11-01", "status": "empty"}]


def test_short_names():
    assert W.short_name("Merck & Company, Inc.") == "Merck"
    assert W.short_name("Eli Lilly and Company") == "Eli Lilly"
    assert W.short_name("ExxonMobil Holdings Corporation") == "ExxonMobil"
    assert W.short_name("Amazon.com, Inc.") == "Amazon.com"
    assert W.short_name("Coca-Cola Company (The)") == "Coca-Cola"


def test_outline_is_one_entry_per_company():
    wed_pm = next(s for s in W.week_outline(DAYS)
                  if s["date"] == "2026-10-28" and s["session"] == "after_close")
    assert wed_pm["symbols"] == ["GOOG", "MSFT"] and wed_pm["names"] == ["Alphabet", "Microsoft"]
    assert wed_pm["caps"] == [50_000, 50_000]


def test_block_is_every_session_from_today_in_order():
    lines = W.block_lines(W.week_outline(DAYS), date(2026, 10, 27), morning_reported=True)
    assert lines == [
        "→ **Tue Oct 27, after the close:** Visa (V)",
        "→ **Wed Oct 28, before the open:** GE Vernova (GEV), Amphenol (APH)",
        "→ **Wed Oct 28, after the close:** Alphabet (GOOG), Microsoft (MSFT)",
        "→ **Thu Oct 29, before the open:** Eli Lilly (LLY), Merck (MRK)",
        "→ **Fri Oct 30, after the close:** Cenovus Energy (CVE)",
        "→ **Sat Oct 31, after the close:** nothing above $10B",
    ]
    before_open = W.block_lines(W.week_outline(DAYS), date(2026, 10, 27))
    assert before_open[0] == "→ **Tue Oct 27, before the open:** Coca-Cola (KO)"


def test_mentions_count_companies_and_skip_one_letter_text():
    """Code review 2026-10-26: "T" in ordinary text is not AT&T."""
    o = W.week_outline([_day("2026-10-22", [("T", "AT&T Inc")], [("F", "Ford Motor Company")])])
    assert W.mentions_any("→ Q3 F/X and T-bills", o, date(2026, 10, 20)) == 0
    assert W.mentions_any("→ Ford after the bell, AT&T early", o, date(2026, 10, 20)) == 2
    o = W.week_outline(DAYS)
    assert W.mentions_any("Alphabet, MSFT and Lilly carry it", o, date(2026, 10, 27)) == 3
