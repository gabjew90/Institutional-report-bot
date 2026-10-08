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
