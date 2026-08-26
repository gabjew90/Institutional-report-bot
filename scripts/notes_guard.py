#!/usr/bin/env python3
"""One post-write assertion for NOTES.md. Import it; don't re-invent it.

WHY THIS EXISTS
===============
On 2026-08-26 a NOTES.md edit used slice-deletion between two anchors and
removed everything between them — which included the DETERMINISTIC FIRST
section and the SESSION TEMPLATE, both explicitly requested, both gone
without a word. Nothing checked the file afterward.

The same check was then written three separate times, in three ad-hoc
forms, in three different scripts. Three copies of a check is zero
checks: each one drifts, and none of them is the one that runs.

This is that check, once.

CONTRACT
========
Any script that writes NOTES.md calls `assert_notes_intact()` AFTER
writing and before exiting. It raises `NotesDamaged` on a missing
section — loudly, because a silent NOTES edit is what this file exists
to prevent.

It is ALSO wired into `scripts/preflight_push.py`, so a script that
forgets to call it is still caught before the damage is pushed. Belt and
braces, deliberately: STANDING RULE 3 says a check that cannot report is
not a check, and a check nobody calls cannot report.

    from scripts.notes_guard import assert_notes_intact
    Path("NOTES.md").write_text(new_text, encoding="utf-8")
    assert_notes_intact()

    python scripts/notes_guard.py       # standalone verdict
"""
from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOTES = os.path.join(REPO, "NOTES.md")

# Sections that must survive any edit. Each entry is (substring, why it
# matters). Add to this when a section becomes load-bearing; never remove
# an entry to make a failing check pass.
REQUIRED_SECTIONS: list[tuple[str, str]] = [
    ("## STANDING RULE 1",
     "config-artifact rule — five findings retracted under it"),
    ("## STANDING RULE 2",
     "full-suite validation rule — two wrong results without it"),
    ("## STANDING RULE 3",
     "erroring-gate rule"),
    ("## DETERMINISTIC FIRST",
     "the 0/7 vs 7/7 enforcement table, the evidence base for "
     "CLAUDE.md rule 1"),
    ("| **0 / 7** |",
     "the prose-caught-nothing row — the number itself, not the heading"),
    ("| **7 / 7** |",
     "the code-caught-everything row"),
    ("## SESSION TEMPLATE",
     "the 7 required steps for moving a rule class into code"),
    ("WIRE IT INTO THE SEND PATH",
     "template step 3 — its absence cost a class its enforcement"),
    ("RUN THE PUSH GATE",
     "template step 7 — a push auto-redeploys the live bot"),
    ("### The nine divergences",
     "the divergence table"),
    ("Where the Gemini money actually goes",
     "SKU-level cost analysis"),
]


class NotesDamaged(AssertionError):
    """Raised when a required NOTES.md section is missing."""


def missing_sections(path: str = NOTES) -> list[tuple[str, str]]:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        raise NotesDamaged(f"cannot read {path}: {e}") from e
    return [(m, why) for m, why in REQUIRED_SECTIONS if m not in text]


def assert_notes_intact(path: str = NOTES) -> None:
    """Raise if any required section is gone. Call AFTER writing."""
    gone = missing_sections(path)
    if not gone:
        return
    lines = "\n".join(f"  - {m!r}\n      needed for: {why}"
                      for m, why in gone)
    raise NotesDamaged(
        f"{len(gone)} required NOTES.md section(s) destroyed by this "
        f"edit:\n{lines}\n\n"
        f"Restore them. Do NOT delete the entry from REQUIRED_SECTIONS "
        f"to make this pass — that is the failure mode this guard was "
        f"written for."
    )


def main() -> int:
    gone = missing_sections()
    if gone:
        for m, why in gone:
            print(f"FAIL missing {m!r} ({why})")
        print(f"\n{len(gone)} of {len(REQUIRED_SECTIONS)} required "
              f"sections missing")
        return 1
    print(f"PASS all {len(REQUIRED_SECTIONS)} required NOTES.md "
          f"sections present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
