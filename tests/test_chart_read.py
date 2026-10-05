"""Reading an attached chart (owner decision, 2026-10-04, audit finding #9).

bulch attached a 30-second CBRS chart and asked for technical analysis;
the bot answered "Indicator reads and pattern calls stay on your screen"
because the prompt said it had no chart view. With an image it does: it
may read levels and patterns off the chart, labelled as the asker's chart.
Without an image the ban stands.
"""
from discord_bot import ask_prompt
from scripts.ask_response_validate import check_self_generated_ta


def test_a_labelled_read_of_the_askers_chart_passes():
    ans = "→ On your chart, support sits at $168.30 with a double top near $170.49."
    assert check_self_generated_ta(ans) == []


def test_an_unlabelled_technical_read_is_still_flagged():
    ans = "→ Support sits at $168.30 with a double top near $170.49."
    assert check_self_generated_ta(ans)


def test_the_prompt_allows_an_attached_chart_and_keeps_the_ban_otherwise():
    p = ask_prompt._ASK_SYSTEM_INSTRUCTION if hasattr(ask_prompt, "_ASK_SYSTEM_INSTRUCTION") \
        else open(ask_prompt.__file__, encoding="utf-8").read()
    assert "a chart image the asker attached" in p
    assert "You have no chart view" not in p
    assert "read off an attached chart" in p
    assert "Without an image, offer what you DO have" in p
