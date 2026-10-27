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
