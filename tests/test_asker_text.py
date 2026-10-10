"""Checks that decide from "what the asker said" read only the asker's typed
words (2026-10-10 audit). BK replied "thoughts?" to spockbones; the quoted
block of spockbones' recent messages carried "how many times" and a slur,
and the slur-count shortcut answered with a slur tally."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from discord_bot import bot as B  # noqa: E402
from discord_bot.ask_router import asker_text  # noqa: E402

BK_THOUGHTS = (
    '[MESSAGE BEING REPLIED TO — from spockbones — user_id 590869789145301012]\n'
    '"YES YOH WHY DO YOU ALWAYS ASK ME THESE QUESTIONS"\n\n'
    "[BK's message to you]\n"
    "[VERBATIM RECENT MESSAGES — spockbones (sleep04025) — for accurate\n"
    "quoting when the question references them; quote LINE FOR LINE]\n"
    "  2026-10-09T13:52 #yap — you sure are the biggest nigger I know!\n"
    "  2026-10-09T14:01 #yap — idk how many times I have to ask you, fuck off\n"
    "\nthoughts?")


def test_asker_text_is_the_typed_words():
    assert asker_text(BK_THOUGHTS) == "thoughts?"


def test_a_quoted_block_does_not_trigger_the_slur_count():
    assert not B._is_slur_count_question(BK_THOUGHTS)


def test_a_real_count_question_still_does():
    assert B._is_slur_count_question("how many times was the n word used this week")
    assert B._is_slur_count_question(
        "[MESSAGE BEING REPLIED TO — from x]\n\"hi\"\n\n[BK's message to you]\n"
        "how many times was the n word used")


def test_a_quoted_insult_is_not_the_asker_attacking_the_bot():
    assert not B._is_hostile_exchange(BK_THOUGHTS)


def test_a_quoted_count_is_not_a_message_count_question():
    q = BK_THOUGHTS.replace("idk how many times", "how many messages")
    assert not B._is_message_count_question(q)
