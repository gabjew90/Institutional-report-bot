"""discord_bot/room_rank.py: rankings run only when asked for (2026-10-02)."""
from discord_bot import data_footer, room_rank

# The real follow-up from 2026-10-01 19:40 UTC that got the racism board.
CROWN_FOLLOWUP = (
    "[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1422761344322502807]\n"
    '"hard to crown anyone when the whole room spends six hours a day staring at '
    'red 0DTE candles and crying about Schwab being down. it\'s a team sport at this point."\n\n'
    "[Ligma's message to you]\nGimmie top 5, who has the crown"
)


def test_unasked_racism_board_is_refused():
    out = room_rank.gate({"metric": "racism"}, CROWN_FOLLOWUP)
    assert out["status"] == room_rank.METRIC_NOT_ASKED
    assert out["users"] == []
    assert "roast" in out["error"]


def test_unasked_trader_board_is_refused():
    assert room_rank.gate({"metric": "trader"}, "who's the most gay in chat?") is not None


def test_asked_racism_board_runs():
    for q in ("who's the most racist in here", "top 5 slurs this month",
              "n word leaderboard", "who's the biggest bigot"):
        assert room_rank.gate({"metric": "racism"}, q) is None, q


def test_followup_on_a_racism_board_runs():
    q = ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"
         '"→ #1, BK, 230 race-edged lines and 181 slurs"\n\n'
         "[Ligma's message to you]\nwho's #6")
    assert room_rank.gate({"metric": "racism", "rank_position": 6}, q) is None


def test_asked_trader_board_runs():
    for q in ("who's the best trader", "top 5 by p&l", "who's winning the most",
              "worst trader in here", "who blew up their account"):
        assert room_rank.gate({"metric": "trader"}, q) is None, q


def test_biggest_loser_is_a_trader_question():
    assert room_rank.gate({"metric": "trader"}, "who's the biggest loser this month") is None
    assert room_rank.superlative_note("who's the biggest loser this month") == ""


def test_rat_race_is_not_a_racism_question():
    assert room_rank.gate({"metric": "racism"}, "who's winning the rat race") is not None


def test_quoted_member_slurs_do_not_unlock_the_racism_board():
    q = ("[VERBATIM RECENT MESSAGES — Monsoon (reportufirst) — for accurate\n"
         "quoting when the question references them; quote LINE FOR LINE]\n"
         "  2026-10-01 15:00 #stonks — dropped another slur lol\n\n"
         "[Sam's message to you]\nwho's the most unhinged in here")
    assert room_rank.gate({"metric": "racism"}, q) is not None
    asked = q.replace("who's the most unhinged in here", "who's the most racist in here")
    assert room_rank.gate({"metric": "racism"}, asked) is None


def test_lookup_by_username_is_never_gated():
    assert room_rank.gate({"username": "bankerkyle"}, "who's the most gay in chat?") is None


def test_superlative_trait_for_unmeasured_traits():
    assert room_rank.superlative_trait("who’s the most gay in chat?") == "gay"
    assert room_rank.superlative_trait("who is the biggest simp here") == "simp"
    assert room_rank.superlative_trait("whos the least funny") == "funny"


def test_no_superlative_note_for_measured_traits_or_other_questions():
    for q in ("who's the most racist in chat", "who's the best trader",
              "who's the worst trader here", "what time is ISM", "who has the crown"):
        assert room_rank.superlative_note(q) == "", q


def test_superlative_reads_only_the_askers_own_words():
    q = ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"
         "\"who's the most annoying here? probably everyone\"\n\n"
         "[Sam's message to you]\nwhat time is ISM")
    assert room_rank.superlative_note(q) == ""


def test_superlative_note_names_the_trait_and_bans_the_rankings():
    note = room_rank.superlative_note("who's the most gay in chat?")
    assert '"gay"' in note and "do not use" in note and "commit" in note.lower()


def test_footer_names_the_real_source_per_metric():
    racism = [{"tool": "lookup_user_profile", "args": {"metric": "racism"}, "status": "ok"}]
    trader = [{"tool": "lookup_user_profile", "args": {"metric": "trader"}, "status": "ok"}]
    member = [{"tool": "lookup_user_profile", "args": {"username": "bk"}, "status": "ok"}]
    assert data_footer.footer(racism) == "\n\nData: room chat tags"
    assert data_footer.footer(trader) == "\n\nData: room trade ledger"
    assert data_footer.footer(member) == "\n\nData: room trade ledger and chat tags"


def test_footer_skips_a_refused_ranking():
    refused = [{"tool": "lookup_user_profile", "args": {"metric": "racism"},
                "status": room_rank.METRIC_NOT_ASKED}]
    assert data_footer.footer(refused) == ""
