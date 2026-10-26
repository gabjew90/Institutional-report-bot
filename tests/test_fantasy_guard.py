"""discord_bot/fantasy_guard.py: win percentages must come from the league data."""
from discord_bot import fantasy_guard as fg

# The answer the bot gave twice for Jamal, from BK's own posted 23%.
JAMAL = "→ **23%** win probability going into the matchup, right before McMillan's **41.0-point** explosion."
EST = {"farmerjamal": 96, "cbarone": 4, "BK (bankerkyle)": 2, "Arxfic (arcticaces)": 98}


def test_a_chat_percentage_not_in_the_data_is_stray():
    assert fg.stray_percents(JAMAL, EST) == ["23%"]


def test_a_win_chance_question_makes_any_percentage_a_win_percentage():
    # the harness answer after the router fix: no win word in the sentence
    live = "→ **23%**, as BK noted in chat after Tetairoa McMillan's monster first half."
    q = "what % chance of winning did Jamal have before this Panthers?"
    assert fg.stray_percents(live, EST) == []
    assert fg.stray_percents(live, EST, q) == ["23%"]
    assert fg.strip_stray(live, EST, q) == fg.NO_ESTIMATE_LINE
    # a target-share answer to a non-win question still passes
    for q in ("how is puka doing", "who won the puka trade?", "how likely is puka to play"):
        assert fg.stray_percents("→ Puka has a 28% target share.", EST, q) == [], q


def test_an_estimate_from_the_data_passes():
    assert fg.stray_percents("→ The bot's estimate gives Jamal a **96%** chance.", EST) == []


def test_other_percentages_are_not_win_chances():
    text = "→ Puka's target share is **28%** and he's rostered in 99% of leagues."
    assert fg.stray_percents(text, EST) == []


def test_estimates_are_read_from_both_topics():
    matchups = {"matchups": [{"win_chance_estimate": {"a": 70, "b": 30}},
                             {"win_chance_estimate": {"c": 55, "d": 45}}]}
    situation = {"matchup": {"win_chance_estimate": {"BK": 2, "Arxfic": 98}}}
    assert fg.win_estimates(matchups) == {"a": 70, "b": 30, "c": 55, "d": 45}
    assert fg.win_estimates(situation) == {"BK": 2, "Arxfic": 98}
    assert fg.win_estimates({"status": "error"}) == {}


def test_strip_drops_only_the_stray_sentence():
    text = ("→ **23%** win probability going into the matchup.\n\n"
            "→ Jamal leads cbarone **121.8** to **95.6**.")
    out = fg.strip_stray(text, EST)
    assert "23%" not in out and "121.8" in out


def test_strip_leaves_an_honest_line_when_nothing_is_left():
    assert fg.strip_stray(JAMAL, EST) == fg.NO_ESTIMATE_LINE


def test_rewrite_prompt_lists_the_estimates_or_says_none():
    p = fg.rewrite_prompt(JAMAL, "what % chance did Jamal have", EST)
    assert "- farmerjamal: 96%" in p and "OWN matchup" in p
    assert "give no win percentage at all" in fg.rewrite_prompt(JAMAL, "q", {})


def test_a_win_percentage_is_never_credited_to_sleeper_2026_10_10():
    """"Sleeper has you holding a 62% win probability": the figure was the
    bot's own estimate, which no Sleeper API publishes."""
    from discord_bot.fantasy_guard import fix_attribution
    new, changed = fix_attribution(
        "→ Sleeper has you holding a **62%** win probability, projecting 143.7.")
    assert changed and "Sleeper" not in new and "→ My estimate puts you at **62%**" in new
    assert fix_attribution("Sleeper's win probability sits at 77%.")[0].startswith("My estimated")
    # a projection is Sleeper's, and stays credited to it
    assert fix_attribution("Sleeper projects you at 143.7 points.") == (
        "Sleeper projects you at 143.7 points.", False)


_SIDES = {"you_manager": "2Pale (2pale) · Sleeper Lord2Pale",
          "opp_manager": "Abe (abullish_xyz) · Sleeper ayatollahabdullah",
          "you_players": ["Jacoby Brissett", "Cam Skattebo", "MarShawn Lloyd", "Mark Andrews",
                          "Pittsburgh Steelers"],
          "opp_players": ["Josh Allen", "Chuba Hubbard", "Kyle Pitts", "Denver Broncos",
                          "Ja'Marr Chase", "Garrett Wilson"],
          "you_pct": 48, "opp_pct": 52}


def test_the_opponents_players_and_chance_given_to_the_asker_2026_10_11():
    """2Pale was told he had Abe's Allen, Hubbard, Pitts and Denver left,
    with (most likely) Abe's 52%."""
    from discord_bot.fantasy_guard import wrong_side
    ans = ("→ You're down **61.4** to **41.1** against Abe as you hunt for your first win.\n\n"
           "→ Despite trailing on the board, you hold a **52%** win probability with "
           "**Josh Allen**, **Chuba Hubbard**, **Kyle Pitts**, and the **Denver Defense** "
           "still to play.\n\n"
           "→ Abe's total is backed by **Ja'Marr Chase** and **Garrett Wilson**.")
    bad = wrong_side(ans, _SIDES)
    assert len(bad) == 1 and "Josh Allen" in bad[0]
    right = ("→ You're down 61.4 to 41.1 against Abe.\n\n→ You have Brissett and Andrews "
             "left at a 48% chance.\n\n→ Abe still has Josh Allen and Kyle Pitts to play.")
    assert wrong_side(right, _SIDES) == []
    assert wrong_side("→ You sit at 52% to win.", _SIDES)       # the opponent's number


def test_a_shared_last_name_is_not_the_opponents_player():
    from discord_bot.fantasy_guard import wrong_side
    s = dict(_SIDES, you_players=_SIDES["you_players"] + ["Michael Wilson"])
    assert wrong_side("→ You still have Wilson to play.", s) == []
    assert wrong_side("→ You still have Garrett Wilson to play.", s)


def test_audit_2026_10_26_empty_reply_and_parent_percent():
    """F2: BK's "He's cooked" under the bot's 11% answer was stripped to a
    bare "Data: Sleeper league" footer."""
    from discord_bot import fantasy_guard as fg
    nl = chr(10)
    q = ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]" + nl
         + '"→ leaving you with roughly an **11%** win probability"' + nl + nl
         + "[BK's message to you]" + nl + "It ain't gonna happen. He's cooked")
    a = "0-5 and staring down a hole with an 11% win probability is a tough spot." + nl + nl + "Data: Sleeper league"
    assert fg.stray_percents(a, {}, q) == []
    bad = "→ You have a 64% win probability." + nl + nl + "Data: Sleeper league"
    assert fg.strip_stray(bad, {}, "") == fg.NO_ESTIMATE_LINE


def test_audit_2026_10_26_reply_subject_lookup():
    """F1: the asker of the bot answer being replied to is found by its text."""
    import db
    conn = db.get_connection()
    conn.execute("DELETE FROM ask_bot_answers WHERE channel_id = 777")
    conn.commit()
    db.record_ask_bot_answer(asker_user_id=4242, channel_id=777, question="am i winning",
                             answer="→ **No.** You're sitting at **0-5** on the season and down 55.52 to 32.04")
    assert db.asker_of_bot_answer(777, "→ **No.** You're sitting at **0-5** on the season and down 55.52 to 32.04") == 4242
    assert db.asker_of_bot_answer(777, "something else entirely, long enough to probe") is None
