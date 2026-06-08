"""Smoke test: Tier-1 filter catches BOTH NFP spellings + impact filter
drops low-impact subreleases.

Background: 2026-06-05 vs 2026-06-08 pulse cross-source NFP drift +
2026-06-08 /ask vs pulse drift on the same data. Root cause was a
string-match bug in `fetch_economic_calendar` (pulse) and
`fetch_economic_calendar_structured` (/ask):

  - Finnhub publishes TWO NFP events on the same 8:30 ET window:
      * "Non Farm Payrolls"          (spaced)   — high-impact  HEADLINE
      * "Nonfarm Payrolls Private"   (compound) — low-impact   PRIVATE
  - Old Tier-1 keyword list had only "nonfarm payroll" (compound),
    which doesn't substring-match "Non Farm Payrolls" (spaced).
  - So the high-impact headline was silently dropped.
  - The 06-05 pulse used 120k Private (the only NFP event the calendar
    block surfaced). The 06-08 pulse used 172k headline (Gemini pulled
    it from corpus research notes, not the calendar). /ask now returns
    120k for "payrolls" queries — same divergence, opposite direction.

Fix (this commit): two-line change applied to BOTH Tier-1 filters:
  1. Add "non farm payroll" (with space) as a separate keyword so the
     spaced spelling matches.
  2. Drop US events whose impact is not in {high, medium} so the
     Private subseries (impact=low) doesn't leak through alongside the
     headline.

This smoke covers:
  - Both NFP spellings pass the filter for high-impact events
  - Low-impact subreleases (Private, Government, Manufacturing, U-6)
    are dropped EVEN when their name matches the keyword list
  - The 2026-06-05 NFP regression anchor: feeding the actual Finnhub
    payload returns the 172k headline + drops the 120k Private + the
    other low-impact employment events
  - Pulse path (`fetch_economic_calendar`) and /ask path
    (`fetch_economic_calendar_structured`) both get the fix
  - Non-payroll Tier-1 events (CPI, ECB rate decision) still work
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# The actual Finnhub payload from prod for the 06-05 8:30 ET release.
# This is the canonical 2026-06-05 regression case — the fix is correct
# iff this payload yields "Non Farm Payrolls" (172k) and DROPS
# "Nonfarm Payrolls Private" (120k), Government Payrolls, Manufacturing
# Payrolls, and U-6 Unemployment.
_REAL_FINNHUB_2026_06_05 = {
    "economicCalendar": [
        # The headline — must SURVIVE the filter post-fix
        {"event": "Non Farm Payrolls", "country": "US",
         "time": "2026-06-05 12:30:00", "impact": "high",
         "estimate": 85, "prev": 179, "actual": 172, "unit": ""},
        # The Private subseries — must be DROPPED post-fix (low impact)
        {"event": "Nonfarm Payrolls Private", "country": "US",
         "time": "2026-06-05 12:30:00", "impact": "low",
         "estimate": 85, "prev": 177, "actual": 120, "unit": ""},
        # Other low-impact subreleases that should be DROPPED
        {"event": "Government Payrolls", "country": "US",
         "time": "2026-06-05 12:30:00", "impact": "low",
         "estimate": None, "prev": 2, "actual": 52, "unit": ""},
        {"event": "Manufacturing Payrolls", "country": "US",
         "time": "2026-06-05 12:30:00", "impact": "low",
         "estimate": 2, "prev": 0, "actual": 7, "unit": ""},
        {"event": "U-6 Unemployment Rate", "country": "US",
         "time": "2026-06-05 12:30:00", "impact": "low",
         "estimate": None, "prev": 8.2, "actual": 8.1, "unit": "%"},
        # The headline unemployment rate — must SURVIVE (high impact)
        {"event": "Unemployment Rate", "country": "US",
         "time": "2026-06-05 12:30:00", "impact": "high",
         "estimate": 4.3, "prev": 4.3, "actual": 4.3, "unit": "%"},
        # Foreign event that should keep its pass-through behavior
        # (event-name match only, no impact filter applied to foreign):
        {"event": "Employment Change", "country": "CA",
         "time": "2026-06-05 12:30:00", "impact": "medium",
         "estimate": 10, "prev": -17.7, "actual": 88, "unit": ""},
    ]
}


def test_structured_fetch_keeps_headline_drops_private():
    """The /ask path: fetch_economic_calendar_structured must now return
    the high-impact "Non Farm Payrolls" 172k and drop the low-impact
    "Nonfarm Payrolls Private" 120k from the same release window."""
    from report import news_data
    with patch("report.news_data._fetch_json",
               return_value=_REAL_FINNHUB_2026_06_05), \
         patch("config.settings.finnhub_api_key", "test_key"):
        rows = news_data.fetch_economic_calendar_structured(
            query="payrolls", days_window=14,
        )
    events = [r["event"] for r in rows]
    actuals_by_event = {r["event"]: r["actual"] for r in rows}

    assert "Non Farm Payrolls" in events, (
        f"the high-impact headline 'Non Farm Payrolls' MUST be in "
        f"results post-fix (was dropped by the substring bug before), "
        f"got {events}"
    )
    assert actuals_by_event.get("Non Farm Payrolls") == 172, (
        f"headline NFP actual must be 172, got "
        f"{actuals_by_event.get('Non Farm Payrolls')}"
    )
    assert "Nonfarm Payrolls Private" not in events, (
        f"the low-impact 'Nonfarm Payrolls Private' subseries MUST be "
        f"dropped post-fix (impact=low filter), got {events}"
    )
    _ok("/ask path: headline 172k SURVIVES, Private 120k DROPPED")


def test_structured_fetch_drops_other_low_impact_payroll_subreleases():
    """Government Payrolls, Manufacturing Payrolls, U-6 Unemployment all
    share the same 8:30 ET BLS window with impact=low. The impact filter
    must drop them so the calendar block doesn't get cluttered."""
    from report import news_data
    with patch("report.news_data._fetch_json",
               return_value=_REAL_FINNHUB_2026_06_05), \
         patch("config.settings.finnhub_api_key", "test_key"):
        rows = news_data.fetch_economic_calendar_structured(days_window=14)
    events = [r["event"] for r in rows]
    for low_impact_subrelease in ("Government Payrolls",
                                   "Manufacturing Payrolls",
                                   "U-6 Unemployment Rate"):
        assert low_impact_subrelease not in events, (
            f"low-impact subrelease {low_impact_subrelease!r} must be "
            f"dropped, got events: {events}"
        )
    _ok("/ask path: 3 low-impact BLS subreleases dropped "
        "(Government / Manufacturing / U-6)")


def test_structured_fetch_keeps_high_impact_unemployment():
    """The headline 'Unemployment Rate' is high-impact and a Tier-1
    keyword match — must survive both filters."""
    from report import news_data
    with patch("report.news_data._fetch_json",
               return_value=_REAL_FINNHUB_2026_06_05), \
         patch("config.settings.finnhub_api_key", "test_key"):
        rows = news_data.fetch_economic_calendar_structured(days_window=14)
    events = [r["event"] for r in rows]
    # Should appear exactly once (US version); CA version uses different
    # filter path so isn't in our Tier-1 results
    us_unemp = [r for r in rows if r["event"] == "Unemployment Rate"
                and r["country"] == "US"]
    assert len(us_unemp) == 1, (
        f"US headline 'Unemployment Rate' must survive, got "
        f"{[r for r in rows if 'Unemployment' in r['event']]}"
    )
    _ok("/ask path: high-impact US 'Unemployment Rate' SURVIVES")


def test_query_filter_matches_both_spellings():
    """A user query of 'NFP' or 'payrolls' or 'May payrolls' must match
    the headline 'Non Farm Payrolls' event post-fix. Before the fix, the
    keyword list excluded the spaced spelling so the headline never even
    reached the query filter step."""
    from report import news_data
    for query in ("payrolls", "NFP", "Non Farm", "May payrolls"):
        with patch("report.news_data._fetch_json",
                   return_value=_REAL_FINNHUB_2026_06_05), \
             patch("config.settings.finnhub_api_key", "test_key"):
            rows = news_data.fetch_economic_calendar_structured(
                query=query, days_window=14,
            )
        headline = [r for r in rows if r["event"] == "Non Farm Payrolls"]
        assert len(headline) == 1, (
            f"query {query!r} must surface the 'Non Farm Payrolls' "
            f"headline event, got {[r['event'] for r in rows]}"
        )
    _ok("/ask path: 4 query shapes ('payrolls', 'NFP', 'Non Farm', "
        "'May payrolls') all surface the headline")


def test_pulse_path_also_keeps_headline_drops_private():
    """The pulse path (`fetch_economic_calendar`) is a separate function
    with its own copy of the Tier-1 filter. Both copies must get the
    fix or the pulse will keep showing 120k Private while /ask shows
    172k Headline (the same drift, just shifted across products)."""
    from report import news_data
    with patch("report.news_data._fetch_json",
               return_value=_REAL_FINNHUB_2026_06_05), \
         patch("config.settings.finnhub_api_key", "test_key"):
        result = news_data.fetch_economic_calendar(days_ahead=7)
    # Pulse path returns a string. Check for the 172k presence and
    # 120k absence directly.
    assert "172" in result, (
        f"pulse path: headline 172k actual must appear in formatted "
        f"output post-fix, got:\n{result}"
    )
    assert "Non Farm Payrolls" in result, (
        f"pulse path: 'Non Farm Payrolls' event name must appear, "
        f"got:\n{result}"
    )
    assert "120" not in result or "Private" not in result, (
        f"pulse path: the 120k Private subseries must NOT appear "
        f"post-fix, got:\n{result}"
    )
    _ok("pulse path (`fetch_economic_calendar`): same fix lands — "
        "172k headline appears, 120k Private absent")


def test_non_payroll_tier1_events_unaffected():
    """CPI, FOMC, ECB rate decisions — none of these have the spelling
    issue. Confirm they still pass through after the impact filter
    addition. Without this check, the impact filter could regress
    pass-through of valid Tier-1 events."""
    from report import news_data
    fake = {
        "economicCalendar": [
            {"event": "CPI YoY", "country": "US",
             "time": "2026-06-10 12:30:00", "impact": "high",
             "estimate": 4.0, "prev": 3.8, "actual": None, "unit": "%"},
            {"event": "ECB Interest Rate Decision", "country": "EU",
             "time": "2026-06-11 12:15:00", "impact": "high",
             "estimate": 2.4, "prev": 2.15, "actual": None, "unit": "%"},
            {"event": "Core CPI MoM", "country": "US",
             "time": "2026-06-10 12:30:00", "impact": "medium",
             "estimate": 0.3, "prev": 0.4, "actual": None, "unit": "%"},
        ]
    }
    with patch("report.news_data._fetch_json", return_value=fake), \
         patch("config.settings.finnhub_api_key", "test_key"):
        rows = news_data.fetch_economic_calendar_structured(days_window=14)
    events = [r["event"] for r in rows]
    assert "CPI YoY" in events
    assert "ECB Interest Rate Decision" in events
    assert "Core CPI MoM" in events, (
        "medium-impact events must pass too — only LOW-impact gets "
        "filtered. Core CPI MoM (medium) being dropped would be a "
        "regression."
    )
    _ok("non-payroll Tier-1 events (CPI / Core CPI / ECB) unaffected")


def test_both_filter_copies_match():
    """The pulse and /ask paths have duplicate copies of the Tier-1
    keyword list + filter. They must stay in sync. Smoke catches the
    case where someone updates one but not the other."""
    import inspect
    from report import news_data
    pulse_src = inspect.getsource(news_data.fetch_economic_calendar)
    ask_src = inspect.getsource(news_data.fetch_economic_calendar_structured)
    # Both must have BOTH NFP spellings
    for fn_name, src in (("pulse", pulse_src), ("/ask", ask_src)):
        assert '"nonfarm payroll"' in src, (
            f"{fn_name} filter must keep 'nonfarm payroll' (compound)"
        )
        assert '"non farm payroll"' in src, (
            f"{fn_name} filter must add 'non farm payroll' (spaced)"
        )
        assert 'impact not in ("high", "medium")' in src, (
            f"{fn_name} filter must drop low-impact US events"
        )
    _ok("both Tier-1 filter copies (pulse + /ask) have BOTH spellings "
        "+ impact gate — stayed in sync")


if __name__ == "__main__":
    print("=== Tier-1 NFP filter fix smoke ===")
    test_structured_fetch_keeps_headline_drops_private()
    test_structured_fetch_drops_other_low_impact_payroll_subreleases()
    test_structured_fetch_keeps_high_impact_unemployment()
    test_query_filter_matches_both_spellings()
    test_pulse_path_also_keeps_headline_drops_private()
    test_non_payroll_tier1_events_unaffected()
    test_both_filter_copies_match()
    print("\nALL TIER-1 NFP FILTER FIX SMOKE TESTS PASS")
