"""Smoke: pulse_driver gates, budgets, and the preflight choke point.

Drives the REAL pulse_draft_validate and pulse_lint through the driver
against crafted fixtures in a temp dir. Asserts the decision tokens,
the re-roll/SCRUB budgets, the deterministic _LEANS restore, the
consensus-amnesia FIXUP path with recheck enforcement, and — the point
of the whole thing — that preflight BLOCKS when a gate was skipped and
PASSES only on a complete gate trail.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def drv(tmp, *args):
    import os
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(
        [sys.executable, "scripts/pulse_driver.py", "--tmp", str(tmp),
         *args],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=str(REPO), env=env,
    )
    out = (p.stdout or "") + (p.stderr or "")
    dec = ""
    for ln in out.splitlines():
        if ln.startswith("DECISION: "):
            dec = ln[len("DECISION: "):].split(" -- ")[0].strip()
    return dec, out, p.returncode


LEANS = "## _LEANS\n- long | $SPY | buyers in control\n"

DRAFT_OK = (
    "# Headline\n\n## 1. RECAP\n\nlede paragraph here.\n\n"
    "- **Walmart reported $0.81 a share against the $0.75 consensus, "
    "a clean beat.**\n\n"
    "## 2. INSIGHTS & ALPHA\n\n### Lead theme\n\n"
    + ("word " * 300) + "\n\n" + LEANS
)

FINAL_OK = (
    "# Headline\n\n## 1. RECAP\n\nlede paragraph here.\n\n"
    "- **Walmart reported $0.81 a share against the $0.75 consensus, "
    "a clean beat.**\n\n"
    "## 2. THE MAIN EVENT\n\n### Lead theme\n\n" + ("word " * 350)
    + "\n\n## 3. BRIEFS\n\n### One brief\n\n" + ("word " * 140)
    + "\n\n## 4. WHAT TO WATCH\n\n- watch this\n\n" + LEANS
)

CTX = {
    "today": "2026-08-20",
    "theme_map": {},
    "prev_consensus_block": (
        "[from your own pulse dated 2026-08-19]\n"
        "- Thursday before the open: Walmart. Consensus is $0.75 a "
        "share on $188.79bn of revenue."
    ),
}


def _seed(tmp, draft=DRAFT_OK, final=FINAL_OK, ctx=CTX):
    (tmp / "ctx.json").write_text(json.dumps(ctx), encoding="utf-8")
    (tmp / "draft.md").write_text(draft, encoding="utf-8")
    (tmp / "final.md").write_text(final, encoding="utf-8")


def test_holiday_skip_blocks_commit():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(tmp)
        (tmp / "holiday_skip.txt").write_text("Labor Day")
        dec, _, _ = drv(tmp, "gate", "holiday")
        assert dec == "SKIP_PULSE", dec
        dec, out, code = drv(tmp, "preflight")
        assert dec == "BLOCK" and code == 3, (dec, code)
        assert "SKIP_PULSE" in out
    _ok("holiday: SKIP_PULSE and preflight refuses the commit")


def test_happy_path_and_preflight_pass():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(tmp)
        for g in ("holiday", "volume", "draft_validate", "lint"):
            dec, out, _ = drv(tmp, "gate", g)
            assert dec in ("CONTINUE", "SKIP_SCRUB"), (g, dec, out)
        dec, out, _ = drv(tmp, "gate", "final_validate")
        assert dec == "CONTINUE", (dec, out)
        dec, _, _ = drv(tmp, "gate", "strip")
        assert dec == "CONTINUE", dec
        dec, out, code = drv(tmp, "preflight")
        assert dec == "PASS" and code == 0, (dec, out)
    _ok("happy path: all gates CONTINUE, preflight PASS exit 0")


def test_skipped_gate_detected():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(tmp)
        for g in ("holiday", "volume", "draft_validate"):
            drv(tmp, "gate", g)
        # lint, final_validate, strip never consulted
        dec, out, code = drv(tmp, "preflight")
        assert dec == "BLOCK" and code == 3, (dec, out)
        assert "gate never consulted: lint" in out, out
    _ok("skipped gate: preflight BLOCKS and names the missing gate")


def test_draft_reroll_budget():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # draft with NO _LEANS block -> leans-block-missing (hard)
        _seed(tmp, draft=DRAFT_OK.replace(LEANS, ""))
        dec, out, _ = drv(tmp, "gate", "draft_validate")
        assert dec == "REROLL_DRAFT", (dec, out)
        assert (tmp / "draft_reroll_feedback.txt").exists()
        fb = (tmp / "draft_reroll_feedback.txt").read_text()
        assert "leans-block-missing" in fb, fb
        dec, _, _ = drv(tmp, "gate", "draft_validate")
        assert dec == "REROLL_DRAFT", "second re-roll within budget"
        dec, out, _ = drv(tmp, "gate", "draft_validate")
        assert dec == "CONTINUE" and "budget spent" in out, (dec, out)
    _ok("draft_validate: 2 re-rolls with feedback file, then ships")


def test_leans_restore_in_final_validate():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # draft HAS leans; final lost them (the STITCH-truncation class)
        _seed(tmp, final=FINAL_OK.replace(LEANS, ""))
        drv(tmp, "gate", "draft_validate")
        dec, out, _ = drv(tmp, "gate", "final_validate")
        assert dec == "CONTINUE", (dec, out)
        assert "RESTORED" in out, out
        assert "## _LEANS" in (tmp / "final.md").read_text(
            encoding="utf-8")
    _ok("final_validate: deleted _LEANS deterministically restored")


def test_fixup_path_and_recheck_enforcement():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # final DENIES a consensus the ledger carries -> consensus-amnesia
        bad_final = FINAL_OK.replace(
            "against the $0.75 consensus, a clean beat",
            "this morning. No consensus figures were posted")
        _seed(tmp, final=bad_final)
        for g in ("holiday", "volume", "draft_validate", "lint"):
            drv(tmp, "gate", g)
        dec, out, _ = drv(tmp, "gate", "final_validate")
        assert dec == "DISPATCH_FIXUP", (dec, out)
        assert (tmp / "final_fixup_violations.json").exists()
        # preflight must BLOCK: FIXUP dispatched but recheck never ran
        dec, out, _ = drv(tmp, "preflight")
        assert dec == "BLOCK" and "recheck never ran" in out, out
        # apply the "fix" and recheck
        (tmp / "final.md").write_text(FINAL_OK, encoding="utf-8")
        dec, _, _ = drv(tmp, "gate", "final_validate", "--recheck")
        assert dec == "CONTINUE", dec
        drv(tmp, "gate", "strip")
        dec, out, _ = drv(tmp, "preflight")
        assert dec == "PASS", out
    _ok("fixup: dispatch, recheck-enforced preflight, then PASS")


def test_scrub_relint_required_when_dispatched():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # a semicolon in prose = hard lint -> DISPATCH_SCRUB
        _seed(tmp, final=FINAL_OK.replace(
            "lede paragraph here.", "lede; with a semicolon."))
        for g in ("holiday", "volume", "draft_validate"):
            drv(tmp, "gate", g)
        dec, out, _ = drv(tmp, "gate", "lint")
        assert dec == "DISPATCH_SCRUB", (dec, out)
        drv(tmp, "gate", "final_validate")
        drv(tmp, "gate", "strip")
        dec, out, _ = drv(tmp, "preflight")
        assert dec == "BLOCK" and "scrub_relint" in out, out
        # simulate the SCRUB fix, then the relint gate
        (tmp / "final.md").write_text(FINAL_OK, encoding="utf-8")
        dec, out, _ = drv(tmp, "gate", "scrub_relint")
        assert dec == "CONTINUE" and "0 hard" in out, (dec, out)
        dec, _, _ = drv(tmp, "preflight")
        assert dec == "PASS", "trail complete after relint"
    _ok("scrub: dispatch -> preflight blocks until relint gate runs")


def test_strip_removes_internal_notes():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _seed(tmp, final=FINAL_OK + "\n## _EDIT NOTES\nsecret\n")
        dec, out, _ = drv(tmp, "gate", "strip")
        assert dec == "CONTINUE", (dec, out)
        md = (tmp / "final.md").read_text(encoding="utf-8")
        assert "_EDIT NOTES" not in md, "strip must remove the section"
    _ok("strip: internal-notes section removed, gate recorded")


if __name__ == "__main__":
    print("=== pulse driver smoke ===")
    test_holiday_skip_blocks_commit()
    test_happy_path_and_preflight_pass()
    test_skipped_gate_detected()
    test_draft_reroll_budget()
    test_leans_restore_in_final_validate()
    test_fixup_path_and_recheck_enforcement()
    test_scrub_relint_required_when_dispatched()
    test_strip_removes_internal_notes()
    print("\nALL PULSE DRIVER SMOKE TESTS PASS")
