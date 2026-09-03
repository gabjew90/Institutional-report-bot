"""Deterministic earnings-slate prefetch (2026-09-01), routed by
discord_bot/ask_router.py since 2026-09-02."""
import sys

from discord_bot import ask_router as R


def f(q: str) -> bool:
    return R.classify(q).shape == R.EARNINGS_SLATE


def test_the_room_questions_are_slate_questions():
    for q in ("who reports earnings today", "who is reporting earnings after close",
              "who reports after the bell tonight", "any big earnings tomorrow",
              "whos reporting tmrw"):
        assert f(q), q


def test_week_questions_are_slate_questions_without_a_prefetch():
    # The slate tool answers one date; a week question must not be
    # injected with today's names labelled authoritative.
    for q in ("what earnings do we have this week", "who reports next week"):
        r = R.classify(q)
        assert r.shape == R.EARNINGS_SLATE and not r.prefetch, (q, r)


def test_single_symbol_and_post_print_questions_are_not():
    for q in ("when does NVDA report", "did PLTR beat last quarter",
              "how did AVGO do", "NVDA results?", "when is CPI"):
        assert not f(q), q


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
