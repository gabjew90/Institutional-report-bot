"""What the room calls a member (2026-09-24).

On 9/22 the bot answered "kyle came out as gay today can you congratulate
him?" with "takes real courage to finally admit his true passion isn't
just blowing accounts on weekly lottos". Kyle's log carried 17 wins to
3 losses. Nobody types "bankerkyle" or "BK", so his record never reached
the writer and the ledger check could not tie "kyle" to him.

The rows below are the display-name counts measured on production the
same day, prank renames included: Grand Nagus Yeezy has posted as
"Banker Kyle" and "Abullish", Aunt Jemima as "Monsoon".
"""
from unittest.mock import patch

import db
from db_parts import chat as C
from discord_bot import bot as B
from discord_bot import pnl_claims as P

BK, SAM, LIGMA, YEEZY, ABE, MONSOON, AUNT, RY, TULCH = (
    423994649317736448, 2, 3, 4, 5, 6, 7, 8, 9)

ROWS = [
    (BK, "BK", 50614), (BK, "bearishkyle", 2649), (BK, "M&AK", 78),
    (SAM, "Sam", 20685), (SAM, "Sammybear", 2039),
    (LIGMA, "Ling Mai", 4784), (LIGMA, "Ligma", 2077), (LIGMA, "Ling Mai Bear", 243),
    (YEEZY, "Grand Nagus Yeezy", 12536), (YEEZY, "A Bullish Grand Nagus", 608),
    (YEEZY, "Banker Kyle", 10), (YEEZY, "Abullish", 3),
    (ABE, "abullish", 3522), (ABE, "abe", 3502), (ABE, "abugs bunny", 2770),
    (MONSOON, "Monsoon", 5982), (MONSOON, "Moonsoon", 3348), (MONSOON, "Moonbear", 1235),
    (AUNT, "Wock", 4287), (AUNT, "Monsoon", 7), (AUNT, "MonsoonIs35AndCantFindAWoman", 11),
    (RY, "Ry_bry", 4943), (RY, "Ry_spaceman", 4255), (RY, "Abullish", 53),
    (TULCH, "bulch", 8181), (TULCH, "Tulch", 6049),
]
AMAP = C.build_member_aliases(ROWS)


# --- the map ------------------------------------------------------------
def test_kyle_is_bankerkyle_by_owner_pin():
    assert AMAP["kyle"] == BK
    assert AMAP["bearishkyle"] == BK


def test_prank_renames_do_not_count():
    """Yeezy's 10 messages as "Banker Kyle" must not make "banker kyle"
    his, and Aunt Jemima's 7 as "Monsoon" must not make "monsoon" hers."""
    assert AMAP.get("banker kyle") is None
    assert AMAP["monsoon"] == MONSOON
    assert AMAP["abullish"] == ABE, "Ry's 53 prank posts as Abullish are below 2%"


def test_retired_names_the_room_still_uses_resolve():
    assert AMAP["ling mai"] == LIGMA and AMAP["ling"] == LIGMA
    assert AMAP["tulch"] == TULCH and AMAP["abe"] == ABE
    assert AMAP["wock"] == AUNT and AMAP["moonsoon"] == MONSOON


def test_ordinary_words_in_names_never_resolve():
    for w in ("bear", "bullish", "grand"):
        assert w not in AMAP, w


def test_a_name_two_members_both_used_is_dropped():
    rows = [(1, "Mark", 500), (2, "Mark", 500)]
    assert "mark" not in C.build_member_aliases(rows)


def test_named_in_text_is_whole_word():
    named = db.members_named_in_text("kyle came out as gay today", AMAP)
    assert list(named) == [BK]
    assert db.members_named_in_text("skyler and kylem", AMAP) == {}


# --- scope ------------------------------------------------------------------
def test_scope_is_the_askers_words_and_the_replied_message_not_quoted_chat():
    q = ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"
         "\"appreciate it. back to the terminals\"\n"
         "[Sam's message to you]\n"
         "[VERBATIM RECENT MESSAGES — Wock (sleep04025) — for accurate\n"
         "quoting when the question references them; quote LINE FOR LINE]\n"
         "  2026-09-22T10:00 #stonks — tulch is cooked\n"
         "Thanks man kyle came out as gay today can you congratulate him?")
    scope = B._named_scope(q)
    assert "kyle" in scope and "appreciate it" in scope
    assert "tulch" not in scope, "quoted chat history is not the question"


# --- the record reaches the writer and the check ---------------------------
LEDGER = {"wins": 17, "losses": 3, "tickers": {"QQQ", "SOXL"},
          "avg_gain_pct": 136.4, "days": 21}


def _records():
    prof = {BK: {"display_name": "BK", "username": "bankerkyle"}}
    with patch("db.get_profiles_for_users", return_value=prof), \
         patch("db.member_ledger_summary", return_value=LEDGER), \
         patch("db.aliases_for", side_effect=lambda u: C.aliases_for(u, AMAP)):
        return db.format_named_member_records({BK: ["kyle"]})


def test_a_named_member_gets_the_record_line_only():
    out = _records()
    assert "- **BK** (bankerkyle, <@423994649317736448>)" in out
    assert "also called: bearishkyle, kyle" in out
    assert "17W/3L" in out and "record only" in out
    assert "Personality" not in out


def test_no_trades_means_no_ledger_bit_not_a_zero_record():
    prof = {BK: {"display_name": "BK", "username": "bankerkyle"}}
    with patch("db.get_profiles_for_users", return_value=prof), \
         patch("db.member_ledger_summary",
               return_value={"wins": 0, "losses": 0, "tickers": set(), "avg_gain_pct": None}), \
         patch("db.aliases_for", return_value=[]):
        out = db.format_named_member_records({BK: ["kyle"]})
    assert "0W/0L" not in out and "no documented" not in out


def test_the_9_22_answer_is_now_caught():
    answer = ("mazel tov to kyle, takes real courage to finally admit his true "
              "passion isn't just blowing accounts on weekly lottos.")
    members = B._profile_member_ids(_records())
    assert members.get("kyle") == BK
    cands = P.claim_candidates(answer, members)
    bad_loss, _ = P.judge_candidates(cands, {BK: LEDGER})
    assert bad_loss and bad_loss[0]["user_id"] == BK


def test_the_prompt_builder_appends_the_records():
    import pathlib
    src = pathlib.Path(B.__file__).read_text(encoding="utf-8")
    assert "db.members_named_in_text, _named_scope(question)" in src
    assert "db.format_named_member_records" in src
