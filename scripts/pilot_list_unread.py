#!/usr/bin/env python3
"""List pilot source documents that have no cards file yet.

The reader workflow's skip-fast step: with ~10 runs a day and ~19
documents, most runs have nothing to do and should cost a minute.
Unread is defined structurally (a source-text file whose matching
cards file is absent) rather than by a cursor, so a crashed run,
a deleted cards file, or a re-run all self-heal without state.

    python scripts/pilot_list_unread.py --root pulse-data/pilot \
        --out /tmp/unread.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_config import reader_tier  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    out = []
    pattern = os.path.join(args.root, "source-text", "*", "*.txt")
    for text_path in sorted(glob.glob(pattern)):
        date = os.path.basename(os.path.dirname(text_path))
        doc_id = os.path.basename(text_path).split("__")[0]
        cards_path = os.path.join(args.root, "cards", date,
                                  f"{doc_id}.json")
        if os.path.exists(cards_path):
            continue
        source = ""
        meta_path = os.path.join(os.path.dirname(text_path),
                                 f"{doc_id}.meta.json")
        try:
            source = (json.loads(open(meta_path, encoding="utf-8").read())
                      .get("source") or "")
        except Exception:
            pass
        tier, model = reader_tier(source)
        out.append({"id": doc_id, "date": date, "text_path": text_path,
                    "source": source, "tier": tier, "model": model})

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"{len(out)} unread document(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
