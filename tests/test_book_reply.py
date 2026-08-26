"""Caller book corrections.

The parser decides whether to write a `close` into the trade log from a
line of casual chat, so its false-positive behaviour matters far more
than its recall. Every NEGATIVE case below is drawn from real messages
posted in the same minute as the incident, several of which name a
ticker that was on the book.
"""
import sys

from analyst_log.book_reply import parse_exit_corrections as P

# The book the bot actually published for BK on 2026-08-26.
BOOK = ["SPXW", "SMCI", "NBIS", "SNDK", "HOOD", "MU", "AVGO"]


# ------------------------------------------------- the incident itself

def test_no_avgo_closes_avgo():
    assert P("No AVGO", BOOK) == ["AVGO"]


def test_no_mu_anymore_closes_mu():
    assert P("No MU anymore", BOOK) == ["MU"]


def test_correction_is_case_insensitive():
    assert P("no avgo", BOOK) == ["AVGO"]


# --------------------------------------- real messages that must NOT fire

def test_confirmation_with_addition_does_not_close():
    """BK, same minute. Names SMCI and SPX, both on the book, and is a
    CONFIRMATION that he holds them plus one more."""
    assert P("^ this plus the SMCI 39c and I added 1 more SPX", BOOK) == []


def test_regret_about_untaken_trade_does_not_close():
    """BK, same minute. 'so close to' + a ticker is not an exit."""
    assert P("I was so close to tailing AAOI dang it", BOOK + ["AAOI"]) == []


def test_bullish_comment_does_not_close():
    assert P("AVGO gnna be the winner", BOOK) == []
    assert P("MU HOD", BOOK) == []
    assert P("And MU 1000", BOOK) == []


def test_chart_command_does_not_close():
    """`fc TICKER` is a chart command, not a position statement."""
    assert P("Fc MU 5", BOOK) == []
    assert P("fc avgo 1h wide", BOOK) == []


def test_still_holding_does_not_close():
    assert P("still in AVGO", BOOK) == []
    assert P("still holding MU", BOOK) == []


def test_negated_sell_does_not_close():
    assert P("not selling AVGO", BOOK) == []
    assert P("didn't sell MU", BOOK) == []
    assert P("never sold AVGO", BOOK) == []
    assert P("I'm not gonna sell MU", BOOK) == []


def test_question_does_not_close():
    """A question about a position is not a report of one."""
    assert P("sold AVGO?", BOOK) == []
    assert P("are you out of MU?", BOOK) == []


def test_adding_does_not_close():
    assert P("added more MU", BOOK) == []
    assert P("bought more AVGO", BOOK) == []


def test_wish_does_not_close():
    assert P("wish I sold MU", BOOK) == []
    assert P("should have sold AVGO", BOOK) == []


# --------------------------------------------------- scoping guarantees

def test_ticker_not_on_the_book_is_ignored():
    """The parser can only ever close what the book listed. A caller
    saying he is out of something never published cannot invent a
    position, and must not close a same-named one by accident."""
    assert P("No TSLA", BOOK) == []


def test_empty_book_closes_nothing():
    assert P("No AVGO", []) == []


def test_empty_message_closes_nothing():
    assert P("", BOOK) == []
    assert P("   ", BOOK) == []


def test_unrelated_sale_does_not_close_a_held_name():
    """THE dangerous shape: one ticker sold, another held, one line.
    A bag-of-words matcher closes both."""
    assert P("sold my SPXW, holding AVGO", BOOK) == ["SPXW"]


def test_multiple_exits_in_one_message():
    got = P("out of MU and out of AVGO", BOOK)
    assert sorted(got) == ["AVGO", "MU"]


# ---------------------------------------------------- other exit shapes

def test_out_of_shape():
    assert P("I'm out of AVGO", BOOK) == ["AVGO"]


def test_closed_and_sold_shapes():
    assert P("closed MU", BOOK) == ["MU"]
    assert P("sold all my AVGO", BOOK) == ["AVGO"]
    assert P("dumped MU", BOOK) == ["MU"]


def test_ticker_then_predicate_deliberately_does_not_fire():
    """"AVGO is closed" reads as an exit to a human and USED to fire.

    The corpus sweep killed it. In that word order a position statement
    and a price statement are the same string, and the price ones are
    far more common: "Spx closed +0.02%", "MU CLOSED 700", "Spx closed
    7413", "MU dead", "Spy flat". Each would have closed a live
    position. Verb-first ("closed MU") keeps the distinction, so that is
    the only order accepted. This is a deliberate recall loss.
    """
    assert P("AVGO is closed", BOOK) == []
    assert P("MU gone", BOOK) == []


def test_price_commentary_never_closes():
    """The real messages that motivated dropping the predicate form."""
    assert P("Spx closed +0.02%", BOOK + ["SPX"]) == []
    assert P("MU CLOSED 700", BOOK) == []
    assert P("Spx closed 7413", BOOK + ["SPX"]) == []
    assert P("MU dead", BOOK) == []
    assert P("Spy flat", BOOK + ["SPY"]) == []
    assert P("Ooooo even btc dead", BOOK + ["BTC"]) == []


def test_third_party_trade_does_not_close():
    """'He sold MRVL' is not this caller closing MRVL. From the sweep."""
    assert P("He sold MRVL", BOOK + ["MRVL"]) == []
    assert P("they closed AVGO", BOOK) == []


def test_deliberation_does_not_close():
    """Both real. A trade being considered is still open."""
    assert P("I was thinking to cut my NVDA puts actually",
             BOOK + ["NVDA"]) == []
    assert P("Hmmm to cut TSLA 0dtes or not", BOOK + ["TSLA"]) == []


def test_completed_exits_from_the_corpus_still_fire():
    """The other side of the ledger: every genuine exit the sweep found
    must survive the tightening."""
    assert P("Sold spx 7450c at 3.00", BOOK + ["SPX"]) == ["SPX"]
    assert P("Sold sndk @ 16.81", BOOK) == ["SNDK"]
    assert P("Sold mu call +50%", BOOK) == ["MU"]
    assert P("Sold my RKLB too soon", BOOK + ["RKLB"]) == ["RKLB"]
    assert P("Sold qqq puts 0.8", BOOK + ["QQQ"]) == ["QQQ"]
    assert P("Cut NVDA calls -30%", BOOK + ["NVDA"]) == ["NVDA"]


def test_no_longer_in_shape():
    assert P("no longer in AVGO", BOOK) == ["AVGO"]


def test_substring_ticker_is_not_matched():
    """MU must not match inside another word. Word boundaries only."""
    assert P("no MUSK stuff", BOOK) == []
    assert P("temu wars", BOOK) == []


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
