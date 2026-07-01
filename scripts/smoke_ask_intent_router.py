"""Smoke test: /ask intent router (structural grounding, 2026-06-19).

Replaces the answer-keyword "trip" with an up-front decision about
whether the QUESTION needs the open web. WEB questions route to a
search-only pass (grounded by construction); banter / self-data take the
normal multi-tool path. The classification is the model's semantic read
of the question, not a regex over the output — so it can't misfire on
arbitrary words like "June 19" or "unlock".

The classifier itself makes a live Gemini call, so here we cover:
parsing, the fail-safe (errors default LOCAL, never raise), the router
instruction's buckets, and that the router is wired into the answer path
with a search-only config on the WEB branch.
"""

import asyncio
import inspect
import os
import sys
import types as _pytypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


class _FakeResp:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = None


def _make_client(text=None, raise_exc=False):
    async def _gen(**kwargs):
        if raise_exc:
            raise RuntimeError("simulated API error")
        return _FakeResp(text)
    models = _pytypes.SimpleNamespace(generate_content=_gen)
    aio = _pytypes.SimpleNamespace(models=models)
    return _pytypes.SimpleNamespace(aio=aio)


def test_parses_web_verdict():
    from discord_bot.bot import _classify_ask_needs_web as f
    for verdict in ["WEB", "web", "  WEB\n", "Web — because it needs a lookup"]:
        client = _make_client(text=verdict)
        got = asyncio.run(f(client, "m", [], "when does spcx unlock"))
        assert got is True, f"{verdict!r} should classify WEB: {got}"
    _ok("router: WEB verdict parsed (case/whitespace/trailing-text tolerant)")


def test_parses_local_verdict():
    from discord_bot.bot import _classify_ask_needs_web as f
    for verdict in ["LOCAL", "local", "  LOCAL", ""]:
        client = _make_client(text=verdict)
        got = asyncio.run(f(client, "m", [], "roast terlin"))
        assert got is False, f"{verdict!r} should classify LOCAL: {got}"
    _ok("router: LOCAL verdict + empty/blank parsed as LOCAL")


def test_failsafe_on_error():
    from discord_bot.bot import _classify_ask_needs_web as f
    client = _make_client(raise_exc=True)
    got = asyncio.run(f(client, "m", [], "anything"))
    assert got is False, "an API error must fail-safe to LOCAL, never raise"
    _ok("router: API error fail-safes to LOCAL (no raise)")


def test_failsafe_on_empty_question():
    from discord_bot.bot import _classify_ask_needs_web as f
    # No call should even be attempted for a blank question.
    client = _make_client(raise_exc=True)
    assert asyncio.run(f(client, "m", [], "   ")) is False
    _ok("router: blank question short-circuits to LOCAL without a call")


def test_router_instruction_buckets():
    from discord_bot.bot import _ASK_ROUTER_INSTRUCTION as instr
    low = instr.lower()
    # Must name the WEB triggers and the LOCAL exemptions, and the
    # output contract.
    for kw in ["lockup", "holiday", "macro", "banter", "roast",
               "live stock price", "web", "local"]:
        assert kw in low, f"router instruction missing bucket keyword: {kw!r}"
    assert "exactly one word" in low, "output contract missing"
    # 2026-07-01: uncertainty default FLIPPED to WEB ("facts first") —
    # a wasted search is cheap, an unverified wrong fact gets traded on.
    # The clearly-LOCAL classes (banter/roast/member-data/live-price) are
    # named so they don't get dragged into the search-only path.
    assert "unsure, answer web" in low, "unsure->WEB default missing"
    assert "clearly" in low and "banter" in low, \
        "the clearly-LOCAL carve-out must be named"
    _ok("router instruction: WEB triggers + LOCAL exemptions + unsure->WEB")


def test_router_covers_type2_factual_edge():
    """Alignment with the Type 2 'search when there's a factual edge'
    clause: pricing/product comparisons, who-won/result, and current
    discourse must route WEB; pure vibe checks stay LOCAL."""
    from discord_bot.bot import _ASK_ROUTER_INSTRUCTION as instr
    low = instr.lower()
    # WEB now covers the factual-edge opinion shapes.
    assert "factual edge" in low, "factual-edge clause missing from router"
    assert "pricing" in low and "product comparison" in low, \
        "pricing/product comparison not routed WEB"
    assert ("who-won" in low or "who won" in low), "who-won hook not routed WEB"
    assert "discourse" in low, "current-discourse hook not routed WEB"
    assert "would a real lookup change" in low, \
        "the 'would a lookup change the answer' test should anchor WEB"
    # Pure vibe checks stay LOCAL.
    assert "vibe check" in low, "pure vibe-check must stay LOCAL"
    _ok("router instruction: Type 2 factual-edge -> WEB, pure vibe -> LOCAL")


def test_router_wired_search_only():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_classify_ask_needs_web(" in src, "router not called in answer path"
    assert "if needs_web:" in src, "needs_web branch missing"
    # The WEB branch must build a SEARCH-ONLY config (no function tools).
    web_branch = src.split("if needs_web:", 1)[1].split("else:", 1)[0]
    assert "google_search=types.GoogleSearch()" in web_branch, \
        "WEB branch must use google_search"
    for fn in ["_build_trade_log_tool", "_build_market_price_tool",
               "_build_chat_search_tool"]:
        assert fn not in web_branch, (
            f"WEB branch must be search-only — {fn} must not appear"
        )
    _ok("router wired: WEB→search-only config, LOCAL→multi-tool path")


if __name__ == "__main__":
    print("=== /ask intent router smoke ===")
    test_parses_web_verdict()
    test_parses_local_verdict()
    test_failsafe_on_error()
    test_failsafe_on_empty_question()
    test_router_instruction_buckets()
    test_router_covers_type2_factual_edge()
    test_router_wired_search_only()
    print("\nALL /ASK INTENT ROUTER SMOKE TESTS PASS")
