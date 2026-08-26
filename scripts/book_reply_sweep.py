#!/usr/bin/env python3
"""Sweep the book-correction parser over the real caller message corpus.

WHY
===
The parser writes a `close` into the trade log. A false positive
silently deletes a live position from a caller's book -- the exact
failure this feature exists to fix, in the opposite direction. Twenty
hand-picked unit tests do not measure that; they measure the twenty
shapes I thought of.

So: run it over every message the two registered callers actually
posted, with a DELIBERATELY WIDE ticker list (far wider than any real
book), and read every hit. A wide list is the point -- it inflates the
false-positive rate on purpose, so anything that survives is a shape
worth looking at.

Each hit prints in full for review. This is not pass/fail; it is the
read-every-flag step from the validator sweeps.

USAGE
=====
    python scripts/book_reply_sweep.py <corpus.json> [--all]
    corpus.json: [{"u": username, "t": ts, "c": content}, ...]
"""
from __future__ import annotations

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from analyst_log.book_reply import parse_exit_corrections  # noqa: E402

# Far wider than any real book. Every ticker either caller has plausibly
# held, plus common chatter names, so the sweep over-triggers by design.
WIDE = [
    "SPXW", "SPX", "SPY", "QQQ", "MU", "AVGO", "SMCI", "NBIS", "SNDK",
    "HOOD", "SOXL", "NVDA", "TSLA", "AMD", "INTC", "ARM", "QCOM", "TSM",
    "RKLB", "ORCL", "COIN", "CSCO", "AAOI", "CRCL", "GOOGL", "RDDT",
    "CRWD", "META", "AAPL", "MSFT", "AMZN", "NFLX", "PLTR", "BTC",
    "ETH", "SOL", "IWM", "GLD", "TLT", "VIX", "UBER", "DELL", "MRVL",
]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    show_all = "--all" in sys.argv
    with open(sys.argv[1], encoding="utf-8") as fh:
        corpus = json.load(fh)

    hits = []
    for rec in corpus:
        text = (rec.get("c") or "").strip()
        if not text:
            continue
        got = parse_exit_corrections(text, WIDE)
        if got:
            hits.append((rec.get("u"), rec.get("t"), got, text))

    print(f"=== book-correction sweep over {len(corpus)} caller messages ===")
    print(f"ticker list: {len(WIDE)} symbols (deliberately wide)\n")
    print(f"fired on {len(hits)} messages "
          f"({100.0 * len(hits) / max(1, len(corpus)):.2f}%)\n")

    limit = len(hits) if show_all else 60
    for u, t, got, text in hits[:limit]:
        flat = re.sub(r"\s+", " ", text)[:150]
        print(f"  {t}  {u:<14} {','.join(got):<16} {flat!r}")
    if len(hits) > limit:
        print(f"  ... {len(hits) - limit} more (pass --all)")

    print("\nRead every line above. A hit on a message that is NOT an exit "
          "is a false positive, and it would silently close a live "
          "position.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
