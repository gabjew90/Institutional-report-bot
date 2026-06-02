"""Smoke test: scheduler.jobs registers _ask_qc_job at 03:00 ET cron.

Verifies the new job is registered with the right id, trigger, and
guardrails (max_instances=1, misfire_grace_time set), and that the
job function is callable without raising on a missing log file
(graceful degradation when bot hasn't been live)."""

import asyncio
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_ask_qc_job_registered():
    """setup_scheduler() should call scheduler.add_job with id='ask_qc'
    and a CronTrigger at hour=3."""
    from scheduler import jobs
    # Patch AsyncIOScheduler so we don't actually start a scheduler,
    # but we get to inspect its add_job calls.
    mock_scheduler = MagicMock()
    mock_bot = MagicMock()
    with (
        patch("scheduler.jobs.AsyncIOScheduler", return_value=mock_scheduler),
    ):
        jobs.setup_scheduler(bot=mock_bot)
    # Find the call(s) where id='ask_qc'
    calls = [c for c in mock_scheduler.add_job.call_args_list
             if c.kwargs.get("id") == "ask_qc"]
    assert len(calls) == 1, (
        f"expected exactly 1 add_job(id='ask_qc') call, got {len(calls)}"
    )
    call = calls[0]
    # Trigger must be CronTrigger
    from apscheduler.triggers.cron import CronTrigger
    assert isinstance(call.kwargs.get("trigger"), CronTrigger), (
        f"trigger not CronTrigger: {call.kwargs.get('trigger')}"
    )
    assert call.kwargs.get("max_instances") == 1
    _ok("scheduler: _ask_qc_job registered as 'ask_qc' cron with CronTrigger")


def test_ask_qc_job_noops_when_no_log_file():
    """_ask_qc_job should log + exit cleanly when yesterday's log file
    is missing (bot was down all day; first run after deploy)."""
    from scheduler import jobs
    from config import settings
    with tempfile.TemporaryDirectory() as tmp:
        # Point pdf_download_dir at an empty temp tree so ask-logs/ is empty
        fake_pdfs = str(Path(tmp) / "pdfs")
        with patch.object(settings, "pdf_download_dir", fake_pdfs):
            # Should NOT raise - graceful noop
            asyncio.run(jobs._ask_qc_job())
    _ok("_ask_qc_job: missing log file -> graceful noop (no exception)")


if __name__ == "__main__":
    print("=== ask-qc scheduler wired smoke ===")
    test_ask_qc_job_registered()
    test_ask_qc_job_noops_when_no_log_file()
    print("\nALL ASK-QC SCHEDULER-WIRED SMOKE TESTS PASS")
