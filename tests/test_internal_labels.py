"""Internal block labels and status tokens never reach the room.

2026-10-18 audit: "[LEAGUE STATE]" closed four fantasy answers (F4) and
"(`past_no_data`)" shipped twice on PPI day (A27). Lines are from those answers.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from discord_bot.internal_labels import strip  # noqa: E402


def test_league_state_tag_is_removed():
    a = "→ Sitting at a **43%** win probability against Arxfic this week [LEAGUE STATE]."
    assert strip(a) == "→ Sitting at a **43%** win probability against Arxfic this week."


def test_status_token_in_parentheses_is_removed():
    a = ("→ The official wire is still syncing through the BLS terminal feed "
         "(`past_no_data`), check your terminal directly")
    assert strip(a) == ("→ The official wire is still syncing through the BLS terminal feed, "
                        "check your terminal directly")


def test_bare_token_is_removed():
    assert "past_no_data" not in strip("the print status is past_no_data right now")


def test_ordinary_brackets_and_words_survive():
    a = "Goldman [GS] sees no data dependence; see [1] and [Stock Titan]."
    assert strip(a) == a


def test_every_inject_label_is_known():
    """A new prefetch block label must be added to the scrubber too."""
    import re
    src = (pathlib.Path(__file__).resolve().parents[1] / "discord_bot" / "ask_router.py").read_text(encoding="utf-8")
    from discord_bot.internal_labels import _LABELS
    labels = set(re.findall(r'^        T_[A-Z]+: \(?"([A-Z][A-Z ]+),', src, re.M))
    assert labels and labels <= set(_LABELS), labels - set(_LABELS)
