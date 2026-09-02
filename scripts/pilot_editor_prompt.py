"""Compose the shadow editor prompt: the frozen editor.md, production's
live voice contract, and today's pack.

The frozen part is editor.md (its sha rides in provenance). The voice
block is production's own `compose_audit_voice_block()`, shared by
both arms at edit time, so a voice-rule change moves both pulses the
same way and never counts as a pilot prompt change.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

EDITOR_MD = os.path.join(REPO, "docs", "superpowers", "routines", "pilot", "editor.md")


def compose(pack_md_path: str) -> str:
    from ai_analysis.voice_rules import compose_audit_voice_block
    with open(EDITOR_MD, encoding="utf-8") as fh:
        editor = fh.read()
    with open(pack_md_path, encoding="utf-8") as fh:
        pack = fh.read()
    return (editor.rstrip() + "\n\n## Voice contract (production's, live)\n\n"
            + compose_audit_voice_block().strip()
            + "\n\n---\n\n" + pack)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pack_md")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    text = compose(a.pack_md)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"editor prompt: {len(text)} chars -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
