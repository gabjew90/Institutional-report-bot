"""The tone dial (2026-09-04): how hot a reply may run, decided in code
from what the asker actually said. The cases are the real 09-04
exchanges where the prompt's rule existed and was not applied."""
import sys

from discord_bot.tone_dial import (asker_message, directive, max_jab_sentences,
                                   provocation_level)


def _q(said: str, quoted: str = "CLOSE **MRVL 220C** @5.08 (+91.0%)") -> str:
    return (f"[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n\"{quoted}\"\n\n"
            f"[bulch's message to you]\n{said}")


def test_the_three_09_04_replies_were_all_dial_zero_or_one():
    # None of these is provocation; all three drew a full clapback built
    # from the asker's profile and P&L history.
    assert provocation_level(_q("Gg Abe")) == 0
    assert provocation_level(_q("Puts pls.")) == 0
    assert provocation_level(_q("Hey buddy straddle hit for 11x")) == 1


def test_a_quoted_trade_alert_is_not_something_the_asker_said():
    # The reply-to machinery prepends the bot's own alert; the dial must
    # read only the asker's words, or every reply-to looks provocative.
    q = _q("Gg Abe", quoted="you absolute clown, this is trash")
    assert asker_message(q) == "Gg Abe"
    assert provocation_level(q) == 0


def test_real_provocation_still_raises_it():
    assert provocation_level(_q("you are actually useless lol")) == 2
    assert provocation_level(_q("shut the fuck up")) == 3
    assert provocation_level(_q("fuck you bot")) == 3


def test_praise_and_thanks_never_raise_it():
    for said in ("gg", "ty", "thanks", "nice", "based", "w"):
        assert provocation_level(_q(said)) == 0, said
    # backhanded praise is a POKE, one dry line, not a clapback
    assert provocation_level(_q("good boy")) == 1
    assert provocation_level(_q("wow it can read")) == 1


def test_a_plain_question_is_dial_zero():
    for q in ("what is NVDA at", "who reports today", "how does the market close friday"):
        assert provocation_level(q) == 0, q


def test_asking_to_be_roasted_is_not_provocation():
    # The prompt handles invitations separately ("the dial governs
    # UNREQUESTED heat"); the dial itself must not read it as an insult.
    assert provocation_level(_q("roast me")) == 0


def test_size_is_part_of_the_match():
    assert max_jab_sentences(0) == 0
    assert max_jab_sentences(1) == 1
    assert max_jab_sentences(2) == 4
    assert max_jab_sentences(3) > 4


def test_directive_names_the_level_and_quotes_only_the_asker():
    lvl, text = directive(_q("Puts pls."))
    assert lvl == 0 and "DIAL 0" in text
    assert "'Puts pls.'" in text
    assert "MRVL" not in text, "the quoted alert must not reach the dial line"
    assert "profile" in text and "history" in text


def test_directive_is_wired_into_the_prompt_extra():
    from discord_bot import bot as B
    src = B._ask_pipeline_source()
    assert "tone_dial import directive" in src
    assert "_dial_extra" in src and "_prompt_extra = _fact_extra" in src
    assert 'tone_dial"] = _dial_level' in src


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
