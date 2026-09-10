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
from scripts.pilot_config import MACRO_KEYS, MAX_LABELS_PER_DOC  # noqa: E402


def _label(c: dict) -> str:
    return " ".join((c.get("topic") or "").split()).strip().lower()


def check_labels(cards: list) -> dict:
    """The label contract (spec §4 amendment, 2026-09-10): at most
    MAX_LABELS_PER_DOC distinct `topic` labels per document, and a
    closed-set `macro_key` on every card with no instrument. Returns
    counts plus the re-ask text the workflow shows the reader; an empty
    `reask` means the document conforms."""
    labels: dict[str, int] = {}
    bad_macro: list[str] = []
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        lab = _label(c)
        if lab:
            labels[lab] = labels.get(lab, 0) + 1
        if not (c.get("instruments") or []):
            mk = str(c.get("macro_key") or "").strip().upper()
            if mk not in MACRO_KEYS:
                bad_macro.append((c.get("claim") or "")[:90])
    over = max(0, len(labels) - MAX_LABELS_PER_DOC)
    parts = []
    if over:
        listed = ", ".join(f"{k} ({n})" for k, n in
                           sorted(labels.items(), key=lambda kv: -kv[1]))
        parts.append(
            f"This document uses {len(labels)} topic labels; the contract "
            f"is at most {MAX_LABELS_PER_DOC}. Fold them to the pulse-theme "
            f"subjects the note actually argues (reuse a KNOWN_LABELS entry "
            f"where one fits) and relabel every card. Labels used: {listed}.")
    if bad_macro:
        parts.append(
            f"{len(bad_macro)} card(s) with no instrument carry no valid "
            f"macro_key. Set one of {' '.join(MACRO_KEYS)} on each: "
            + " | ".join(bad_macro[:8]))
    return {"label_count": len(labels), "labels_over_cap": over,
            "macro_key_invalid": len(bad_macro), "reask": "\n".join(parts)}


def coerce_macro_keys(cards: list) -> int:
    """Final pass: a macro card whose key is still not in the set gets
    OTHER, so the ledger's hard key is always well-formed. Returns the
    number coerced (recorded in the verify block)."""
    n = 0
    for c in cards or []:
        if not isinstance(c, dict):
            continue
        if c.get("instruments") or []:
            c["macro_key"] = ""
            continue
        mk = str(c.get("macro_key") or "").strip().upper()
        if mk not in MACRO_KEYS:
            c["macro_key"] = "OTHER"
            n += 1
        else:
            c["macro_key"] = mk
    return n


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
    labels = check_labels(doc.get("cards") or [])
    coerced = coerce_macro_keys(ok + failed) if args.final else 0

    prev = (doc.get("verify") or {}).get("reasked", 0)
    needs = (bool(failed) or bool(labels["reask"])) and not args.final
    doc["verify"] = {
        **stats,
        "label_count": labels["label_count"],
        "labels_over_cap": labels["labels_over_cap"],
        "macro_key_invalid": labels["macro_key_invalid"],
        "macro_key_coerced": coerced,
        "reasked": prev + (1 if needs else 0),
        "dropped": len(failed) if args.final else 0,
        "needs_reask": needs,
    }
    doc["cards"] = ok if args.final else ok + failed

    with open(args.cards_json, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)

    if args.reask_out and needs:
        with open(args.reask_out, "w", encoding="utf-8") as fh:
            json.dump({"failed_cards": failed,
                       "label_contract": labels["reask"]}, fh, indent=1)

    verdict = "FINAL" if args.final else "PASS1"
    print(f"{verdict}: {stats['matched']}/{stats['total']} verified, "
          f"{stats['failed']} failed"
          f"{' (dropped)' if args.final else ' (re-ask pending)'}"
          f"; {labels['label_count']} labels"
          f"{' (' + str(labels['labels_over_cap']) + ' over cap)' if labels['labels_over_cap'] else ''}"
          f"{', ' + str(coerced) + ' macro_key coerced' if coerced else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
