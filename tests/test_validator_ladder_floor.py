"""The validator ladder trims, never guts, and refuses only plumbing.

2026-10-18 audit A8: "how much ww2 cost the us" was replaced with the canned
"pull it from your broker" refusal, because "% of GDP" read as a macro print.
A22: "why is crypto dropping" shipped as one line after the strip rung.
"""
from scripts import ask_response_validate as V

WWII = ("→ **$341 billion** in nominal terms, about **$4.6 trillion** in today's dollars.\n"
        "→ That spending peaked near **37% of GDP** in 1944, the largest share in US history.")


def test_a_historical_gdp_share_is_not_a_macro_print():
    assert V.check_macro_unsourced(WWII, tool_calls=[]) == []


def test_a_current_print_without_the_tool_still_flags():
    a = "→ Core CPI came in at 0.3% this morning, a touch above the 0.2% consensus."
    assert [v.rule for v in V.check_macro_unsourced(a, tool_calls=[])] == ["macro-unsourced"]


def test_a_strip_that_would_gut_the_answer_ships_the_original():
    long = ("→ Bitcoin is down 4% today as spot ETF outflows hit $600 million and core CPI ran at 0.3%, "
            "which lifted yields. → Ether fell harder, down 6%, as leveraged longs were liquidated "
            "across the big venues. → Solana followed, off 5%, with funding rates flipping negative.")
    out, how = V.resolve_violations(long, None, tool_calls=[],
                                    strip_fn=lambda a, bad: "→ Soft spot demand.")
    assert how == "shipped" and out == long


def test_a_grounded_answer_never_gets_the_canned_refusal():
    a = "→ Core CPI came in at 0.3% this morning."
    out, how = V.resolve_violations(a, None, tool_calls=[], strip_fn=lambda a, bad: "",
                                    grounded=True)
    assert out == a and V.SAFE_REFUSAL not in out


def test_an_ungrounded_unsourced_print_still_gets_the_refusal():
    # When the strip leaves something, an ungrounded violation that cannot be
    # fixed still gets the refusal. A strip that leaves NOTHING ships the
    # original since 2026-10-26 (audit A2: the refusal replaced a correct
    # answer after a misfire), see the test below.
    a = "→ Core CPI came in at 0.3% this morning. → It was hot."
    out, how = V.resolve_violations(a, None, tool_calls=[],
                                    strip_fn=lambda x, bad: "→ Core CPI came in at 0.3%.")
    assert how == "replaced" and out == V.SAFE_REFUSAL


def test_the_tool_gated_figure_checks_are_retired_2026_11_09():
    """The price, chain-statistic and trend checks keyed on which tool ran,
    not on the figure, and none of their 11 fires in 60 days caught a wrong
    figure (it cut FVRR's all-time high as an unsourced price). The
    figure-provenance check covers an unsourced figure."""
    for rule in ("unforced-price", "unforced-market-data", "unforced-time-series"):
        assert rule not in V._CHECKS
    a = "→ **FVRR** hit an all-time high of **$336.00** in February 2021."
    assert V.validate(a, tool_calls=[]) == []


def test_a_strip_that_empties_a_short_answer_ships_the_original():
    """Audit A2: a short answer built on a tool or a search ships whole; one
    with neither source still gets the refusal (code review 2026-10-26)."""
    a = "→ Core CPI came in at 0.3% this morning."
    out, how = V.resolve_violations(a, None, tool_calls=["lookup_economic_calendar"],
                                    strip_fn=lambda x, bad: "")
    assert how in ("shipped", "clean") and out == a
    out, how = V.resolve_violations(a, None, tool_calls=[], grounded=True,
                                    strip_fn=lambda x, bad: "")
    assert how in ("shipped", "clean") and out == a
    out, how = V.resolve_violations(a, None, tool_calls=[], strip_fn=lambda x, bad: "")
    assert how == "replaced" and out == V.SAFE_REFUSAL
