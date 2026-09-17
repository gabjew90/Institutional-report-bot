"""The price backstop reads tickers through the router's extractor
(2026-09-16). Its own copy fetched 'ATM' as a ticker from "the ATM
straddle" on a COST implied-move answer (ask-log 2026-09-14)."""
from discord_bot import bot as B


def test_option_jargon_is_not_a_ticker():
    a = ("$COST implied move is **4.2%** off the ATM straddle at **$912**. "
         "OTM puts are bid.")
    assert B._answer_price_tickers(a) == ["COST"]


def test_cashtag_and_bare_tickers_still_come_through():
    a = "ORCL is trading near $245 after the print. NVDA at $180 holds."
    assert B._answer_price_tickers(a) == ["ORCL", "NVDA"]


def test_no_price_assertion_means_no_fetch():
    assert B._answer_price_tickers("AAPL makes phones and the room likes it.") == []


def test_one_extractor_for_the_ask_path():
    import inspect
    src = inspect.getsource(B._answer_price_tickers)
    assert "extract_tickers" in src and "re.finditer" not in src
