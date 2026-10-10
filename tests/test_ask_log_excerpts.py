"""The ask log keeps what each tool returned (2026-10-10 audit: checking a
fantasy answer's numbers needed a replay of the question)."""
import pathlib
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from discord_bot import bot as B  # noqa: E402


def test_excerpt_is_clipped_json():
    ex = B._trace_excerpt({"status": "ok", "rows": ["x" * 5000]})
    assert ex.startswith('{"status": "ok"') and len(ex) == B._TRACE_EXCERPT_CHARS


def test_ask_log_shows_tool_results():
    from config import settings
    with tempfile.TemporaryDirectory() as d:
        with patch.object(settings, "pdf_download_dir", str(pathlib.Path(d) / "pdfs")):
            path = db.append_ask_interaction(
                asker_display_name="BK", asker_username="bankerkyle", channel_name="ff",
                question="how does my matchup look", answer="you're up",
                tool_trace=[{"tool": "lookup_fantasy_league", "args": {"topic": "situation"},
                             "status": "ok", "result_chars": 40,
                             "excerpt": '{"projected_total": 143.7}'}])
            text = pathlib.Path(path).read_text(encoding="utf-8")
    assert "Tool results" in text and '"projected_total": 143.7' in text
