"""No reminder, timer or ping capability: never claim one (2026-10-08 audit)."""
from discord_bot import action_claims as A


def test_reminder_requests_get_the_honest_answer():
    for q in ("remind me to ping Wock in 2h13m", "can you remind me tomorrow to sell",
              "set a timer for 10 min", "ping BK in an hour"):
        assert A.guard("stopwatch is running.", q) == (A.HONEST_LINE, True), q


def test_a_claimed_ping_is_replaced():
    assert A.guard("consider wock officially pinged.", "you never sent me a reminder")[1]


def test_ordinary_answers_are_untouched():
    for q, a in (("who pinged me earlier", "BK pinged you at 3 PM about NVDA."),
                 ("whats NVDA at", "NVDA is at 180"),
                 ("remind me what you said about MU", "You said MU earnings are the 17th.")):
        assert A.guard(a, q) == (a, False), q


def test_questions_about_pings_and_dates_are_not_requests():
    for q in ("why did BK ping me in general?", "remind me when NVDA reports"):
        assert A.guard("BK pinged you about NVDA; NVDA reports Nov 19.", q)[1] is False, q
    assert A.guard("ok", "can you remind me to sell at 2")[1]
