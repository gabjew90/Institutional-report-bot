"""Smoke test: /ask system prompt has a binding rule against meta-
narration about the bot's own plumbing / dev-register.

Background: 2026-06-07 02:30 UTC, when the bot's developer
(gabjew90) /ask'd about SPY OI, the bot correctly returned the
snapshot from lookup_options_chain in bullet 1, but then closed
with: "If you're building the backend for the bot's tracker, you'll
need to poll the chain daily and store the snapshot to get a
reliable trend, the current feed only gives us the static state-up
the real-time aggregate snapshot."

That sentence is THREE violations stacked:
  (a) addresses the asker as the dev ("if you're building the
      backend for the bot's tracker")
  (b) reveals plumbing details ("the current feed only gives us")
  (c) explains the fix path for a limitation that's the bot's,
      not the asker's ("you'll need to poll the chain daily and
      store the snapshot")

Root cause: the model identified the asker as the bot's maintainer
(via WHO'S TALKING profile + chat history flags) and switched to
dev-talk register. The existing ghost-writer framing covers normal
asker types but doesn't explicitly handle "asker IS the dev."

Fix: a binding rule that says — even when the asker is recognizable
as the bot's developer/maintainer, answer in the trader register,
not the dev register. Don't explain plumbing. Don't reveal tool
mechanics. Don't say "you'd need to poll." The reader is a trader
asking about OI, not a code reviewer.

This smoke covers:
  - The rule header exists in the prompt with distinctive caps
  - The rule is marked 'binding'
  - The rule explicitly names the dev-flag failure mode (asker
    recognized as gabjew90 / f.jamal / maintainer)
  - The rule names the specific forbidden behaviors (plumbing,
    polling, feed limitations, schemas, intervals)
  - The rule references the concrete 2026-06-07 SPY OI failure
    + quotes the specific failing closing sentence
  - The rule gives the correct fallback ("I don't have that")
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
    import discord_bot.bot as bot_mod
    return bot_mod._ASK_SYSTEM_INSTRUCTION


def test_no_meta_plumbing_rule_header_present():
    src = _load_system_instruction()
    assert "NEVER META-NARRATE THE BOT'S OWN PLUMBING" in src, (
        "'NEVER META-NARRATE THE BOT'S OWN PLUMBING' rule header must "
        "be in the prompt — distinctive caps so the model recognizes "
        "it as a named binding rule"
    )
    # Must be flagged binding
    rule_anchor = src.find("NEVER META-NARRATE THE BOT'S OWN PLUMBING")
    rule_window = src[rule_anchor:rule_anchor + 200]
    assert "binding" in rule_window.lower(), (
        "the rule must be marked 'binding' so the model treats it as a "
        "hard rule, not soft guidance"
    )
    _ok("'NEVER META-NARRATE THE BOT'S OWN PLUMBING' rule present + "
        "marked binding")


def test_rule_names_dev_asker_failure_mode():
    """The rule must explicitly name the specific dev-asker recognition
    pattern so the model knows what triggers the fallback. Without
    naming the trigger, the rule reads as advisory."""
    src = _load_system_instruction()
    rule_anchor = src.find("NEVER META-NARRATE THE BOT'S OWN PLUMBING")
    rule_window = src[rule_anchor:rule_anchor + 2500]
    # Must name the asker recognition pattern
    asker_flags = ["gabjew90", "developer", "maintainer", "builder", "dev"]
    matched = [f for f in asker_flags if f in rule_window]
    assert len(matched) >= 3, (
        f"rule must name >=3 dev-asker recognition patterns so the "
        f"model knows what triggers the fallback, got {matched}"
    )
    _ok(f"rule names {len(matched)} dev-asker recognition flags "
        f"({matched})")


def test_rule_names_specific_forbidden_behaviors():
    """The rule must enumerate the specific forbidden behaviors — not
    just say 'no meta.' Without enumeration, the model has too much
    latitude on what counts as 'meta'."""
    src = _load_system_instruction()
    rule_anchor = src.find("NEVER META-NARRATE THE BOT'S OWN PLUMBING")
    rule_window = src[rule_anchor:rule_anchor + 2500]
    forbidden = [
        "fetch data",
        "poll",
        "feed",
        "snapshot",
        "tool inventory",
        "schema",
        "storage",
        "API",
        "plumbing",
    ]
    matched = [f for f in forbidden if f in rule_window]
    assert len(matched) >= 5, (
        f"rule must enumerate >=5 forbidden plumbing-talk behaviors so "
        f"the model recognizes them in its own output, got {matched}"
    )
    _ok(f"rule enumerates {len(matched)} forbidden plumbing-talk "
        f"behaviors ({matched})")


def test_rule_references_2026_06_07_concrete_failure():
    """The rule must quote the specific 2026-06-07 02:30 UTC failure
    sentence as a pattern anchor. Abstract rules don't bind as well
    as 'here's the exact mistake to not repeat.'"""
    src = _load_system_instruction()
    rule_anchor = src.find("NEVER META-NARRATE THE BOT'S OWN PLUMBING")
    rule_window = src[rule_anchor:rule_anchor + 2500]
    assert "2026-06-07" in rule_window, (
        "rule must include the 2026-06-07 date as the failure anchor"
    )
    # Must quote a fragment of the specific failing closing sentence
    failing_fragments = [
        "building the backend",
        "bot's tracker",
        "poll the chain daily",
        "current feed only gives us",
        "store the snapshot",
    ]
    matched = [f for f in failing_fragments if f in rule_window]
    assert len(matched) >= 3, (
        f"rule must quote >=3 specific fragments of the 2026-06-07 "
        f"failing sentence so the model recognizes the exact pattern, "
        f"got {matched}"
    )
    _ok(f"rule references 2026-06-07 failure + quotes {len(matched)} "
        f"specific failing-sentence fragments")


def test_rule_gives_correct_fallback():
    """The rule must specify what the model SHOULD say when it doesn't
    have data — without an explicit fallback, the model defaults to
    explaining the gap (which IS the dev-register behavior the rule
    is supposed to suppress)."""
    src = _load_system_instruction()
    rule_anchor = src.find("NEVER META-NARRATE THE BOT'S OWN PLUMBING")
    rule_window = src[rule_anchor:rule_anchor + 2500]
    fallback_phrases = [
        "I don't have",
        "pull it from your broker",
        "data vendor",
        "full stop",
    ]
    matched = [p for p in fallback_phrases if p in rule_window]
    assert len(matched) >= 3, (
        f"rule must give >=3 fallback phrasing hints so the model has "
        f"an explicit replacement for the dev-register explanation, "
        f"got {matched}"
    )
    # And it must explicitly forbid the "explain why" pattern
    assert ("Do NOT explain WHY" in rule_window
            or "do NOT explain WHY" in rule_window
            or "Do not explain" in rule_window), (
        "rule must explicitly forbid explaining WHY data is missing — "
        "that's the dev-register escape hatch the model defaults to"
    )
    _ok(f"rule gives fallback phrasing ({len(matched)} hints) + "
        f"explicitly forbids explain-why escape")


def test_rule_closes_with_role_clarifier():
    """The rule must close with a clear role-clarifier — 'the asker is
    a trader, every time' — so the model has a positive statement of
    what the asker IS, not just a negative list of what not to say."""
    src = _load_system_instruction()
    rule_anchor = src.find("NEVER META-NARRATE THE BOT'S OWN PLUMBING")
    rule_window = src[rule_anchor:rule_anchor + 3000]
    role_phrases = [
        "ghost-writer",
        "trader, every time",
        "asker is a trader",
        "is invisible to your output",
    ]
    matched = [p for p in role_phrases if p in rule_window]
    assert len(matched) >= 2, (
        f"rule must close with a positive role-clarifier ('the asker "
        f"is a trader' / 'the dev role is invisible to your output') "
        f"so the model has a positive guide, not just a no-list. "
        f"Matched: {matched}"
    )
    _ok(f"rule closes with role-clarifier ({matched})")


if __name__ == "__main__":
    print("=== /ask no-meta-plumbing rule smoke ===")
    test_no_meta_plumbing_rule_header_present()
    test_rule_names_dev_asker_failure_mode()
    test_rule_names_specific_forbidden_behaviors()
    test_rule_references_2026_06_07_concrete_failure()
    test_rule_gives_correct_fallback()
    test_rule_closes_with_role_clarifier()
    print("\nALL NO-META-PLUMBING RULE SMOKE TESTS PASS")
