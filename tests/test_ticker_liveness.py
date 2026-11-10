"""Tickers an answer introduces must still trade (2026-10-08 audit: GMS and
BECN, both acquired in 2025, recommended as shorts)."""
import asyncio

from discord_bot import ticker_liveness as L

ANSWER = ("→ **GMS (`GMS`)**: drywall distributor\n"
          "→ **Beacon Roofing Supply (`BECN`)**: roofing\n"
          "→ **Builders FirstSource (`BLDR`)**: lumber")


def _feed(dead_syms):
    async def price(args):
        return {"quotes": [({"symbol": s, "error": f"no live feed for {s}"} if s in dead_syms
                            else {"symbol": s, "price": 100.0}) for s in args["symbols"]]}
    return price


def test_delisted_names_are_dropped():
    out, gone = asyncio.run(L.guard(ANSWER, "what are we shorting", _feed({"GMS", "BECN"})))
    assert gone == ["GMS", "BECN"]
    assert out == "→ **Builders FirstSource (`BLDR`)**: lumber"


def test_a_feed_outage_changes_nothing():
    out, gone = asyncio.run(L.guard(ANSWER, "what are we shorting", _feed({"GMS", "BECN", "BLDR"})))
    assert (out, gone) == (ANSWER, [])


def test_names_the_asker_gave_are_not_rechecked():
    assert "GMS" not in L.introduced_tickers(ANSWER, "is GMS a good short")


def test_acronyms_and_delisting_explanations_are_kept():
    ans = "→ DRAM contract prices rose for `MU`\n→ BECN (`BECN`) was acquired by QXO"
    assert L.introduced_tickers(ans, "which stocks win on memory") == ["MU", "BECN"]
    assert L.drop_lines(ans, ["BECN"]) == ans


def test_a_spelled_out_acronym_is_not_a_ticker_2026_10_10():
    """"what's the difference of AI vs super intelligence" lost its second
    half: "(ASI)" was priced, found nowhere, and its line dropped."""
    from discord_bot import ticker_liveness as TL
    ans = ("→ **AI** covers current systems built for specific tasks.\n\n"
           "→ **Artificial Superintelligence (ASI)** is a hypothetical system "
           "that exceeds human cognition in every field.")
    assert "ASI" not in TL.introduced_tickers(ans, "difference of AI vs super intelligence")
    # a company and its symbol on a trading line is still checked
    short = "→ Short **Beacon Roofing (BECN)** into the housing slowdown."
    assert "BECN" in TL.introduced_tickers(short, "what are we shorting")
    frc = "→ Short **First Republic Corp (FRC)** here."
    assert "FRC" in TL.introduced_tickers(frc, "what are we shorting")


def test_a_company_name_carrying_its_symbol_is_still_checked():
    from discord_bot import ticker_liveness as TL
    ans = "→ Home Depot bought GMS Supply (GMS) last year."
    assert "GMS" in TL.introduced_tickers(ans, "who did home depot buy")


def test_an_exact_acronym_is_a_definition_even_on_a_trading_line():
    """2026-10-18 audit A17: the investor-day answer lost its key line, the
    sell-off on "no wafer fabrication equipment (WFE) forecast"."""
    import re
    from discord_bot.ticker_liveness import _defines_acronym
    a = ("→ **Omitted WFE guidance:** The stock faced heavy selling because management declined "
         "to issue a forward wafer fabrication equipment (WFE) spending forecast")
    m = re.search(r"\(WFE\)", a)
    assert _defines_acronym(a, m, "WFE")
    b = "→ Beacon Roofing (BECN) shares fell 4%"
    assert not _defines_acronym(b, re.search(r"\(BECN\)", b), "BECN")


# 2026-11-09: the check reads the router's one not-a-ticker list.
# "Operating Cash Flow (TTM)" lost its line on a QCOM answer because the
# check read only the router's common words.

def test_reporting_shorthand_in_parentheses_is_not_priced():
    a = ("→ **Operating Cash Flow (TTM):** **$12.4B** against capital spending of "
         "**$2.0B**, leaving free cash flow (OCF minus capex) at **$10.4B**")
    assert L.introduced_tickers(a, "qcom net cash flow 12 months basis and debts") == []


def test_a_company_and_its_ticker_is_still_checked():
    a = "→ **GMS Inc. (GMS):** specialty distributor of wallboard, short it"
    assert L.introduced_tickers(a, "what are we shorting") == ["GMS"]


# 2026-11-10: the check runs only where the answer puts a name forward to
# trade. Every fire to that date, from the ask logs.

def test_terms_in_an_explanation_are_not_priced_2026_11_10():
    cases = [
        ("what’s the difference of AI vs super intelligence",
         "→ **Superintelligence (ASI)** is a theoretical phase where an AI vastly "
         "outperforms human capability across every domain"),
        ("what was the investor day mentions for amat",
         "→ **Omitted WFE guidance:** The stock faced heavy selling during the presentation "
         "because management declined to issue a forward wafer fabrication equipment (WFE) "
         "spending forecast"),
        ("explain this one too",
         "→ **The Thermal & Cooling Angle:** JCI supplies high-capacity industrial chillers "
         "(via brands like YORK), air handlers (CRAH), and direct-to-chip liquid cooling "
         "distribution units (CDUs)"),
    ]
    for q, a in cases:
        assert L.introduced_tickers(a, q) == [], q


def test_a_request_for_names_is_still_checked():
    a = ("→ **Vista Outdoor / Revelyst / The Kinetic Group:** Historically a primary "
         "ammunition and outdoor recreation player (`VSTO`), though corporate split-ups have "
         "divided its sporting products business")
    assert L.introduced_tickers(a, "what are all the gun and ammo public stocks") == ["VSTO"]


def test_the_original_gms_answer_is_still_checked():
    """2026-10-06: "give me tickers", asked in reply to the bot's list of
    freight-exposed distributors. No line says "short"; the request does."""
    q = ("[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1]\n"
         "\"→ **Heavy building product distributors:** wholesalers of drywall\"\n"
         "[2Pale's message to you]\ngive me tickers")
    a = ("→ **GMS (`GMS`)**, Specialty distributor of wallboard and ceilings\n"
         "→ **Beacon Roofing Supply (`BECN`)**, Wholesale distributor of heavy roofing materials\n"
         "→ **The Andersons (`ANDE`)**, Bulk agricultural input and commodity distributor")
    assert L.introduced_tickers(a, q) == ["GMS", "BECN", "ANDE"]


def test_a_trade_call_on_its_own_line_is_checked_on_any_question():
    a = "Housing is slowing.\n→ Short **Beacon Roofing (BECN)** into the spring."
    assert L.introduced_tickers(a, "how is housing looking") == ["BECN"]
    # describing a sell-off or a long-term trend is not a call
    b = "→ Beacon Roofing (BECN) led the sell-off on long-term margin worries"
    assert L.introduced_tickers(b, "how is housing looking") == []
    c = "→ Sell-side desks flag short interest and short-term air handler (CRAH) demand"
    assert L.introduced_tickers(c, "how long has JCI made chillers") == []
