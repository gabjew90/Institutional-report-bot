"""What the writer is shown about a member: the record, not the verdict.

Two changes, one cause (2026-09-19). The WHO'S TALKING dossier used to
carry the two scoring rationales on every call and no trading record at
all, so the only material the model had about a member was prose written
to justify a number.

That produced both faults the owner reported on 2026-09-18:

  - the register. `racism_rationale` is written in trust-and-safety voice
    ("anchored by casual bigotry and stereotyping dropped directly into
    chat"), and injected every call it became the bot's default way of
    describing a person.
  - the repetition. Four answers in eight minutes reused the same cached
    phrasing about the same member, because the cached paragraph was the
    supply.

And it left the model inferring outcomes from the room's register, where
losing is the joke that always fits: bankerkyle's "entire existence is
posting unhinged property arbitrage fantasies between blowing up
accounts" shipped on a day his log carried +234% and +208% closes, and
sunny was handed "MSTR puts into a freight train" when the MSTR puts
were BK's and Monsoon's.

So the rationales come out (`lookup_user_profile` still serves them when
a question is actually about a score) and the documented ledger goes in.
"""
import pathlib
import sys
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from db_parts.analyst import format_member_ledger_line  # noqa: E402

BK, SUNNY = 423994649317736448, 318466418301730816

RACISM_PROSE = ("Racial humor score is anchored by casual bigotry and "
                "stereotyping dropped directly into chat without hesitation. "
                "The tone is unapologetically crude.")
TRADER_PROSE = ("Chat reads like a high-octane options degen who full-ports "
                "into deep OTM index contracts thirty minutes before close.")


def _profile(uid, name):
    return {
        "display_name": name, "username": name.lower(),
        "racial_humor_score": 70, "slur_count": 19,
        "racism_rationale": RACISM_PROSE,
        "trader_rationale": TRADER_PROSE,
        "profile_text": f"**Personality and style.** {name} trades a lot.",
        "message_count_at_update": 8909,
    }


def _render(summaries):
    profiles = {uid: _profile(uid, f"m{uid}") for uid in summaries}
    with patch("db.get_profiles_for_users", return_value=profiles), \
         patch("db.get_global_trader_ranks",
               return_value=({uid: 2 for uid in summaries}, 59)), \
         patch("db.member_ledger_summary", side_effect=lambda u, **k: summaries[u]):
        return db.format_user_profiles_for_context(list(summaries))


WINNER = {"wins": 17, "losses": 3, "tickers": {"QQQ", "SOXL"},
          "avg_gain_pct": 136.4}
EMPTY = {"wins": 0, "losses": 0, "tickers": set(), "avg_gain_pct": None}


# --- the record reaches the writer ------------------------------------
def test_a_winning_record_is_in_the_dossier():
    out = _render({BK: WINNER})
    assert "17W/3L" in out, out
    assert "avg +136% on closes" in out


def test_the_traded_tickers_are_named():
    """The MSTR misattribution: the log knew whose position it was."""
    out = _render({BK: WINNER})
    assert "traded: QQQ, SOXL" in out
    assert "MSTR" not in out


def test_a_member_with_no_logged_trades_gets_no_line():
    """Most of the room never posts a screenshot. Silence is not a
    losing record, and an empty '0W/0L' would read as one."""
    out = _render({SUNNY: EMPTY})
    assert "0W/0L" not in out
    assert "documented" not in out


def test_a_losing_record_is_reported_as_it_stands():
    out = _render({SUNNY: {"wins": 0, "losses": 13, "tickers": {"PLTR"},
                           "avg_gain_pct": -41.2}})
    assert "0W/13L" in out and "avg -41% on closes" in out


def test_a_ledger_read_that_raises_does_not_lose_the_dossier():
    profiles = {BK: _profile(BK, "BK")}
    with patch("db.get_profiles_for_users", return_value=profiles), \
         patch("db.get_global_trader_ranks", return_value=({BK: 2}, 59)), \
         patch("db.member_ledger_summary", side_effect=RuntimeError("db gone")):
        out = db.format_user_profiles_for_context([BK])
    assert "BK" in out and "trades a lot" in out


# --- the verdicts do not -----------------------------------------------
def test_neither_rationale_is_injected():
    out = _render({BK: WINNER})
    assert "casual bigotry" not in out, "racism rationale must not be injected"
    assert "high-octane options degen" not in out, \
        "trader rationale must not be injected"


def test_the_ranks_themselves_are_kept():
    """The ranks answer comparative questions ('who's the most racist')
    and the owner liked that answer. Only the justifying prose goes."""
    out = _render({BK: WINNER})
    assert "trader-rank #2/59" in out
    assert "humor:70/100" in out and "slurs:19" in out


def test_the_render_side_trimmer_is_gone():
    """`trim_rationale` shortened the injected rationale at render time.
    It was patching the symptom; the rationale is no longer injected, so
    the patch is deleted rather than left to rot."""
    import db_parts.summaries as s
    assert not hasattr(s, "trim_rationale")
    assert not hasattr(s, "_SCORE_META_RE")


# --- one source of truth ------------------------------------------------
def test_the_guard_reads_the_same_summary_the_dossier_renders():
    """If the check that grades a draft reads a different record from
    the one the writer was shown, the two can disagree and the rewrite
    argues with evidence the model never saw."""
    import inspect

    from discord_bot import bot as B
    src = inspect.getsource(B._member_ledger_stats)
    assert "db.member_ledger_summary" in src
    assert "compute_member_points" not in src


# --- the line itself ----------------------------------------------------
def test_the_line_omits_an_average_when_nothing_closed():
    line = format_member_ledger_line(
        {"wins": 2, "losses": 0, "tickers": {"QQQ"}, "avg_gain_pct": None})
    assert "avg" not in line and "2W/0L" in line


def test_a_long_ticker_list_is_capped():
    line = format_member_ledger_line({
        "wins": 5, "losses": 1, "avg_gain_pct": None,
        "tickers": {f"T{i}" for i in range(12)},
    })
    assert "(+4 more)" in line
    assert line.count(",") == 7


def test_the_window_travels_with_the_numbers():
    """A `days` argument on the renderer separate from the summary let a
    30-day record print 'documented 21d' — a false receipt in the one
    block whose job is being the true one."""
    import inspect

    from db_parts import analyst
    assert "days" not in inspect.signature(
        analyst.format_member_ledger_line).parameters
    line = format_member_ledger_line(
        {"wins": 4, "losses": 1, "tickers": {"QQQ"},
         "avg_gain_pct": None, "days": 30})
    assert "documented 30d" in line
    # a summary from before the field existed still renders
    assert "documented 21d" in format_member_ledger_line(
        {"wins": 4, "losses": 1, "tickers": {"QQQ"}, "avg_gain_pct": None})
