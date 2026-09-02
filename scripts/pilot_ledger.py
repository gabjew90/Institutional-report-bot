#!/usr/bin/env python3
"""Build the pilot ledger from verified claim cards.

Spec section 4. Pure Python, deterministic, unit-tested — the shadow
editor consumes this output, so any nondeterminism here would show up
as editor variance and be misread as a synthesis problem.

THE STRUCTURAL RULE, which this file exists to honor
====================================================
**Grouping filters nothing.** The editor receives every card. Grouping
produces COUNTS that inform selection; it never removes a claim from
the editor's view. The spec is explicit that a filter here would hide
the fragmentation the pilot is trying to measure.

HARD vs SOFT KEYS
=================
Bank-deduplicated counts are keyed HARD on instruments and figures:
exact grouping, no fuzz. Topic labels group SOFT (normalized text)
and are expected to fragment sometimes — that fragmentation is metric
1's subject, so it must be visible rather than smoothed away. The
split exists because blocking arithmetic over soft labels gives
arithmetic's confidence with the labels' softness.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _norm_label(s: str) -> str:
    """Soft key for topic labels: case, punctuation, and filler words
    folded. Deliberately weak — the residual fragmentation is what
    metric 1 measures."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = re.sub(r"\b(the|a|an|of|in|on|for|and|to)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _figure_key(claim: str) -> tuple:
    """Hard key: the set of numeric tokens in a claim, normalized only
    by stripping separators. '$751B' and '751 bn' share a key; '$751B'
    and '$757B' never do."""
    nums = re.findall(r"\d[\d,.]*", claim or "")
    return tuple(sorted(n.replace(",", "").rstrip(".") for n in nums))


def load_cards(cards_root: str) -> list[dict]:
    """Every verified card, with its document context attached."""
    out = []
    for path in sorted(glob.glob(os.path.join(
            cards_root, "**", "*.json"), recursive=True)):
        try:
            doc = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            continue
        tier = doc.get("reader_tier") or "unknown"
        for c in doc.get("cards") or []:
            if isinstance(c, dict):
                out.append({**c, "_file": os.path.basename(path),
                            "_reader_tier": tier})
    return out


def build(cards: list[dict]) -> dict:
    """The ledger the editor reads."""
    by_instrument = defaultdict(lambda: {"for": [], "against": [],
                                         "neutral": []})
    by_figure = defaultdict(list)
    by_label = defaultdict(list)
    banks = defaultdict(int)

    for c in cards:
        bank = (c.get("bank") or "unknown").strip()
        banks[bank] += 1
        side = {"bullish": "for", "bearish": "against"}.get(
            (c.get("direction") or "").lower(), "neutral")

        # HARD key: instruments. One entry per instrument, bank-deduped
        # inside each side so five notes from one bank are one voice.
        for inst in (c.get("instruments") or []) or ["_macro"]:
            k = str(inst).strip().upper()
            bucket = by_instrument[k][side]
            if bank not in [b["bank"] for b in bucket]:
                bucket.append({"bank": bank, "claim": c.get("claim"),
                               "conviction": c.get("conviction"),
                               "status": c.get("status"),
                               "tier": c.get("_reader_tier")})

        # HARD key: figures.
        fk = _figure_key(c.get("claim") or "")
        if fk:
            by_figure[fk].append({"bank": bank, "claim": c.get("claim")})

        # SOFT key: the reader's `topic` label (added 2026-09-02 after
        # shakedown day 1 measured 48% "fragmentation" on labels derived
        # from claim text, which fragment by construction). Cards from
        # readers that predate the field fall back to the claim's
        # leading clause. Fragmentation here is the measurement, not a
        # defect.
        label = _norm_label((c.get("topic") or "").strip()
                            or (c.get("claim") or "").split(",")[0][:60])
        if label:
            by_label[label].append({"bank": bank, "claim": c.get("claim")})

    total = len(cards) or 1
    concentration = {b: round(n / total, 3)
                     for b, n in sorted(banks.items(),
                                        key=lambda kv: -kv[1])}

    return {
        "card_count": len(cards),
        "document_count": len({c["_file"] for c in cards}),
        "bank_concentration": concentration,
        "tier_counts": {
            t: sum(1 for c in cards if c.get("_reader_tier") == t)
            for t in sorted({c.get("_reader_tier") for c in cards})
        },
        "by_instrument": {
            k: {"for": v["for"], "against": v["against"],
                "neutral": v["neutral"],
                "bank_count": len({b["bank"] for s in v.values()
                                   for b in s})}
            for k, v in sorted(by_instrument.items(),
                               key=lambda kv: -len(kv[1]["for"])
                               - len(kv[1]["against"]))
        },
        "by_figure": {
            "|".join(k): v for k, v in by_figure.items() if len(v) > 1
        },
        "by_topic_label": {
            k: v for k, v in sorted(by_label.items(),
                                    key=lambda kv: -len(kv[1]))
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cards_root")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cards = load_cards(args.cards_root)
    ledger = build(cards)
    text = json.dumps(ledger, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    print(f"ledger: {ledger['card_count']} cards from "
          f"{ledger['document_count']} documents, "
          f"{len(ledger['by_instrument'])} instruments, "
          f"{len(ledger['by_topic_label'])} topic labels")
    if not args.out:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
