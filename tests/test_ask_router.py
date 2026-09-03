"""Deterministic /ask router (2026-09-02): shapes, prefetch, tool policy.

The labelled questions are the room's own, from the 08-31 to 09-02 ask
logs and the QC queue, plus the shapes the prompt used to route by
text. A wrong shape here is a wrong tool in production, so every case
names the one it must get.
"""
import sys
from types import SimpleNamespace

from discord_bot import ask_router as R

CASES = [
    # slate
    ("who reports earnings today", R.EARNINGS_SLATE, [R.T_SLATE]),
    ("who is reporting earnings after close", R.EARNINGS_SLATE, [R.T_SLATE]),
    ("what earnings are today", R.EARNINGS_SLATE, [R.T_SLATE]),
    ("any big earnings tomorrow", R.EARNINGS_SLATE, [R.T_SLATE]),
    # A week question keeps the slate tools but is not prefetched: the
    # tool answers one date and today's names are not the week.
    ("what on the economic calendar for this week ? Who's reporting earnings", R.EARNINGS_SLATE, []),
    # single-ticker earnings
    ("when does NVDA report", R.EARNINGS_DATE, [R.T_EDATE]),
    ("did PLTR beat last quarter", R.EARNINGS_DATE, [R.T_EDATE]),
    ("what's expected for AVGO earnings", R.EARNINGS_DATE, [R.T_EDATE]),
    # news / odds
    ("odds HPE beats earnings", R.NEWS_EVENT, [R.T_PRICE, R.T_EDATE]),
    ("why is mrvl down off avgo earnings", R.NEWS_EVENT, [R.T_PRICE]),
    ("explain pltr death", R.NEWS_EVENT, [R.T_PRICE]),
    # price
    ("what's TSLA at", R.PRICE, [R.T_PRICE]),
    ("how's BTC doing", R.PRICE, [R.T_PRICE]),
    ("is SPY green today", R.PRICE, [R.T_PRICE]),
    # options
    ("SPY 0dte put/call ratio", R.OPTIONS_CHAIN, [R.T_CHAIN]),
    ("what's the OI on NVDA 200c", R.OPTIONS_CHAIN, [R.T_CHAIN]),
    # macro
    ("when is CPI", R.ECON_CALENDAR, [R.T_ECON]),
    ("what did NFP come in at", R.ECON_CALENDAR, [R.T_ECON]),
    ("is the fed cutting this meeting", R.ECON_CALENDAR, [R.T_ECON]),
    # history
    ("how has NVDA done since january", R.PRICE_HISTORY, [R.T_HISTORY]),
    ("PLTR ytd", R.PRICE_HISTORY, [R.T_HISTORY]),
    # company profile
    ("what does CLS do", R.COMPANY_PROFILE, [R.T_PRICE]),
    ("what does SPSC do?", R.COMPANY_PROFILE, [R.T_PRICE]),
    ("tell me about GOLD , gold.com", R.COMPANY_PROFILE, [R.T_PRICE]),
    # ledger
    ("show all of Abe's current holdings", R.MEMBER_LEDGER, []),
    ("what is Abe's % win rate on semi calls ?", R.MEMBER_LEDGER, []),
    ("what is Abe's win rate on positions that he lost on", R.MEMBER_LEDGER, []),
    ("if I started 2026 with $1MM and only full ported into Kyle's winning plays", R.MEMBER_LEDGER, []),
    # chat history
    ("what did BK say about MU yesterday", R.CHAT_HISTORY, []),
    ("what's the room saying about IBIT", R.CHAT_HISTORY, []),
    # historical statistic
    ("how has the market performed history on September 11th", R.HISTORICAL_STAT, []),
    ("when's the last time both software and semis went up a lot together", R.HISTORICAL_STAT, []),
    ("how does the market do in september on average", R.HISTORICAL_STAT, []),
    # banter / unknown -> classifier
    ("Thinking to take the other side of this trade.", R.UNKNOWN, []),
    ("you good?", R.UNKNOWN, []),
    ("is the dave chappelle show the best show ever?", R.UNKNOWN, []),
    ("thoughts on NVDA here", R.UNKNOWN, []),
]


def test_every_labelled_question_gets_its_shape():
    bad = []
    for q, shape, tools in CASES:
        r = R.classify(q)
        got_tools = [t for t, _ in r.prefetch]
        if r.shape != shape or got_tools != tools:
            bad.append((q, r.shape, got_tools, "expected", shape, tools))
    assert not bad, "\n".join(str(b) for b in bad)


def test_reply_context_is_stripped_before_shaping():
    q = ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n\"→ **MDB** drops AMC\"\n\n"
         "[SV's message to you]\nyou missed dell")
    r = R.classify(q)
    assert r.shape in (R.UNKNOWN, R.BANTER) or "DELL" in r.tickers
    q2 = "[VERBATIM RECENT MESSAGES — BK (bankerkyle) — for accurate quoting]\n  2026-09-02 x\n\nif this was 10k what is teh return"
    assert R.classify(q2).shape == R.UNKNOWN


def test_tickers_prefer_cashtags_and_skip_stopwords():
    assert R.extract_tickers("$AAPL and $nvda") == ["AAPL", "NVDA"]
    assert R.extract_tickers("is AI capex peaking, CPI tomorrow") == []
    assert R.extract_tickers("what's HPE at") == ["HPE"]
    assert R.extract_tickers("BTC and ETH ripping") == ["BTC", "ETH"]


def test_fantasy_only_when_a_league_is_configured():
    assert R.classify("who's top of the standings this week").shape != R.FANTASY
    assert R.classify("who's top of the standings this week", fantasy_enabled=True).shape == R.FANTASY


def test_tool_policy_hides_chat_search_from_data_shapes():
    price = R.classify("what's TSLA at")
    assert R.T_CHAT not in price.allowed_tools() and R.T_PRICE in price.allowed_tools()
    ledger = R.classify("show all of Abe's current holdings")
    assert R.T_TRADES in ledger.allowed_tools() and not ledger.google_allowed()
    slate = R.classify("who reports today")
    assert not slate.google_allowed(), "a slate question never goes to Google"


def _tool(names=None, google=False, code=False):
    t = SimpleNamespace(function_declarations=None, google_search=None, code_execution=None)
    if names:
        t.function_declarations = [SimpleNamespace(name=n) for n in names]
    if google:
        t.google_search = object()
    if code:
        t.code_execution = object()
    return t


def test_filter_tools_keeps_code_execution_and_applies_policy():
    tools = [_tool(google=True), _tool(code=True), _tool([R.T_CHAT]), _tool([R.T_PRICE]),
             _tool([R.T_SLATE]), _tool(["some_new_tool"])]
    kept = R.filter_tools(R.classify("what's TSLA at"), tools)
    kinds = [(t.google_search is not None, t.code_execution is not None,
              [d.name for d in (t.function_declarations or [])]) for t in kept]
    assert (True, False, []) in kinds, "PRICE allows Google for the why"
    assert (False, True, []) in kinds, "code execution always survives"
    assert (False, False, [R.T_PRICE]) in kinds
    assert (False, False, [R.T_CHAT]) not in kinds
    assert (False, False, ["some_new_tool"]) in kinds, "unknown declarations are kept"
    kept2 = R.filter_tools(R.classify("who reports today"), tools)
    assert not any(t.google_search is not None for t in kept2)


def test_factual_and_web_flags_follow_the_shape():
    assert R.classify("what's TSLA at").is_factual and not R.classify("what's TSLA at").needs_web
    assert R.classify("why is mrvl down off avgo earnings").needs_web
    assert R.classify("show all of Abe's current holdings").is_factual
    assert not R.classify("you good?").deterministic


def test_inject_text_says_so_on_error():
    txt = R.inject_text(R.T_SLATE, {"status": "error", "error": "feed down"})
    assert "do not fill the gap from memory" in txt and "feed down" in txt


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


# 2026-09-02 review: shapes that stripped the right tool, and lead-in
# words that became tickers.
def test_quote_and_public_figure_questions_are_not_room_history():
    r = R.classify("quote TSLA")
    assert r.shape == R.PRICE and r.prefetch == [(R.T_PRICE, {"symbols": ["TSLA"]})], r
    r = R.classify("what did powell say today")
    assert r.shape != R.CHAT_HISTORY and r.google_allowed(), r
    assert R.classify("what did abe say about semis").shape == R.CHAT_HISTORY
    assert R.classify("quote abe's take on nvda").shape == R.CHAT_HISTORY


def test_lead_in_words_are_not_tickers():
    assert R.extract_tickers("what's the price of gold") == []
    r = R.classify("when is the next fed meeting")
    assert r.shape == R.ECON_CALENDAR and r.prefetch, r
    assert R.classify("what is CPI in march").shape == R.ECON_CALENDAR


def test_index_and_crypto_price_questions_route_to_the_price_tool():
    r = R.classify("what's the VIX at right now")
    assert r.shape == R.PRICE and r.prefetch == [(R.T_PRICE, {"symbols": ["^VIX"]})], r
    r = R.classify("what's spx at")
    assert r.prefetch == [(R.T_PRICE, {"symbols": ["^GSPC"]})], r
    r = R.classify("btc price?")
    assert r.shape == R.PRICE and r.prefetch == [(R.T_PRICE, {"symbols": ["BTC"]})], r


def test_ledger_and_chat_shapes_are_factual():
    assert R.classify("show all of Abe's current holdings").is_factual
    assert R.classify("what did abe say about semis").is_factual


def test_every_prefetch_tool_has_an_executor_in_phase_2():
    from discord_bot import bot as B
    src = B._ask_pipeline_source()
    used = set()
    for q in ("who reports today", "when does NVDA report", "what's TSLA at",
              "NVDA options chain", "when is CPI", "how has NVDA done ytd",
              "why is nvda down, odds it beats"):
        used |= {t for t, _ in R.classify(q).prefetch}
    used |= {t for t, _ in R.classify("league standings", fantasy_enabled=True).prefetch}
    assert used >= {R.T_SLATE, R.T_EDATE, R.T_PRICE, R.T_CHAIN, R.T_ECON, R.T_HISTORY, R.T_FANTASY}
    for t in used:
        const = [k for k, v in vars(R).items() if k.startswith("T_") and v == t][0]
        assert f"_ask_router.{const}: _execute_" in src, t
