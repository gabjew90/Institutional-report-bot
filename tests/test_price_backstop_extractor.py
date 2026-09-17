"""The price backstop reads tickers through the router's extractor
(2026-09-16). Its own copy fetched 'ATM' as a ticker from "the ATM
straddle" on a COST implied-move answer (ask-log 2026-09-14). Review
2026-09-17: the router's cashtag-first rule dropped a bare ticker in
the same sentence, its stopword set lacked 31 acronyms the old copy
had (USD is a real ETF), and index names went to Finnhub unmapped."""
from discord_bot import bot as B
from discord_bot import ask_router as R


def test_option_jargon_is_not_a_ticker():
    a = ("$COST implied move is **4.2%** off the ATM straddle at **$912**. "
         "OTM puts are bid.")
    assert B._answer_price_tickers(a) == ["COST"]


def test_cashtag_and_bare_tickers_still_come_through():
    a = "ORCL is trading near $245 after the print. NVDA at $180 holds."
    assert B._answer_price_tickers(a) == ["ORCL", "NVDA"]


def test_a_cashtag_and_a_bare_ticker_in_one_sentence_are_both_fetched():
    a = "$SPX is at 6500 while NVDA sits at $180 after the print."
    assert B._answer_price_tickers(a) == ["^GSPC", "NVDA"]


def test_currency_codes_agencies_and_shouted_words_are_not_tickers():
    assert B._answer_price_tickers("EURUSD is trading at 1.08 USD after the ECB, up 0.5%.") == ["ECB"]
    assert B._answer_price_tickers("FDA approval sent the stock up 12% to $45.") == []
    assert B._answer_price_tickers("OPEC cut 1m bpd and Brent is trading near $85.") == []
    assert B._answer_price_tickers("AWS grew 20% YOY to $30B, NOT ALL of it cloud.") == []


def test_index_names_go_to_the_tool_in_yahoo_form():
    assert B._answer_price_tickers("VIX is trading near 18 into the print.") == ["^VIX"]


def test_no_price_assertion_means_no_fetch():
    assert B._answer_price_tickers("AAPL makes phones and the room likes it.") == []


def test_one_stopword_set_for_the_ask_path():
    import inspect
    src = inspect.getsource(B._answer_price_tickers)
    assert "extract_tickers" in src and "re.finditer" not in src
    assert B._TICKER_FALSE_POSITIVES == frozenset(R._NOT_TICKERS - R._KEEP)
    assert B._clapback_claim_tokens("the ATM straddle on $NVDA")[:1] == ["NVDA"]
    assert "ATM" not in B._clapback_claim_tokens("the ATM straddle on $NVDA")
