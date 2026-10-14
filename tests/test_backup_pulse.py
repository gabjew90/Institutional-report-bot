"""The Gemini backup pulse (owner, 2026-10-13): when the Claude routine has
not delivered by 10:45 ET, the worker writes the pulse with Gemini. The
path had been dead since August and crashed on prompt placeholders."""
import asyncio
import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))


def test_fill_leaves_unknown_braces_alone():
    from report.synthesizer import _fill
    from ai_analysis.prompts import DRAFT_USER
    try:
        DRAFT_USER.format(pdf_count=1)
        raised = False
    except (KeyError, IndexError):
        raised = True
    assert raised, "str.format on DRAFT_USER is what crashed this path"
    out = _fill("a {pdf_count} b `# {title}` c", pdf_count=7)
    assert out == "a 7 b `# {title}` c"


class _Resp:
    def __init__(self, text):
        self.text = text
        self.usage_metadata = SimpleNamespace(prompt_token_count=10, candidates_token_count=5)


class _Client:
    """Stands in for Gemini: DRAFT then AUDIT, each returns a pulse."""
    def __init__(self):
        self.prompts = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, model, contents, config):
        self.prompts.append(contents)
        return _Resp("# Backup Headline Here\n\n## 1. RECAP\nx\n\n## 2. INSIGHTS & ALPHA\n"
                     "### A theme\ny\n\n## 3. WHAT TO WATCH\n### Today\n- z\n")


def test_the_gemini_path_writes_a_pulse_end_to_end():
    from report import synthesizer as S
    from ai_analysis.models import PdfAnalysis
    client = _Client()
    a = PdfAnalysis(pdf_file_id=1, file_name="gs.pdf", source="Goldman Sachs",
                    title="Note", report_type="strategy", priority="high",
                    key_insights=["Yields rose 10bp on the week."])
    with patch.object(S, "_get_client", return_value=client), \
            patch.object(S, "fetch_market_snapshot", return_value="MARKET"), \
            patch.object(S, "fetch_news_snapshot", return_value="NEWS"), \
            patch.object(S, "fetch_earnings_calendar", return_value="EARN"), \
            patch.object(S, "fetch_economic_calendar", return_value="ECON"):
        report = asyncio.run(S.synthesize_daily_pulse([a], use_prev_context=True))
    assert len(client.prompts) == 2, "DRAFT and AUDIT both ran"
    assert "{pdf_count}" not in client.prompts[0] and "{draft_markdown}" not in client.prompts[1]
    assert report.markdown_content.startswith("# Backup Headline Here")


def test_the_backup_job_runs_once_and_only_when_no_pulse_landed():
    from scheduler import jobs as J
    calls = []

    async def fake_pulse(bot=None, *, backup=False):
        calls.append(backup)
        return SimpleNamespace(pdf_count=3)

    import db
    with patch("pipeline.orchestrator.run_daily_pulse", fake_pulse), \
            patch.object(db, "daily_pulse_delivered_on", return_value=True):
        asyncio.run(J._backup_pulse_job(None))
    assert calls == [], "a delivered pulse means no backup"
    with patch("pipeline.orchestrator.run_daily_pulse", fake_pulse), \
            patch.object(db, "daily_pulse_delivered_on", return_value=False), \
            patch("world_context.is_us_market_holiday", return_value=False), \
            patch("scheduler.jobs.datetime", create=True):
        # pin a weekday regardless of when the suite runs
        from datetime import datetime as _dt
        import pytz
        monday = pytz.timezone("America/New_York").localize(_dt(2026, 10, 12, 10, 45))
        with patch("datetime.datetime") as dtm:
            dtm.now.return_value = monday
            asyncio.run(J._backup_pulse_job(None))
            asyncio.run(J._backup_pulse_job(None))
    assert calls == [True], "one backup, labelled as one"
