#!/usr/bin/env python3
"""Turn a reader agent's raw stdout into a cards file, or fail loudly.

The reader's contract is STRICT JSON, but an agent occasionally wraps
it in a fence or a sentence. Salvaging that is worth doing HERE, in
deterministic code, rather than asking the prompt to try harder: a
parse failure costs the document its entire read, and the pilot's
reader-failure rate is metric 5.

What this does NOT do is repair the artifact's CONTENT. A missing
brief or a non-list cards field is a failed read, reported as such —
inventing structure would put unverified material into the ledger,
which is the one thing the pilot's design forbids.

Stamps provenance (plan 3.7): requested model, returned version when
the agent reported one, and the prompt hash, so the freeze rule is
auditable after the fact instead of a promise.

    python scripts/pilot_finalize_read.py --raw OUT.txt --out CARDS.json \
        --source TEXT.txt --tier top --model claude-opus-5 \
        --prompt docs/superpowers/routines/pilot/reader.md
Exit 0 on success, 1 on unusable output.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_config import provenance  # noqa: E402


def extract_json(raw: str) -> dict | None:
    """The agent's artifact, however it wrapped it."""
    text = (raw or "").strip()
    if not text:
        return None
    # 1. clean JSON
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except Exception:
        pass
    # 2. fenced block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            d = json.loads(m.group(1))
            return d if isinstance(d, dict) else None
        except Exception:
            pass
    # 3. widest brace span
    i, j = text.find("{"), text.rfind("}")
    if 0 <= i < j:
        try:
            d = json.loads(text[i:j + 1])
            return d if isinstance(d, dict) else None
        except Exception:
            pass
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--source", required=True)
    ap.add_argument("--tier", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt", required=True)
    args = ap.parse_args()

    raw = open(args.raw, encoding="utf-8", errors="replace").read()
    doc = extract_json(raw)
    if doc is None:
        print("FAIL: reader output is not parseable JSON")
        return 1

    brief = (doc.get("brief") or "").strip()
    cards = doc.get("cards")
    if not brief or not isinstance(cards, list):
        # Structure, not content, is what makes this unusable: an
        # EMPTY cards list is legitimate (an admin note has no
        # checkable claims), a missing brief or a non-list is not.
        print(f"FAIL: brief={'present' if brief else 'MISSING'}, "
              f"cards={type(cards).__name__}")
        return 1

    ver = None
    m = re.search(r"model[_ ]version[\"':\s]+([A-Za-z0-9._-]+)", raw)
    if m:
        ver = m.group(1)

    # Carry the re-ask counter across the rewrite. The re-ask path calls
    # this script again over the same path, and a fresh document with no
    # `verify` block reset the counter to 0, so `verify.reasked` read 0
    # for every document whether or not it was re-asked, which is the
    # one thing the field exists to record (2026-09-03 review).
    prior_reasked = 0
    try:
        with open(args.out, encoding="utf-8") as fh:
            prior_reasked = int(
                (json.load(fh).get("verify") or {}).get("reasked") or 0)
    except Exception:
        pass

    out = {
        "brief": brief,
        "cards": cards,
        "reader_tier": args.tier,
        "source_text_path": args.source,
        "provenance": provenance(
            args.model, ver,
            open(args.prompt, encoding="utf-8").read()),
    }
    if prior_reasked:
        out["verify"] = {"reasked": prior_reasked}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"OK: brief {len(brief.split())} words, {len(cards)} card(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
