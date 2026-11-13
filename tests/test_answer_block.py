"""A list or a calculation is posted as code rendered it, under the
model's opening (discord_bot/answer_block.py, 2026-10-27)."""
import inspect
from datetime import date

from discord_bot import answer_block as A
from discord_bot import margin_math as M
from tests.test_slate_week import DAYS
from discord_bot import slate_week as W

TODAY = date(2026, 10, 27)


def _slate_block():
    return A.build("lookup_earnings_slate", {"status": "ok", "week_outline": W.week_outline(DAYS)},
                   TODAY, morning_reported=True)


def test_slate_answer_keeps_the_opening_and_drops_the_model_list():
    """The 2026-10-27 harness answer: the model's own lines out of order."""
    ans = ("→ Megacap week, the cloud names land Wednesday night.\n\n"
           "→ **Visa ($V)** headlines Tuesday after-close\n\n"
           "→ **Coca-Cola ($KO)** leads Tuesday morning\n\n"
           "Sources:\n[1] [x](<https://x.com>)")
    out, kept = A.compose(ans, [_slate_block()], "all the big earnings this week")
    assert kept and out.startswith("→ Megacap week, the cloud names land Wednesday night.\n\n→ **Tue Oct 27")
    assert "Coca-Cola ($KO)" not in out and "x.com" not in out
    assert out.endswith("→ **Mon Nov 2, after the close:** FUBO (date not confirmed)")


def test_an_opening_that_is_itself_a_list_is_dropped():
    ans = "→ Alphabet, Microsoft and Lilly all report, plus Visa tonight."
    out, kept = A.compose(ans, [_slate_block()])
    assert not kept and out.startswith("→ **Tue Oct 27, after the close:** Visa (V)")


def test_an_opening_with_its_own_figure_is_dropped():
    """SpaceX, 2026-10-26 audit A19: the model's +4.2% against Yahoo's +23%."""
    r = M.compute(4.0, 25000.0, [{"symbol": "NBIS", "entry": 175.0}], {"NBIS": 150.0})
    blk = A.build("margin_math", {"status": "ok", **r}, TODAY)
    out, kept = A.compose("→ He's cooked, equity is down to 9.1%.", [blk], "4x margin NBIS $175 25k")
    assert not kept and out.startswith("→ **The setup:**")
    good = f"→ He's cooked, equity is {r['equity_now_pct_of_position']}% of the position."
    out, kept = A.compose(good, [blk], "4x margin NBIS $175 25k")
    assert kept and out.startswith(good)


def test_fallbacks_are_never_the_opening():
    out, kept = A.compose("→ Thought myself in circles and ran out of room.", [_slate_block()])
    assert not kept and out.startswith("→ **Tue Oct 27")


def test_no_block_for_other_tools_or_failed_results():
    assert A.build("lookup_market_price", {"status": "ok"}, TODAY) is None
    assert A.build("margin_math", {"status": "error"}, TODAY) is None
    assert A.build("lookup_earnings_slate", {"status": "ok", "date": "2026-10-27"}, TODAY) is None


def test_phase_9_composes_and_phase_2_shows_the_model_the_lines():
    from discord_bot import bot
    assert "_ab.compose(answer, _ask_meta[\"answer_blocks\"]" in inspect.getsource(
        bot._ask_09_rank_and_regen_guards)
    src = inspect.getsource(bot._ask_02_call_model_with_tools)
    assert "_pf_text += _answer_block.note(_blk)" in src
    assert "_ask_router.T_EVENTS: _execute_event_returns" in src


def test_an_opening_that_only_repeats_the_lines_is_dropped_2026_11_12():
    from discord_bot import answer_block as AB
    body = ("\u2192 **NVDA** reports in 5 days (Tue Nov 17), after market close.\n\n"
            "\u2192 Consensus: EPS **$2.48** (+90.4% y/y) on revenue **$109.0B** (+91.3% y/y).\n\n"
            "\u2192 Last report (Aug 26): EPS **$2.22**, beat the **$2.09** estimate.")
    q = "when is nvda earnings"
    assert AB.adds_nothing("\u2192 **NVDA** reports earnings on **November 17, 2026** after market close", body, q)
    assert not AB.adds_nothing("Revenue is expected to nearly double, and the last four reports all beat.", body, q)
