#!/usr/bin/env python3
"""Verify a reader's claim-card anchors against the source text.

Spec section 3: "Python validates every card anchor against the
extracted source text with normalized matching". This is that step,
run by the reader workflow immediately after each reader agent
returns, in the same session so a re-ask is cheap.

It reuses `ai_analysis.anchor_check.normalize` rather than
reimplementing the matching rules — the pulse's step-2 checker already
learned which PDF artifacts must fold (soft-wrap hyphens, ligatures,
smart quotes) and which must not (digits, ever). Two normalizers would
drift, and the pilot's fidelity numbers would stop being comparable to
the production baseline they are measured against.

CONTRACT
========
    python scripts/pilot_verify_cards.py CARDS_JSON SOURCE_TEXT [--reask-out FILE]

Rewrites CARDS_JSON in place with a `verify` block and, on the second
pass, drops the cards that still fail. Exit 0 always; the caller reads
`verify.needs_reask` to decide whether to run the re-ask round.

    pass 1 (no --final): failing cards are KEPT and listed in the
      re-ask file so the agent can correct them
    pass 2 (--final):    failing cards are DROPPED and counted
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from ai_analysis.anchor_check import (MIN_ANCHOR_CHARS,  # noqa: E402
                                      normalize)


def verify(cards: list, source_text: str) -> tuple[list, list, dict]:
    """(ok_cards, failed_cards, stats)."""
    hay = normalize(source_text or "")
    ok, failed = [], []
    stats = {"total": 0, "matched": 0, "failed": 0, "too_short": 0}
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        stats["total"] += 1
        anchor = (c.get("anchor") or "").strip()
        # Too-short anchors are a FAILURE here, not a separate bucket
        # as in the pulse checker: there the field was advisory, here
        # a card is a claim that must be traceable, and "4.4%" traces
        # to nothing in particular.
        if len(anchor) < MIN_ANCHOR_CHARS:
            stats["too_short"] += 1
            stats["failed"] += 1
            failed.append({**c, "_reason": "anchor too short to verify"})
            continue
        if normalize(anchor) in hay:
            stats["matched"] += 1
            ok.append(c)
        else:
            stats["failed"] += 1
            failed.append({**c, "_reason": "anchor not found in source"})
    return ok, failed, stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cards_json")
    ap.add_argument("source_text")
    ap.add_argument("--reask-out", default=None)
    ap.add_argument("--final", action="store_true",
                    help="second pass: drop failures instead of "
                         "keeping them for a re-ask")
    args = ap.parse_args()

    doc = json.loads(open(args.cards_json, encoding="utf-8").read())
    source = open(args.source_text, encoding="utf-8",
                  errors="replace").read()

    ok, failed, stats = verify(doc.get("cards") or [], source)

    prev = (doc.get("verify") or {}).get("reasked", 0)
    doc["verify"] = {
        **stats,
        "reasked": prev + (0 if args.final else (1 if failed else 0)),
        "dropped": len(failed) if args.final else 0,
        "needs_reask": bool(failed) and not args.final,
    }
    doc["cards"] = ok if args.final else ok + failed

    with open(args.cards_json, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)

    if args.reask_out and failed and not args.final:
        with open(args.reask_out, "w", encoding="utf-8") as fh:
            json.dump({"failed_cards": failed}, fh, indent=1)

    verdict = "FINAL" if args.final else "PASS1"
    print(f"{verdict}: {stats['matched']}/{stats['total']} verified, "
          f"{stats['failed']} failed"
          f"{' (dropped)' if args.final else ' (re-ask pending)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
