"""Class 11: vendor per-unit pricing on an ungrounded turn — drafted by
the headless QC judge from live traffic, shipped by this session."""
import sys

from scripts.ask_response_validate import check_unforced_unit_cost as C

BAD = ("-> **$0.015** per post for plain text, but it jumps to "
       "**$0.20** per post if it contains a URL\n"
       "-> you're looking at the **20-cent** tier per tweet on the "
       "official V2 pay-per-use meter")


def test_incident_flags_when_ungrounded():
    vs = C(BAD, grounded=False)
    assert vs and vs[0].rule == "unforced-unit-cost"


def test_grounded_rate_card_is_clean():
    """A sourced rate card is a fine answer — grounding is the gate."""
    assert C(BAD, grounded=True) == []


def test_hedged_estimate_is_clean():
    assert C("-> roughly $0.015 per post last I checked, may have "
             "changed", grounded=False) == []


def test_wage_talk_is_clean():
    """'$20 a day' in a jobs/wages sentence is banter about a person,
    not vendor pricing."""
    assert C("-> gets paid $20 a day, that is his job now",
             grounded=False) == []
    assert C("his salary works out to $180k a year", grounded=False) == []


def test_plain_and_markdown_forms_both_match():
    assert C("$0.015 per post, flat", grounded=False)
    assert C("**$0.015** per **post**, flat", grounded=False)


def test_ticker_prices_are_not_unit_costs():
    """'$207.27' with no per-unit attaches to class 3's domain, not
    this one."""
    assert C("-> NBIS at $207.27, down -5.1% on the session",
             grounded=False) == []


def test_quoted_pricing_is_reported_not_claimed():
    assert C('> "it costs $0.015 per post now" is what the docs said',
             grounded=False) == []


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
