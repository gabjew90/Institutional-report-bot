"""Write the FULLY COMPOSED prompt constants to files for the routine.

Why this exists (2026-08-11)
----------------------------
STEP 1 of the synthesis routine used to say `cat ai_analysis/prompts.py`
and read the triple-quoted strings. That reads the SOURCE text, but
several prompts are only finished at Python import time:

    SCRUB_SYSTEM  <<SCRUB_REFERENCE_BLOCK>>  -> compose_scrub_reference_block()
    DRAFT_SYSTEM  <<VOICE_RULES_BLOCK>>      -> compose_audit_voice_block()
    AUDIT_SYSTEM  <<VOICE_RULES_BLOCK>>      -> compose_audit_voice_block()

A reader of the raw file gets the literal `<<PLACEHOLDER>>` token instead
of the composed block. The SCRUB one predates this script; the two voice
placeholders were added the same day and would have shipped the same way,
which would have been the second time the voice contract silently failed
to reach a model.

Run this and read the output files instead:

    python3 scripts/dump_prompts.py /tmp/prompts

It exits non-zero if any composed prompt still contains a `<<...>>`
token, so an unresolved placeholder fails the routine loudly at STEP 1
rather than silently degrading the pulse eight steps later.
"""

from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Every constant the routine consumes, in the order STEP 1 lists them.
PROMPT_NAMES = (
    "ADJUDICATION_SYSTEM",
    "ADJUDICATION_USER",
    "DRAFT_SYSTEM",
    "DRAFT_USER",
    "AUDIT_SYSTEM",
    "AUDIT_USER",
    "SCRUB_SYSTEM",
    "SCRUB_USER",
    "QC_SYSTEM",
    "QC_USER",
)

_PLACEHOLDER_RE = re.compile(r"<<[A-Z_]+>>")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: dump_prompts.py <output_dir>", file=sys.stderr)
        return 2
    outdir = argv[1]
    os.makedirs(outdir, exist_ok=True)

    import ai_analysis.prompts as prompts

    missing: list[str] = []
    unresolved: list[str] = []
    written: list[tuple[str, int]] = []

    for name in PROMPT_NAMES:
        text = getattr(prompts, name, None)
        if not isinstance(text, str):
            missing.append(name)
            continue
        found = _PLACEHOLDER_RE.findall(text)
        if found:
            unresolved.append(f"{name}: {', '.join(sorted(set(found)))}")
        path = os.path.join(outdir, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        written.append((name, len(text)))

    for name, size in written:
        print(f"  wrote {name}.txt ({size:,} chars)")

    if missing:
        print(f"\nERROR: prompts.py is missing {', '.join(missing)}",
              file=sys.stderr)
        return 1
    if unresolved:
        print("\nERROR: composed prompts still contain placeholders:",
              file=sys.stderr)
        for line in unresolved:
            print(f"  {line}", file=sys.stderr)
        print("A placeholder here means the block never reaches the model. "
              "Check the interpolation at the bottom of prompts.py.",
              file=sys.stderr)
        return 1

    print(f"\n{len(written)} prompts written to {outdir}, no unresolved "
          f"placeholders.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
