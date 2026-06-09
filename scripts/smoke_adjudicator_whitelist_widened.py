"""Smoke test: adjudicator AGENCY_WHITELIST widened to include known
Tier-2 European + APAC bank desks, preventing the recurring
`bank not in inputs or agency whitelist: 'Berenberg'` false-positive.

Background: the adjudicator (defined in docs/superpowers/routines/
synthesis-routine.md) validates each adjudicated theme by checking
that all cited `banks_for` / `banks_against` are EITHER (a) in the
corpus's theme_stances or (b) on a static AGENCY_WHITELIST. The
whitelist's original purpose was non-bank entities (IEA, BLS, OPEC,
ECB, etc.) — adding banks felt conceptually wrong.

But the recurring failure mode is real:
  - 2026-06-04 QC: Scotiabank discarded as `bank not in inputs`
    despite being in the corpus (Phase B contextual mention budget
    consumed by other banks)
  - 2026-06-08 QC: Berenberg discarded from `fed policy outlook`
    despite 4 PDFs in the corpus

Root cause is the synthetic-stance budget cap (MAX_TOTAL_STANCE_ENTRIES
= 25). When Phase A real stances fill the cap, Phase B contextual
mentions don't make it into theme_stances even if the bank IS in the
corpus. The fix is two-pronged:

1. Widen the whitelist to allow known Tier-2 desks through as a
   fallback even when they didn't land in theme_stances for this
   specific theme.
2. Reframe the whitelist as a "known-source allowlist" (agencies +
   known banks) rather than strictly agencies.

This smoke locks in the widening.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _load_routine_text():
    """Read the synthesis-routine.md text where the whitelist lives."""
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / "docs" / "superpowers" / "routines" / "synthesis-routine.md"
    return path.read_text(encoding="utf-8")


def test_routine_has_widened_whitelist_section():
    """The whitelist comment must reflect the new dual purpose: agencies
    AND known Tier-2 bank desks. Without this reframing the next dev
    sees a static 'AGENCY_WHITELIST' and won't think to add banks."""
    text = _load_routine_text()
    assert "Tier-2" in text or "tier-2" in text or "tier 2" in text, (
        "whitelist comment must reference 'Tier-2' so the next reader "
        "understands the dual purpose"
    )
    assert ("known-source allowlist" in text
            or "Known Tier-2" in text
            or "known Tier-2" in text), (
        "whitelist must explicitly call out the known-bank carve-out"
    )
    _ok("whitelist comment reframed to cover agencies + Tier-2 banks")


def test_berenberg_is_in_whitelist():
    """The specific 2026-06-08 false-positive bank must be allowlisted."""
    text = _load_routine_text()
    # Find the AGENCY_WHITELIST block
    anchor = text.find("AGENCY_WHITELIST = frozenset(")
    assert anchor != -1, "AGENCY_WHITELIST frozenset not found"
    block = text[anchor:anchor + 3500]
    assert "'Berenberg'" in block, (
        "Berenberg (the 2026-06-08 false-positive case) must be in "
        "AGENCY_WHITELIST"
    )
    _ok("Berenberg in AGENCY_WHITELIST (2026-06-08 false-positive "
        "closed)")


def test_scotiabank_is_in_whitelist():
    """The 2026-06-04 false-positive bank must also be allowlisted."""
    text = _load_routine_text()
    anchor = text.find("AGENCY_WHITELIST = frozenset(")
    block = text[anchor:anchor + 3500]
    assert "'Scotiabank'" in block, (
        "Scotiabank (the 2026-06-04 false-positive case) must be in "
        "AGENCY_WHITELIST"
    )
    _ok("Scotiabank in AGENCY_WHITELIST (2026-06-04 false-positive "
        "closed)")


def test_european_tier2_banks_added():
    """The bot's own QC review on 2026-06-08 listed 8 specific Tier-2
    European desks worth widening. All 8 must be in the whitelist —
    not as a cosmetic addition but because each has been observed in
    real corpora as a supporting voice on validated themes."""
    text = _load_routine_text()
    anchor = text.find("AGENCY_WHITELIST = frozenset(")
    block = text[anchor:anchor + 3500]
    qc_recommended = [
        "Berenberg",
        "UniCredit",
        "Mizuho International",
        "CA-CIB",      # Crédit Agricole CIB
        "ANZ",         # ANZ Research
        "SEB",
        "Lloyds",
        "Scotiabank",
        "Nordea",
    ]
    missing = [b for b in qc_recommended if f"'{b}'" not in block]
    assert not missing, (
        f"all 9 QC-recommended Tier-2 desks must be in the whitelist, "
        f"missing: {missing}"
    )
    _ok(f"all {len(qc_recommended)} QC-recommended Tier-2 desks "
        f"allowlisted")


def test_agencies_still_preserved():
    """The whitelist widening must NOT remove the original agency
    entries. Regression check that IEA / BLS / FOMC / etc. are still
    present — without this, the IEA false-positive from 2026-06-01
    could regress."""
    text = _load_routine_text()
    anchor = text.find("AGENCY_WHITELIST = frozenset(")
    block = text[anchor:anchor + 3500]
    must_keep = ["IEA", "BLS", "OPEC", "Bloomberg", "Reuters",
                 "FOMC", "ECB", "BOJ"]
    missing = [a for a in must_keep if f"'{a}'" not in block]
    assert not missing, (
        f"the original agency entries must remain (regression check), "
        f"missing: {missing}"
    )
    _ok("original 8 agency entries (IEA / BLS / OPEC / etc.) preserved")


def test_whitelist_comment_anchors_concrete_failures():
    """The whitelist comment must cite the 2026-06-04 + 2026-06-08
    failures as concrete pattern anchors so future developers understand
    WHY each bank is on the list, not just that it's allowed."""
    text = _load_routine_text()
    anchor = text.find("AGENCY_WHITELIST = frozenset(")
    # Look in the 800 chars BEFORE the frozenset (where comments live)
    comment_window = text[max(0, anchor - 2000):anchor]
    assert "2026-06-04" in comment_window
    assert "2026-06-08" in comment_window
    assert ("Berenberg" in comment_window
            or "Scotiabank" in comment_window), (
        "comment block must name at least one of the specific banks "
        "that triggered the widening"
    )
    _ok("whitelist comment anchors 2026-06-04 + 2026-06-08 failures "
        "with named banks")


if __name__ == "__main__":
    print("=== Adjudicator AGENCY_WHITELIST widening smoke ===")
    test_routine_has_widened_whitelist_section()
    test_berenberg_is_in_whitelist()
    test_scotiabank_is_in_whitelist()
    test_european_tier2_banks_added()
    test_agencies_still_preserved()
    test_whitelist_comment_anchors_concrete_failures()
    print("\nALL ADJUDICATOR WHITELIST SMOKE TESTS PASS")
