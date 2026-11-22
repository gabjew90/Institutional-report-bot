"""A model's reading of a question the regex router could not place
(discord_bot/ask_reader.py, 2026-11-22). The reading is used only where the
labelled comparison supports it, its tickers are checked in code, and the
data plan is the one the router builds for that kind."""
from discord_bot import ask_reader as RD
from discord_bot import ask_router as R


def _read(kind, tickers=()):
    return {"kind": kind, "tickers": list(tickers), "asked": ""}


def _apply(question, kind, tickers=(), **kw):
    r0 = R.classify(question)
    return r0, R.apply_reading(r0, question, _read(kind, tickers), **kw)


# ------------------------------------------------------------- the call's reply

def test_the_reply_is_read_strictly():
    assert RD.parse('{"shape": "news_event", "tickers": ["$aaoi", "CB RS"], "asked": "why it fell"}') == {
        "kind": "news_event", "tickers": ["AAOI", "CBRS"], "asked": "why it fell"}
    assert RD.parse('{"shape": "made_up", "tickers": []}') is None
    assert RD.parse("not json") is None and RD.parse("") is None


def test_a_question_or_request_against_a_statement():
    for q in ("why is aaoi dead", "any news on elmt?", "give me a percentage", "What time is the call",
              "show me bitcoin 5 years ago", "tldr; spx and qqq up or down"):
        assert RD.asks(q), q
    for q in ("Hard name to play", "Followed. 3.4 fill", "Saylor already stepped in, the stock is +14%",
              "180 by end of month"):
        assert not RD.asks(q), q


# ------------------------------------------------------ when a reading is used

def test_only_the_catch_all_and_the_fallback_view_take_a_reading():
    assert R.reading_wanted(R.classify("what happened on October 7th"))            # nothing recognised
    assert R.reading_wanted(R.classify("why is AAOI dead"))                        # a ticker and no pattern
    for q in ("when does nvda report", "what do you think of MU", "who reports earnings today",
              "why is NVDA down", "spy ytd"):
        assert not R.reading_wanted(R.classify(q)), q                              # the router placed it
    # a reaction the router already settled keeps its route
    r = R.Route(shape=R.UNKNOWN, reason="reaction to a message about a ticker: price only",
                prefetch=[(R.T_PRICE, {"symbols": ["ASTS"]})])
    assert not R.reading_wanted(r)
    assert not R.reading_wanted(R.Route(shape=R.UNKNOWN, follow_up="reaction"))


def test_an_unplaced_question_takes_the_models_kind():
    r0, r1 = _apply("what happened on October 7th", "news_event")
    assert r0.shape == R.UNKNOWN and r1.shape == R.NEWS_EVENT and r1.reason.startswith("reader: ")
    r0, r1 = _apply("does Levi also have earnings call", "earnings_date", ["LEVI"])
    assert r1.shape == R.EARNINGS_DATE and r1.tickers == ["LEVI"]
    assert [t for t, _ in r1.prefetch][:2] == [R.T_EDATE, R.T_PRICE]
    r0, r1 = _apply("show me bitcoin 5 years ago", "price_history", ["BTC"])
    assert r1.shape == R.PRICE_HISTORY and r1.tickers == ["BTC"]
    r0, r1 = _apply("what G and wock were talking about", "chat_history")
    assert r1.shape == R.CHAT_HISTORY and r1.prefetch == []


def test_a_statement_or_reaction_stays_with_the_catch_all():
    """The model's one error: it names a kind for a remark. There it was
    right as often as wrong, so its reading is not used."""
    for q, kind, tickers in (("Hard name to play", "ticker_opinion", []),
                             ("Followed. 3.4 fill", "member_ledger", []),
                             ("The next print will make this look like pennies", "news_event", [])):
        r0, r1 = _apply(q, kind, tickers)
        assert r1 is r0, q


def test_kinds_the_model_was_not_reliably_right_on_are_not_used():
    for q, kind in (("who has the better run game, bama or Georgia", "fantasy"),
                    ("how do we bring down the 10Y yield", "econ_calendar"),
                    ("what should we do here?", "ticker_opinion"),
                    ("how long is this route on average in the winter", "historical_stat")):
        r0, r1 = _apply(q, kind)
        assert r1 is r0, q


def test_the_fallback_view_gives_way_to_the_models_kind():
    """A ticker named and no pattern matched: the router called it a view
    on the stock. When the model named another kind it was right 41 to 1."""
    r0, r1 = _apply("why is AAOI dead", "news_event", ["AAOI"])
    assert r0.shape == R.TICKER_OPINION and r1.shape == R.NEWS_EVENT and r1.tickers == ["AAOI"]
    assert R.T_NEWS in [t for t, _ in r1.prefetch]
    r0, r1 = _apply("wtf is POWL stock", "company_profile", ["POWL"])
    assert r1.shape == R.COMPANY_PROFILE
    # a view the model also calls a view keeps the router's route
    r0, r1 = _apply("AAOI looks ready to run here", "ticker_opinion", ["AAOI"])
    assert r1 is r0


def test_a_ticker_the_router_misread_is_not_carried_over():
    # TLDR read as a symbol: the model names none, so none is fetched
    r0, r1 = _apply("give me a TLDR; why is tech getting hammered today", "news_event", [])
    assert r0.tickers == ["TLDR"] and r1.shape == R.NEWS_EVENT and "TLDR" not in r1.tickers
    assert all(a.get("symbol") != "TLDR" for _, a in r1.prefetch)


def test_not_a_question_about_the_stock_gets_no_rundown():
    r0, r1 = _apply("how much is one $BA 737max jet compared to the entire gdp of Ethiopia", "other", [])
    assert r0.shape == R.TICKER_OPINION and r1.shape == R.UNKNOWN and r1.prefetch == []
    r0, r1 = _apply("do any serious business people actually use mac. AAPL doesnt count", "banter", ["AAPL"])
    assert r1.shape == R.UNKNOWN and r1.prefetch == [(R.T_PRICE, {"symbols": ["AAPL"]})]


# ------------------------------------------------------- the reading is checked

def test_a_ticker_must_be_listed_and_named_in_the_message():
    q = "[MESSAGE BEING REPLIED TO]\nNVDA reports Tuesday after the close\n\nWhat time is the call"
    assert R.reading_tickers(["NVDA"], q) == ["NVDA"]                     # named in the message above
    assert R.reading_tickers(["CFTC"], "So no") == []                     # not listed
    assert R.reading_tickers(["AMD"], "So no") == []                      # listed, and nobody named it
    assert R.reading_tickers(["CB RS"], "when does cbrs lockup end") == ["CBRS"]
    assert R.reading_tickers(["NVDA"], "is nvidia reporting this week") == ["NVDA"]   # by name
    assert R.reading_tickers(["SPX"], "why is the s&p down") == ["SPX"]
    # a symbol that is also a word the room types must be written as a symbol
    assert R.reading_tickers(["HAS"], "who has earnings") == []
    assert R.reading_tickers(["HAS"], "is $HAS a buy") == ["HAS"]


def test_the_price_kind_needs_the_routers_own_price_words():
    """The price kind posts the quote and company figures as the answer.
    The model also calls a revenue history and a holdings question a price."""
    for q, tickers in (("What was their revenue each year since 2021", ["ORCL"]),
                       ("how much avgo does pelosi own", ["AVGO"])):
        r0, r1 = _apply(q, "price", tickers)
        assert r1 is r0 or r1.shape != R.PRICE, q
    # a change in a figure is not the figure
    r0, r1 = _apply("How much mcap has nvda lost these last week due to affair", "price", ["NVDA"])
    assert r1.shape != R.PRICE


def test_a_members_record_is_about_a_member_the_asker_or_the_room():
    r0, r1 = _apply("show me pelosis portfolio breakdown by %", "member_ledger")
    assert r1 is r0                                                       # no web search on that kind
    r0, r1 = _apply("Where do i stand", "member_ledger")
    assert r1.shape == R.MEMBER_LEDGER
    r0, r1 = _apply("how is terlin doing lately", "member_ledger", names_member=True)
    assert r1.shape == R.MEMBER_LEDGER


def test_no_reading_leaves_the_route_alone():
    r0 = R.classify("what happened on October 7th")
    assert R.apply_reading(r0, "what happened on October 7th", None) is r0
    # and a route the router placed is never touched
    r0 = R.classify("when does nvda report")
    assert R.apply_reading(r0, "when does nvda report", _read("news_event", ["NVDA"])) is r0


def test_the_reader_is_wired_in_behind_its_switch():
    import inspect
    import config
    from discord_bot import bot as B
    assert config.Settings.model_fields["ask_reader_enabled"].default is True
    src = inspect.getsource(B._ask_00_setup_tools_and_context)
    assert "settings.ask_reader_enabled and _ask_router.reading_wanted(_ask_route)" in src
    assert "_ask_router.apply_reading(" in src and "the router's route stands" in src
    # phase 1 does not call the flag classifier a second time
    assert "_ask_route.flags is not None" in inspect.getsource(B._ask_01_build_prompt)
