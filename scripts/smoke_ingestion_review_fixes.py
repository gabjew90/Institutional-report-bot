"""Smoke: the 2026-08-20 ingestion/analyzer review fixes.

  1. [HIGH] watcher registers a FAILED pdf_files row when a download
     fails, BEFORE the cursor advances — nothing silently lost.
  2. orchestrator re-downloads a missing local file from dropbox_path
     instead of burning a retry on "file not found".
  3. extract_pdf reuses triage pages instead of re-extracting.
  4. analyze_batch (dead, drifted signature) is gone.
  5. truncation retry reserves its extra output tokens.
  6. token_budget docstring no longer claims DB persistence.
  7. RateLimiter rechecks the window after sleeping (no RPM overshoot).
"""

import asyncio
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_failed_download_registers_row():
    import dropbox_client.watcher as w
    import db
    calls = {"insert": [], "cursor": []}
    entry = SimpleNamespace(
        path="/Current/x/a.pdf", name="a.pdf", rev="r1", size=10,
        server_modified="2026-08-20",
    )
    with patch.object(w, "list_new_files",
                      return_value=([entry], "CUR2")), \
         patch.object(w, "download_file",
                      side_effect=RuntimeError("boom")), \
         patch.object(db, "get_pdf_by_path", return_value=None), \
         patch.object(db, "insert_pdf_file",
                      side_effect=lambda **kw: calls["insert"].append(kw) or 7), \
         patch.object(db, "log_event", lambda *a, **k: None), \
         patch.object(db, "update_dropbox_cursor",
                      side_effect=lambda c: calls["cursor"].append(c)):
        w.poll_and_download()
    assert calls["insert"], "failed download must register a row"
    assert calls["insert"][0]["status"] == "FAILED", calls["insert"][0]
    assert calls["cursor"] == ["CUR2"], \
        "cursor must still advance (the row makes that safe)"
    _ok("watcher: failed download -> FAILED row, cursor advances safely")


def test_orchestrator_redownloads_missing_file(tmp_dir=None):
    import tempfile, os
    import pipeline.orchestrator as orch
    import db
    d = tempfile.mkdtemp()
    missing = os.path.join(d, "gone.pdf")
    statuses = []

    def fake_download(dropbox_path, local_path):
        open(local_path, "wb").write(b"%PDF-1.4 fake")

    async def fake_triage(*a, **k):
        raise RuntimeError("stop after re-download")  # end the test here

    import dropbox_client.watcher as w
    with patch.object(w, "download_file", side_effect=fake_download), \
         patch.object(db, "update_pdf_status",
                      side_effect=lambda i, s, m=None: statuses.append(s)), \
         patch.object(db, "log_event", lambda *a, **k: None), \
         patch.object(orch, "extract_text_per_page",
                      side_effect=RuntimeError("stop")):
        asyncio.run(orch.process_single_pdf({
            "id": 1, "file_name": "gone.pdf", "local_path": missing,
            "dropbox_path": "/Current/x/gone.pdf",
        }))
    assert os.path.exists(missing), "file must have been re-downloaded"
    assert "FAILED" not in statuses[:1], \
        f"must not fail on missing file when re-download works: {statuses}"
    _ok("orchestrator: missing local file re-downloads from dropbox_path")


def test_extract_pdf_reuses_pages():
    import pdf_processing.extractor as ex
    calls = []
    fake_pages = [SimpleNamespace(page_number=0, text="hello")]
    with patch.object(ex, "extract_text_per_page",
                      side_effect=lambda p: calls.append(p) or fake_pages):
        out = ex.extract_pdf("x.pdf", None, fake_pages)
    assert calls == [], "must NOT re-extract when pages are passed"
    assert "hello" in out.full_text
    _ok("extract_pdf: reuses triage pages, no double extraction")


def test_analyze_batch_gone():
    import ai_analysis.analyzer as an
    assert not hasattr(an, "analyze_batch"), \
        "dead analyze_batch (drifted signature) must stay deleted"
    _ok("analyzer: dead analyze_batch removed")


def test_retry_reserves_budget():
    import inspect
    import ai_analysis.analyzer as an
    src = inspect.getsource(an.analyze_pdf_deep)
    seg = src.split("new_cap = max(8192", 1)[1][:800]
    assert "reserve_or_raise" in seg, \
        "truncation retry must reserve its extra output tokens"
    assert "BudgetExceeded" in seg, "and skip the retry when over budget"
    _ok("analyzer: truncation retry is budget-reserved")


def test_budget_docstring_honest():
    import ai_analysis.token_budget as tb
    doc = tb.__doc__ or ""
    assert "PROCESS-MEMORY ONLY" in doc
    assert "Persists in DB via" not in doc.replace(
        "previously claimed DB persistence", "")
    _ok("token_budget: docstring no longer claims DB persistence")


def test_rate_limiter_rechecks_window():
    from ai_analysis.rate_limiter import RateLimiter
    rl = RateLimiter(max_concurrent=5, rpm_limit=1)
    # Prefill: one request 59.9s ago — the window frees up in ~0.1s.
    rl._request_times.append(time.monotonic() - 59.9)
    t0 = time.monotonic()
    asyncio.run(rl.acquire())
    waited = time.monotonic() - t0
    assert 0.05 <= waited < 2.0, f"should wait ~0.1s for the window: {waited}"
    assert len(rl._request_times) == 1, \
        "expired timestamp pruned, new one appended after recheck"
    _ok("rate limiter: sleeps then RE-CHECKS the window before appending")


if __name__ == "__main__":
    print("=== ingestion review fixes smoke ===")
    test_failed_download_registers_row()
    test_orchestrator_redownloads_missing_file()
    test_extract_pdf_reuses_pages()
    test_analyze_batch_gone()
    test_retry_reserves_budget()
    test_budget_docstring_honest()
    test_rate_limiter_rechecks_window()
    print("\nALL INGESTION-REVIEW-FIXES SMOKE TESTS PASS")
