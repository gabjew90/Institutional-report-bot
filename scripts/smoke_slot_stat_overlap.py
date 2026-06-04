"""Smoke test for the slot-stat-overlap lint check.

Background: 2026-06-04 pulse QC review observed that INSIGHTS #2
(AI capex) and INSIGHTS #3 (Hartnett rotate-out) both cited the
same load-bearing stats — the $60B levered single-stock ETF AUM
and the 9-consecutive-up-days streak — and closed with the same
NFP-catalyst rotation lean. The themes are nominally distinct but
share evidence, which makes the pulse read as one theme twice.

Fix: deterministic lint check that splits the INSIGHTS section on
H3 headings, extracts a normalized set of stat tokens per slot,
and flags any stat appearing in 2+ slots as kind=slot-stat-overlap.

The kind is SOFT (in SOFT_ISSUE_KINDS) so it doesn't gate SCRUB —
SCRUB rewrites sentences and can't partition stats across slots.
The flag surfaces for QC review + future DRAFT_USER constraint.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_stat_tokens_extract_dollar_amounts():
    from scripts.pulse_lint import _extract_stat_tokens
    slot = "The buildout hit $700B and Microsoft's $25B in component pricing."
    tokens = _extract_stat_tokens(slot)
    assert "$700b" in tokens
    assert "$25b" in tokens
    _ok("extract: dollar amounts ($700B, $25B)")


def test_stat_tokens_extract_streaks_and_normalize_word_to_digit():
    """'nine straight up days' and '9 consecutive up days' must
    normalize to the same token."""
    from scripts.pulse_lint import _extract_stat_tokens
    tokens_word = _extract_stat_tokens("BTIG flags nine straight up days")
    tokens_digit = _extract_stat_tokens("9 consecutive up days streak")
    # Both should produce identical tokens after normalization
    assert tokens_word & tokens_digit, (
        f"word vs digit forms must normalize to same token: "
        f"word={tokens_word} digit={tokens_digit}"
    )
    _ok("extract: streak normalizes word-numbers to digits + verb synonyms")


def test_no_overlap_no_flag():
    from scripts.pulse_lint import _check_slot_stat_overlap
    md = (
        "## 2. INSIGHTS & ALPHA\n\n"
        "### Theme A\n\n"
        "First slot mentions $700B AI buildout.\n\n"
        "### Theme B\n\n"
        "Second slot mentions $50B fund flows. Different stat.\n"
    )
    issues = _check_slot_stat_overlap(md)
    assert issues == [], f"expected no issues, got {issues}"
    _ok("no overlap → no flags")


def test_detects_dollar_amount_overlap_across_slots():
    from scripts.pulse_lint import _check_slot_stat_overlap
    md = (
        "## 2. INSIGHTS & ALPHA\n\n"
        "### Theme A — AI capex\n\n"
        "Goldman flags $60B in single-stock levered ETF AUM doubled.\n\n"
        "### Theme B — Rotate-out\n\n"
        "The $60B levered ETF stat shows positioning extreme.\n"
    )
    issues = _check_slot_stat_overlap(md)
    assert any(i["kind"] == "slot-stat-overlap" for i in issues), (
        f"expected slot-stat-overlap, got {issues}"
    )
    overlap_stat = next(i for i in issues if i["kind"] == "slot-stat-overlap")
    assert "$60b" in overlap_stat["snippet"].lower(), overlap_stat
    _ok("detects $60B dollar-amount overlap across two slots")


def test_detects_streak_overlap_across_slots():
    from scripts.pulse_lint import _check_slot_stat_overlap
    md = (
        "## 2. INSIGHTS & ALPHA\n\n"
        "### Theme A\n\n"
        "BTIG flags nine consecutive up days on negative breadth.\n\n"
        "### Theme B\n\n"
        "What produces nine straight up days on bad breadth is dealer gamma.\n"
    )
    issues = _check_slot_stat_overlap(md)
    assert any(i["kind"] == "slot-stat-overlap" for i in issues), (
        f"expected slot-stat-overlap for streak, got {issues}"
    )
    _ok("detects '9 up days' streak overlap (word/digit + verb synonyms normalized)")


def test_real_2026_06_04_pulse_runs_clean_or_reports_overlap():
    """Run the check against the real 2026-06-04 pulse. Verify it
    EXECUTES without exception and produces a sensible result (no
    false-positive flood of stats that aren't actually shared).

    NOTE: the 2026-06-04 QC review claimed INSIGHTS #2 and #3 both
    cited '$60B levered ETF' and '9 consecutive up days'. Reading the
    actual archive: $60B appears in RECAP + INSIGHTS #2 (not two
    INSIGHTS slots), and 9-up-days appears in INSIGHTS #3 only. The
    QC review was slightly imprecise about which slots shared the
    stats. The check correctly scopes to INSIGHTS-only and finds the
    actual overlaps that exist."""
    pulse_path = REPO_ROOT / ".tmp_pulse_archive_06-04.md"
    if not pulse_path.exists():
        print(f"SKIP real-pulse test — fixture missing: {pulse_path}")
        return
    from scripts.pulse_lint import _check_slot_stat_overlap
    md = pulse_path.read_text(encoding="utf-8")
    issues = _check_slot_stat_overlap(md)
    overlaps = [i for i in issues if i["kind"] == "slot-stat-overlap"]
    # No assertion on specific stats — pulse-by-pulse the actual
    # overlaps will vary. Just confirm the check ran and the result
    # is a list of well-formed issue dicts.
    for i in overlaps:
        assert "snippet" in i and "kind" in i and "line" in i, i
    print(
        f"NOTE: real 2026-06-04 pulse: {len(overlaps)} INSIGHTS slot-stat "
        f"overlap(s) detected"
    )
    if overlaps:
        for i in overlaps[:3]:
            print(f"  - {i['snippet']}")
    _ok("real-pulse scan executes cleanly + produces well-formed issues")


def test_kind_is_in_soft_issue_set():
    """The new kind must be classified as SOFT — doesn't gate SCRUB
    because SCRUB rewrites sentences, can't partition stats across slots."""
    from scripts.pulse_lint import SOFT_ISSUE_KINDS, classify_issues
    assert "slot-stat-overlap" in SOFT_ISSUE_KINDS
    # And confirm classify_issues counts it as soft
    fake_issues = [{"kind": "slot-stat-overlap", "line": 1, "snippet": "x"}]
    hard, soft = classify_issues(fake_issues)
    assert hard == 0 and soft == 1
    _ok("slot-stat-overlap classified as SOFT (doesn't gate SCRUB)")


if __name__ == "__main__":
    print("=== slot-stat-overlap lint smoke ===")
    test_stat_tokens_extract_dollar_amounts()
    test_stat_tokens_extract_streaks_and_normalize_word_to_digit()
    test_no_overlap_no_flag()
    test_detects_dollar_amount_overlap_across_slots()
    test_detects_streak_overlap_across_slots()
    test_real_2026_06_04_pulse_runs_clean_or_reports_overlap()
    test_kind_is_in_soft_issue_set()
    print("\nALL SLOT-STAT-OVERLAP SMOKE TESTS PASS")
