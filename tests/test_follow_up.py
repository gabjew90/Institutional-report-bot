"""A reply to the bot's own answer: sorted in code, routed from the answer's
record (discord_bot/follow_up.py). The questions are the room's real replies."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from discord_bot import ask_router as R
from discord_bot import follow_up as F


def _reply(own: str, parent: str = "→ **Operating cash flow** sits at **$146.7B**") -> str:
    return (f"[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n\"{parent}\"\n\n"
            f"[asker's message to you]\n{own}")


def _turn(tickers=("AAPL",), asked="what does apple do", shape="ticker_opinion", factual=True) -> dict:
    return {"tickers": list(tickers), "asked": asked, "shape": shape, "factual": factual}


# --- sorting


@pytest.mark.parametrize("own", ["Gracias", "👍", "Ah fuck", "Cheap", "lol", "@abe (abullish_xyz)"])
def test_a_reaction(own):
    assert F.kind(own) == F.REACTION


@pytest.mark.parametrize("own", [
    "Cashflow 12 months basis ?", "Which way we going this time", "Give me a guess on foward pe",
    "How about the lastest report 12 month basis", "Do I close ?",
])
def test_a_question(own):
    assert F.kind(own) == F.QUESTION


@pytest.mark.parametrize("own", [
    "So nbis is so shit you can't even come up with a number",
    "Why would I care about before the bell idiot", "Useless robot it's already live",
    "Im not looking for excuses. Results only", "But you just said it's closed.",
    "I'm asking you a question not telling you to define my actions", "Be more specific",
])
def test_a_complaint(own):
    assert F.kind(own) == F.COMPLAINT


def test_an_insult_shaped_like_a_question_asks_nothing():
    assert F.kind("why are you so dumb") == F.REACTION
    # unless it also names something the data answers
    assert F.kind("why is the float so low idiot", data_words=True) == F.QUESTION


# --- what an answer was about


def test_the_subject_is_what_a_tool_returned_not_what_the_router_guessed():
    trace = [{"tool": "lookup_market_price", "status": "ok", "args": {"symbols": "['AAPL']"}},
             {"tool": "lookup_ticker_snapshot", "status": "ok", "args": {"symbol": "AAPL"}},
             {"tool": "ticker_news", "status": "ok", "args": {"symbol": "TOO"}},
             {"tool": "lookup_research", "status": "error", "args": {"symbol": "ORDER"}}]
    turn = F.record("what is apple", {"route_shape": "price", "kind": "FACT"}, trace)
    assert turn == {"shape": "price", "tickers": ["AAPL"], "asked": "what is apple", "factual": True}


def test_an_index_is_not_a_subject():
    trace = [{"tool": "lookup_market_price", "status": "ok", "args": {"symbols": "['^GSPC']"}}]
    assert F.record("spx", {"route_shape": "price", "kind": "FACT"}, trace)["tickers"] == []


def test_a_stock_view_is_a_data_answer_though_it_runs_as_banter():
    assert F.record("mu?", {"route_shape": "ticker_opinion", "kind": "BANTER"}, [])["factual"]
    assert not F.record("lol", {"route_shape": "unknown", "kind": "BANTER"}, [])["factual"]


def test_a_complaints_record_keeps_the_question_complained_about():
    meta = {"route_shape": "price", "kind": "FACT", "route_reask": "whats nbis forward pe"}
    assert F.record("answer the question", meta, [])["asked"] == "whats nbis forward pe"


def test_two_names_or_a_banter_answer_are_no_subject():
    assert F.subject(_turn(("ARM", "GS"))) == ""
    assert F.subject(_turn(factual=False)) == ""
    assert F.subject(None) == ""
    assert F.subject(_turn()) == "AAPL"


# --- routing


def test_a_field_question_under_a_stock_answer_gets_that_stock():
    r = R.classify(_reply("Cashflow 12 months basis ?"), turn=_turn())
    assert r.shape == R.PRICE and r.tickers == ["AAPL"] and r.follow_symbol == "AAPL"
    assert (R.T_SNAPSHOT, {"symbol": "AAPL"}) in r.prefetch


@pytest.mark.parametrize("own", ["What's the earnings numbers", "How about the lastest report 12 month basis",
                                 "So they'll likely do a 10:1 reverse split to maintain compliance with NYSE listing?"])
def test_any_other_question_gets_the_data_and_no_outline(own):
    r = R.classify(_reply(own), turn=_turn(("APLD",)))
    assert r.shape == R.FOLLOW_UP and r.is_factual and r.needs_web and r.google_allowed()
    assert (R.T_SNAPSHOT, {"symbol": "APLD"}) in r.prefetch
    assert R.T_CHAT not in r.allowed_tools()
    from discord_bot.stock_outline import _STOCK_SHAPES
    assert r.shape not in _STOCK_SHAPES


def test_the_record_finds_a_subject_the_answer_text_does_not_show():
    parent = "→ **Apple AAPL** reports earnings on **Monday, November 2, 2026** after the market close"
    assert R.reply_parent_tickers(_reply("x", parent)) == []
    assert R.classify(_reply("Cashflow 12 months basis ?", parent), turn=_turn()).tickers == ["AAPL"]


@pytest.mark.parametrize("own,shape,ticker", [
    ("What about vst", None, "VST"),
    ("how much cash does $NVDA have", None, "NVDA"),
])
def test_a_stock_the_reply_names_wins(own, shape, ticker):
    assert R.classify(_reply(own), turn=_turn()).tickers[0] == ticker


@pytest.mark.parametrize("own", [
    "whats bitcoin at", "is the market open?", "who won the game last night?",
    "what's your favorite color?", "what's up", "what time is it",
])
def test_a_reply_about_something_else_does_not_get_the_stock(own):
    r = R.classify(_reply(own), turn=_turn())
    assert r.shape != R.FOLLOW_UP and not r.follow_symbol and "AAPL" not in r.tickers


@pytest.mark.parametrize("own", ["is that good", "how is it doing today", "ok but what's the float?", "Do I close ?"])
def test_a_reply_carrying_on_gets_the_stock(own):
    r = R.classify(_reply(own), turn=_turn())
    assert r.tickers[0] == "AAPL" and r.follow_symbol == "AAPL"


def test_market_cap_is_not_another_market():
    assert R.classify(_reply("How much the market cap"), turn=_turn()).tickers[0] == "AAPL"


@pytest.mark.parametrize("own", ["no", "nah", "Nah it's fine, thanks", "nope all good"])
def test_a_soft_no_is_not_a_complaint(own):
    assert F.kind(own) != F.COMPLAINT
    r = R.classify(_reply(own), turn=_turn())
    assert not r.force_fact and not r.reask


@pytest.mark.parametrize("own", ["Nah like if I just decided to go take it next month",
                                 "no that's wrong", "nah you forgot the revenue"])
def test_a_no_that_says_something_is_a_complaint(own):
    assert F.kind(own) == F.COMPLAINT


def test_a_complaint_past_an_english_word_guess_still_asks_again():
    r = R.classify(_reply("the float is wrong"), turn=_turn(("NBIS",), asked="whats the float"))
    assert r.force_fact


def test_the_complaint_note_without_a_question_names_none():
    assert "that question" not in F.complaint_note("")
    assert "that question" in F.complaint_note("whats the float")


def test_a_shape_the_reply_earned_stands():
    r = R.classify(_reply("when is the next fed meeting"), turn=_turn())
    assert r.shape == R.ECON_CALENDAR and not r.follow_symbol


def test_a_reaction_gets_the_price_and_keeps_its_register():
    r = R.classify(_reply("Ah fuck"), turn=_turn())
    assert r.shape == R.UNKNOWN and not r.is_factual
    assert r.prefetch == [(R.T_PRICE, {"symbols": ["AAPL"]})]


def test_a_bare_tag_is_left_alone():
    r = R.classify(_reply("@abe (abullish_xyz)"), turn=_turn())
    assert not r.follow_up and not r.prefetch


def test_a_question_about_a_member_is_not_about_the_stock():
    r = R.classify(_reply("how is he doing?"), turn=_turn(), names_member=True)
    assert not r.follow_symbol


def test_a_complaint_asks_the_question_again_with_the_stock_and_the_facts_only():
    r = R.classify(_reply("So nbis is so shit you can't even come up with a number"),
                   turn=_turn(("NBIS",), asked="Give me a guess on foward pe", shape="price"))
    assert r.shape == R.PRICE and r.tickers == ["NBIS"]
    assert r.reask == "Give me a guess on foward pe" and r.force_fact and r.is_factual
    assert r.follow_up == F.COMPLAINT


def test_a_complaint_under_a_slate_gets_the_slate():
    r = R.classify(_reply("Why would I care about before the bell idiot"),
                   turn=_turn((), asked="who reports today", shape="earnings_slate"))
    assert r.shape == R.EARNINGS_SLATE and r.reask == "who reports today" and r.force_fact


def test_a_complaint_with_no_shape_still_gets_the_facts_register():
    r = R.classify(_reply("Be more specific"), turn=_turn((), asked="when does the AI bubble pop", shape="unknown"))
    assert r.shape == R.UNKNOWN and r.force_fact and r.is_factual and r.reask


def test_a_complaint_naming_a_stock_routes_on_that_stock():
    r = R.classify(_reply("You forgot spcx"), turn=_turn((), asked="who reports today", shape="earnings_slate"))
    assert r.tickers == ["SPCX"] and not r.reask and r.force_fact


def test_a_complaint_under_a_league_answer_stays_in_the_league():
    r = R.classify(_reply("do analysis again"), turn=_turn((), asked="how's my team", shape="fantasy"),
                   fantasy_enabled=True, channel_name="fantasy-football-yapping")
    assert not r.reask and r.force_fact


def test_a_complaint_under_a_roast_is_not_treated_as_one():
    r = R.classify(_reply("No idiot"), turn=_turn((), asked="define wocking", shape="unknown", factual=False))
    assert not r.force_fact and not r.follow_up


def test_no_record_routes_as_before():
    q = _reply("Short squeeze potential or what", "→ **$BBAI $2.40** up on federal contracts")
    assert R.classify(q).shape == R.TICKER_OPINION
    assert R.classify(q, turn=None).tickers == ["BBAI"]


def test_a_cash_flow_question_is_one_field_not_the_rundown():
    r = R.classify("AAPL free cash flow and operating cash flow, trailing 12 months")
    assert r.shape == R.PRICE and r.tickers == ["AAPL"]


# --- the notes the model sees


def test_the_notes_name_what_to_do():
    q = F.question_note("AAPL", "what does apple do")
    assert "$AAPL" in q and "what does apple do" in q
    c = F.complaint_note("whats nbis forward pe")
    assert "whats nbis forward pe" in c and "fair point" in c
    assert "—" not in q + c


# --- the stored record


def test_the_record_is_found_from_the_reply_parent():
    import db
    conn = db.get_connection()
    chan, other = 987650001, 987650002
    answer = "→ **Operating cash flow** sits at **$146.72B**\n\n→ **Free cash flow** prints at **$136.68B**"
    older = "→ an older answer with no record of its own"
    try:
        db.record_ask_bot_answer(1, chan, "q", answer, turn=_turn())
        db.record_ask_bot_answer(1, chan, "q", older, turn=None)
        assert db.ask_turn_of_answer(chan, answer) == _turn()
        assert db.ask_turn_of_answer(chan, "→ **Operating cash flow** sits at") == _turn()
        assert db.ask_turn_of_answer(other, answer) is None
        assert db.ask_turn_of_answer(chan, older) is None
        assert db.ask_turn_of_answer(chan, "short") is None
    finally:
        conn.execute("DELETE FROM ask_bot_answers WHERE channel_id IN (?, ?, 7)", (chan, other))
        conn.commit()
