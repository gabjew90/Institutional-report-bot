#!/usr/bin/env python3
"""Run the Omnipulse readers over the unread list, several documents at a time.

    python scripts/pilot_read.py --unread /tmp/unread.json --workers 3
    python scripts/pilot_read.py --root pilot-data/pilot --dry-run 3    # speed test, commits nothing

Why (2026-11-25): the readers took one document at a time, a median 8 minutes
each, in runs cut off at 90 minutes. That morning 26 HIGH notes were ready
three hours before the editor, 13 had cards when it started, and the pulse
went out with the classic body. The analysis lane had the same shape until
2026-11-23 and went from 50 notes an hour to 500 with parallel workers and a
call that loads nothing it does not need.

Each document is read by scripts/pilot_read_one.sh, which holds the reading
steps unchanged. This file only decides how many run at once and when to stop
handing out documents, so a run ends by itself before the job's time limit
instead of being cancelled with documents in flight.

What parallel reading costs: a reader sees the labels of every document
already finished, not of the ones being read beside it (pilot_known_labels is
rebuilt per document, as before). With three workers that is at most two
documents it cannot see.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

ONE = os.path.join("scripts", "pilot_read_one.sh")
RETIRED = 4      # pilot_read_one.sh: the document used up its attempts


def read_one(doc: dict, dry: bool = False) -> int:
    env = dict(os.environ, PILOT_DRY="1" if dry else "0")
    return subprocess.run(
        ["bash", ONE, str(doc["id"]), doc["text_path"], doc["model"], doc["tier"], doc["date"]],
        env=env).returncode


def recent_read(root: str, n: int) -> list[dict]:
    """The n newest HIGH documents that already have cards: the input of a
    dry run, which reads them again into a temp folder to time the readers."""
    from scripts.pilot_config import CARDS_SUBDIR, SOURCE_TEXT_SUBDIR, reader_tier
    out = []
    for folder in sorted(glob.glob(os.path.join(root, SOURCE_TEXT_SUBDIR, "20*")), reverse=True):
        date = os.path.basename(folder)
        for text_path in sorted(glob.glob(os.path.join(folder, "*.txt")), reverse=True):
            doc_id = os.path.basename(text_path).split("__")[0]
            if not os.path.exists(os.path.join(root, CARDS_SUBDIR, date, f"{doc_id}.json")):
                continue
            meta = {}
            try:
                meta = json.load(open(os.path.join(folder, f"{doc_id}.meta.json"), encoding="utf-8"))
            except (OSError, ValueError):
                pass
            tier, model = reader_tier(meta.get("source") or "")
            out.append({"id": doc_id, "date": date, "text_path": text_path, "tier": tier, "model": model})
            if len(out) >= n:
                return out
    return out


def run(docs: list[dict], *, workers: int = 3, budget_s: float = 4500, one=read_one,
        dry: bool = False, say=print) -> dict:
    """Read `docs` in order, `workers` at a time. No document is started after
    `budget_s`: the ones in flight finish and the rest wait for the next run."""
    started = time.monotonic()
    lock = threading.Lock()
    queue = list(docs)
    stats = {"total": 0, "failed": 0, "gave_up": 0, "left": 0, "seconds": []}

    def work() -> None:
        while True:
            with lock:
                if not queue or time.monotonic() - started > budget_s:
                    return
                doc = queue.pop(0)
                stats["total"] += 1
            t0 = time.monotonic()
            try:
                code = one(doc, dry)
            except Exception as e:
                say(f"::warning::reader for {doc.get('id')} did not run: {e}")
                code = 3
            took = time.monotonic() - t0
            with lock:
                stats["seconds"].append(round(took))
                if code != 0:
                    stats["failed"] += 1
                    if code == RETIRED:
                        stats["gave_up"] += 1
            say(f"{doc['id']}: {'read' if code == 0 else 'FAILED'} in {took / 60:.1f} min")

    threads = [threading.Thread(target=work, name=f"reader-{i}") for i in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stats["left"] = len(queue)
    stats["wall_seconds"] = round(time.monotonic() - started)
    return stats


def verdict(stats: dict) -> tuple[int, str]:
    """(exit code, message). One poisoned PDF must not stop the day, a total
    wipeout should be loud, and a run whose every failure was a retirement is
    the give-up rule working, not a wipeout (2026-09-07)."""
    total, failed, gave_up = stats["total"], stats["failed"], stats["gave_up"]
    if total > 0 and failed == total:
        if gave_up >= failed:
            return 0, (f"::warning::every document this run was retired after exhausting "
                       f"its read attempts ({gave_up}); see read-failures/")
        return 1, "::error::every reader failed this run"
    return 0, ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--unread", help="the JSON list scripts/pilot_list_unread.py wrote")
    ap.add_argument("--root", default="pilot-data/pilot")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--budget-minutes", type=float, default=75)
    ap.add_argument("--dry-run", type=int, default=0,
                    help="read this many already-read documents again and commit nothing")
    a = ap.parse_args(argv)
    dry = a.dry_run > 0
    docs = recent_read(a.root, a.dry_run) if dry else json.load(open(a.unread, encoding="utf-8"))
    st = run(docs, workers=a.workers, budget_s=a.budget_minutes * 60, dry=dry)
    secs = sorted(st["seconds"])
    print(f"read {st['total']} document(s), {st['failed']} failed, {st['left']} left for the next run, "
          f"{a.workers} at a time, {st['wall_seconds'] / 60:.1f} min wall"
          + (f", per document median {secs[len(secs) // 2] / 60:.1f} min max {secs[-1] / 60:.1f} min" if secs else ""))
    if not dry:
        # Metric 5 (ops): one entry per run, upserted by run id.
        subprocess.run(["bash", ONE, "--ops", str(st["total"]), str(st["failed"])])
    code, message = verdict(st)
    if message:
        print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
