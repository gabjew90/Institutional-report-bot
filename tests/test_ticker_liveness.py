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
    assert L.introduced_tickers(ans, "q") == ["MU", "BECN"]
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
