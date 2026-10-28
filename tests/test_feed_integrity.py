"""Files Dropbox delivers that are not PDFs (2026-10-27: 305 notes saved as
the 17-byte reply {"success":false}), and their re-upload."""
import uuid
from pathlib import Path

import db
from dropbox_client import watcher as W
from dropbox_client.models import DropboxFileEntry

JUNK = b'{"success":false}'
PDF = b"%PDF-1.7\n" + b"x" * 3000


def _entry(path, rev, size):
    return DropboxFileEntry(path=path, name=Path(path).name, rev=rev, size=size,
                            server_modified="2026-10-27T16:56:00")


def _poll(monkeypatch, entries, body):
    alerts = []
    monkeypatch.setattr(W, "list_new_files", lambda: (entries, "cursor"))

    def fake_download(path, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        return dest
    monkeypatch.setattr(W, "download_file", fake_download)
    import discord_bot.ops_alert as oa
    monkeypatch.setattr(oa, "ops_alert_sync", lambda text, **k: alerts.append(text))
    return W.poll_and_download(), alerts


def test_reason_names_size_and_first_bytes(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(JUNK)
    assert W.not_pdf_reason(p).startswith("not a PDF: Dropbox file is 17 bytes")
    p.write_bytes(PDF)
    assert W.not_pdf_reason(p) == ""


def test_a_non_pdf_is_failed_for_good_and_alerted_once(monkeypatch):
    path = f"/Current/2026/October/Oct 26/UBS/{uuid.uuid4().hex}.pdf"
    out, alerts = _poll(monkeypatch, [_entry(path, "r1", 17)], JUNK)
    assert out == []
    row = db.get_pdf_by_path(path)
    assert row["status"] == "FAILED" and row["retry_count"] == 99
    assert row["error_message"].startswith(W.NOT_PDF_PREFIX)
    assert W.is_junk_row(row)
    assert len(alerts) == 1 and "not PDFs" in alerts[0]
    assert row not in db.get_failed_pdfs_for_retry(max_retries=3)


def test_a_reupload_over_a_non_pdf_is_queued(monkeypatch):
    path = f"/Current/2026/October/Oct 26/GS/{uuid.uuid4().hex}.pdf"
    _poll(monkeypatch, [_entry(path, "r1", 17)], JUNK)
    out, alerts = _poll(monkeypatch, [_entry(path, "r2", len(PDF))], PDF)
    row = db.get_pdf_by_path(path)
    assert [r["id"] for r in out] == [row["id"]]
    assert row["status"] == "DOWNLOADED" and row["retry_count"] == 0
    assert row["error_message"] is None and row["dropbox_rev"] == "r2"
    assert Path(row["local_path"]).read_bytes() == PDF
    assert alerts == []


def test_the_same_revision_is_not_downloaded_again(monkeypatch):
    path = f"/Current/2026/October/Oct 26/JPM/{uuid.uuid4().hex}.pdf"
    _poll(monkeypatch, [_entry(path, "r1", 17)], JUNK)
    out, _ = _poll(monkeypatch, [_entry(path, "r1", 17)], PDF)
    assert out == [] and db.get_pdf_by_path(path)["status"] == "FAILED"


def test_a_processed_pdf_at_the_same_path_is_still_skipped(monkeypatch):
    path = f"/Current/2026/October/Oct 24/GS/{uuid.uuid4().hex}.pdf"
    _poll(monkeypatch, [_entry(path, "r1", len(PDF))], PDF)
    pid = db.get_pdf_by_path(path)["id"]
    db.update_pdf_status(pid, "PROCESSED")
    out, _ = _poll(monkeypatch, [_entry(path, "r2", len(PDF))], PDF)
    assert out == [] and db.get_pdf_by_path(path)["dropbox_rev"] == "r1"


def test_old_failed_rows_under_a_kilobyte_count_as_junk():
    assert W.is_junk_row({"status": "FAILED", "file_size_bytes": 17,
                          "error_message": "Failed to open file 'x.pdf' as type pdf."})
    assert not W.is_junk_row({"status": "FAILED", "file_size_bytes": 900_000,
                              "error_message": "timeout"})
    assert not W.is_junk_row({"status": "PROCESSED", "file_size_bytes": 17})


def test_a_failed_row_without_a_size_is_not_junk():
    assert not W.is_junk_row({"status": "FAILED", "file_size_bytes": None,
                              "error_message": "timeout"})
