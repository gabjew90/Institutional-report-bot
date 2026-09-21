"""A list is not a glitch.

2026-09-19: `repetition` was the most-fired rewrite guard in the /ask
pipeline — 14 firings in the 14 days to 2026-09-19, 1.0/day. Twelve of
them were list-shaped answers: the top-10 message ranking, the econ
calendar, the CPI print rows. The gates cannot distinguish a token loop
from structurally parallel lines, because four rows of
"... vs consensus (prior ...)" repeat that bigram by construction, and a
ten-row ranking repeats "messages" ten times.

Each firing burned a temp-0.7 retry that returned an essentially
identical answer (96-99% similar on the turns where the log preserved
both), and four of them shipped the original anyway after the strip
fallback also failed. Zero of the fourteen were real loops.

The fix keeps the detector's documented scope — end-of-generation
repetition — and applies the gates to the last run carrying text rather
than to the whole answer. Replayed over all 187 logged turns in that
window it silences exactly the false positives and fires on nothing new.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from discord_bot.bot import (  # noqa: E402
    _has_repetition_glitch,
    _repetition_glitch_sentences,
    _repetition_runs,
)

A = "→"

CPI_ROWS = "\n".join([
    f"{A} **CPI m/m:** **+0.07%** vs **+0.4%** consensus (prior **+0.1%**)",
    f"{A} **CPI y/y:** **+3.52%** vs **+3.4%** consensus (prior **+3.4%**)",
    f"{A} **Core CPI m/m:** **+0.22%** vs **+0.2%** consensus (prior **+0.2%**)",
    f"{A} **Core CPI y/y:** **+2.67%** vs **+2.4%** consensus (prior **+2.5%**)",
])
RANKING = "\n".join(
    f"{A} **{i}. member{i}**, **{50000 - i * 900}** messages" for i in range(1, 11)
)
TWO_ROWS = "\n".join([
    f"{A} **CPI m/m & y/y** drops tomorrow at **08:30 ET**, street consensus "
    f"expects **+0.4%** MoM (prior **+0.1%**) and **3.4%** YoY (prior **3.4%**)",
    f"{A} **Core CPI m/m & y/y** prints alongside it, consensus is **+0.2%** "
    f"MoM (prior **+0.2%**) and **2.4%** YoY (prior **2.5%**)",
])

# The three loops recorded in the detector's own comments.
AVGO = ("the stock will get punished by the hyperscalers will get punished "
        "instantly kills the momentum will get punished hardliners will get "
        "punished hard on any guidance miss")
HPE = ("margins carry the lumpiness that plagued previous quarters past "
       "quarters-lumpy delays that plagued recent-history narrative dragging "
       "delays")
VOL = ("compounding risk and volatility decay risks of volatility decay and "
       "volatility decay")


# --- the shipped false positives --------------------------------------
def test_the_cpi_print_rows_are_not_a_glitch():
    assert not _has_repetition_glitch(CPI_ROWS)


def test_the_message_ranking_is_not_a_glitch():
    assert not _has_repetition_glitch(RANKING)


def test_two_parallel_rows_are_already_structure():
    """The 2026-09-10 'what's being reported tomorrow' answer. Two rows
    is enough parallelism to read as a list; the marker floor is 2."""
    assert not _has_repetition_glitch(TWO_ROWS)


def test_a_closing_code_fence_does_not_make_the_scan_vacuous():
    """Gemini wraps these answers in ```text. If the last run is the
    fence, scanning it finds nothing and every glitch walks."""
    fenced = f"```text\n{AVGO}\n```"
    assert _has_repetition_glitch(fenced)


# --- real loops still trip --------------------------------------------
def test_the_recorded_loops_still_trip():
    for loop in (AVGO, HPE, VOL):
        assert _has_repetition_glitch(loop), loop[:40]


def test_a_loop_in_the_final_bullet_trips():
    answer = "\n".join([
        f"{A} **Nvidia** beats on data center revenue tonight",
        f"{A} **AMD** reports Tuesday after the bell",
        f"{A} **Broadcom** {AVGO}",
    ])
    assert _has_repetition_glitch(answer)


def test_a_loop_in_trailing_prose_after_a_list_trips():
    answer = "\n".join([
        f"{A} **CPI** prints at 8:30",
        f"{A} **PPI** follows Thursday",
        f"{A} **Retail Sales** lands Friday",
        "net net the tape is pricing a cut pricing a cut pricing a cut",
    ])
    assert _has_repetition_glitch(answer)


# --- the split itself --------------------------------------------------
def test_marker_lines_split_into_their_own_runs():
    runs = _repetition_runs(CPI_ROWS)
    assert len(runs) == 4
    assert all(r.lstrip().startswith(A) for r in runs)


def test_prose_is_one_run_and_behaves_as_before():
    """Fewer than two marker lines is prose that happens to carry a
    dash, and the whole-answer scan is preserved for it."""
    prose = "Nothing on the calendar today - the tape is quiet into the close."
    assert _repetition_runs(prose) == [prose]
    assert _repetition_runs(AVGO) == [AVGO]


def test_consecutive_unmarked_lines_group_together():
    text = f"opening prose line one\nopening prose line two\n{A} a\n{A} b\n{A} c"
    runs = _repetition_runs(text)
    assert runs[0] == "opening prose line one\nopening prose line two"
    assert len(runs) == 4


# --- the strip fallback is unaffected ---------------------------------
def test_the_strip_fallback_still_isolates_one_bad_bullet():
    answer = "\n".join([
        f"{A} **Nvidia** beats on data center revenue tonight",
        f"{A} **Broadcom** {AVGO}",
        f"{A} **Marvell** guides next week",
    ])
    bad = _repetition_glitch_sentences(answer)
    assert len(bad) == 1 and "punished" in bad[0]
