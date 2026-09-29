"""A LOW-priority PDF skips deep analysis and is recorded at triage cost
once. Until 2026-09-29 the LOW branch copied triage's token counts into
the PdfAnalysis and insert_analysis added triage's counts again, so every
LOW row carried exactly double its real spend."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

from ai_analysis.models import TriageResult
from pdf_processing.models import PageText
from pipeline import orchestrator as O

PAGE = PageText(page_number=1, text="text", char_count=4, has_images=False,
                has_tables=False, image_count=0)


def test_low_pdf_records_triage_tokens_once(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    triage = TriageResult(priority="low", report_type="other", key_tickers=[],
                          summary="peripheral", source="UBS",
                          input_tokens=10177, output_tokens=102)
    recorded = {}

    def insert(**kw):
        recorded.update(kw)
        return 1

    publish = {}

    def fake_publish(**kw):
        publish.update(kw)
        return False

    with patch.object(O, "extract_text_per_page", return_value=[PAGE]), \
         patch("github_bridge.pilot_publish.publish_high_document", side_effect=fake_publish), \
         patch.object(O, "triage_pdf", AsyncMock(return_value=triage)), \
         patch.object(O, "analyze_pdf_deep", AsyncMock(side_effect=AssertionError("deep ran"))), \
         patch.object(O.db, "update_pdf_status"), patch.object(O.db, "log_event"), \
         patch.object(O.db, "update_pdf_priority"), \
         patch.object(O.db, "insert_analysis", side_effect=insert), \
         patch.object(O.settings, "pilot_publish_enabled", False, create=True):
        asyncio.run(O.process_single_pdf(
            {"id": 7, "file_name": "x.pdf", "local_path": str(pdf)}))
    assert recorded["priority"] == "low"
    assert recorded["input_tokens"] == 10177 and recorded["output_tokens"] == 102
    a = json.loads(recorded["analysis_json"])
    assert a["source"] == "UBS" and a["key_insights"] == ["peripheral"]
    # the pilot hook is reached with the triage text; before 2026-09-29
    # the LOW branch raised NameError here (`extraction` unbound)
    assert publish.get("full_text") == "text" and publish.get("priority") == "low"
