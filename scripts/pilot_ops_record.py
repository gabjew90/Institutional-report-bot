#!/usr/bin/env python3
"""Upsert this run's entry in pilot/ops/<date>.json (metric 5).

WHY A SCRIPT (2026-09-03). This lived as inline Python inside the
readers workflow, which meant no test could reach it, and it ran ONCE
at the end of the read loop. Every readers run on 2026-09-03 was killed
by `timeout-minutes: 45` partway through the loop, so the ops record
was never written and neither were the cards: 20 runs, 273 verified
cards in the 01:00 run alone, all discarded. The loop now calls this
after every document, so the record survives a kill.

Entries are keyed by run id and UPDATED in place, so calling it nine
times in one run leaves one entry with the latest counts, not nine.

    python scripts/pilot_ops_record.py --root pilot-data/pilot \
        --total 9 --failed 0
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_config import OPS_SUBDIR  # noqa: E402

# The pulse is written 13:55-14:15 UTC; a read landing inside that
# window competes with it for the same source tree (metric 5).
PULSE_WINDOW = (13 * 60 + 55, 14 * 60 + 15)


def upsert(doc: dict, run_id: str, total: int, failed: int,
           now: datetime, quiet: bool = False) -> dict:
    """One entry per run id, updated in place."""
    runs = [r for r in (doc.get("runs") or []) if isinstance(r, dict)]
    hm = now.hour * 60 + now.minute
    entry = {
        "run_id": run_id,
        "finished_utc": now.strftime("%H:%M"),
        "total": int(total),
        "failed": int(failed),
        "in_pulse_window": PULSE_WINDOW[0] <= hm <= PULSE_WINDOW[1],
    }
    if quiet:
        entry["quiet"] = True
    for i, r in enumerate(runs):
        if str(r.get("run_id")) == str(run_id):
            runs[i] = entry
            break
    else:
        runs.append(entry)
    t = sum(int(r.get("total") or 0) for r in runs)
    f = sum(int(r.get("failed") or 0) for r in runs)
    return {
        "runs": runs,
        "reader_failure_rate": round(f / t, 3) if t else None,
        "collided_with_pulse_window": any(
            r.get("in_pulse_window") and (r.get("total") or 0) > 0
            for r in runs),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="the pilot root in the checkout")
    ap.add_argument("--total", type=int, required=True)
    ap.add_argument("--failed", type=int, required=True)
    ap.add_argument("--quiet-run", action="store_true",
                    help="a run that found nothing to read")
    ap.add_argument("--date", default=None, help="override the UTC date")
    a = ap.parse_args()

    now = datetime.now(timezone.utc)
    date = a.date or now.strftime("%Y-%m-%d")
    path = os.path.join(a.root, OPS_SUBDIR, f"{date}.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    doc = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception:
            doc = {}

    out = upsert(doc, os.environ.get("GITHUB_RUN_ID", "local"),
                 a.total, a.failed, now, quiet=a.quiet_run)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(f"ops: {a.total} read, {a.failed} failed "
          f"(day rate {out['reader_failure_rate']}) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
