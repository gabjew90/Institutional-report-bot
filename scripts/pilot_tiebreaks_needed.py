#!/usr/bin/env python3
"""Print the grade dimensions of one day that need a third grader.

WHY (2026-09-05). The spec leaves grader disagreements to the owner, who
may delegate. Three shakedown days produced seven disagreements and
zero tiebreaks, and the scoreboard's `m2 pass` cannot compute while any
stand. The graders workflow now calls this after the a/b pass and runs
a third fresh agent (same frozen prompt, same input) on every stem it
prints, saved as `<stem>-tiebreak.json`. The owner can still override
with `<stem>-owner.json`.

    python scripts/pilot_tiebreaks_needed.py --grades-dir pilot-data/pilot/grades/2026-09-04

One stem per line, e.g. `fidelity-production`; nothing when the day is
settled. Uses the scoreboard's own agreement rule so the two cannot
drift.
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

from scripts.pilot_scoreboard import tiebreak_stems  # noqa: E402


def load_grades(grades_dir: str) -> dict:
    grades: dict = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(grades_dir, "*.json"))):
        name = os.path.basename(path)[:-5]
        parts = name.split("-")
        try:
            with open(path, encoding="utf-8") as fh:
                grades["-".join(parts[:-1])][parts[-1]] = json.load(fh)
        except Exception:
            continue
    return dict(grades)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--grades-dir", required=True)
    a = ap.parse_args()
    for stem in tiebreak_stems(load_grades(a.grades_dir)):
        print(stem)
    return 0


if __name__ == "__main__":
    sys.exit(main())
