"""Reader bookkeeping (2026-09-05): the read give-up marker and the
unread scanner that honours it. Five 64-132 KB documents failed at the
turn limit on every one of fourteen runs on 2026-09-04, were re-read
each time, and kept the editor's unread count above zero, which voided
the day."""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from scripts.pilot_config import MAX_READ_ATTEMPTS, READ_FAILURES_SUBDIR, SOURCE_TEXT_SUBDIR
from scripts.pilot_read_failure import failure_path, given_up, record

NOW = datetime(2026, 9, 5, 1, 0, tzinfo=timezone.utc)


def test_record_counts_attempts_and_gives_up_at_the_limit():
    doc = None
    for i in range(1, MAX_READ_ATTEMPTS + 1):
        doc = record(doc, f"max turns {i}", run_id=str(100 + i), now=NOW)
        assert doc["attempts"] == i
        assert doc["given_up"] is (i >= MAX_READ_ATTEMPTS)
    assert doc["last_reason"] == f"max turns {MAX_READ_ATTEMPTS}"
    assert [h["run_id"] for h in doc["history"]] == [str(100 + i) for i in range(1, MAX_READ_ATTEMPTS + 1)]


def test_record_survives_corrupt_input_and_caps_history():
    doc = record("not a dict", "x", "1", NOW)
    assert doc["attempts"] == 1 and doc["given_up"] is False
    for i in range(20):
        doc = record(doc, "again", str(i), NOW)
    assert len(doc["history"]) == 10 and doc["attempts"] == 21


def _seed(root, date, doc_id, attempts=None):
    d = os.path.join(root, SOURCE_TEXT_SUBDIR, date)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{doc_id}__slug.txt"), "w", encoding="utf-8") as fh:
        fh.write("text")
    with open(os.path.join(d, f"{doc_id}.meta.json"), "w", encoding="utf-8") as fh:
        json.dump({"source": "Goldman"}, fh)
    if attempts is not None:
        p = failure_path(root, date, doc_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"attempts": attempts}, fh)


def test_given_up_reads_the_marker_and_tolerates_absence():
    with tempfile.TemporaryDirectory() as td:
        _seed(td, "2026-09-03", "1", attempts=MAX_READ_ATTEMPTS)
        _seed(td, "2026-09-03", "2", attempts=MAX_READ_ATTEMPTS - 1)
        _seed(td, "2026-09-03", "3")
        assert given_up(td, "2026-09-03", "1") is True
        assert given_up(td, "2026-09-03", "2") is False
        assert given_up(td, "2026-09-03", "3") is False


def test_list_unread_skips_given_up_documents_and_reports_them():
    with tempfile.TemporaryDirectory() as td:
        _seed(td, "2026-09-03", "1", attempts=MAX_READ_ATTEMPTS)   # gave up: not unread
        _seed(td, "2026-09-03", "2", attempts=1)                   # still has attempts left
        _seed(td, "2026-09-04", "3")                               # never tried
        out = os.path.join(td, "unread.json")
        gu = os.path.join(td, "given_up.json")
        res = subprocess.run(
            [sys.executable, "scripts/pilot_list_unread.py", "--root", td, "--out", out,
             "--given-up-out", gu], capture_output=True, text=True, check=True)
        unread = json.load(open(out, encoding="utf-8"))
        assert sorted(d["id"] for d in unread) == ["2", "3"], unread
        assert [d["id"] for d in json.load(open(gu, encoding="utf-8"))] == ["1"]
        assert "2 unread document(s), 1 given up" in res.stdout


def test_read_failure_cli_upserts_one_file_per_document():
    with tempfile.TemporaryDirectory() as td:
        for _ in range(MAX_READ_ATTEMPTS):
            res = subprocess.run(
                [sys.executable, "scripts/pilot_read_failure.py", "--root", td, "--id", "9",
                 "--date", "2026-09-04", "--reason", "Error: Reached max turns (12)"],
                capture_output=True, text=True, check=True)
        assert f"attempts={MAX_READ_ATTEMPTS} given_up=True" in res.stdout
        files = os.listdir(os.path.join(td, READ_FAILURES_SUBDIR, "2026-09-04"))
        assert files == ["9.json"]
        assert given_up(td, "2026-09-04", "9")


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
