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
    ("when does NVDA report", R.EARNINGS_DATE, [R.T_EDATE, R.T_SNAPSHOT, R.T_PRIMER]),
    ("did PLTR beat last quarter", R.EARNINGS_DATE, [R.T_EDATE, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]),
    ("what's expected for AVGO earnings", R.EARNINGS_DATE, [R.T_EDATE, R.T_SNAPSHOT, R.T_PRIMER]),
    # news / odds
    ("odds HPE beats earnings", R.NEWS_EVENT, [R.T_PRICE, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_EDATE]),
    ("why is mrvl down off avgo earnings", R.NEWS_EVENT, [R.T_PRICE]),
    ("explain pltr death", R.NEWS_EVENT, [R.T_PRICE, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]),
    # price
    ("what's TSLA at", R.PRICE, [R.T_PRICE, R.T_SNAPSHOT, R.T_PRIMER]),
    ("how's BTC doing", R.PRICE, [R.T_PRICE]),
    ("is SPY green today", R.PRICE, [R.T_PRICE]),
    # options
    ("SPY 0dte put/call ratio", R.OPTIONS_CHAIN, [R.T_CHAIN]),
    ("what's the OI on NVDA 200c", R.OPTIONS_CHAIN, [R.T_CHAIN, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]),
    # macro
    ("when is CPI", R.ECON_CALENDAR, [R.T_ECON]),
    ("what did NFP come in at", R.ECON_CALENDAR, [R.T_ECON]),
    ("is the fed cutting this meeting", R.ECON_CALENDAR, [R.T_ECON]),
    # history
    ("how has NVDA done since january", R.PRICE_HISTORY, [R.T_HISTORY, R.T_SNAPSHOT, R.T_PRIMER]),
    ("PLTR ytd", R.PRICE_HISTORY, [R.T_HISTORY, R.T_SNAPSHOT, R.T_PRIMER]),
    # company profile
    ("what does CLS do", R.COMPANY_PROFILE, [R.T_PRICE, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]),
    ("what does SPSC do?", R.COMPANY_PROFILE, [R.T_PRICE, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]),
    ("tell me about GOLD , gold.com", R.COMPANY_PROFILE, [R.T_PRICE, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER]),
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
    # a view on one stock: the bank research is the prefetched primary
    # source (2026-09-30, the MU question had been answered from the room)
    ("thoughts on NVDA here", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("what do you think of MU upcoming earnings", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE, R.T_EDATE]),
    ("how does AMD look into the print", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE, R.T_EDATE]),
    ("bullish on $CRWV?", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("should i buy PLTR here", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("what do you think of mu", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("what your thoughts on MU", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("MU thoughts?", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("MU thoughts??", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("mu thoughts", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("$MU bull or bear?", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    # a trailing "thoughts?" is tied to the ticker before it
    ("market thoughts?", R.UNKNOWN, []),
    ("quick thoughts?", R.UNKNOWN, []),
    ("bear thoughts", R.UNKNOWN, []),
    ("whats MU at, thoughts?", R.PRICE, [R.T_PRICE, R.T_SNAPSHOT, R.T_PRIMER]),
    ("what do you think of gold", R.UNKNOWN, []),      # a word, not Barrick
    # 2026-09-30: an options question on a stock is a view with a chain attached
    ("should i slam calls on ACN earnings", R.OPTIONS_CHAIN,
     [R.T_CHAIN, R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_EDATE]),
    ("what's the OI on SPY 600c", R.OPTIONS_CHAIN, [R.T_CHAIN]),     # a fund: no snapshot
    # a bare ticker is a view question; a word or a member is not
    ("MU?", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("$aeva", R.TICKER_OPINION, [R.T_RESEARCH, R.T_NEWS, R.T_SNAPSHOT, R.T_PRIMER, R.T_PRICE]),
    ("BK?", R.UNKNOWN, []),
    ("huh?", R.UNKNOWN, []),
    ("SPY?", R.UNKNOWN, []),
    # not opinions: a price read, a profile, a room read
    ("how is MU looking", R.PRICE, [R.T_PRICE, R.T_SNAPSHOT, R.T_PRIMER]),
    ("what do you think of the market today", R.UNKNOWN, []),
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
    # 2026-09-30: the room's chat is a room tool, never a research source
    view = R.classify("what do you think of MU upcoming earnings")
    assert R.T_CHAT not in view.allowed_tools() and R.T_RESEARCH in view.allowed_tools()
    assert view.google_allowed() and view.needs_web and not view.is_factual
    unknown = R.classify("you good?")
    assert R.T_CHAT not in unknown.allowed_tools(), "the catch-all no longer offers chat search"
    assert R.T_CHAT in R.classify("what did BK say about MU yesterday").allowed_tools()
    assert R.T_CHAT in R.classify("show all of Abe's current holdings").allowed_tools()


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
    assert r.shape == R.PRICE and r.prefetch == [(R.T_PRICE, {"symbols": ["TSLA"]}),
                                                  (R.T_SNAPSHOT, {"symbol": "TSLA"}),
                                                  (R.T_PRIMER, {"symbol": "TSLA", "wait": False})], r
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
    used |= {t for t, _ in R.classify("what is the room piled into").prefetch}
    assert used >= {R.T_SLATE, R.T_EDATE, R.T_PRICE, R.T_CHAIN, R.T_ECON, R.T_HISTORY, R.T_FANTASY, R.T_ROOM}
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
              "trending adds", "who is in the league", "whats on my roster",
              # 2026-09-13: went UNKNOWN in the gambling channel and the
              # model called lookup_market_price with message words as tickers
              "MHJ or Sutton in non PPR", "who do i want in half-ppr"):
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

    # A manager gets their whole week first (roster with projections
    # and slots, opponent's lineup, record, standings); topics the
    # situation payload lacks ride alongside.
    sit = (R.T_FANTASY, {"topic": "situation", "member": "BK"})
    expected = {
        "who's gonna win the matchup": [sit],
        "whos gonna win the league": [sit],
        "hows my matchup": [sit],
        "hows my outlook": [sit],
        "compare bk and declan teams": [sit],
        "is puka better than nabers": [sit],
        "rank the qbs": [sit],
        "who should i drop for puka": [sit],
        "should i pick up puka": [sit, (R.T_FANTASY, {"topic": "trending"})],
        "who won the draft": [sit, (R.T_FANTASY, {"topic": "draft"})],
        "who dropped puka": [sit, (R.T_FANTASY, {"topic": "transactions"})],
    }
    for q, pf in expected.items():
        r = route(q)
        assert r.shape == R.FANTASY, q
        assert r.prefetch == pf, (q, r.prefetch)
    # Not a manager: the narrow topic only, as before.
    r = R.classify("who should i start this week", fantasy_enabled=True, channel_name=_FC)
    assert r.prefetch == [(R.T_FANTASY, {"topic": "projections"})], r.prefetch


_SUNDAY_DEFERRAL = (
    "[MESSAGE BEING REPLIED TO — from 2pale — user_id 264777559026171905]\n"
    "\"Any chance my fantasy team is gomna pull out a win?\"\n\n"
    "[BK's message to you]\n"
    "[VERBATIM RECENT MESSAGES — 2Pale (2pale) — for accurate\n"
    "quoting when the question references them; quote LINE FOR LINE]\n"
    "  2026-10-05T01:14 #🏈-fantasy-football-yapping-🏈 — Im cracked\n\n"
    "Weigh in on this.")


def test_last_line_skips_a_quoted_block_after_the_reply_marker():
    assert R._last_line(_SUNDAY_DEFERRAL) == "Weigh in on this."


def test_a_handed_over_question_is_routed_as_its_author_asked_it():
    d = R.deferred_question(_SUNDAY_DEFERRAL, bot_user_id=1422761344322502807)
    assert d == {"question": "Any chance my fantasy team is gomna pull out a win?",
                 "author": "2pale", "user_id": 264777559026171905}
    r = R.classify(d["question"], fantasy_enabled=True, channel_name=_FC,
                   asker_manager="2Pale")
    assert r.shape == R.FANTASY
    assert r.prefetch[0] == (R.T_FANTASY, {"topic": "situation", "member": "2Pale"})
    assert "2pale" in R.deferred_note("2pale")


def test_a_real_question_in_a_reply_is_not_a_hand_over():
    for own in ("how many points did puka score", "thoughts on puka?",
                "weigh in on nvda calls"):
        assert R.deferred_question(_SUNDAY_DEFERRAL.replace("Weigh in on this.", own)) is None, own
    for own in ("thoughts?", "weigh in", "answer him bot", "wdyt"):
        assert R.deferred_question(_SUNDAY_DEFERRAL.replace("Weigh in on this.", own)), own
    q = _SUNDAY_DEFERRAL.replace("Weigh in on this.", "how many points did puka score")
    to_bot = _SUNDAY_DEFERRAL.replace("user_id 264777559026171905", "user_id 1")
    assert R.deferred_question(to_bot, bot_user_id=1) is None


def test_win_chance_questions_in_the_football_channel_get_league_data():
    for q in ("what % chance of winning did Jamal have before this Panthers?",
              "whats my win probability", "odds of winning this week?"):
        r = R.classify(q, fantasy_enabled=True, channel_name=_FC, asker_manager="BK")
        assert r.shape == R.FANTASY, q
        assert (R.T_FANTASY, {"topic": "matchups"}) in r.prefetch, q
    # outside the football channel the same words stay a market question
    r = R.classify("what are the odds of a fed cut in december", fantasy_enabled=True,
                   channel_name="stonks-yapping", asker_manager="BK")
    assert r.shape == R.NEWS_EVENT


def test_first_person_lookups_need_a_known_manager():
    # A non-manager asking "my roster" gets no prefetch rather than a
    # "could not match a manager" payload labelled authoritative.
    r = R.classify("whats on my roster", fantasy_enabled=True, channel_name=_FC, asker_manager="")
    assert r.shape == R.FANTASY and r.prefetch == []
    r = R.classify("whats on my roster", fantasy_enabled=True, channel_name=_FC, asker_manager="BK")
    assert r.prefetch == [(R.T_FANTASY, {"topic": "situation", "member": "BK"})]
    # Someone else's roster: the asker's situation is still the base
    # payload (it carries the standings and the week); the model calls
    # roster for the named manager itself.
    r = R.classify("whats on declans roster", fantasy_enabled=True, channel_name=_FC, asker_manager="BK")
    assert r.prefetch == [(R.T_FANTASY, {"topic": "situation", "member": "BK"})], r.prefetch


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
             R.T_ECON: 1, R.T_HISTORY: 1, R.T_FANTASY: 1, R.T_RESEARCH: 1, R.T_SNAPSHOT: 1,
             R.T_NEWS: 1, R.T_PRIMER: 1}
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
    assert plan == [] and missing == [R.T_PRICE, R.T_SNAPSHOT, R.T_PRIMER], (plan, missing)


def test_every_router_tool_has_a_production_prefetch_executor():
    """A tool the router prefetches but bot.py cannot run is dropped
    silently at runtime; pin that every T_* the router emits is mapped."""
    import inspect
    from discord_bot import bot
    src = inspect.getsource(bot._ask_02_call_model_with_tools)
    for name in ("T_SLATE", "T_EDATE", "T_PRICE", "T_CHAIN", "T_ECON", "T_HISTORY",
                 "T_FANTASY", "T_ROOM", "T_RESEARCH", "T_SNAPSHOT", "T_NEWS", "T_PRIMER"):
        assert f"_ask_router.{name}:" in src, name


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
    assert r.shape == R.OPTIONS_CHAIN and r.prefetch == [
        (R.T_CHAIN, {"symbol": "LULU", "through_earnings": True}), (R.T_RESEARCH, {"symbol": "LULU", "days": 14}),
        (R.T_NEWS, {"symbol": "LULU"}), (R.T_SNAPSHOT, {"symbol": "LULU"}),
        (R.T_PRIMER, {"symbol": "LULU", "wait": True}),
        (R.T_EDATE, {"symbol": "LULU"})], r
    assert R.classify("expected move on nvda").shape == R.OPTIONS_CHAIN
    # After the print the chain prices the next expiry; the answer lives
    # on the web, so the shape must keep Google. Chat search left the
    # catch-all on 2026-09-30 (the room is not a research source); a
    # room-history question still reaches it through the CHAT shape.
    r = R.classify("what was the implied move for LULU earnings?")
    # not the options chain; since 2026-10-08 a ticker with no other shape
    # gets the stock data, which keeps Google
    assert r.shape in (R.UNKNOWN, R.TICKER_OPINION) and r.shape != R.OPTIONS_CHAIN
    assert r.google_allowed() and R.T_CHAT not in r.allowed_tools(), r


# 2026-09-04, owner: "is the bot able to tell when most of the room are
# piled into the same plays?" The ledger could; nothing pointed at it,
# and "what's the room piled into" matched the CHAT shape's "what's the
# room" and searched chat text for ticker mentions instead.
def test_crowding_questions_route_to_the_room_positions_tool():
    for q in ("what is the room piled into", "what are most people in right now",
              "are we all in the same trade", "whats the most crowded position in the room",
              "what is everyone holding", "whos in PLTR", "who is all in nvda"):
        r = R.classify(q, fantasy_enabled=True)
        assert r.shape == R.ROOM_CROWDING, (q, r.shape)
        assert r.prefetch and r.prefetch[0][0] == R.T_ROOM, (q, r.prefetch)
        assert not r.google_allowed(), "the room's book is not on the web"
        assert R.T_ROOM in r.allowed_tools() and R.T_CHAT not in r.allowed_tools()
    # "right now" narrows the window
    assert R.classify("what are most people in right now").prefetch == [(R.T_ROOM, {"days": 3})]
    assert R.classify("what is the room piled into").prefetch == [(R.T_ROOM, {"days": 14})]


def test_crowding_shape_does_not_steal_neighbouring_shapes():
    assert R.classify("whats the room saying about IBIT").shape == R.CHAT_HISTORY
    assert R.classify("what did BK say about MU yesterday").shape == R.CHAT_HISTORY
    assert R.classify("show all of Abe's current holdings").shape == R.MEMBER_LEDGER
    assert R.classify("who is in the league", fantasy_enabled=True).shape == R.FANTASY
    assert R.classify("whats nvda at").shape == R.PRICE
    assert R.classify("are we in a recession").shape != R.ROOM_CROWDING


def test_room_positions_lead_carries_the_entry_bias_caveat():
    txt = R.inject_text(R.T_ROOM, {"status": "ok", "positions": [{"ticker": "PLTR", "members": 9}]})
    assert "entry-biased" in txt and "upper bound" in txt and "PLTR" in txt


def test_bot_reexports_every_tool_builder_and_executor():
    """bot.py imports the tool layer by an explicit name list and then
    CALLS those names in _tools_all and _tool_executors. A builder or
    executor missing from the list is a NameError on the first /ask
    after deploy, which no import-time check catches (2026-09-04: the
    room-positions tool was declared, registered and undocumented in
    the re-export list all at once)."""
    import re
    from discord_bot import ask_tools as T, bot as B
    names = [n for n in dir(T) if re.match(r"_(build_\w+_tool|execute_\w+)$", n)]
    assert names, "no tool-layer names found"
    missing = [n for n in names if not hasattr(B, n)]
    assert missing == [], f"bot.py does not re-export: {missing}"
    src = B._ask_pipeline_source()
    for n in names:
        if n.startswith("_build_") and f"{n}()" in src:
            assert hasattr(B, n), n

def test_an_attached_image_is_the_subject_for_every_prefetch():
    """Owner 2026-09-09: "not all screenshots are lineups, just consider
    screenshots in the context in general." A bet slip, a chart, a fill
    or an article is as much the question as a roster is, and every
    prefetch is injected as authoritative without having seen any of
    them."""
    for tool in (R.T_PRICE, R.T_SLATE, R.T_ROOM, R.T_ECON, R.T_CHAIN,
                 R.T_FANTASY):
        txt = R.inject_text(tool, {"status": "ok"}, has_images=True)
        assert "AN IMAGE IS ATTACHED" in txt, tool
        assert "the image is the subject" in txt, tool
        assert "AN IMAGE IS ATTACHED" not in R.inject_text(tool, {"status": "ok"}), tool


def test_a_fantasy_answer_is_told_where_to_get_numbers():
    """The other half of the same complaint: the football answers were
    adjectives. topic=projections carries pts_ppr for every NFL player,
    so an outside-league screenshot can be rated on real figures."""
    txt = R.inject_text(R.T_FANTASY, {"status": "ok"})
    assert "NUMBERS OR IT IS NOT AN ANSWER" in txt
    assert "topic=projections" in txt
    assert "EVERY NFL player" in txt


def test_an_attached_image_outranks_the_prefetched_sleeper_roster():
    """2026-09-09: three "rate my team" asks with a screenshot of a
    DIFFERENT league. Two came back naming the same three players from
    the asker's Omnibeta Sleeper roster, for two different screenshots,
    and the asker re-asked with "using the screenshot attached" and got
    the same answer. The payload is authoritative for Omnibeta and must
    not be authoritative for the picture."""
    payload = {"status": "ok", "topic": "roster", "starters": ["Mahomes"]}
    with_img = R.inject_text(R.T_FANTASY, payload, has_images=True)
    assert "AN IMAGE IS ATTACHED" in with_img
    assert "different team" in with_img
    no_img = R.inject_text(R.T_FANTASY, payload)
    assert "AN IMAGE IS ATTACHED" not in no_img
    # the league-state framing survives in both
    assert "LEAGUE STATE" in with_img and "LEAGUE STATE" in no_img


def test_only_fantasy_gets_the_omnibeta_clarification():
    """The generic note goes on every tool; only fantasy has a league of
    its own to be confused with."""
    for tool in (R.T_PRICE, R.T_SLATE, R.T_ROOM, R.T_ECON):
        assert "OMNIBETA" not in R.inject_text(
            tool, {"status": "ok"}, has_images=True), tool
    assert "OMNIBETA" in R.inject_text(
        R.T_FANTASY, {"status": "ok"}, has_images=True)


def test_the_caller_passes_the_image_flag_through():
    from discord_bot import bot as B
    src = B._ask_pipeline_source()
    i = src.index("inject_text(")
    assert "has_images=_has_attached_image" in src[i:i + 200], src[i:i + 200]
    # and the flag is derived from the parts the images were put into
    assert "_has_attached_image = any(" in src
    assert "inline_data" in src


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


def test_audit_2026_10_08_fantasy_routing():
    # a sex joke ending "gimme a second chance" got BK's Week 5 matchup
    q = "I’ll clap Brenda’s cheeks in the school bathroom then tell her to gimme a second chance"
    assert R.classify(q, fantasy_enabled=True, channel_name=_FC).shape != R.FANTASY
    # "how am I doing" in the football channel is the asker's team, with a
    # phone's curly apostrophe
    for q in ("how do I know if I’m doing well", "how am I doing", "hows my team doing"):
        r = R.classify(q, fantasy_enabled=True, channel_name=_FC, asker_manager="DeeP FRieD")
        assert r.shape == R.FANTASY, q
        assert (R.T_FANTASY, {"topic": "situation", "member": "DeeP FRieD"}) in r.prefetch, q
    # a real win-chance question still gets league data
    assert R.classify("what % chance of winning did Jamal have", fantasy_enabled=True,
                      channel_name=_FC).shape == R.FANTASY


def test_curly_apostrophes_route_like_straight_ones():
    q = "Abe’s record on slams the last 90 days"
    assert R.classify(q).shape == R.classify(q.replace("’", "'")).shape == R.MEMBER_LEDGER


def _reply(parent, own):
    return (f'[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"{parent}"\n\n'
            f"[Monsoon's message to you]\n{own}")


def test_audit_2026_10_08_stock_questions_get_stock_data():
    # a ticker and a market word, no other shape
    for q, sym in (("why is WDC hammered today", "WDC"), ("is NVDA a buy", "NVDA")):
        r = R.classify(q)
        assert r.shape == R.TICKER_OPINION and r.tickers[0] == sym, q
        assert (R.T_SNAPSHOT, {"symbol": sym}) in r.prefetch
    assert R.classify("what happened to twst").tickers == ["TWST"]
    # a follow-up takes the ticker of the message it replies to
    r = R.classify(_reply("$APLD reports today after the close", "give me the earnings numbers"))
    assert r.tickers == ["APLD"] and (R.T_EDATE, {"symbol": "APLD"}) in r.prefetch
    assert R.classify(_reply("CoreWeave ($CRWV) carries $35B of debt", "how much the cap worths")
                      ).tickers == ["CRWV"]
    # banter stays banter
    for q in ("WTF?", "LMAO", _reply("CoreWeave ($CRWV) carries $35B of debt", "lol")):
        assert R.classify(q).shape == R.UNKNOWN, q


def test_a_sector_question_prices_its_bellwethers():
    r = R.classify("why is memory down today")
    assert r.shape == R.NEWS_EVENT and r.tickers[0] == "MU"
    assert (R.T_PRICE, {"symbols": ["MU", "SNDK", "WDC", "STX"]}) in r.prefetch
    assert (R.T_NEWS, {"symbol": "MU"}) in r.prefetch
    assert R.classify("what is the room positioned in heading into tomorrow").prefetch == [
        (R.T_ROOM, {"days": 3})]


def test_a_chat_count_question_searches_before_the_first_call():
    r = R.classify('how many times has Abe said "slam" in the last 90 days')
    assert r.shape == R.CHAT_HISTORY
    assert r.prefetch == [(R.T_CHAT, {"keyword": "slam", "days": 90})]
    assert R.classify("what did kloh say about gamma").prefetch == []


def test_an_apostrophe_is_not_a_quote():
    assert R._quoted_term("how many times did Abe's crew say 'slam'") == "slam"
    assert R._quoted_term('did he type "send it" today') == "send it"


def test_election_and_correction_questions_are_news_2026_10_10():
    """"How many votes did Hitler Mussolini win by today in Peru" went to
    banter with no search and was answered "zero"; he had won. The asker's
    "Wrong. Hitler Mussolini won." then got a second joke."""
    assert R.classify("How many votes did Hitler Mussolini win by today in Peru").shape == R.NEWS_EVENT
    assert R.classify("who won the election in argentina").shape == R.NEWS_EVENT
    q = ('[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"zero, unless"\n\n'
         "[Sam's message to you]\nWrong. Hitler Mussolini won. You’re a fool.")
    assert R.classify(q).shape == R.NEWS_EVENT
    # a correction about the asker is banter, not news
    assert R.classify("nope you lost").shape != R.NEWS_EVENT


def test_math_with_how_many_times_is_not_chat_history_2026_10_10():
    assert R.classify("How many times do I need to 10x $100 to get to $1b").shape != R.CHAT_HISTORY
    assert R.classify("how many times has kyle said gay this week").shape == R.CHAT_HISTORY
