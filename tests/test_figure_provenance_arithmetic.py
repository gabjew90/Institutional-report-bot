"""Figure provenance accepts arithmetic a reader can check on the line
(2026-11-09 replay review). The replay of 60 days of /ask answers found
the check removing correct calculations: a percent move between two
levels written on the same line, a position's return from its entry and
its last price, and a stake compounded through a list of logged returns.
Each case below is the real answer, cut to the line that was removed."""
from discord_bot import figure_provenance as FP


def test_a_percent_move_between_two_levels_on_the_line_is_sourced():
    """2026-09-23: the 17.2% is (9,000 - 7,680) / 7,680."""
    ans = ("→ **Bull case:** A ~17.2% rise from current levels (~**7,680**) "
           "straight to **9,000** in a matter of days.\n\n"
           "→ **Base case:** the index holds near **7,680**.")
    rep = FP.check(ans, "SPX last 7680.12 target 9000")
    assert rep.action == "none", [f.token for f in rep.unsourced]


def test_a_return_from_entry_and_last_price_is_sourced():
    """2026-10-19: -14.9% is (2.56 - 3.01) / 3.01, both prices sourced."""
    ans = ("→ **Abe, DDOG 240P (10/23)**: Entry **$4.68**, last price **$5.43**, "
           "an unrealized gain of **+16.0%**\n"
           "→ **Abe, NOW 138P (10/23)**: Entry **$3.01**, last price **$2.56**, "
           "an unrealized return of **-14.9%**")
    ev = "NOW 138P 10-23 @3.01  DDOG 240P 10-23 @4.68  last 2.56  last 5.43"
    rep = FP.check(ans, ev)
    assert rep.action == "none", [f.token for f in rep.unsourced]


def test_a_stake_compounded_through_logged_returns_is_sourced():
    """2026-09-10: $1,000 through +80.41%, +958.83%, +750% and +2,062% is
    $3.51M. Every return is in the trade log; the product was stripped."""
    ans = ("Starting with a grand on the MSTR 142C (+80.41%), rolling that into "
           "the SNDK 1590C (+958.83%), then the META 625C and 645C (+750% and "
           "+2,062%), compounding straight through turns that $1,000 into "
           "**$3,510,715.26**.")
    q = "what if I had put $1,000 in following his trades"
    # the bot's evidence always carries the question (bot._ask_evidence_text)
    ev = ("CLOSE MSTR 142C +80.41%  CLOSE SNDK 1590C +958.83%  "
          "CLOSE META 625C +750%  CLOSE META 645C +2062%\n" + q)
    rep = FP.check(ans, ev, question=q)
    assert rep.action == "none", [f.token for f in rep.unsourced]


def test_a_wrong_percent_move_is_still_removed():
    ans = ("→ A 24% rise from **7,680** to **9,000**.\n\n"
           "→ The index holds near **7,680**.")
    rep = FP.check(ans, "7680 9000")
    assert rep.action == "stripped" and "24%" in rep.stripped_lines[0]


def test_a_wrong_compounded_total_is_still_removed():
    ans = ("→ $1,000 through +80.41% and +958.83% ends at **$25,400**.\n\n"
           "→ Both trades are in the log.")
    rep = FP.check(ans, "+80.41% +958.83% 1000")
    assert rep.action == "stripped" and "$25,400" in rep.stripped_lines[0]


def test_an_invented_win_chance_is_still_removed():
    """2026-11-02: the one figure the replay found the check right to strip."""
    ans = ("→ **6. BK (4-4)** jumps into the top eight with **977 PF**.\n\n"
           "→ Your playoff probability in an 8-team format is around **65%**.")
    rep = FP.check(ans, "BK 4-4 977 PF")
    assert rep.action == "stripped" and "65%" in rep.stripped_lines[0]


def test_a_percentage_is_checked_against_neighbouring_levels_only():
    """Code review 2026-11-09: on a dense line, any two levels could match a
    recalled percentage by chance. 20.0% is $6.01B over $5.01B, two levels
    that are not next to each other on the line."""
    ans = ("→ **FY revenue** **$6.01B** with **$2.0B** of free cash flow and "
           "**$5.01B** in cash, up **20.0%**.\n\n→ Debt is **$820M**.")
    rep = FP.check(ans, "6.01e9 2.0e9 5.01e9 820e6 revenue 6010000000 fcf 2000000000 "
                        "cash 5010000000 debt 820000000")
    assert rep.action == "stripped" and "20.0%" in rep.stripped_lines[0]
