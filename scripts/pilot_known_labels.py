#!/usr/bin/env python3
"""The running label list a reader sees before it labels a document.

Metric 1 measured 39-60% fragmented card mass through 2026-09-09 and
only 4 points of it were wording variants; the rest was readers that
never saw each other's labels coining a fresh one per document (median
8 labels per document against a contract of at most five). A fixed
vocabulary would not have helped: half the day's subjects are
single-note stories no list anticipates. So the vocabulary is the
ledger window's own labels, handed to each reader in turn. The reader
workflow reads documents one at a time, so every document after the
first sees the labels of every document before it.

    python scripts/pilot_known_labels.py --root pilot-data/pilot \
        --date 2026-09-10 --out /tmp/known_labels.txt [--days 1]

Writes one label per line, most-used first, with card and document
counts. Exit 0 always; an empty list is a legitimate first read.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

MAX_LINES = 300


def collect(cards_root: str, day_iso: str, days: int = 1) -> list[tuple[str, int, int]]:
    """[(label, n_cards, n_docs)] for the window, most-used first."""
    d0 = date.fromisoformat(day_iso)
    wanted = [(d0 - timedelta(days=i)).isoformat() for i in range(days + 1)]
    cards_n: dict[str, int] = defaultdict(int)
    docs: dict[str, set] = defaultdict(set)
    for day in wanted:
        for path in sorted(glob.glob(os.path.join(cards_root, day, "*.json"))):
            try:
                doc = json.loads(open(path, encoding="utf-8").read())
            except Exception:
                continue
            for c in doc.get("cards") or []:
                if not isinstance(c, dict):
                    continue
                label = " ".join((c.get("topic") or "").split()).strip()
                if not label:
                    continue
                cards_n[label] += 1
                docs[label].add(os.path.basename(path))
    rows = [(lab, n, len(docs[lab])) for lab, n in cards_n.items()]
    rows.sort(key=lambda r: (-r[2], -r[1], r[0].lower()))
    return rows[:MAX_LINES]


def render(rows: list[tuple[str, int, int]]) -> str:
    if not rows:
        return "(none yet: this is the first document in the window)\n"
    return "".join(f"{lab}  [{n} card{'s' if n != 1 else ''}, {k} doc{'s' if k != 1 else ''}]\n"
                   for lab, n, k in rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="pilot root holding cards/")
    ap.add_argument("--date", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=1,
                    help="days back to include besides --date (default 1: yesterday)")
    a = ap.parse_args()
    rows = collect(os.path.join(a.root, "cards"), a.date, a.days)
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(render(rows))
    print(f"known labels: {len(rows)} for {a.date} (+{a.days} day back)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
