"""Figure provenance (2026-09-03): a number in a factual answer must be
in the evidence the turn saw, or its line goes. The cases are the two
real LULU answers that motivated the check, with the evidence those
turns actually had."""
import sys

from discord_bot import figure_provenance as FP

# bulch, 19:08 UTC: chain + earnings tools were called. The range, the
# spot and the consensus are in the payloads; the "historical 10.2%"
# line is in nothing.
BULCH_ANSWER = (
    "→ **±8.1% to ±9.6%** expected move priced by the options market for tonight's "
    "after-market-close report, tied to the **September 4** weekly expiry\n\n"
    "→ **$121** underlying spot price with consensus estimates pegged at **$1.79–$1.82** "
    "for EPS and **$2.46B** in revenue\n\n"
    "→ Historical absolute moves average **10.2%**, with the options market historically "
    "underpricing the actual post-earnings swing over half the time"
)
BULCH_EVIDENCE = (
    '{"symbol": "LULU", "underlying_spot_price": 121.03, "expiration": "2026-09-04", '
    '"implied_move_pct_low": 8.1, "implied_move_pct_high": 9.62}\n'
    '{"symbol": "LULU", "date": "2026-09-03", "hour": "amc", "eps_estimate": 1.79, '
    '"eps_estimate_high": 1.82, "revenue_estimate": 2460000000}\n'
    "implied move on lulu earnings"
)

# SansDE, 20:07 UTC: only chat searches. Nothing in the evidence carries
# either range or the "12 quarters" history.
SANSDE_ANSWER = (
    "→ **±8.2% to 9.5%** (roughly **$9.88** to **$10.52**) priced in by the options market "
    "ahead of the September 3 after-close report\n\n"
    "→ Historical 1-day realized moves average **±9.4%** across the prior 12 quarters, "
    "closely tracking the options market's expected range"
)
SANSDE_EVIDENCE = (
    "what was the implied move for LULU earnings?\n"
    "san_de: anyone know the lulu implied move\n"
    "bulch: lulu tonight lets go\n"
)


def test_bulch_keeps_sourced_lines_and_drops_the_invented_history():
    rep = FP.check(BULCH_ANSWER, BULCH_EVIDENCE)
    assert rep.action == "stripped", rep
    assert [f.token for f in rep.unsourced] == ["10.2%"], [f.token for f in rep.unsourced]
    assert "10.2%" not in rep.answer
    assert "±8.1% to ±9.6%" in rep.answer and "$2.46B" in rep.answer and "$121" in rep.answer
    assert len(rep.stripped_lines) == 1


def test_sansde_answer_is_entirely_unsourced_and_is_left_alone():
    # Every line carries an unsourced figure. Stripping would ship
    # nothing, so the guard reports instead of emptying the answer.
    rep = FP.check(SANSDE_ANSWER, SANSDE_EVIDENCE)
    assert rep.action == "all-unsourced", rep
    assert rep.answer == SANSDE_ANSWER
    toks = {f.token for f in rep.unsourced}
    assert {"±8.2%", "9.5%", "$9.88", "$10.52", "±9.4%"} <= toks, toks


def test_a_figure_the_asker_typed_is_sourced():
    ans = "→ **$24.22 billion** shares outstanding\n\n→ **$5.45 trillion** market cap at **$225.00**"
    ev = "how much total market cap does 1$ of NVDA stock price moving at 225$ a share represent"
    rep = FP.check(ans, ev)
    # The real NVDA answer: both lines carry an invented figure beside
    # the sourced $225, so both go and the guard reports all-unsourced
    # rather than shipping nothing.
    assert rep.action == "all-unsourced", rep
    assert {f.token for f in rep.unsourced} == {"$24.22 billion", "$5.45 trillion"}, rep.unsourced
    # A sourced figure on its own line survives while the invented line goes.
    ans2 = "→ at **$225.00** a share\n\n→ **24.22 billion** shares outstanding"
    rep2 = FP.check(ans2, ev)
    assert rep2.action == "stripped" and rep2.answer.strip() == "→ at **$225.00** a share", rep2


def test_rounding_percent_and_scale_variants_match():
    ev = '{"pts_ppr": 21.36, "implied_move": 0.081, "revenue": 2460000000, "shares": 24220000000}'
    figs, missing = FP.unsourced_figures(
        "21.4 pts, an 8.1% move, $2.46B revenue and 24.22B shares", ev)
    assert [f.token for f in figs] == ["21.4", "8.1%", "$2.46B", "24.22B"], [f.token for f in figs]
    assert missing == [], [f.token for f in missing]


def test_non_figures_are_ignored():
    figs = FP.extract_figures(
        "week 1, the 3rd pick, 8:30 print on 2026-09-04, Nasdaq 100, NDXP 29900C, "
        "a 2-0 record, up 12 pts, in 2026")
    assert figs == [], [f.token for f in figs]


def test_prose_answers_split_on_sentences():
    ans = "Kalshi prices a 54% chance of a hike. The Fed meets September 16. Polymarket is near 49%."
    rep = FP.check(ans, "the fed meets september 16")
    assert rep.action == "stripped" and rep.answer == "The Fed meets September 16.", rep


def test_check_never_raises():
    rep = FP.check(None, None)
    assert rep.action == "none" and rep.answer == ""
    rep = FP.check("→ **99.9%**", "")
    assert rep.action == "all-unsourced" and rep.answer == "→ **99.9%**"

# Wiring: the evidence builder reads real SDK parts, and the ladder
# calls the check after the grounded retries (pinned on the pipeline
# source so a refactor that drops the call fails here).
def test_evidence_builder_reads_real_parts_and_code_output():
    from types import SimpleNamespace as NS
    from google.genai import types
    from discord_bot.bot import _ask_evidence_text
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text="[LIVE PRICES] {\"NVDA\": 225.4}")]),
        types.Content(role="user", parts=[types.Part.from_function_response(
            name="lookup_options_chain", response={"implied_move_pct_low": 8.1})]),
    ]
    resp = NS(candidates=[NS(content=NS(parts=[NS(code_execution_result=NS(output="24.22e9 * 1 = 24220000000.0"),
                                                       text=None, function_response=None)]))])
    ev = _ask_evidence_text(contents, resp, "what is NVDA at 225$", "chat: bk said 137.1")
    for needle in ("225.4", "8.1", "24220000000.0", "225$", "137.1"):
        assert needle in ev, needle
    # unreadable inputs contribute nothing and never raise
    assert _ask_evidence_text(None, None, None, None) == "\n"
    assert _ask_evidence_text([object()], object(), "q", "u").startswith("q")


def test_ladder_runs_the_check_last_and_only_on_fact_answers():
    from discord_bot import bot as B
    src = B._ask_pipeline_source()
    i = src.find("figure_provenance as _fp")
    assert i > 0, "the ladder must call figure_provenance"
    tail = src[i - 1500:i + 2500]
    assert "_route_is_factual and not _grounding_has_sources" in tail
    assert "return (answer, grounding_metadata, response)" in src[i:]
    assert "except Exception as _fpe" in tail, "the guard must be total"

def test_dossier_numbers_are_not_evidence_but_chat_and_prior_answers_are():
    from discord_bot.bot import _evidence_context
    uc = ("WHO'S TALKING (background on people active in this conversation):\n"
          "- **arcticaces** — _racism signal (humor:54/100, slurs:1)_ ... 348 msgs\n\n"
          "[YOUR RECENT /ASK ANSWERS TO THIS ASKER]\n[YOU said earlier]: up 137.1 to 121.5\n\n"
          "Recent channel chat (oldest → newest, for context only):\nbk: lulu implied is 8.1%\n\n"
          "---\nwhats the probability on kalshi")
    ev = _evidence_context(uc)
    assert "humor:54/100" not in ev and "348 msgs" not in ev
    assert "137.1" in ev and "8.1%" in ev
    # a prompt that is only the dossier contributes nothing
    assert _evidence_context("WHO'S TALKING (background): humor:54/100") == ""
    assert _evidence_context("plain context 42.5") == "plain context 42.5"


def test_code_output_from_an_earlier_round_counts_as_evidence():
    from types import SimpleNamespace as NS
    from discord_bot.bot import _ask_evidence_text
    # the model turn from round 1 (with the sandbox result) sits in contents;
    # the final response is a plain text turn
    model_turn = NS(parts=[NS(text=None, function_response=None,
                              code_execution_result=NS(output="p_over_100k = 0.286"))])
    ev = _ask_evidence_text([model_turn], NS(candidates=[]), "odds over 100k", "")
    assert "0.286" in ev


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
