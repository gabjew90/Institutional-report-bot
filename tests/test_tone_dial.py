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

def test_the_insult_must_be_aimed_at_the_bot_not_a_third_party():
    # Errors fall DOWNWARD: the prompt's rule is "unsure whether
    # something was a jab? It wasn't." At a 40-char window
    # "you know abe is an idiot" scored 2 and aimed a clapback at
    # someone who was insulting a third party.
    assert provocation_level(_q("you know abe is an idiot")) == 0
    assert provocation_level(_q("abe is an idiot")) == 0
    assert provocation_level(_q("his takes are trash")) == 0
    assert provocation_level(_q("the tape is trash today")) == 0
    assert provocation_level(_q("this shit is crazy")) == 0
    # but a real second-person insult still lands
    assert provocation_level(_q("you are useless")) == 2
    assert provocation_level(_q("ur takes are trash")) == 2


def test_an_insult_works_in_either_word_order():
    # "this bot is dogshit" put the subject first and scored 0,
    # missing a direct insult outright.
    for said in ("this bot is dogshit", "the bot is useless", "bot is trash"):
        assert provocation_level(_q(said)) == 2, said


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


# --- the dial must not read someone else's words (2026-09-20) ---------
VERBATIM_TAIL = (
    "[2Pale's message to you]\n"
    "[VERBATIM RECENT MESSAGES — Sam (theorb_18574) — for accurate\n"
    "quoting when the question references them; quote LINE FOR LINE]\n"
    "  2026-09-18T22:40 #stonks — You're woke\n"
    "  2026-09-18T22:42 #stonks — You fukn idiot Kyle's been crushing it\n"
    "  2026-09-19T11:13 #gambling — Blew my back out on Chelsea\n"
)


def test_a_quoted_members_insults_do_not_set_the_dial():
    """2026-09-20: 2Pale replied to someone else's ASCII art with no
    text of his own. The VERBATIM block quoting Sam sits AFTER the
    "message to you" marker, so it was captured as 2Pale's own words,
    scored a 2 ("a clapback is earned"), and the bot aimed two invented
    personal insults at a man who had written nothing. DarkMark asked an
    hour later, quote, why are you always putting all of us down for no
    reason."""
    assert asker_message(VERBATIM_TAIL) == ""
    assert provocation_level(VERBATIM_TAIL) == 0


def test_the_askers_own_words_after_a_verbatim_block_still_count():
    """The strip must take the block and nothing else: DarkMark wrote
    "Sunny lol..." after one, and that 'lol' is a real level-1 tease."""
    q = VERBATIM_TAIL + "\nSunny lol. When did you become older than me old bot"
    assert asker_message(q).startswith("Sunny lol")
    assert provocation_level(q) == 1


def test_a_real_insult_from_the_asker_still_scores():
    q = VERBATIM_TAIL + "\nyou are a useless bot"
    assert provocation_level(q) == 2
