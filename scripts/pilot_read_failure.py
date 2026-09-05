#!/usr/bin/env python3
"""Record one failed read attempt for a pilot document.

WHY (2026-09-05). "Unread" is structural: a source-text file with no
cards file. A document the reader cannot finish (turn limit, unparseable
output, anchors that never verify) therefore stayed unread forever, was
re-read on every run, and kept the editor's unread count above zero,
which voids the day. This script upserts read-failures/<date>/<id>.json
with an attempt count; pilot_list_unread skips a document once the
count reaches MAX_READ_ATTEMPTS.

    python scripts/pilot_read_failure.py --root pilot-data/pilot \
        --id 15241 --date 2026-09-03 --reason "max turns"

Prints `attempts=N given_up=<bool>`; always exits 0 so the reader loop
never aborts on bookkeeping.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_config import (MAX_READ_ATTEMPTS,  # noqa: E402
                                  READ_FAILURES_SUBDIR)


def failure_path(root: str, date: str, doc_id: str) -> str:
    return os.path.join(root, READ_FAILURES_SUBDIR, date, f"{doc_id}.json")


def record(doc: dict | None, reason: str, run_id: str, now: datetime) -> dict:
    """One more attempt on top of whatever the file already holds."""
    doc = dict(doc) if isinstance(doc, dict) else {}
    attempts = int(doc.get("attempts") or 0) + 1
    history = [h for h in (doc.get("history") or []) if isinstance(h, dict)]
    history.append({"at": now.strftime("%Y-%m-%dT%H:%MZ"), "run_id": run_id,
                    "reason": (reason or "")[:200]})
    return {
        "attempts": attempts,
        "given_up": attempts >= MAX_READ_ATTEMPTS,
        "last_reason": (reason or "")[:200],
        "history": history[-10:],
    }


def given_up(root: str, date: str, doc_id: str) -> bool:
    """True once a document has used up its read attempts."""
    try:
        with open(failure_path(root, date, doc_id), encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return False
    return int(doc.get("attempts") or 0) >= MAX_READ_ATTEMPTS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--id", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--reason", default="")
    a = ap.parse_args()
    path = failure_path(a.root, a.date, a.id)
    existing = None
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
    except Exception:
        existing = None
    doc = record(existing, a.reason, os.environ.get("GITHUB_RUN_ID", "local"),
                 datetime.now(timezone.utc))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(f"attempts={doc['attempts']} given_up={doc['given_up']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
