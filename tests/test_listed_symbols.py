"""Tickers are checked against Nasdaq's symbol directory (2026-10-30
quality audit, design note 1: DOD, EXXON and TTM were looked up as stocks)."""
from discord_bot import ask_router as R
from discord_bot import listed_symbols as L

NASDAQ = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
HOOD|Robinhood Markets, Inc. - Class A Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZZZT|Test Stock|Q|Y|N|100|N|N
AACBW|Artius II Acquisition Inc. - Warrants|G|N|N|100|N|N
File Creation Time: 1030202618:01||||||"""
OTHER = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
XOM|ExxonMobil Holdings Corporation Common Stock|N|XOM|N|100|N|XOM
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|40|N|BRK.B
ABR$D|Arbor Realty Trust 6.375% Series D Preferred|N|ABR-D|N|100|N|ABR-D"""


def test_parse_drops_tests_preferreds_and_warrants():
    rows = L.parse(NASDAQ, OTHER)
    assert [r[0] for r in rows] == ["HOOD", "QQQ", "XOM", "BRK.B"]
    assert rows[0] == ("HOOD", "Robinhood Markets, Inc.", False)
    assert rows[1] == ("QQQ", "", True)
    assert L._deserialize(L.serialize(rows))["XOM"] == ("ExxonMobil Holdings Corporation", False)


def test_by_name_needs_one_match():
    saved = L._table, L._by_word
    try:
        L.use({"XOM": ("ExxonMobil Holdings", False), "AAPL": ("Apple Inc.", False),
               "APLE": ("Apple Hospitality REIT", False), "QQQ": ("", True)})
        assert L.by_name("exxon") == "XOM"
        assert L.by_name("apple") is None       # two companies
        assert L.by_name("ex") is None          # too short
    finally:
        L._table, L._by_word = saved


def test_the_snapshot_knows_the_audit_cases():
    for sym in ("HOOD", "ELMT", "RDDT", "SPCX", "XOM", "BRK.B", "QQQ"):
        assert L.is_listed(sym), sym
    for word in ("DOD", "TTM", "EXXON", "GMS", "BECN"):
        assert not L.is_listed(word), word


def test_an_agency_acronym_is_not_the_stock():
    """2026-10-28 15:48: 'DOD' ran five tools on an ELMT question."""
    r = R.classify("what does elmt do? How much stake does DOD have in it?")
    assert r.shape == R.COMPANY_PROFILE and r.tickers == ["ELMT"]


def test_a_company_name_in_a_ticker_slot():
    """2026-10-30 19:09: 'When is Exxon earnings' looked up EXXON."""
    r = R.classify("When is Exxon earnings")
    assert r.shape == R.EARNINGS_DATE and r.tickers == ["XOM"]


def test_a_lowercase_ticker_with_a_market_word():
    """2026-10-28 00:33 (audit A3): 'is hood in a crypto hole ?' got no stock
    data and was answered from BTC's price."""
    r = R.classify("is hood in a crypto hole ?")
    assert r.shape == R.TICKER_OPINION and r.tickers == ["HOOD"]
    assert R.T_NEWS in [t for t, _ in r.prefetch]


def test_room_words_are_not_tickers():
    # "tho" is listed (Thor Industries); the room types it 661 times as a word
    assert R.extract_tickers("how has rddt performed last 3 earnings tho") == ["RDDT"]
    assert "THO" not in R.extract_tickers("what's the move tho")


def test_unlisted_symbols_survive_when_nothing_listed_is_named():
    assert R.extract_tickers("why is PEPE pumping") == ["PEPE"]
    assert R.extract_tickers("$PEPE news") == ["PEPE"]
    assert R.extract_tickers("pepe news") == ["PEPE"]
    assert R.extract_tickers("KRKNF earnings") == ["KRKNF"]


def test_a_link_is_not_the_askers_words():
    assert R.extract_tickers("any news on this https://x.com/rddt/status/1") == []


def test_todays_move_after_a_beat_stays_news():
    assert R.classify("NVDA beat and still dropped why").shape != R.HISTORICAL_STAT
    assert R.classify("why did NVDA drop after it beat").shape == R.NEWS_EVENT


def test_a_member_name_is_not_a_lowercase_ticker():
    r = R.classify("is sam up on his calls", names_member=True)
    assert r.shape != R.TICKER_OPINION


def test_a_pasted_article_is_not_about_google():
    """2026-10-27 21:43 (audit A16): a Discord-privacy question priced GOOGL
    from 'indexed by Google ... per TC'."""
    q = ('[MESSAGE BEING REPLIED TO — from BK — user_id 2]\n'
         '"Discord will let public messages from Discoverable servers be indexed by Google '
         'and other search engines, per TC."\n\n'
         "[BK's message to you]\nhow the heck do we make this chat undiscoverable")
    r = R.classify(q)
    assert r.tickers == [] and r.prefetch == []


def test_fact_questions_about_a_stock_are_factual():
    """Audit A17: 'is Amazon on net negative cashflow ?' ran as banter."""
    for q in ("is Amazon on net negative cashflow ?",
              "when are those sandisk guys splitting the stock"):
        r = R.classify(q)
        assert r.shape == R.TICKER_OPINION and r.is_factual, q
    for q in ("what do you think of MU", "is hood a good short here", "MU?",
              "do you think NVDA rips tomorrow on earnings"):
        r = R.classify(q)
        assert not r.is_factual, q
