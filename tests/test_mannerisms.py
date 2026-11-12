"""AI mannerisms are removed from /ask answers in code (2026-11-12, the
owner's voice contract)."""
from discord_bot import mannerisms as M


def test_a_transition_that_opens_a_sentence_is_dropped():
    assert M.strip("→ Furthermore, margins rose 2 points") == "→ Margins rose 2 points"
    assert M.strip("Revenue beat. Additionally, the guide was raised.") == "Revenue beat. The guide was raised."
    assert M.strip("→ EPS $4.16\n\n→ In conclusion, the print was clean") == "→ EPS $4.16\n\n→ The print was clean"
    assert M.strip("It's worth noting that short interest is 1.8%") == "Short interest is 1.8%"
    assert M.strip("Overall, the quarter beat") == "The quarter beat"
    # an answer written in lowercase stays in lowercase
    assert M.strip("furthermore, you sold the bottom") == "you sold the bottom"


def test_the_same_words_inside_a_sentence_are_left_alone():
    for text in ("Overall revenue rose 53%", "Notably absent from the guide was China",
                 "In addition to the buyback, the dividend rose", "That said, the guide was light",
                 "Meanwhile, the 10-year rose 12 bps", "Margins, notably, held"):
        assert M.strip(text) == text


def test_the_named_filler_words_become_the_plain_word():
    assert M.strip("a crucial level at $380") == "a key level at $380"
    assert M.strip("Pivotal quarter for the foundry business") == "Key quarter for the foundry business"
    assert M.strip("Goldman delves into the margin bridge") == "Goldman looks at the margin bridge"
    assert M.strip("worth delving deeper into the filing") == "worth looking at the filing"
    # capitalised inside a sentence it is a name
    assert M.strip("VMware bought Pivotal Software in 2019") == "VMware bought Pivotal Software in 2019"


def test_quoted_text_keeps_its_own_words():
    text = 'He wrote "Furthermore, this is a crucial level" at 9:41'
    assert M.strip(text) == text


def test_the_ask_cleaner_applies_it_and_records_it():
    from discord_bot import bot
    out, kinds = bot._clean_voice_violations("→ Furthermore, the crucial driver is AI chip demand")
    assert out == "→ The key driver is AI chip demand" and "mannerism" in kinds
    out, kinds = bot._clean_voice_violations("→ AMAT $381.95, down 3.6% today")
    assert out == "→ AMAT $381.95, down 3.6% today" and "mannerism" not in kinds
    # code blocks are not prose
    code = "```python\nx = 'crucial'\n```"
    assert bot._clean_voice_violations(code)[0] == code
