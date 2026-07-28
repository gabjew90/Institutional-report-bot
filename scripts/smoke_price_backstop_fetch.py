"""Smoke test for the price-backstop ticker extraction.

Context (2026-07-27): two banter-routed answers asserted ORCL prices
two minutes apart — "$118, nowhere near a 4% rip" (wrong, accused the
member of making up numbers) then "$119.94, you're on the level". The
ZERO UNFORCED PRICE ASSERTIONS prompt rule was violated because banter
passes skip tools, and the grounding backstop's forced retry strips
the function tools — the ladder literally could not fetch the price.
The fix: when the backstop trips, extract tickers from the offending
answer in code, call the market-price executor directly, and inject
the live numbers into the retry.

Covers `_answer_price_tickers`:
  - bare ticker near a price assertion extracted (the ORCL case)
  - cashtag ticker extracted
  - uppercase non-tickers (CEO, AI, ET) never extracted
  - sentences with no price shape contribute nothing
  - cap at 4 symbols
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_bare_ticker_near_price():
    from discord_bot.bot import _answer_price_tickers
    ans = ("ORCL is trading around $118, nowhere near a 4% rip. "
           "You're either looking at the wrong ticker or just making "
           "up numbers to cope with those $125 calls.")
    assert _answer_price_tickers(ans) == ["ORCL"], _answer_price_tickers(ans)
    _ok("bare ticker near price assertion extracted (ORCL case)")


def test_cashtag_ticker():
    from discord_bot.bot import _answer_price_tickers
    ans = "→ **$NVDA $878** as of 14:32 ET — up **+1.4%** on session"
    assert _answer_price_tickers(ans) == ["NVDA"], _answer_price_tickers(ans)
    _ok("cashtag ticker extracted, ET not mistaken for a ticker")


def test_uppercase_words_not_tickers():
    from discord_bot.bot import _answer_price_tickers
    ans = ("The CEO said the company will spend $5 billion on AI "
           "data centers, roughly 12% of revenue.")
    assert _answer_price_tickers(ans) == [], _answer_price_tickers(ans)
    _ok("CEO / AI near dollar figures not extracted")


def test_no_price_shape_no_extraction():
    from discord_bot.bot import _answer_price_tickers
    ans = "TSLA has a shareholder meeting coming and the room is split."
    assert _answer_price_tickers(ans) == [], _answer_price_tickers(ans)
    _ok("ticker without any price assertion -> nothing to fetch")


def test_symbol_cap():
    from discord_bot.bot import _answer_price_tickers
    ans = ("AAPL at $210, MSFT at $500, GOOG at $190, AMZN at $220, "
           "META at $700, NVDA at $878.")
    out = _answer_price_tickers(ans)
    assert len(out) == 4, out
    assert out == ["AAPL", "MSFT", "GOOG", "AMZN"], out
    _ok("extraction capped at 4 symbols, order preserved")


if __name__ == "__main__":
    print("=== price backstop fetch smoke ===")
    test_bare_ticker_near_price()
    test_cashtag_ticker()
    test_uppercase_words_not_tickers()
    test_no_price_shape_no_extraction()
    test_symbol_cap()
    print("\nALL PRICE BACKSTOP FETCH SMOKE TESTS PASS")
