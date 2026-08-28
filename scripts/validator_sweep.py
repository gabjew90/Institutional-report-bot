#!/usr/bin/env python3
"""Sweep every validator class against the recorded answer corpus.

WHY
===
Classes 1 and 2 were accepted on fixture pass rate alone. Class 3 was
too -- and then a sweep found it had flagged 17 answers, most of them
wrong: a joke about "$3 courthouse parking receipts", a BTC holdings
COUNT, a revenue figure, and 1,200,000 of open interest. Two of those
fixtures show a strip rung firing, so it was deleting correct sentences
while the suite pass rate went UP.

A pass rate measures what the fixtures assert. It says nothing about a
validator quietly mangling answers the fixtures never look at. This
sweep is the other half.

THE HEURISTIC
=============
Every recorded answer in docs/ask-*.json carries the failures its own
fixture recorded. So:

  flagged AND the fixture failed  -> TRUE POSITIVE (probably)
  flagged AND the fixture passed  -> FALSE POSITIVE (almost certainly)

A validator firing on an answer that the fixture's own assertions
ACCEPTED is, by construction, flagging something nobody considered
wrong. That is the signal. It is a heuristic, not proof -- a fixture can
pass while containing a violation of a DIFFERENT class -- so every flag
prints with its text for review rather than being auto-classified away.

USAGE
=====
    python scripts/validator_sweep.py            # every class
    python scripts/validator_sweep.py --rule unforced-price
    exit 0 -> no false positives.  1 -> review needed.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.ask_response_validate import _CHECKS  # noqa: E402


# Flags REVIEWED and confirmed to be genuine violations that the
# fixture's own assertions do not cover. The heuristic cannot see these:
# the fixture passed, so it looks like a false positive, but the answer
# really does violate a DIFFERENT class.
#
# An entry here is a claim that a human looked. Never add one to silence
# a flag you have not read.
REVIEWED_TRUE_POSITIVES = {
    ("11c-no-self-technical-analysis", "unforced-price"):
        "'$NVDA is currently trading at $126.46' with no "
        "lookup_market_price call. Real unforced price; 11c only asserts "
        "the no-self-TA rule, so its own fixture passes.",
    ("07a-no-time-series-claims", "unforced-market-data"):
        "'Call open interest down -10.7%' with no lookup_options_chain "
        "call. Real class-4 violation.",
    ("07a-no-time-series-claims", "unforced-time-series"):
        "'over the past **5 days**' from snapshot tools. A real class-5 "
        "violation that 07a's own assertion MISSED because the pattern "
        "was markdown-blind. The assertion is now fixed, but the corpus "
        "records the verdict as it stood when the answer was captured, "
        "so history cannot be relabelled -- hence this entry.",
}


def load_corpus() -> list[dict]:
    """Every distinct recorded answer, with its fixture's own verdict."""
    out, seen = [], set()
    for path in sorted(glob.glob(os.path.join(REPO, "docs", "ask-*.json"))):
        try:
            d = json.loads(open(path, encoding="utf-8").read())
        except Exception:
            continue
        for f in d.get("fixtures") or []:
            ans = (f.get("answer") or "").strip()
            if not ans:
                continue
            key = (f["id"], ans)
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "id": f["id"],
                "answer": ans,
                "tools_called": f.get("tools_called") or [],
                "grounded": bool(f.get("grounded")),
                "fixture_failed": bool(f.get("failures")),
            })
    return out


def sweep(rule: str, fn, corpus: list[dict]) -> dict:
    tp, fp = [], []
    for rec in corpus:
        vs = fn(rec["answer"], rec["tools_called"],
                question=rec.get("question"), fetched=None,
                grounded=rec.get("grounded", False))
        if not vs:
            continue
        entry = (rec["id"], vs[0].match, vs[0].line.strip()[:88])
        if (rec["id"], rule) in REVIEWED_TRUE_POSITIVES:
            tp.append(entry)
            continue
        (tp if rec["fixture_failed"] else fp).append(entry)
    return {"rule": rule, "scanned": len(corpus), "tp": tp, "fp": fp}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rule", default=None)
    args = ap.parse_args()

    corpus = load_corpus()
    print(f"=== validator sweep over {len(corpus)} recorded answers ===\n")
    total_fp = 0
    for rule, fn in _CHECKS.items():
        if args.rule and rule != args.rule:
            continue
        r = sweep(rule, fn, corpus)
        total_fp += len(r["fp"])
        print(f"{rule}")
        print(f"  caught on FAILING answers  (true positives): "
              f"{len(r['tp'])}")
        for fid, m, line in r["tp"][:6]:
            print(f"     TP {fid:<30} {m!r:>22}  {line!r}")
        if len(r["tp"]) > 6:
            print(f"     ... {len(r['tp']) - 6} more")
        print(f"  caught on PASSING answers (FALSE POSITIVES): "
              f"{len(r['fp'])}")
        for fid, m, line in r["fp"][:10]:
            print(f"     FP {fid:<30} {m!r:>22}  {line!r}")
        print()
    if total_fp:
        print(f"{total_fp} false positive(s) — a validator firing on an "
              f"answer its fixture ACCEPTED is stripping correct content.")
        print("Tighten before deleting any prompt prose (SESSION TEMPLATE "
              "step 2b).")
        return 1
    print("no false positives across any class")
    return 0


if __name__ == "__main__":
    sys.exit(main())
