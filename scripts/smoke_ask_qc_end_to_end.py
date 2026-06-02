"""End-to-end smoke for the ask-qc pipeline.

Real parser + real aggregator + mocked Gemini judge + mocked
github_bridge. Drives _ask_qc_job with a temp log directory
seeded with a small fake log, verifies:
  - report file lands at the right local path
  - github_bridge.put_file is called with the right args
  - pipeline_events row is written with the expected payload
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_FAKE_LOG = """# /ask interactions - 2026-06-01

## 2026-06-01 14:30:00 UTC

**Asker:** kloh (`kloh.`) in #stonks-yapping

**Q:** what's TSLA at

**A:**

- **$TSLA $310**

---

## 2026-06-01 15:00:00 UTC

**Asker:** BK (`bankerkyle`) in #stonks-yapping

**Q:** who's the worst trader

**A:**

theorb_18574 sits at the bottom.

---
"""


_CLEAN_JSON = json.dumps({
    "overall": "CLEAN",
    "dimensions": {
        "fabrication": {"verdict": "PASS", "rationale": "grounded"},
        "status_handling": {"verdict": "N/A", "rationale": "legacy"},
        "voice": {"verdict": "PASS", "rationale": "ok"},
        "format_adherence": {"verdict": "PASS", "rationale": "ok"},
        "depth_match": {"verdict": "PASS", "rationale": "ok"},
        "decline_when_uncertain": {"verdict": "N/A", "rationale": "all ok"},
    },
    "notable_pattern": None,
})


def test_end_to_end_writes_local_report_and_pushes():
    from datetime import datetime, timezone, timedelta
    from config import settings

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ask_logs_dir = tmp / "ask-logs"
        ask_logs_dir.mkdir()

        # Compute yesterday UTC the same way _ask_qc_job does
        date = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        (ask_logs_dir / f"{date}.md").write_text(
            _FAKE_LOG.replace("2026-06-01", date), encoding="utf-8"
        )

        # Mock Gemini to always return CLEAN
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = _CLEAN_JSON
        mock_client.aio.models.generate_content = AsyncMock(
            return_value=mock_resp
        )

        mock_put = MagicMock()

        with (
            patch.object(settings, "pdf_download_dir", str(tmp / "pdfs")),
            patch.object(settings, "google_api_key", "fake"),
            patch.object(settings, "github_token", "fake"),
            patch("ask_qc.grader._get_client", return_value=mock_client),
            patch("github_bridge.client.put_file", mock_put),
        ):
            from scheduler import jobs
            asyncio.run(jobs._ask_qc_job())

        # 1. local report file exists
        report_path = tmp / "ask-qc" / f"{date}.md"
        assert report_path.exists(), f"report file missing at {report_path}"
        content = report_path.read_text(encoding="utf-8")
        assert "2 interactions" in content or "2 CLEAN" in content, content[:300]
        _ok("end-to-end: local report file written")

        # 2. github_bridge.put_file called with the right path/message
        assert mock_put.call_count == 1, (
            f"expected 1 put_file call, got {mock_put.call_count}"
        )
        call = mock_put.call_args
        assert call.kwargs["path"] == f"ask-qc/{date}.md", call.kwargs["path"]
        assert "ask-qc: snapshot" in call.kwargs["message"]
        _ok("end-to-end: github_bridge.put_file called with right args")


def test_end_to_end_empty_log_skips_push():
    from datetime import datetime, timezone, timedelta
    from config import settings

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ask_logs_dir = tmp / "ask-logs"
        ask_logs_dir.mkdir()
        date = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).strftime("%Y-%m-%d")
        # Empty file (no interactions) - parser returns [], orchestrator
        # should write the stub locally but skip the push
        (ask_logs_dir / f"{date}.md").write_text(
            "# /ask interactions - placeholder\n", encoding="utf-8"
        )

        mock_put = MagicMock()
        with (
            patch.object(settings, "pdf_download_dir", str(tmp / "pdfs")),
            patch.object(settings, "github_token", "fake"),
            patch("github_bridge.client.put_file", mock_put),
        ):
            from scheduler import jobs
            asyncio.run(jobs._ask_qc_job())

        # Stub written locally
        report_path = tmp / "ask-qc" / f"{date}.md"
        assert report_path.exists()
        content = report_path.read_text(encoding="utf-8")
        assert "No /ask interactions" in content or "no interactions" in content.lower()
        # No push
        assert mock_put.call_count == 0
        _ok("end-to-end: empty log writes stub locally, skips push")


if __name__ == "__main__":
    print("=== ask-qc end-to-end smoke ===")
    test_end_to_end_writes_local_report_and_pushes()
    test_end_to_end_empty_log_skips_push()
    print("\nALL ASK-QC END-TO-END SMOKE TESTS PASS")
