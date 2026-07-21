"""Calendar-question grounding — 2026-07-20: "what econ data and
earnings do we have tomorrow / today" routed WEB/FACT but the answer
(times + ticker lists, no $ figures / % / dates) matched nothing in
_FACTUAL_SPECIFIC_RE, so it shipped from model memory with no search.
A calendar slate is exactly the kind of specific that must be grounded,
so the question SHAPE forces the grounded retry regardless of what the
answer looks like.
"""
from discord_bot.bot import _is_calendar_question


def test_todays_incident_question_matches():
    assert _is_calendar_question(
        "what econ data and earnings do we have tomorrow / today"
    )


def test_earnings_this_week_matches():
    assert _is_calendar_question("any earnings this week?")


def test_econ_calendar_tomorrow_matches():
    assert _is_calendar_question("what's on the econ calendar tomorrow")


def test_data_releases_today_matches():
    assert _is_calendar_question("what data releases do we get today")


def test_past_earnings_question_no_match():
    # Retrospective, not a calendar lookup — the normal nets handle it.
    assert not _is_calendar_question("how did NVDA earnings go")


def test_no_subject_no_match():
    assert not _is_calendar_question("what do we have tomorrow")


def test_banter_no_match():
    assert not _is_calendar_question("roast terlin for papering again")
