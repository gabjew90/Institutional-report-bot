"""A parallel statement is not a loop.

2026-11-13: "did amat earnings drop" ended with one arrow carrying two
analysts' target cuts. The detector's tokens are letters only, so
"target to $520 from $650 ... target to $475 from $700" read as
"target to from ... target to from" and the loose bigram gate counted
("target", "to") twice. The turn spent a retry, the retry tripped the
same way, the strip found nothing to remove (the sentence splitter cut
the arrow at "B. Riley") and the original was posted with
`repetition-shipped`.

A loop repeats the same text, figures included. Two occurrences of a
bigram are different text when both carry a figure in the same position
(directly before, inside or directly after the bigram) and the figures
differ. Replayed over the 8,855 distinct sentences in the ask logs, the
change stops 32 from tripping, all of them correct statements, and makes
nothing new trip.
"""
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from discord_bot.bot import (  # noqa: E402
    _has_repetition_glitch,
    _rep_same_twice,
)

A = "→"

AMAT_TARGETS = (
    f"{A} Bank of America lowered its price target to **$520** from **$650** "
    "with a Buy rating, and B. Riley cut its target to **$475** from **$700** "
    "following the print"
)
AMAT_ANSWER = "\n".join([
    f"{A} **AMAT** $231.10, +0.6% after hours",
    f"{A} The semiconductor systems segment drives the result, with "
    "leading-edge foundry and DRAM spending ahead of plan",
    AMAT_TARGETS,
])

# The three loops in the Gate 3 comment.
AVGO = ("the stock will get punished by the hyperscalers will get punished "
        "instantly kills the momentum will get punished hardliners will get "
        "punished hard")
HPE = ("margins carry the lumpiness that plagued previous quarters past "
       "quarters-lumpy delays that plagued recent-history narrative dragging "
       "delays")
TMO = "$TMO is the first to the first to see the volume"


# --- must not fire -----------------------------------------------------
def test_two_target_cuts_in_one_arrow_are_not_a_loop():
    assert not _has_repetition_glitch(AMAT_TARGETS)
    assert not _has_repetition_glitch(AMAT_ANSWER)


def test_a_figure_before_the_repeated_words_counts_too():
    """Gate 2 shape: both words are content words, the figure leads."""
    line = (f"{A} Bank of America carries a **$520** price target and "
            "B. Riley a **$475** price target after the report")
    assert not _has_repetition_glitch(line)


def test_parallel_results_from_the_logs_are_not_loops():
    """Sentences the bot posted that the old gates flagged when read
    alone, which is how the strip fallback reads them."""
    for line in (
        f"{A} **TXN Q2 Earnings:** Reported **$2.14 EPS** (beating the "
        "**$1.93** consensus) and **$5.46B** revenue (beating the "
        "**$5.24B** estimate).",
        f"{A} **76.9M** shares outstanding, with a public float of "
        "**56.2M** shares (Yahoo snapshot)",
        f"{A} **November 7** at **Illinois** and **November 14** at "
        "**Rutgers** for a two-game road swing",
    ):
        assert not _has_repetition_glitch(line), line[:50]


def test_a_figure_between_two_equal_words_is_not_a_doubled_word():
    """Gate 0 read "to 1 to 2" as "to to"."""
    for line in (
        f"{A} **Monthly positions:** Keep to **1 to 2**, sized the same "
        "as the weeklies.",
        f"{A} **M4 launch (late 2024):** Hit the market at **$599** for "
        "the base 16GB/256GB configuration, a top-tier value leader",
    ):
        assert not _has_repetition_glitch(line), line[:50]


# --- must fire ----------------------------------------------------------
def test_the_documented_loops_still_trip():
    for loop in (AVGO, HPE, TMO):
        assert _has_repetition_glitch(loop), loop[:40]


def test_a_loop_that_repeats_its_figure_still_trips():
    loop = (f"{A} Bank of America cut its target to **$520** from **$650** "
            "and cut its target to **$520** from **$650** after the print")
    assert _has_repetition_glitch(loop)


def test_a_figure_beside_only_one_occurrence_does_not_excuse_a_loop():
    loop = ("the print gets punished by 2027 buyers and the print gets "
            "punished hard on any guidance miss this week")
    assert _has_repetition_glitch(loop)


def test_a_doubled_word_still_trips():
    assert _has_repetition_glitch(
        "bouncing between zero-day option panics and momentum momentum plays")


# --- the comparison itself ---------------------------------------------
def test_same_twice_needs_a_differing_figure_in_the_same_position():
    none = ((), (), ())
    assert _rep_same_twice([none, none])
    assert _rep_same_twice([none, ((), (), ("520",))])
    assert _rep_same_twice([((), (), ("520",)), ((), (), ("520",))])
    assert not _rep_same_twice([((), (), ("520",)), ((), (), ("475",))])
    # a figure before one and after the other is not the same position
    assert _rep_same_twice([(("5",), (), ()), ((), (), ("7",))])
    # three occurrences, two of them alike
    assert _rep_same_twice([((), (), ("520",)), ((), (), ("475",)),
                            ((), (), ("520",))])
    assert not _rep_same_twice([none])


# --- the log keeps what the check fired on ------------------------------
def test_the_ask_log_keeps_the_answer_the_check_fired_on():
    import db
    from config import settings
    with tempfile.TemporaryDirectory() as d:
        with patch.object(settings, "pdf_download_dir",
                          str(pathlib.Path(d) / "pdfs")):
            kwargs = dict(asker_display_name="BK", asker_username="bankerkyle",
                          channel_name="stonks", question="did amat earnings drop",
                          answer="the retried answer")
            path = db.append_ask_interaction(
                **kwargs, meta={"guards": ["repetition"],
                                "repetition_original": AMAT_ANSWER})
            db.append_ask_interaction(**kwargs, meta={"guards": []})
            # posted as it was: the raw block already carries the text
            db.append_ask_interaction(
                **kwargs, raw_answer=AMAT_ANSWER,
                meta={"guards": ["repetition", "repetition-shipped"],
                      "repetition_original": AMAT_ANSWER})
            text = pathlib.Path(path).read_text(encoding="utf-8")
    assert text.count("Answer the repetition check fired on") == 1
    assert "its target to **$475** from **$700**" in text
