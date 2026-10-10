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
