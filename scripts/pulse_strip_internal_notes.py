#!/usr/bin/env python3
"""Strip internal-notes sections from a pulse markdown before publish.

The EDIT and DRAFT sub-agents emit `## _DRAFT NOTES` and `## _EDIT NOTES`
sections containing editorial-decision metadata (which themes were dropped
and why, which adjudication entries were folded together, etc.). These are
useful for the QC reviewer to see editorial intent but MUST NOT reach the
published pulse. They were leaking through because the synthesis routine
had no explicit strip step.

Identifying internal sections: any H2 header whose text starts with `_`
immediately after the `## ` marker — e.g. `## _DRAFT NOTES`, `## _EDIT NOTES`.
Normal sections like `## 1. RECAP` or `## 2. INSIGHTS & ALPHA` are preserved.

Idempotent: running on an already-stripped pulse produces no change and
returns 0. Safe to run multiple times.

Called by `docs/superpowers/routines/synthesis-routine.md` STEP 5.8 between
SCRUB and the final commit.
"""

import re
import sys


# Matches a section that starts with `## _` (the underscore-prefixed header
# indicates internal) and consumes through the next non-internal `## ` header
# OR end of file. The lookahead `(?=\n## (?!_)|\Z)` is the key — it stops at
# the next regular H2 without consuming it.
_INTERNAL_SECTION_RE = re.compile(
    r"\n*## _[^\n]*\n.*?(?=\n## (?!_)|\Z)",
    flags=re.DOTALL,
)


def strip_internal_notes(text: str) -> str:
    """Return `text` with all `## _...` sections removed.

    Trailing whitespace is normalized — the output always ends with a single
    newline. Returns the input unchanged when no internal sections are found.
    """
    cleaned = _INTERNAL_SECTION_RE.sub("\n", text)
    # Collapse 3+ consecutive newlines into 2, which can happen when an
    # internal section was the trailing block of the file.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.rstrip() + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: pulse_strip_internal_notes.py <pulse.md>", file=sys.stderr)
        return 2
    path = sys.argv[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            original = f.read()
    except FileNotFoundError:
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return 1
    cleaned = strip_internal_notes(original)
    if cleaned == original:
        print(f"pulse_strip_internal_notes: no internal-notes sections found in {path}")
        return 0
    with open(path, "w", encoding="utf-8") as f:
        f.write(cleaned)
    delta = len(original) - len(cleaned)
    print(f"pulse_strip_internal_notes: stripped {delta} chars from {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
