"""Smoke: the voice contract reaches the stages that write prose, and the
linter measures glossing instead of jargon presence.

2026-08-11, from a reader report that the pulses read "wordy and jargony".
Measured across five shipped pulses, total length had FALLEN 26% and
average sentence length was down. What rose 5.6x was inline definitions:
0.15 -> 0.84 glosses per 100 words. Nearly every sentence had stopped to
define a term.

Root cause: compose_audit_voice_block() in voice_rules.py holds the
"REWRITE the sentence, do NOT just append a parenthetical translation"
rule, and it had ZERO callers. It had never reached a model. Meanwhile
every live instruction pushed the other way — DRAFT offered inline
translation as a coequal first option, AUDIT called parenthetical
glossing "the right pattern", and pulse_lint flagged bare jargon it could
not distinguish from glossed jargon, so the cheapest way to look
compliant was to add a definition.

These tests pin the wiring, the ordering, and the calibration.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_voice_block_reaches_draft_and_audit():
    """The dead-code bug. If this fails, the contract is aspirational."""
    import ai_analysis.prompts as p
    from ai_analysis.voice_rules import compose_audit_voice_block
    block = compose_audit_voice_block()
    marker = "REWRITE the sentence, do NOT just append"
    if marker not in block:
        _fail("the rewrite-over-gloss rule is gone from voice_rules")
    for name in ("DRAFT_SYSTEM", "AUDIT_SYSTEM"):
        text = getattr(p, name)
        if "<<VOICE_RULES_BLOCK>>" in text:
            _fail(f"{name} still has an unsubstituted placeholder")
        if marker not in text:
            _fail(f"{name} does not carry the shared voice contract — "
                  f"this is the zero-callers bug returning")
    _ok("voice contract is interpolated into DRAFT_SYSTEM and AUDIT_SYSTEM")


def test_rewrite_outranks_gloss():
    from ai_analysis.voice_rules import compose_audit_voice_block
    b = compose_audit_voice_block()
    rewrite = b.find("**Rewrite the sentence**")
    gloss = b.find("**Parenthetical gloss**")
    if rewrite < 0 or gloss < 0:
        _fail("the preference ladder is missing")
    if rewrite > gloss:
        _fail("parenthetical gloss is ranked above sentence rewrite")
    _ok("preference ladder ranks rewrite above parenthetical gloss")


def test_no_live_prompt_calls_glossing_the_right_pattern():
    """AUDIT used to say 'The right pattern: PARENTHETICAL gloss'. That
    line, written for a narrow ratio-precision case, read as house style."""
    import ai_analysis.prompts as p
    for name in ("DRAFT_SYSTEM", "DRAFT_USER", "AUDIT_SYSTEM", "AUDIT_USER"):
        text = getattr(p, name)
        if "The right pattern: PARENTHETICAL gloss" in text:
            _fail(f"{name} still presents parenthetical glossing as the "
                  f"general right pattern")
    _ok("no live prompt presents glossing as the default pattern")


def test_worked_examples_obey_the_punctuation_ban():
    """The block teaches by example. Its 'good' rewrites must not contain
    the punctuation it bans, or it teaches the violation."""
    from ai_analysis.voice_rules import compose_audit_voice_block
    bad = []
    for line in compose_audit_voice_block().splitlines():
        if line.startswith("- Good (sentence rewrite)") or \
           line.startswith("- ✅"):
            if "—" in line or ";" in line:
                bad.append(line[:100])
    if bad:
        _fail("worked examples contain banned punctuation:\n  "
              + "\n  ".join(bad))
    _ok("worked examples obey the em-dash and semicolon ban")


def test_jargon_presence_check_is_retired():
    from ai_analysis.voice_rules import compose_jargon_lint_patterns
    if compose_jargon_lint_patterns():
        _fail("the term-presence jargon check is back — it fires on correct "
              "prose and the only way to clear it is to delete nomenclature "
              "the contract requires keeping")
    _ok("term-presence jargon check is retired")


CLEAN = (
    "Thirty-year Treasury bonds fell this morning because the government "
    "is auctioning a wave of new long-term debt this week, and more supply "
    "means buyers demand higher yields. Goldman raised its 2026 capex "
    "estimate to $755B. The jobs report printed -23k against an 80k "
    "consensus (Deutsche Bank), the weakest of the cycle. $SMCI reports "
    "Tuesday after the close (est. $0.98 EPS on $11.78B revenue). Silver "
    "has to clear and hold $64 or the squeeze stalls. Long $GLD. "
) * 4

GLOSSY = (
    "Participation, the share of adults working or looking for work, fell "
    "to 61.4%. The long end (the longest-maturity, most rate-sensitive "
    "bonds) sits at post-2007 highs. Hyperscaler spreads, the extra yield "
    "investors demand to hold their debt, widened 32 basis points. "
    "Trend-following funds (CTAs, funds that mechanically chase price "
    "moves) are covering. Real rates, Treasury yields after subtracting "
    "inflation, are the trigger. Lease rates, the cost to borrow physical "
    "metal, are softening. "
) * 4


def test_gloss_density_separates_clean_from_glossy():
    from ai_analysis.voice_rules import gloss_density, GLOSS_DENSITY_LIMIT
    _, _, clean = gloss_density(CLEAN)
    _, _, glossy = gloss_density(GLOSSY)
    if clean > GLOSS_DENSITY_LIMIT:
        _fail(f"clean prose flagged at {clean:.2f} — attribution and data "
              f"parentheticals are being counted as definitions")
    if glossy <= GLOSS_DENSITY_LIMIT:
        _fail(f"gloss-heavy prose scored only {glossy:.2f}")
    _ok(f"clean {clean:.2f} vs glossy {glossy:.2f}, limit "
        f"{GLOSS_DENSITY_LIMIT}")


def test_attribution_parentheticals_are_not_glosses():
    """(Goldman), (est. $0.98 EPS), (Buy, PT $260) are data, not
    definitions. Counting them would recreate the old false-positive noise."""
    from ai_analysis.voice_rules import count_inline_glosses
    for s in ("Goldman raised 2026 capex to $755B (Goldman).",
              "$SMCI reports Tuesday (est. $0.98 EPS on $11.78B revenue).",
              "Deutsche Bank $DASH (Buy, PT $260) after the beat.",
              "Payrolls printed -23k (Deutsche Bank)."):
        if count_inline_glosses(s):
            _fail(f"attribution counted as a gloss: {s}")
    _ok("attribution and data parentheticals are not counted")


def test_lint_emits_gloss_density_as_soft():
    import scripts.pulse_lint as pl
    if "gloss-density" not in pl.SOFT_ISSUE_KINDS:
        _fail("gloss-density must be soft — the fix is a sentence rewrite, "
              "which is DRAFT/EDIT work, not a SCRUB substitution")
    issues = pl._check_gloss_density(
        "## 2. THE MAIN EVENT\n\n" + GLOSSY)
    if not any(i["kind"] == "gloss-density" for i in issues):
        _fail("lint did not flag gloss-heavy prose")
    if pl._check_gloss_density("## 1. RECAP\n\n" + CLEAN):
        _fail("lint flagged clean prose")
    _ok("lint emits gloss-density as a soft issue on glossy prose only")


if __name__ == '__main__':
    test_voice_block_reaches_draft_and_audit()
    test_rewrite_outranks_gloss()
    test_no_live_prompt_calls_glossing_the_right_pattern()
    test_worked_examples_obey_the_punctuation_ban()
    test_jargon_presence_check_is_retired()
    test_gloss_density_separates_clean_from_glossy()
    test_attribution_parentheticals_are_not_glosses()
    test_lint_emits_gloss_density_as_soft()
    print("\nAll voice-contract wiring smoke tests passed.")
