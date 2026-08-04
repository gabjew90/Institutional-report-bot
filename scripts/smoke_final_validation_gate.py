"""Smoke: SCRUB dispatch reads the lint sidecar; final.md gets re-validated.

2026-08-04 review, B1 + B2 — the two highest-severity pipeline findings:

B1: STEP 5.7.1 re-derived the SCRUB gate inline with
    `kind != 'top-3-theme-missing'`, disagreeing with pulse_lint's OWN
    five soft kinds. Soft-only lint (bare jargon, slot overlaps that
    SCRUB structurally cannot fix) dispatched SCRUB anyway, reproducing
    the 2026-05-29 cosmetic-regression class, and the inflated count was
    substituted into SCRUB_USER, defeating its no-op gate. The
    `.decision` sidecar existed precisely to prevent this and nothing
    read it.

B2: every structural/fact check ran on /tmp/draft.md only. EDIT (which
    injects live market data — the highest fact-risk step) and SCRUB
    (a second full rewrite) both mutate the document afterwards with
    only a voice re-lint. A wrong weekday, an estimate-as-print, or a
    deleted ## _LEANS introduced post-draft shipped unchecked.
"""

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTINE = os.path.join(REPO, "docs", "superpowers", "routines",
                       "synthesis-routine.md")


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_decision_sidecar_carries_soft_kinds():
    """Consumers must not need to import pulse_lint to know which kinds
    are soft — the sidecar is the contract surface."""
    with tempfile.TemporaryDirectory() as td:
        md = os.path.join(td, "final.md")
        out = os.path.join(td, "lint.json")
        open(md, "w", encoding="utf-8").write(
            "# T\n\n## 1. RECAP\n\nplain clean text\n")
        r = subprocess.run(
            [sys.executable, os.path.join(REPO, "scripts", "pulse_lint.py"),
             md, out],
            capture_output=True, text=True, cwd=REPO)
        dec_path = out + ".decision"
        assert os.path.exists(dec_path), (
            f"decision sidecar missing (exit {r.returncode}: {r.stderr})"
        )
        dec = json.load(open(dec_path))
        assert "soft_kinds" in dec and "jargon-bare" in dec["soft_kinds"], (
            f"sidecar must publish the soft-kind list: {dec}"
        )
    _ok("lint decision sidecar publishes soft_kinds")


def test_routine_reads_sidecar_not_inline_rederivation():
    doc = open(ROUTINE, encoding="utf-8").read()
    assert "lint_report.json.decision" in doc, (
        "STEP 5.7.1 must read the .decision sidecar pulse_lint writes"
    )
    assert "!= 'top-3-theme-missing'" not in doc, (
        "the inline hard/soft re-derivation is still present — it "
        "disagrees with pulse_lint's five soft kinds and dispatches "
        "SCRUB on soft-only lint"
    )
    _ok("SCRUB gate reads the sidecar; inline re-derivation gone")


def test_scrub_receives_hard_issues_only():
    doc = open(ROUTINE, encoding="utf-8").read()
    assert "lint_hard.json" in doc, (
        "SCRUB's input must be filtered to hard issues — feeding the "
        "full report inflates {issue_count} and defeats its no-op gate"
    )
    _ok("SCRUB is fed hard issues only")


def test_final_md_revalidated_before_ship():
    doc = open(ROUTINE, encoding="utf-8").read()
    assert "pulse_draft_validate.py /tmp/final.md" in doc, (
        "no post-SCRUB structural validation — everything EDIT/SCRUB "
        "introduce ships unchecked (B2)"
    )
    i_final = doc.find("pulse_draft_validate.py /tmp/final.md")
    i_strip = doc.find("## STEP 5.8")
    assert i_strip != -1 and i_final < i_strip, (
        "final validation must run BEFORE STEP 5.8 strips ## _LEANS — "
        "the validator's leans checks need the block present"
    )
    _ok("final.md re-validated before internal-notes strip")


def test_leans_block_restored_deterministically():
    doc = open(ROUTINE, encoding="utf-8").read()
    assert "leans-block-missing" in doc.split(
        "pulse_draft_validate.py /tmp/final.md", 1)[1][:4000], (
        "a leans block deleted by EDIT/SCRUB must be spliced back from "
        "/tmp/draft.md deterministically — an empty trade board is a "
        "silent product failure"
    )
    _ok("deleted _LEANS block is restored from the draft, not shipped empty")


def test_contract_updated():
    doc = open(os.path.join(REPO, "ROUTINE_CONTRACTS.md"),
               encoding="utf-8").read()
    assert "final_validation.json" in doc or "final.md" in doc, (
        "ROUTINE_CONTRACTS.md must document the final re-validation"
    )
    _ok("contract documents final re-validation")


if __name__ == "__main__":
    print("=== final-validation gate smoke ===")
    test_decision_sidecar_carries_soft_kinds()
    test_routine_reads_sidecar_not_inline_rederivation()
    test_scrub_receives_hard_issues_only()
    test_final_md_revalidated_before_ship()
    test_leans_block_restored_deterministically()
    test_contract_updated()
    print("\nALL FINAL-VALIDATION GATE SMOKE TESTS PASS")
