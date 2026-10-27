"""/ask has its own token budget pool (2026-10-27): a PDF backlog used
the shared 4M by 1:06 PM PT and every /ask after it was refused."""
import inspect

import pytest

from ai_analysis import token_budget as TB


def test_pdf_spend_does_not_block_ask(monkeypatch):
    monkeypatch.setenv("GEMINI_DAILY_TOKEN_BUDGET", "1000")
    monkeypatch.setenv("GEMINI_ASK_DAILY_TOKEN_BUDGET", "500")
    monkeypatch.setattr(TB.TokenBudget, "_log_budget_event", lambda *a, **k: None)
    TB.reset_for_tests()
    try:
        TB.get_budget().reserve_or_raise(estimated_tokens=900, caller="pdf_deep_analysis")
        with pytest.raises(TB.BudgetExceeded):
            TB.get_budget().reserve_or_raise(estimated_tokens=200, caller="pdf_deep_analysis")
        TB.get_budget("ask").reserve_or_raise(estimated_tokens=400, caller="ask:q")
        assert TB.get_budget("ask").daily_budget == 500
        assert TB.get_budget("ask") is TB.get_budget("ask")
        with pytest.raises(ValueError):
            TB.get_budget("pulse")
    finally:
        TB.reset_for_tests()


def test_ask_reserves_and_reconciles_in_its_own_pool():
    from discord_bot import bot
    src = bot._ask_pipeline_source()
    assert 'get_budget("ask").reserve_or_raise(' in src
    assert 'get_budget("ask").record_actual(' in src
    assert "get_budget()." not in src
    from ai_analysis import analyzer
    assert "get_budget()." in inspect.getsource(analyzer)


def test_exhausted_leaves_headroom(monkeypatch):
    monkeypatch.setenv("GEMINI_DAILY_TOKEN_BUDGET", "100000")
    monkeypatch.setattr(TB.TokenBudget, "_log_budget_event", lambda *a, **k: None)
    TB.reset_for_tests()
    try:
        b = TB.get_budget()
        assert not b.exhausted()
        b.reserve_or_raise(estimated_tokens=85_000, caller="pdf")
        assert b.exhausted() and not TB.get_budget("ask").exhausted()
    finally:
        TB.reset_for_tests()


def test_a_budget_refusal_does_not_use_a_retry_2026_10_27(tmp_path, monkeypatch):
    """62 backlog notes spent all three retries against the same exhausted
    budget in 15 minutes and were written off."""
    import asyncio
    from types import SimpleNamespace
    import db
    import pipeline.orchestrator as orch
    from ai_analysis.models import TriageResult
    local = tmp_path / "budget.pdf"
    local.write_bytes(b"%PDF")
    pid = db.insert_pdf_file("/Current/UBS/budget.pdf", "budget.pdf", str(local))

    async def triage(*a, **k):
        return TriageResult(priority="medium", report_type="macro", key_tickers=[], summary="s",
                            source="UBS", input_tokens=1, output_tokens=1)

    async def deep(**kw):
        raise TB.BudgetExceeded("daily Gemini token budget exhausted")

    monkeypatch.setattr(orch, "extract_text_per_page", lambda p: [SimpleNamespace(text="page")])
    monkeypatch.setattr(orch, "extract_pdf", lambda *a: SimpleNamespace())
    monkeypatch.setattr(orch, "triage_pdf", triage)
    monkeypatch.setattr(orch, "analyze_pdf_deep", deep)
    out = asyncio.run(orch.process_single_pdf(
        {"id": pid, "file_name": "budget.pdf", "local_path": str(local),
         "dropbox_path": "/Current/UBS/budget.pdf"}, use_claude_lane=False))
    row = db.get_connection().execute(
        "SELECT status, retry_count FROM pdf_files WHERE id = ?", (pid,)).fetchone()
    assert out is None and tuple(row) == ("DOWNLOADED", 0)


def test_the_queue_waits_while_the_budget_is_spent(monkeypatch):
    import asyncio
    import pipeline.orchestrator as orch
    import db
    monkeypatch.setattr(TB.TokenBudget, "exhausted", lambda self, headroom=20_000: True)
    monkeypatch.setattr(db, "get_pending_pdfs",
                        lambda **k: (_ for _ in ()).throw(AssertionError("queue must not be read")))
    assert asyncio.run(orch.process_pending_queue()) == []
