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


def test_week_questions_prefetch_the_whole_week():
    # Since 2026-10-18 the slate tool answers a week in one call (audit A1),
    # so a week question gets that week, never today's names.
    for q, wk in (("what earnings do we have this week", "this_week"), ("who reports next week", "next_week")):
        r = R.classify(q)
        assert r.shape == R.EARNINGS_SLATE and r.prefetch == [(R.T_SLATE, {"date": wk})], (q, r)


def test_single_symbol_and_post_print_questions_are_not():
    for q in ("when does NVDA report", "did PLTR beat last quarter",
              "how did AVGO do", "NVDA results?", "when is CPI"):
        assert not f(q), q


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
