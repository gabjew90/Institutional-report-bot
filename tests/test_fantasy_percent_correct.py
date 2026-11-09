"""A stray win percentage is corrected to the estimate of the side its
sentence is about (owner, 2026-11-09), and only when code can tell which
side that is. Everything else still goes to the strip and its floor."""
from discord_bot import bot as B
from discord_bot import fantasy_guard as fg

# labels as report/sleeper_data._owner_label writes them
MATCHUPS = {"Jamal (farmerjamal) · Sleeper farmerjamal": 41,
            "cbarone (not on discord)": 59,
            "BK (bankerkyle) · Sleeper bankerkyle": 19,
            "Arxfic (arcticaces) · Sleeper arcticaces": 77}
SITUATION = {"BK (bankerkyle) · Sleeper bankerkyle (you)": 62,
             "Tulch (tulchy) · Sleeper tulchy (opponent)": 38}


def test_a_sentence_naming_one_manager_takes_that_managers_estimate():
    out, fixed, left = fg.correct_stray(
        "→ Jamal is sitting on a **23%** win chance with McMillan left to play.", MATCHUPS)
    assert (fixed, left) == (1, 0) and "**41%**" in out and "23%" not in out


def test_a_sentence_to_the_asker_takes_the_you_side():
    out, fixed, _ = fg.correct_stray(
        "→ You're at a 55% chance with Kraft still to play.", SITUATION)
    assert fixed == 1 and "62%" in out


def test_a_sentence_to_the_asker_that_names_the_opponent_is_not_guessed():
    """'Tulch is at 55.52 against your 32.04, a 62% chance': the chance
    could be either side's."""
    text = "→ Tulch is at 55.52 against your 32.04, which is a 70% win chance."
    out, fixed, left = fg.correct_stray(text, SITUATION)
    assert (out, fixed, left) == (text, 0, 1)


def test_two_sides_named_or_two_strays_are_not_guessed():
    for text in ("→ Jamal and BK are both near a 50% win chance.",
                 "→ Jamal has a 30% win chance, maybe 35%."):
        out, fixed, left = fg.correct_stray(text, MATCHUPS)
        assert (out, fixed, left) == (text, 0, 1), text


def test_a_sentence_about_the_unnamed_other_side_is_not_guessed():
    for text in ("→ Jamal needs a miracle, his opponent has a 90% win chance.",
                 "→ Jamal is a 70% underdog to win this one."):
        assert fg.correct_stray(text, MATCHUPS) == (text, 0, 1), text


def test_an_earlier_moment_is_not_given_the_current_estimate():
    """2026-10-05: Jamal's chance 'going into the matchup' is not now's."""
    text = "→ Jamal had a **23%** win probability going into the matchup."
    assert fg.correct_stray(text, MATCHUPS) == (text, 0, 1)


def test_the_guard_corrects_before_it_strips():
    answer = ("→ Tulch is at 55.52 against your 32.04 with three players left, and "
              "Lloyd, Kraft and Dicker still have to play on your side tonight, so "
              "you're looking at a 55% win chance if Kraft gets loose late.")
    meta = {"route_shape": "fantasy", "fantasy_estimates": dict(SITUATION), "guards": []}
    # names Tulch, so not corrected: the floor keeps the long sentence
    assert B._fantasy_percent_guard(answer, "how am I looking", meta) == answer
    meta = {"route_shape": "fantasy", "fantasy_estimates": dict(SITUATION), "guards": []}
    out = B._fantasy_percent_guard(
        "→ Three players left on your side tonight.\n→ You're at a 55% win chance.",
        "how am I looking", meta)
    assert "62%" in out and "55%" not in out
    assert "fantasy-percent:corrected:1" in meta["guards"]
    assert not any(g.startswith("fantasy-percent:stripped") for g in meta["guards"])


def test_an_estimate_already_right_is_untouched():
    text = "→ Jamal is at a 41% win chance."
    assert fg.correct_stray(text, MATCHUPS) == (text, 0, 0)
