"""Smoke test: /ask system prompt has the unforced-price-assertion guard
+ index examples for lookup_market_price.

Background: 2026-06-05 17:17 UTC, SV asked "when is the last time NDX
was down 3.5% or more." Bot answered with a HISTORICAL question's data
(no 3.5% drops in current regime, March 2026 -4.89% monthly) but then
volunteered: "the index currently holding near its 52-week highs around
$30,500." That number is wrong-symbol — NDX in 2026 trades in the
~$22-23k range; $30,500 doesn't match NDX, SPX, or RUT. Closest match
is DJIA which is a different index entirely.

Root cause: the HARD ROUTING RULES in _ASK_SYSTEM_INSTRUCTION force
lookup_market_price on live-price QUESTIONS but say nothing about
unforced price ASSERTIONS the model volunteers in answers to other
question shapes. Plus the lookup_market_price examples block lists
$TSLA / BTC / $SPY / $LMT but no indices, so the model may not know
the tool covers NDX / SPX / DJI / RUT.

Fix: extend the examples block to explicitly cover indices, then add
a binding 'ZERO UNFORCED PRICE ASSERTIONS' rule that says: if you
state a price level for anything, source it from the tool or don't
state it.

This smoke covers:
  - Index examples (NDX, SPX, DJI) added to the lookup_market_price
    examples block
  - The new ZERO UNFORCED PRICE ASSERTIONS rule is present and
    marked 'binding'
  - The rule explicitly names the failure-mode shapes (closing
    flourish, parenthetical, background flavor)
  - The rule references the concrete 2026-06-05 NDX $30,500 failure
    so the model has the pattern shape
  - The rule explicitly disallows pulling from memory / chat-context /
    Google snippets (the four wrong sources)
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


def _load_system_instruction():
    """Load the /ask system prompt string from discord_bot.bot."""
    import discord_bot.bot as bot_mod
    return bot_mod._ASK_SYSTEM_INSTRUCTION


def test_index_examples_added():
    """The lookup_market_price examples block must explicitly show
    NDX / SPX / DJI calls so the model knows the tool covers indices,
    not just single names + ETFs + crypto."""
    src = _load_system_instruction()
    # Must explicitly name the index tickers as supported
    for idx in ["NDX", "SPX", "DJI"]:
        assert idx in src, (
            f"index ticker {idx!r} should be in lookup_market_price "
            f"examples so the model knows the tool covers it"
        )
    # And tie at least one to a recognizable index question
    assert "NDX" in src and "Dow" in src, (
        "examples block should cite at least one index by both ticker "
        "(NDX/SPX/DJI) and common name (Dow) so the model maps "
        "natural-language index references to tool calls"
    )
    _ok("lookup_market_price examples block covers NDX / SPX / DJI as "
        "first-class targets")


def test_unforced_price_assertion_rule_present():
    """The new binding rule must exist with a clear, scan-friendly name."""
    src = _load_system_instruction()
    assert "ZERO UNFORCED PRICE ASSERTIONS" in src, (
        "the 'ZERO UNFORCED PRICE ASSERTIONS' rule header must be in the "
        "system prompt — distinctive caps so the model treats it as a "
        "named binding rule, not advisory commentary"
    )
    # Must be flagged binding
    assert "binding" in src.lower(), (
        "the rule must be marked 'binding' — the prompt has multiple "
        "soft suggestions; without 'binding' the model treats this one "
        "as another soft suggestion"
    )
    _ok("'ZERO UNFORCED PRICE ASSERTIONS' rule present and marked binding")


def test_rule_names_failure_mode_shapes():
    """The rule must name the SHAPES the failure takes (closing
    flourish, parenthetical, background flavor) so the model recognizes
    its own about-to-fabricate moments."""
    src = _load_system_instruction()
    shape_phrases = [
        "closing flourish",
        "parenthetical",
        "background flavor",
        "currently around",
    ]
    matched = [p for p in shape_phrases if p in src]
    assert len(matched) >= 3, (
        f"the rule should name >=3 unforced-assertion shapes so the "
        f"model self-recognizes the pattern; got {matched}"
    )
    _ok(f"rule names {len(matched)} concrete failure-mode shapes "
        f"({matched})")


def test_rule_references_concrete_2026_06_05_failure():
    """The rule must include the concrete NDX $30,500 / 2026-06-05
    failure as a pattern anchor — abstract rules don't bind as well
    as 'here's the exact mistake to not repeat'."""
    src = _load_system_instruction()
    assert "2026-06-05" in src, (
        "the rule must reference the 2026-06-05 NDX failure date as "
        "the pattern anchor — abstract 'don't fabricate prices' rules "
        "land softer than 'here's the exact mistake'"
    )
    assert "30,500" in src or "$30,500" in src, (
        "the rule must quote the specific wrong number ($30,500) so "
        "the model recognizes the pattern shape"
    )
    assert "NDX" in src, "rule must name NDX (the symbol that failed)"
    _ok("rule references concrete 2026-06-05 NDX $30,500 failure as "
        "the pattern anchor")


def test_rule_disallows_four_wrong_sources():
    """The rule must explicitly forbid pulling levels from the four
    wrong sources: memory, WHO'S TALKING profiles, chat-context block,
    Google snippets."""
    src = _load_system_instruction()
    wrong_sources = [
        "memory",
        "Google snippet",
        "chat-context",
        "WHO'S TALKING",
    ]
    # Find the rule body window
    rule_anchor = src.find("ZERO UNFORCED PRICE ASSERTIONS")
    assert rule_anchor != -1, "ZERO UNFORCED PRICE ASSERTIONS anchor missing"
    rule_window = src[rule_anchor:rule_anchor + 2000]
    matched = [s for s in wrong_sources if s in rule_window]
    # At least 3 of 4 wrong sources should be named so the model
    # recognizes them all as off-limits
    assert len(matched) >= 3, (
        f"the rule body should disallow >=3 of the four wrong sources "
        f"({wrong_sources}); the rule window only matched {matched}. "
        f"Without naming them, the model treats some sources as "
        f"acceptable price-evidence"
    )
    _ok(f"rule disallows {len(matched)} of 4 wrong sources for prices "
        f"({matched})")


def test_rule_has_clear_action_when_level_not_needed():
    """The rule must tell the model what to do when a level isn't
    needed: OMIT IT. Without this, the model has a rule against
    fabricating but no explicit fallback for 'I don't have the tool
    output' — and the default fallback is to fabricate."""
    src = _load_system_instruction()
    rule_anchor = src.find("ZERO UNFORCED PRICE ASSERTIONS")
    rule_window = src[rule_anchor:rule_anchor + 2000]
    assert ("OMIT" in rule_window or "omit it" in rule_window
            or "Omit" in rule_window), (
        "the rule must explicitly tell the model to OMIT the price "
        "level when it's not strictly needed — without the omit "
        "instruction, the model's fallback is to fabricate"
    )
    # And the "either you called the tool or you don't state it" cap
    assert ("Either you called the tool" in rule_window
            or "you don't state a number" in rule_window
            or "or you don't state" in rule_window), (
        "the rule should have the binary cap: tool-sourced OR omitted, "
        "no third option"
    )
    _ok("rule has explicit OMIT action + binary tool-sourced-or-omitted "
        "cap")


def test_existing_routing_rule_untouched():
    """Sanity: the existing HARD ROUTING RULES for live-price QUESTIONS
    must remain intact. The new rule is additive (covers unforced
    ASSERTIONS), it should not have weakened the question-routing rule."""
    src = _load_system_instruction()
    assert "HARD ROUTING RULES" in src, (
        "HARD ROUTING RULES section must remain — this is the live-"
        "price-QUESTION routing that the new rule complements"
    )
    assert "call `lookup_market_price` FIRST" in src, (
        "the 'call lookup_market_price FIRST' binding for live-price "
        "questions must remain intact"
    )
    _ok("existing HARD ROUTING RULES for live-price questions intact")


if __name__ == "__main__":
    print("=== /ask unforced-price-assertion guard smoke ===")
    test_index_examples_added()
    test_unforced_price_assertion_rule_present()
    test_rule_names_failure_mode_shapes()
    test_rule_references_concrete_2026_06_05_failure()
    test_rule_disallows_four_wrong_sources()
    test_rule_has_clear_action_when_level_not_needed()
    test_existing_routing_rule_untouched()
    print("\nALL UNFORCED-PRICE-ASSERTION GUARD SMOKE TESTS PASS")
