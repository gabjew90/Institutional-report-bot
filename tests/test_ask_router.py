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


# 2026-09-03: the fantasy shape used to require "draft grade"/"draft
# pick" and prefetched standings for every question, so a draft ask got
# pre-season zeros injected as the authoritative block.
def test_fantasy_gate_catches_the_room_phrasings():
    for q in ("who won the draft", "grade my draft", "draft recap",
              "best waiver pickup", "who should i start this week",
              "start or sit gibbs", "my matchup this week", "faab left",
              "trending adds", "who is in the league", "whats on my roster"):
        assert R.classify(q, fantasy_enabled=True).shape == R.FANTASY, q


def test_fantasy_gate_does_not_steal_trading_questions():
    # The shape strips Google and every market tool, so a false positive
    # is expensive. "my team" and "first place" are deliberately not gate
    # words.
    for q in ("my team is bleeding on this trade", "first place in the s&p sectors",
              "whats DKNG at", "draftkings earnings date", "who reports today",
              "whats the lineup for tomorrow earnings", "trade idea on nvda",
              "how many times did bk say bench"):
        assert R.classify(q, fantasy_enabled=True).shape != R.FANTASY, q


def test_fantasy_prefetch_topic_follows_the_question():
    cases = {
        "who won the draft": "draft",
        "grade my draft": "draft",
        "best waiver pickup": "transactions",
        "faab left": "transactions",
        "trending adds": "trending",
        "who should i start this week": "projections",
        "my matchup this week": "matchups",
        "who is in the league": "league",
        "league standings": "standings",
        "hows the league doing": "standings",   # fallback
    }
    for q, topic in cases.items():
        r = R.classify(q, fantasy_enabled=True)
        assert r.prefetch == [(R.T_FANTASY, {"topic": topic})], (q, r.prefetch)


def test_roster_questions_are_not_prefetched():
    # topic=roster needs a manager the router cannot resolve; prefetching
    # would inject "could not match a manager" as authoritative.
    r = R.classify("whats on my roster", fantasy_enabled=True)
    assert r.shape == R.FANTASY and r.prefetch == [], r


def test_fantasy_topics_are_all_real_sleeper_topics():
    from report.sleeper_data import TOPICS
    for _topic, _rx in R._TOPIC_RES:
        assert _topic in TOPICS, _topic
    assert R.fantasy_topic("something with no topic words") in TOPICS


def test_code_execution_survives_the_fantasy_tool_filter():
    class _FD:
        def __init__(s, n): s.name = n

    class _T:
        def __init__(s, names=None, google=None, code=None):
            s.function_declarations = [_FD(n) for n in names] if names else None
            s.google_search = google
            s.code_execution = code
    tools = [_T(google="g"), _T(code="c")] + [_T([n]) for n in sorted(R.ALL_TOOLS - {R.T_GOOGLE})]
    route = R.classify("who won the draft", fantasy_enabled=True)
    kept = R.filter_tools(route, tools)
    assert any(t.code_execution for t in kept), "sandbox must stay: it is how the answer is computed"
    # Google stays for player news and injuries, which the league tool
    # cannot answer; league STATE still comes from the injected payload.
    assert any(t.google_search for t in kept)
    decls = {d.name for t in kept if t.function_declarations for d in t.function_declarations}
    assert decls == {R.T_FANTASY, R.T_CHAT}, decls


# 2026-09-03, owner: "if the asker is asking in the football channel,
# it's gonna be about the sleeper fantasy".
_FC = "🏈-fantasy-football-yapping-🏈"
_SC = "💬-stonks-yapping-💬"


def test_football_channel_makes_loose_questions_league_questions():
    for q in ("hows my team looking", "whos winning this week", "whats declans record",
              "should i bench my te", "stream a defense this week", "is bijan a good start"):
        assert R.classify(q, fantasy_enabled=True, channel_name=_FC).shape == R.FANTASY, q
        # Outside that channel the same words stay generic.
        assert R.classify(q, fantasy_enabled=True, channel_name=_SC).shape != R.FANTASY, q
    # An unambiguous phrase is a league question in any channel.
    assert R.classify("thoughts on my flex spot", fantasy_enabled=True,
                      channel_name=_SC).shape == R.FANTASY


def test_football_channel_does_not_swallow_stronger_shapes_or_banter():
    strong = {"whats nvda at": R.PRICE, "when is CPI": R.ECON_CALENDAR,
              "who reports today": R.EARNINGS_SLATE, "abes trade log": R.MEMBER_LEDGER}
    for q, shape in strong.items():
        assert R.classify(q, fantasy_enabled=True, channel_name=_FC).shape == shape, q
    for q in ("you good?", "lol what", "yo"):
        assert R.classify(q, fantasy_enabled=True, channel_name=_FC).shape == R.UNKNOWN, q


def test_channel_fallback_does_not_prefetch_a_guessed_topic():
    # A player-news ask has no league topic; injecting standings would
    # label an irrelevant payload authoritative.
    for q in ("any injury news on cmc", "is bijan a good start", "stream a defense this week"):
        r = R.classify(q, fantasy_enabled=True, channel_name=_FC)
        assert r.shape == R.FANTASY and r.prefetch == [], (q, r.prefetch)
    # One that does name a topic still prefetches it.
    r = R.classify("whos winning this week", fantasy_enabled=True, channel_name=_FC)
    assert r.prefetch == [(R.T_FANTASY, {"topic": "matchups"})], r.prefetch


def test_fantasy_shape_keeps_google_and_chat_search():
    r = R.classify("who won the draft", fantasy_enabled=True)
    assert r.google_allowed(), "player news and injuries need the web"
    assert R.T_CHAT in r.allowed_tools(), "'what did BK say about the draft' needs chat"
    assert R.T_PRICE not in r.allowed_tools()


# 2026-09-03, owner named the six shapes the room will actually ask.
def test_the_six_common_league_questions():
    from report.sleeper_data import manager_for_discord_id
    bk = manager_for_discord_id(423994649317736448)
    assert bk == "BK", bk

    def route(q):
        return R.classify(q, fantasy_enabled=True, channel_name=_FC, asker_manager=bk)

    expected = {
        # who's gonna win the match / league
        "who's gonna win the matchup": {"topic": "matchups"},
        "whos gonna win the league": {"topic": "standings"},
        # how's my matchup / outlook
        "hows my matchup": {"topic": "matchups"},
        "hows my outlook": {"topic": "projections", "member": "BK"},
        # compare two teams or players
        "compare bk and declan teams": {"topic": "projections"},
        "is puka better than nabers": {"topic": "projections"},
        # rank a position
        "rank the qbs": {"topic": "projections"},
        "rank wr this week": {"topic": "projections"},
        # who should I drop for X
        "who should i drop for puka": {"topic": "roster", "member": "BK"},
        # should I pick up player X
        "should i pick up puka": {"topic": "trending"},
    }
    for q, args in expected.items():
        r = route(q)
        assert r.shape == R.FANTASY, q
        assert r.prefetch == [(R.T_FANTASY, args)], (q, r.prefetch)


def test_first_person_lookups_need_a_known_manager():
    # A non-manager asking "my roster" gets no prefetch rather than a
    # "could not match a manager" payload labelled authoritative.
    r = R.classify("whats on my roster", fantasy_enabled=True, channel_name=_FC, asker_manager="")
    assert r.shape == R.FANTASY and r.prefetch == []
    r = R.classify("whats on my roster", fantasy_enabled=True, channel_name=_FC, asker_manager="BK")
    assert r.prefetch == [(R.T_FANTASY, {"topic": "roster", "member": "BK"})]
    # Someone else's roster is never aimed at the asker.
    r = R.classify("whats on declans roster", fantasy_enabled=True, channel_name=_FC, asker_manager="BK")
    assert r.prefetch == [], r.prefetch


def test_every_manager_maps_to_a_room_name():
    from report.sleeper_data import DISCORD_TO_MANAGER, SLEEPER_TO_DISCORD, manager_for_discord_id
    assert len(DISCORD_TO_MANAGER) >= len(SLEEPER_TO_DISCORD)
    for aid, _u, room in SLEEPER_TO_DISCORD.values():
        assert manager_for_discord_id(aid) == room, aid
    assert manager_for_discord_id(None) == "" and manager_for_discord_id("nope") == ""


# 2026-09-03 incident: phase 2 built its "missing executor" list with
# set(route.prefetch), and a prefetch entry is (tool, dict), so every
# routed question raised TypeError and answered "Something broke on my
# end" for a day. This runs the real splitter over every shape.
def test_prefetch_plan_never_hashes_the_args_dict():
    from discord_bot.bot import _ask_prefetch_plan
    execs = {R.T_SLATE: 1, R.T_EDATE: 1, R.T_PRICE: 1, R.T_CHAIN: 1,
             R.T_ECON: 1, R.T_HISTORY: 1, R.T_FANTASY: 1}
    questions = [
        "who reports today", "when does NVDA report", "what's TSLA at",
        "NVDA options chain", "when is CPI", "how has NVDA done since january",
        "why is nvda down, odds it beats", "what does CLS do", "who won the draft",
        "who should i start this week", "whats on my roster", "you good?",
    ]
    for q in questions:
        route = R.classify(q, fantasy_enabled=True, channel_name=_FC, asker_manager="BK")
        plan, missing = _ask_prefetch_plan(route, execs)
        assert missing == [], (q, missing)
        assert plan == list(route.prefetch), (q, plan)
        for _tool, args in plan:
            assert isinstance(args, dict), (q, args)


def test_prefetch_plan_reports_a_tool_with_no_executor():
    from discord_bot.bot import _ask_prefetch_plan
    route = R.classify("what's TSLA at")
    assert route.prefetch, "fixture needs a prefetch"
    plan, missing = _ask_prefetch_plan(route, {})
    assert plan == [] and missing == [R.T_PRICE], (plan, missing)


# 2026-09-03 ask-log review: figures answered from memory with no tool
# and no search. These shapes now route to the web with FACT register.
def test_sourced_figure_questions_route_to_the_web():
    for q in ("whats the probability according to kalshi or polymarket on fed raising rates",
              "how much total market cap does 1$ of NVDA stock price moving represent",
              "what's NVDA shares outstanding", "why didnt you tell us there was a cybercab event today"):
        r = R.classify(q)
        assert r.shape == R.NEWS_EVENT and r.google_allowed() and r.is_factual, (q, r)
    # "float" alone is a verb in this room.
    assert R.classify("float the idea to abe").shape == R.UNKNOWN


def test_implied_move_routes_to_the_chain_unless_past_tense():
    r = R.classify("implied move on lulu earnings")
    assert r.shape == R.OPTIONS_CHAIN and r.prefetch == [(R.T_CHAIN, {"symbol": "LULU"})], r
    assert R.classify("expected move on nvda").shape == R.OPTIONS_CHAIN
    # After the print the chain prices the next expiry; the answer lives
    # in chat or on the web, so the shape must keep those tools.
    r = R.classify("what was the implied move for LULU earnings?")
    assert r.shape == R.UNKNOWN and r.google_allowed() and R.T_CHAT in r.allowed_tools(), r

if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
