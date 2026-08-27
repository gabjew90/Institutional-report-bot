#!/usr/bin/env python3
"""Run the production validator classes over ONE answer — the triage
tool the headless ask-QC judge uses to classify a FAIL.

A judge FAIL lands in one of three buckets, and the first is
mechanical: "an existing validator class should have caught this."
This CLI answers that deterministically, so the agent classifies by
RUNNING the code instead of reasoning about what the code would do.

USAGE
=====
    python scripts/validate_answer.py --file answer.txt \
        --tools lookup_market_price,google_search
    echo "the answer text" | python scripts/validate_answer.py

Prints JSON: {"violations": [{"rule", "match", "line", "why"}]}.
Exit 0 always (an empty list is a result, not an error).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.ask_response_validate import validate  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=None,
                    help="file holding the answer text; stdin if omitted")
    ap.add_argument("--tools", default="",
                    help="comma-separated tool names the turn called")
    ap.add_argument("--question", default="",
                    help="the question, for question-shape checks")
    args = ap.parse_args()

    if args.file:
        text = open(args.file, encoding="utf-8").read()
    else:
        text = sys.stdin.read()
    tools = [t.strip() for t in args.tools.split(",") if t.strip()]

    vs = validate(text, tools, question=args.question, fetched=None)
    print(json.dumps({"violations": [
        {"rule": v.rule, "match": v.match, "line": v.line.strip()[:140],
         "why": v.why} for v in vs
    ]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
