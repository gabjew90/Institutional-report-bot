"""Every Gemini call lands in gemini_calls with its caller (2026-09-29).
Before this only the PDF pipeline recorded tokens."""
import asyncio
from types import SimpleNamespace

import db
from ai_analysis import usage_ledger as L


class _Usage(SimpleNamespace):
    pass


def _resp(prompt=1000, out=100, thoughts=0, cached=0):
    return SimpleNamespace(usage_metadata=_Usage(
        prompt_token_count=prompt, candidates_token_count=out,
        thoughts_token_count=thoughts, cached_content_token_count=cached),
        text="ok")


class _FakeModels:
    def __init__(self, resp):
        self.resp = resp
        self.calls = []

    def generate_content(self, *a, **k):
        self.calls.append((a, k))
        return self.resp


class _FakeAsyncModels(_FakeModels):
    async def generate_content(self, *a, **k):
        self.calls.append((a, k))
        return self.resp


def _fake_client(resp):
    return SimpleNamespace(models=_FakeModels(resp),
                           aio=SimpleNamespace(models=_FakeAsyncModels(resp)))


def _fresh():
    db.reset_connections()
    conn = db.get_connection()
    conn.execute("DELETE FROM gemini_calls")
    conn.commit()
    return conn


def test_async_and_sync_calls_are_recorded_under_the_caller():
    conn = _fresh()
    c = L.instrument(_fake_client(_resp(1000, 100, thoughts=50)), "ask")
    out = asyncio.run(c.aio.models.generate_content(model="gemini-3.5-flash-lite", contents="q"))
    assert out.text == "ok"
    c.models.generate_content("gemini-3.1-flash-lite", contents="q")
    rows = conn.execute(
        "SELECT caller, model, input_tokens, output_tokens, thinking_tokens "
        "FROM gemini_calls ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("ask", "gemini-3.5-flash-lite", 1000, 150, 50),
        ("ask", "gemini-3.1-flash-lite", 1000, 150, 50),
    ]


def test_as_caller_narrows_the_label_sync_and_async():
    conn = _fresh()
    c = L.instrument(_fake_client(_resp()), "pdf_analysis")

    async def go():
        async with L.as_caller("pdf_triage"):
            await c.aio.models.generate_content(model="m", contents="q")
        await c.aio.models.generate_content(model="m", contents="q")
    asyncio.run(go())
    with L.as_caller("pdf_deep"):
        c.models.generate_content(model="m", contents="q")
    callers = [r[0] for r in conn.execute("SELECT caller FROM gemini_calls ORDER BY id")]
    assert callers == ["pdf_triage", "pdf_analysis", "pdf_deep"]


def test_a_response_without_usage_is_not_recorded_and_never_raises():
    conn = _fresh()
    c = L.instrument(_fake_client(SimpleNamespace(text="x")), "ask")
    assert c.models.generate_content(model="m", contents="q").text == "x"
    assert conn.execute("SELECT COUNT(*) FROM gemini_calls").fetchone()[0] == 0
    # a ledger failure must not fail the call
    import unittest.mock as um
    with um.patch.object(db, "record_gemini_call", side_effect=RuntimeError("db down")):
        c2 = L.instrument(_fake_client(_resp()), "ask")
        assert c2.models.generate_content(model="m", contents="q").text == "ok"


def test_spend_is_priced_per_model_and_sorted():
    _fresh()
    db.record_gemini_call(caller="ask", model="gemini-3.5-flash-lite",
                          input_tokens=2_000_000, output_tokens=100_000)
    db.record_gemini_call(caller="pdf_deep", model="gemini-3.1-flash-lite",
                          input_tokens=4_000_000, output_tokens=200_000)
    db.record_gemini_call(caller="pdf_deep", model="gemini-3.1-flash-lite-preview",
                          input_tokens=0, output_tokens=0)
    spend = db.gemini_spend(days=7)
    assert [s["caller"] for s in spend] == ["pdf_deep", "ask"]
    pdf, ask = spend
    assert abs(pdf["usd"] - (4 * 0.25 + 0.2 * 1.50)) < 1e-9 and pdf["calls"] == 2
    assert abs(ask["usd"] - (2 * 0.30 + 0.1 * 2.50)) < 1e-9
    assert pdf["models"] == ["gemini-3.1-flash-lite", "gemini-3.1-flash-lite-preview"]


def test_unknown_models_price_at_the_default():
    assert L.price_per_m("gemini-9-ultra") == L._DEFAULT_PRICE
    assert L.cost_usd("gemini-3.5-flash-lite", 1_000_000, 0) == 0.30
