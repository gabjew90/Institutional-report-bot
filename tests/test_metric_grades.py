"""Grading words beside snapshot figures are deleted, the figures kept
(discord_bot/metric_grades.py; owner: narrate the data, never grade it)."""
from discord_bot.metric_grades import strip


def test_the_logged_october_cases():
    cases = [
        ("→ **Positioning:** Heavy short interest standing at **29.5%** of float",
         "→ **Positioning:** Short interest standing at **29.5%** of float", ["Heavy"]),
        ("up **+25.9%** on heavy volume of **13.9M** shares",
         "up **+25.9%** on volume of **13.9M** shares", ["heavy"]),
        ("a market cap of **$710.9M** and elevated short interest at **27.8% of float**.",
         "a market cap of **$710.9M** and short interest at **27.8% of float**.", ["elevated"]),
        ("before the open, with short interest sitting heavy at **27.8% of float** (Yahoo)",
         "before the open, with short interest sitting at **27.8% of float** (Yahoo)", ["heavy"]),
    ]
    for text, want, words in cases:
        assert strip(text) == (want, words)


def test_articles_adverbs_and_bold():
    assert strip("It carries an elevated short interest of 12% and a very high beta of 2.3.") == (
        "It carries a short interest of 12% and a beta of 2.3.", ["elevated", "high"])
    assert strip("a big float of 30M shares")[0] == "a float of 30M shares"
    assert strip("Short interest is high at 30% of float.")[0] == "Short interest is at 30% of float."
    assert strip("on **heavy** volume of 13.9M")[0] == "on volume of 13.9M"
    assert strip("on **heavy volume** of 13.9M")[0] == "on **volume** of 13.9M"


def test_no_figure_no_edit():
    for text in ("High valuation expectations and heavy growth pricing mean any miss hurts.",
                 "Volume ran light today.",
                 "The stock hit a 52-week high of $40 on strong demand.",
                 "Heavy short interest. Earnings are on 11/16."):
        assert strip(text) == (text, [])


def test_with_is_not_a_figure_lead_in():
    assert strip("Cash is solid with debt low at $10M") == ("Cash is solid with debt at $10M", ["low"])
