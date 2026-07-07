"""Smoke: WEB-routed answers must actually ground (2026-07-06).

The CXW/GEO chain exposed that "search-only" mode doesn't FORCE a search
— Gemini grounding is discretionary, so a WEB-routed question can answer
from a pasted doc + memory and never search (it invented bed counts,
market cap, and contract dates). The market-fact-SHAPE backstop misses
non-market facts. The structural fix ties the grounding requirement to
the router's own WEB decision: a WEB-routed answer stating specifics
with no grounding forces a search retry — any fact shape, not an
enumerated list.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_web_ungrounded_specifics_fires_on_any_fact_shape():
    from discord_bot.bot import _ungrounded_web_specifics as f
    # the exact CXW/GEO confabulations — none are "market-fact shapes"
    for s in [
        "GEO Group operates approximately 75,000 beds across its facilities.",
        "The California City contract expires in August 2027, Otay Mesa "
        "runs through December 2029.",
        "CoreCivic's total capacity hovers in the 65,000-70,000 bed range.",
    ]:
        assert f(s, None, was_web=True) is True, f"WEB+specifics must fire: {s!r}"


def test_web_grounding_respects_router_and_grounding():
    from discord_bot.bot import _ungrounded_web_specifics as f
    fact = "GEO operates approximately 75,000 beds."
    # LOCAL-routed (was_web=False): NOT this guard's job (banter/member-data)
    assert f(fact, None, was_web=False) is False, "LOCAL answers are exempt"
    # grounded WEB answer: exempt (it actually searched)
    class _GM:
        grounding_chunks = [1]
    assert f(fact, _GM(), was_web=True) is False, "grounded answers exempt"
    # a WEB answer with NO specifics (a decline / opinion) does not retry
    assert f("Couldn't find a verified bed count for GEO.", None,
             was_web=True) is False, "figureless declines must not fire"
    assert f("Creatine is still worth it for most lifters.", None,
             was_web=True) is False, "specific-free opinion must not fire"
    _ok("web-grounding: fires on WEB+specifics+ungrounded; "
        "LOCAL / grounded / figureless exempt")


def test_trigger_and_retry_wired():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # the retry must be triggered by BOTH the market-shape backstop AND
    # the router-tied web-ungrounded check
    assert "_ground_trigger_web = _ungrounded_web_specifics(" in src, \
        "web-routed grounding trigger not wired"
    assert "grounding_metadata, needs_web" in src, \
        "the web trigger must read the router's needs_web decision"
    # the retry's accept-clean check must use the same broadened condition
    assert "_retry_still_ungrounded" in src, \
        "retry acceptance must re-check the web-ungrounded condition"
    # the directive must forbid extrapolating from a pasted doc
    window = src.split("[GROUNDING REQUIRED]", 1)[1][:600]
    assert "pasted" in window and "memory" in window, \
        "directive must forbid answering from memory / a pasted doc"
    _ok("wired: web trigger + retry acceptance + no-extrapolation directive")


if __name__ == "__main__":
    print("=== /ask WEB-grounding smoke ===")
    test_web_ungrounded_specifics_fires_on_any_fact_shape()
    test_web_grounding_respects_router_and_grounding()
    test_trigger_and_retry_wired()
    print("\nALL WEB-GROUNDING SMOKE TESTS PASS")
