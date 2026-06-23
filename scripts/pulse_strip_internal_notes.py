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


# Matches ONE `## _` section (underscore-prefixed = internal), bounded by
# the next `## ` header of ANY kind (or end of file). Matching each
# internal section separately — rather than greedily merging consecutive
# ones — is what lets the `keep` exemption decide per-section (e.g. strip
# `## _DRAFT NOTES` but keep `## _LEANS` even when they sit adjacent).
_INTERNAL_SECTION_RE = re.compile(
    r"\n*## _[^\n]*\n.*?(?=\n## |\Z)",
    flags=re.DOTALL,
)


def strip_internal_notes(text: str, keep: tuple = ("_LEANS",)) -> str:
    """Return `text` with all `## _...` sections removed, EXCEPT any whose
    header name is in `keep`.

    `_LEANS` is kept by default: it's the TRADE BOARD's structural source
    (2026-06-23), emitted by DRAFT and consumed by the bridge worker at
    post-time, which strips it AFTER building the board and BEFORE the
    pulse reaches Discord. So the routine must leave it in the committed
    pulse; the bridge does the final strip.

    Trailing whitespace is normalized — the output always ends with a single
    newline. Returns the input unchanged when no internal sections are found.
    """
    keep_upper = tuple(k.upper().lstrip("_") for k in keep)

    def _repl(m: "re.Match") -> str:
        hm = re.match(r"\n*## _(\S+)", m.group(0))
        name = hm.group(1).upper() if hm else ""
        return m.group(0) if name in keep_upper else "\n"

    cleaned = _INTERNAL_SECTION_RE.sub(_repl, text)
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
