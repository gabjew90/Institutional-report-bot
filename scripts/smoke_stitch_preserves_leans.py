"""Smoke test: STITCH's `## _DRAFT NOTES` strip must not eat `## _LEANS`.

Background: 2026-07-30 scheduled pulse. DRAFT emitted, in order,
`## _DRAFT NOTES` (fold rationale for the QC reviewer) followed by
`## _LEANS` (the machine-readable TRADE BOARD source). STITCH ran, and
the EDIT sub-agent came back reporting there was no `## _LEANS` block in
the draft it received. Without that block the bridge builds an EMPTY
TRADE BOARD, so the pulse's five trade leans silently never reach the
reader.

Root cause: the strip regex was correct — `.*?(?=\\n##\\s|\\Z)` stops at
the next `## ` header, so it matched ONLY the notes section. The bug was
in how the match was applied:

    new_md = new_md[: notes_match.start()].rstrip() + '\\n'

That truncates the document at the START of the match and throws away
everything after it, including the `## _LEANS` block the regex had
deliberately excluded. `notes_match.end()` was never used.

Why nothing caught it: `scripts/pulse_draft_validate.py` has a HARD
`leans-block-missing` check, but the routine runs it at STEP 4.5 —
BEFORE STITCH. Nothing re-validates the post-STITCH artifact.

Fix: splice out only the matched span, keeping the tail.
"""

import os
import subprocess
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_FAILURES = []


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    _FAILURES.append(msg)


def _stitch(src: str) -> str:
    """Run scripts/pulse_stitch.py over `src` and return the output."""
    in_path = os.path.join(tempfile.mkdtemp(), "in.md")
    out_path = os.path.join(tempfile.mkdtemp(), "out.md")
    with open(in_path, "w") as f:
        f.write(src)
    subprocess.run(
        [sys.executable, os.path.join(_ROOT, "scripts", "pulse_stitch.py"),
         in_path, out_path],
        capture_output=True, text=True, check=True,
    )
    with open(out_path) as f:
        return f.read()


_BODY = """# Forced sellers run dry

## 3. WHAT TO WATCH

- **Tonight after the close: Apple and Amazon.** The last two prints.
"""

_NOTES = """
## _DRAFT NOTES (internal — strip before publish)

- Folded adjudicated theme "warsh fed policy" into the Fed brief.
"""

_LEANS = """
## _LEANS (internal, TRADE BOARD source, stripped before publish)
- long | $SMH call spreads | forced selling done, capex still revised up
- short | $TLT | three dissents, 30Y through 5.20%
"""


def test_notes_then_leans():
    """The regression: notes immediately followed by _LEANS."""
    out = _stitch(_BODY + _NOTES + _LEANS)
    if "_DRAFT NOTES" in out:
        _fail("notes+leans: ## _DRAFT NOTES leaked into stitched output")
    elif "## _LEANS" not in out:
        _fail("notes+leans: ## _LEANS was destroyed by the notes strip")
    elif "$SMH call spreads" not in out or "short | $TLT" not in out:
        _fail("notes+leans: _LEANS header survived but its lean lines did not")
    else:
        _ok("notes followed by _LEANS: notes stripped, every lean line preserved")


def test_notes_only():
    """No _LEANS present: notes still get stripped, nothing else changes."""
    out = _stitch(_BODY + _NOTES)
    if "_DRAFT NOTES" in out:
        _fail("notes-only: ## _DRAFT NOTES leaked into stitched output")
    elif "WHAT TO WATCH" not in out:
        _fail("notes-only: strip ate the body above the notes")
    else:
        _ok("notes with no _LEANS: notes stripped, body intact")


def test_leans_only():
    """No notes present: _LEANS must pass through untouched."""
    out = _stitch(_BODY + _LEANS)
    if "## _LEANS" not in out or "short | $TLT" not in out:
        _fail("leans-only: ## _LEANS block was altered when no notes existed")
    else:
        _ok("_LEANS with no notes: passes through untouched")


def test_watch_section_survives():
    """The content between the body and the notes must not be clipped."""
    out = _stitch(_BODY + _NOTES + _LEANS)
    if "Apple and Amazon" not in out:
        _fail("WHAT TO WATCH content was clipped by the notes strip")
    else:
        _ok("WHAT TO WATCH content above the notes survives the strip")


if __name__ == "__main__":
    test_notes_then_leans()
    test_notes_only()
    test_leans_only()
    test_watch_section_survives()
    print()
    if _FAILURES:
        print(f"{len(_FAILURES)} STITCH _LEANS-PRESERVATION SMOKE TEST(S) FAILED")
        sys.exit(1)
    print("ALL STITCH _LEANS-PRESERVATION SMOKE TESTS PASS")
