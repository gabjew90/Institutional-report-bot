"""2026-10-10 audit: "How many times do I need to 10x $100 to get to $1b"
shipped as "→ The ladder: $100". The figure check split the ladder at its
inline arrows, matched "$1,000" inside "$1,000,000,000", and could not see
that each rung is the asker's own arithmetic."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from discord_bot import figure_provenance as FP  # noqa: E402
from discord_bot.guard_floor import gutted  # noqa: E402

Q = "How many times do I need to 10x $100 to get to $1b"
RAW = (
    "→ **7 clean 10x runs** take a single **$100** bill straight to **$1,000,000,000**\n\n"
    "→ **The ladder:** $100 → **$1,000** → **$10,000** → **$100,000** → **$1,000,000** "
    "→ **$10,000,000** → **$100,000,000** → **$1,000,000,000**\n\n"
    "→ **The math:** seven consecutive decimal expansions represent a "
    "**10,000,000x** cumulative return multiplier")


def test_the_ladder_survives_whole():
    rep = FP.check(RAW, Q, Q)
    assert rep.action == "none", (rep.action, [f.token for f in rep.unsourced])
    assert rep.answer == RAW


def test_arithmetic_needs_the_askers_numbers():
    """Without the question as seed the rungs are still unsourced, and the
    floor ships the answer with the hedge instead of a stub."""
    rep = FP.check(RAW, Q)
    assert rep.action == "all-unsourced"


def test_a_recalled_figure_is_still_stripped():
    ans = ("→ LULU's average move after earnings is **10.2%** over the last 12 quarters\n\n"
           "→ The options price **$7.40** for the straddle this time, so the market expects "
           "a bigger move than the stock usually makes after it reports its quarter")
    rep = FP.check(ans, "straddle 7.40 implied move", "what's the lulu straddle")
    assert rep.action == "stripped"
    assert "10.2%" not in rep.answer and "$7.40" in rep.answer


def test_a_rung_is_not_found_inside_a_bigger_number():
    assert not FP._carries("to **$1,000,000,000**", "$1,000")
    assert FP._carries("→ **$1,000** next", "$1,000")


def test_inline_arrows_are_not_bullets():
    assert len(FP._lines(RAW)) == 3


def test_floor():
    long = " ".join(["word"] * 40)
    assert gutted(long, "word word")
    assert not gutted(long, " ".join(["word"] * 20))
    assert not gutted("two short lines here", "")


def test_a_figure_before_a_comma_is_still_found():
    assert FP._carries("revenue hit 4.2B, up from last year", "4.2B")
    assert FP._carries("(119.88,116.56)", "116.56")
    assert not FP._carries("1,000,000", "1,000")


def test_a_percentage_of_the_askers_number_is_arithmetic():
    ans = ("→ **5%** of a **$20,000** account is **$1,000** at risk on the trade, "
           "which is the size most of the room would call a full position for a swing")
    q = "what is 5% of my $20,000 account"
    rep = FP.check(ans, q, q)   # production evidence carries the question
    assert rep.action == "none", [f.token for f in rep.unsourced]
