"""Fixes from the 2026-10-04 quality audit, by finding.

#6  code     "what happened" took a Cornell story; the asker had just
             charted STX and WDC.
#8a code     "why r people in chat bullish" went to the web.
#10 code     the same punchline twice in three minutes.
#5  process  the Dropbox feed stalled two days with no alert.
"""
from discord_bot import ask_router as R
from discord_bot import bot as B
from scheduler import jobs


def test_subjectless_questions_are_detected():
    for q in ("what happened", "wtf happened?", "whats going on", "why"):
        assert R.is_subjectless(q), q
    for q in ("what happened to MU", "why is MU down", "going on vacation", "how are you"):
        assert not R.is_subjectless(q), q


def test_subject_comes_from_the_askers_own_chart_commands():
    msgs = ["what happened", "Fc wdc 5", "Fc stx stock 5", "Fc stx 5", "lol"]
    assert R.recent_subject(msgs) == ["WDC", "STX"]
    assert R.classify("what happened with $WDC and $STX").shape == R.NEWS_EVENT
    assert "$WDC, $STX" in R.subject_note(["WDC", "STX"])


def test_why_the_room_is_bullish_goes_to_the_room():
    for q in ("why they all bullish cbrs", "I’m asking why r people in. Chat bullish",
              "why are y'all so bearish"):
        r = R.classify(q)
        assert r.shape == R.CHAT_HISTORY, q
        assert r.prefetch[0][0] == R.T_ROOM, q
        assert R.T_CHAT in r.allowed_tools() and not r.google_allowed(), q
    # crowding stays a position count, never a chat search
    r = R.classify("is everyone long nvda")
    assert r.shape == R.ROOM_CROWDING and R.T_CHAT not in r.allowed_tools()
    # market participants are not the room
    for q in ("why are people buying gold", "why is everyone selling bonds"):
        assert R.classify(q).shape != R.CHAT_HISTORY, q
    assert R.classify("why is everyone in here bullish on cbrs").shape == R.CHAT_HISTORY


def test_a_reused_punchline_is_caught_and_ordinary_phrasing_is_not():
    a4 = ("spockbones is a junior M&A analyst billing corporate hours to build AI "
          "models while having public breakdowns in chat, and watching his mom's "
          "CRDO bags get torched.")
    a5 = ("wock's been junior since he started billing corporate hours to stare at "
          "spreadsheets while his mom's CRDO bags get torched.")
    assert "crdo bags get torched" in B._repeated_joke(a5, [a4])
    assert B._repeated_joke("fair enough, that is how it goes in here sometimes.",
                            ["that is how it goes when you trade"]) == ""


def test_research_feed_alert_on_a_long_gap_only():
    # 2026-09-22: 0 PDFs by 11 AM after 90 the day before, about a 20 h gap
    assert jobs.research_feed_alert(20.0) == ""
    assert jobs.research_feed_alert(jobs.RESEARCH_FEED_MAX_GAP_HOURS) == ""
    # 2026-10-02 at 4 PM ET: last PDF 12:44 UTC the day before, about 31 h
    assert "31 hours ago" in jobs.research_feed_alert(31.2)
    assert "no PDF on record" in jobs.research_feed_alert(None)


def test_market_gap_skips_weekends_and_holidays():
    from datetime import datetime, timezone
    utc = timezone.utc
    never = lambda d: False                                    # noqa: E731
    # Friday 3 PM ET (19:00 UTC) to Monday 11 AM ET (15:00 UTC): 68 h raw, 20 h of market days
    fri = datetime(2026, 9, 25, 19, 0, tzinfo=utc)
    mon = datetime(2026, 9, 28, 15, 0, tzinfo=utc)
    assert round(jobs.market_gap_hours(fri, mon, "America/New_York", never)) == 20
    assert jobs.research_feed_alert(jobs.market_gap_hours(fri, mon, "America/New_York", never)) == ""
    # the real stall: Oct 1 12:44 UTC to Oct 2 4 PM ET (20:00 UTC)
    stall = jobs.market_gap_hours(datetime(2026, 10, 1, 12, 44, tzinfo=utc),
                                  datetime(2026, 10, 2, 20, 0, tzinfo=utc),
                                  "America/New_York", never)
    assert jobs.research_feed_alert(stall)
    # a holiday in between counts as closed
    gap = jobs.market_gap_hours(datetime(2026, 9, 4, 19, 0, tzinfo=utc),
                                datetime(2026, 9, 8, 15, 0, tzinfo=utc), "America/New_York",
                                lambda d: d == "2026-09-07")
    assert round(gap) == 20
