"""The input of a triage side-by-side (2026-11-28). Run ON THE WORKER, read-only.

Samples recent notes at random, downloads each again from Dropbox, extracts
the text the way the pipeline does and writes what Gemini's triage saw, with
what Gemini answered, to one gzip file under /tmp. No Gemini call. Nothing is
written to the database, which is opened read-only without importing `db`.

  railway ssh "cat > /tmp/triage_bundle.py && /opt/venv/bin/python /tmp/triage_bundle.py 360 2026-11-14" < scripts/triage_bundle.py

Arguments: how many notes, and the earliest pdf_files.created_at day. About
nine notes a minute. Copy /tmp/triage_bundle.jsonl.gz off the worker, put it
where the runner can read it, run scripts/pilot_triage.py on it (the
`triage_bundle` input of pilot-analysis.yml) and read the result with
scripts/triage_compare.py. The file holds the notes' text: remove it from
the branch when the run is done.
"""
import asyncio
import gzip
import json
import os
import random
import sqlite3
import sys
import zlib  # noqa: F401


def _adopt():
    if os.environ.get("TRIAGE_EXP_ENV"):
        return
    try:
        with open("/proc/1/environ", "rb") as h:
            env1 = dict(kv.split("=", 1) for kv in h.read().decode(errors="replace").split("\0") if "=" in kv)
    except OSError:
        return
    env = dict(os.environ, TRIAGE_EXP_ENV="1")
    if env1.get("LD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = env1["LD_LIBRARY_PATH"]
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


_adopt()
sys.path.insert(0, "/app")
from pathlib import Path  # noqa: E402
from dropbox_client import watcher as W  # noqa: E402
from pdf_processing.extractor import extract_text_per_page  # noqa: E402

N = int(sys.argv[1]) if len(sys.argv) > 1 else 360
SINCE = sys.argv[2] if len(sys.argv) > 2 else "2026-11-14"
OUT = "/tmp/triage_bundle.jsonl.gz"
DL = asyncio.Semaphore(4)


async def one(row):
    pid, name, dpath, pri, created, tj = row
    tmp = f"/tmp/triage_bundle_pdfs/{pid}.pdf"
    try:
        async with DL:
            await asyncio.to_thread(W.download_file, dpath, Path(tmp))
            pages = await asyncio.to_thread(extract_text_per_page, tmp)
        text = "\n".join(p.text for p in pages)
    except Exception as e:
        return {"id": pid, "err": f"{type(e).__name__}: {str(e)[:80]}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        t = json.loads(tj or "{}")
    except ValueError:
        t = {}
    return {"id": pid, "file_name": name, "folder": os.path.dirname(dpath or ""), "created": created,
            "chars": len(text), "pages": len(pages), "preview": text[:32000],
            "gemini": {"priority": pri, "triage_priority": t.get("priority"),
                       "report_type": t.get("report_type"), "source": t.get("source"),
                       "summary": t.get("summary")}}


async def main():
    os.makedirs("/tmp/triage_bundle_pdfs", exist_ok=True)
    conn = sqlite3.connect("file:/data/reports.db?mode=ro", uri=True, timeout=5)
    rows = conn.execute(
        "SELECT f.id, f.file_name, f.dropbox_path, f.priority, f.created_at, "
        "(SELECT a.triage_json FROM pdf_analyses a WHERE a.pdf_file_id = f.id ORDER BY a.id DESC LIMIT 1) "
        "FROM pdf_files f WHERE f.status='PROCESSED' AND f.priority IN ('high','medium','low') "
        "AND f.created_at >= ? ORDER BY f.id", (SINCE,)).fetchall()
    conn.close()
    random.Random(20261128).shuffle(rows)
    rows = rows[:N]
    print("population sample:", len(rows), flush=True)
    done = errs = 0
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        for t in asyncio.as_completed([asyncio.create_task(one(r)) for r in rows]):
            rec = await t
            if rec.get("err"):
                errs += 1
                print("ERR", rec["id"], rec["err"], flush=True)
                continue
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            done += 1
            if done % 40 == 0:
                print("done", done, flush=True)
    print("DONE", done, "errors", errs, "bytes", os.path.getsize(OUT), flush=True)


asyncio.run(main())
