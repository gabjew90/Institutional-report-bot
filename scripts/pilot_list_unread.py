#!/usr/bin/env python3
"""List pilot source documents that have no cards file yet.

The reader workflow's skip-fast step: with ~10 runs a day and ~19
documents, most runs have nothing to do and should cost a minute.
Unread is defined structurally (a source-text file whose matching
cards file is absent) rather than by a cursor, so a crashed run,
a deleted cards file, or a re-run all self-heal without state. The one
exception is a document that has used up its read attempts
(read-failures/<date>/<id>.json, see pilot_read_failure.py): it is
reported as given up, never as unread, so one unreadable PDF cannot be
re-read on every run or void every editor day (2026-09-05).

At most MAX_PER_DAY documents per day folder are read, newest first
(owner call 2026-10-07). That day the feed delivered two days of
research at once, 72 HIGH notes at 09:00-10:00 UTC; the readers took
them oldest first, read 36 of Monday's 37 and 6 of Tuesday's 35, and
the Omnipulse missed the pulse. The readers manage about 8 notes per
90-minute run, so about 25 fit between a morning dump and the 13:55 UTC
editor. Each folder's documents are ranked newest first (the bank's day
from meta `note_date`, then arrival order by id); the top MAX_PER_DAY
are in scope whether or not they are read yet. A document below the cap
is over_cap: never read and never counted as unread, so it cannot block
the editor or the Omnipulse.

    python scripts/pilot_list_unread.py --root pulse-data/pilot \
        --out /tmp/unread.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_config import (CARDS_SUBDIR,  # noqa: E402
                                  SOURCE_TEXT_SUBDIR, reader_tier)
from scripts.pilot_read_failure import given_up  # noqa: E402

MAX_PER_DAY = 25


def _num(doc_id: str) -> int:
    return int(doc_id) if doc_id.isdigit() else 0


def _meta(text_path: str, doc_id: str) -> dict:
    path = os.path.join(os.path.dirname(text_path), f"{doc_id}.meta.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--given-up-out", default=None,
                    help="also write the ids that have used up their read attempts")
    ap.add_argument("--max-per-day", type=int, default=MAX_PER_DAY,
                    help="documents read per day folder, newest first; 0 = no cap")
    args = ap.parse_args()

    by_day: dict[str, list[dict]] = defaultdict(list)
    pattern = os.path.join(args.root, SOURCE_TEXT_SUBDIR, "*", "*.txt")
    for text_path in sorted(glob.glob(pattern)):
        date = os.path.basename(os.path.dirname(text_path))
        doc_id = os.path.basename(text_path).split("__")[0]
        meta = _meta(text_path, doc_id)
        by_day[date].append({"id": doc_id, "date": date, "text_path": text_path,
                             "source": meta.get("source") or "",
                             "note_date": meta.get("note_date") or date})

    out, gave_up, over_cap = [], [], 0
    for date, docs in by_day.items():
        docs.sort(key=lambda d: (d["note_date"], _num(d["id"])), reverse=True)
        rank = 0
        for d in docs:
            if os.path.exists(os.path.join(args.root, CARDS_SUBDIR, date, f"{d['id']}.json")):
                rank += 1         # a read note keeps its place under the cap
                continue
            if given_up(args.root, date, d["id"]):
                gave_up.append({"id": d["id"], "date": date})
                continue          # retired: reported, and takes no slot
            rank += 1
            if args.max_per_day and rank > args.max_per_day:
                over_cap += 1
                continue
            tier, model = reader_tier(d["source"])
            out.append({**d, "tier": tier, "model": model})

    out.sort(key=lambda d: (d["note_date"], _num(d["id"])), reverse=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    if args.given_up_out:
        with open(args.given_up_out, "w", encoding="utf-8") as fh:
            json.dump(gave_up, fh, indent=1)
    print(f"{len(out)} unread document(s), {len(gave_up)} given up, "
          f"{over_cap} over the {args.max_per_day or 'no'}-a-day cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
