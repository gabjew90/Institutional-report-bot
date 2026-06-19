"""Smoke test: grounding backstop (2026-06-18).

Gemini's Google-Search grounding is DISCRETIONARY — "Type 1 always
searches" was a prompt instruction the model could (and did) ignore,
confabulating the SPCX unlock schedule from priors with zero grounding
and zero tool calls (2026-06-17). This adds a structural check: if an
answer asserts market-fact specifics but nothing grounded it (no
grounding chunks, no data tool), force one grounded retry; if that
still doesn't ground, append a hedge. The asker sees ONE final answer.

Scoped to MARKET-fact shapes so it never misfires on the roast
register (a personal "$3,500 soccer tickets" jab must NOT trigger it).

Covers the detector (the structural gate) + that the retry/hedge block
is wired into the answer path.
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


class _GM:
    def __init__(self, chunks):
        self.grounding_chunks = chunks


_SPCX_CONFAB = (
    "SpaceX unlock: the first tranche hits August 20 releasing 7% of "
    "eligible shares, followed by 7% tranches; a 28% block after Q3 "
    "earnings; full expiry December 8. Watch the $175.50 level."
)


def test_detector_fires_on_confab_no_source():
    from discord_bot.bot import _is_ungrounded_market_fact as f
    assert f(_SPCX_CONFAB, None, []) is True, "confab w/ no grounding+no tool"
    _ok("detector: fires on market-fact specifics with no grounding + no tool")


def test_detector_skips_when_grounded():
    from discord_bot.bot import _is_ungrounded_market_fact as f
    assert f(_SPCX_CONFAB, _GM([{"web": "x"}]), []) is False
    _ok("detector: skips when Google grounding fired")


def test_detector_skips_when_tool_fired():
    from discord_bot.bot import _is_ungrounded_market_fact as f
    assert f(_SPCX_CONFAB, None, [{"tool": "lookup_market_price"}]) is False
    _ok("detector: skips when a data tool fired")


def test_detector_roast_safe():
    from discord_bot.bot import _is_ungrounded_market_fact as f
    roasts = [
        "Enjoy the 200% while it lasts, you're one confluence away from rape.",
        "blowing $3,500 on soccer tickets while your portfolio gets nuked",
        "your girlfriend blew through your $25k ring savings, clown",
        "spending $150 on Pokemon cards while crying about rent",
    ]
    for r in roasts:
        assert f(r, None, []) is False, f"must NOT fire on roast: {r!r}"
    _ok("detector: roast-safe — personal dollar jabs don't trigger")


def test_detector_strong_markers():
    from discord_bot.bot import _is_ungrounded_market_fact as f
    for s in [
        "JPMorgan reiterated Overweight, PT $580 on the custom-silicon lead.",
        "The lockup releases 20% on the first earnings date.",
        "Public float is roughly 4.3% of shares outstanding.",
        "Next CPI prints Aug 12 with consensus 4.2%.",
    ]:
        assert f(s + " " * 0, None, []) is True, f"strong marker should fire: {s!r}"
    _ok("detector: single strong analyst-fact marker fires (PT / lockup / "
        "float / consensus)")


def test_detector_no_false_positive_on_calendar():
    """2026-06-19 fix: bare calendar dates were stamping CORRECT
    common-knowledge answers with the unverified hedge. A market-hours /
    calendar answer with NO unlock/PT/tranche shape must NOT fire."""
    from discord_bot.bot import _is_ungrounded_market_fact as f
    benign = [
        # id 118 — correct, got falsely hedged before the fix
        "Yes, the market closes at the normal time of 4:00 PM ET today, "
        "Thursday, June 18. It is closed Friday, June 19 for Juneteenth, "
        "reopening Monday, June 22.",
        # id 117 — a date-bearing answer with no confab shape
        "Rebalancing is effective after the close tomorrow, Friday, June 19.",
        "Earnings are scheduled for the week of October 27.",
    ]
    for s in benign:
        assert f(s, None, []) is False, f"calendar answer must NOT fire: {s!r}"
    _ok("detector: bare calendar dates no longer false-trigger (id-118 fix)")


def test_detector_density_threshold():
    from discord_bot.bot import _is_ungrounded_market_fact as f
    # 3 generic specifics, no strong marker → fires
    assert f("It moved 2% then 3% and again 4% intraday.", None, []) is True
    # 2 generics → does not fire (one-off-number safe)
    assert f("It moved 2% then 3% intraday, nothing wild.", None, []) is False
    _ok("detector: density path fires at >=3 generic specifics, not 2")


def test_grounding_has_sources():
    from discord_bot.bot import _grounding_has_sources as g
    assert g(None) is False
    assert g(_GM([])) is False
    assert g(_GM([{"web": "x"}])) is True
    _ok("_grounding_has_sources: None/empty False, chunks True")


def test_backstop_block_wired():
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "_is_ungrounded_market_fact(" in src, "detector must gate the path"
    assert "GROUNDING REQUIRED" in src, "forced-retry directive missing"
    assert "Couldn't verify these specifics" in src, "hedge fallback missing"
    _ok("backstop wired into _answer_with_gemini: detect → forced retry → hedge")


def test_forced_retry_is_search_only():
    """2026-06-19 fix: the forced retry must strip the function tools so
    Google Search is the model's ONLY move — otherwise it skips search,
    re-answers from priors, and falls through to the hedge instead of
    actually grounding. Guard the SEARCH-ONLY config + the directive
    that tells the model search is its only tool."""
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    # The forced-retry block must carry the search-only marker + the
    # single-tool google_search config, and must NOT re-list the
    # function-tool builders inside that retry.
    assert "SEARCH-ONLY tool config" in src, "search-only retry marker missing"
    assert "Google Search is now your" in src, "search-only directive missing"
    # The grounding retry directive must precede a single-tool config.
    after = src.split("[GROUNDING REQUIRED]", 1)[1].split("forced_resp", 1)[0]
    assert "_build_trade_log_tool" not in after, (
        "forced retry must NOT re-include function tools (search-only)"
    )
    assert "google_search=types.GoogleSearch()" in after, "search tool missing"
    _ok("forced retry is SEARCH-ONLY (function tools stripped)")


if __name__ == "__main__":
    print("=== grounding backstop smoke ===")
    test_detector_fires_on_confab_no_source()
    test_detector_skips_when_grounded()
    test_detector_skips_when_tool_fired()
    test_detector_roast_safe()
    test_detector_strong_markers()
    test_detector_no_false_positive_on_calendar()
    test_detector_density_threshold()
    test_grounding_has_sources()
    test_backstop_block_wired()
    test_forced_retry_is_search_only()
    print("\nALL GROUNDING BACKSTOP SMOKE TESTS PASS")
