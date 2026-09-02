"""Deterministic earnings-slate prefetch (2026-09-01)."""
import sys

from discord_bot.bot import _is_earnings_slate_question as f


def test_the_three_room_questions_are_slate_questions():
    for q in ("who reports earnings today", "who is reporting earnings after close",
              "who reports after the bell tonight", "any big earnings tomorrow",
              "what earnings do we have this week", "whos reporting tmrw"):
        assert f(q), q


def test_single_symbol_and_post_print_questions_are_not():
    for q in ("when does NVDA report", "did PLTR beat last quarter",
              "how did AVGO do", "NVDA results?", "when is CPI"):
        assert not f(q), q


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
