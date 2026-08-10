"""Smoke test: STITCH strips ## _DRAFT NOTES without eating the sections
that follow it.

Background: the 2026-08-07 run (and at least four before it) lost the
`## _LEANS` block between DRAFT and the published pulse. Yesterday's QC
blamed EDIT. The real cause was in pulse_stitch.py: the _DRAFT NOTES
strip matched the section with a lookahead for the next `## ` header,
then threw the match away and sliced the document to match.start() —
truncating to EOF. Everything after _DRAFT NOTES went with it, and
_LEANS sits there by contract (it is the TRADE BOARD's source). The
2026-08-07 pulse only shipped a TRADE BOARD because the orchestrator
hand-spliced the block back.

Fix: splice out the matched span only, keep the tail.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_leans_survives_draft_notes_strip():
    """The 2026-08-07 failure case: _LEANS follows _DRAFT NOTES and must
    survive the strip."""
    from scripts.pulse_stitch import stitch
    md = (
        "# Payrolls turn negative\n\n"
        "## 1. RECAP\n\nbody\n\n"
        "## 4. WHAT TO WATCH\n\n- watch this\n\n"
        "## _DRAFT NOTES\n\n- folded gold into slot 2\n- dropped rhode\n\n"
        "## _LEANS\n\n- Long $NVDA\n- Long $GLD\n"
    )
    new_md, fixes, _ = stitch(md)

    if '_DRAFT NOTES' in new_md:
        _fail("_DRAFT NOTES was not stripped")
    if '## _LEANS' not in new_md:
        _fail("_LEANS was deleted by the _DRAFT NOTES strip (the 2026-08-07 bug)")
    if 'Long $NVDA' not in new_md or 'Long $GLD' not in new_md:
        _fail("_LEANS body was truncated")
    if 'folded gold into slot 2' in new_md:
        _fail("_DRAFT NOTES body leaked into the output")
    if not any('_DRAFT NOTES' in f for f in fixes):
        _fail("strip was not reported in fixes")
    _ok("_LEANS survives the _DRAFT NOTES strip")


def test_watch_section_intact():
    """Content BEFORE _DRAFT NOTES must be untouched."""
    from scripts.pulse_stitch import stitch
    md = (
        "## 4. WHAT TO WATCH\n\n- CPI Wednesday\n\n"
        "## _DRAFT NOTES\n\n- note\n\n"
        "## _LEANS\n\n- Long $MU\n"
    )
    new_md, _, _ = stitch(md)
    if '- CPI Wednesday' not in new_md:
        _fail("WHAT TO WATCH content was lost")
    if new_md.index('## 4. WHAT TO WATCH') > new_md.index('## _LEANS'):
        _fail("section order was scrambled by the splice")
    _ok("preceding sections and section order intact")


def test_draft_notes_at_eof_still_stripped():
    """The no-tail case must behave exactly as before — strip to EOF."""
    from scripts.pulse_stitch import stitch
    md = (
        "## 1. RECAP\n\nbody\n\n"
        "## _DRAFT NOTES\n\n- folded X\n- dropped Y\n"
    )
    new_md, fixes, _ = stitch(md)
    if '_DRAFT NOTES' in new_md or 'folded X' in new_md:
        _fail("_DRAFT NOTES at EOF was not stripped")
    if 'body' not in new_md:
        _fail("body was lost")
    if not new_md.endswith('\n'):
        _fail("output does not end with a newline")
    _ok("_DRAFT NOTES at EOF strips to EOF as before")


def test_no_draft_notes_is_a_noop():
    """A DRAFT with no notes section must pass through unchanged."""
    from scripts.pulse_stitch import stitch
    md = "## 1. RECAP\n\nbody\n\n## _LEANS\n\n- Long $NVDA\n"
    new_md, fixes, _ = stitch(md)
    if '## _LEANS' not in new_md or 'Long $NVDA' not in new_md:
        _fail("_LEANS was touched when no _DRAFT NOTES existed")
    if any('_DRAFT NOTES' in f for f in fixes):
        _fail("reported a strip that did not happen")
    _ok("no-_DRAFT NOTES input is a no-op for _LEANS")


if __name__ == '__main__':
    test_leans_survives_draft_notes_strip()
    test_watch_section_intact()
    test_draft_notes_at_eof_still_stripped()
    test_no_draft_notes_is_a_noop()
    print("\nAll _DRAFT NOTES span-strip smoke tests passed.")
