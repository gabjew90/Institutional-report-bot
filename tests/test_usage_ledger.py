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


# ---- deprecated request parameters (Google notice, 2026-10-06) ----

from google.genai import types as _t

from ai_analysis import usage_ledger as _UL


def _cfg():
    return _t.GenerateContentConfig(
        temperature=0.3, top_p=0.9, max_output_tokens=500,
        system_instruction="sys",
        thinking_config=_t.ThinkingConfig(thinking_budget=2000))


def test_current_models_get_requests_unchanged():
    for model in ("gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "models/gemini-3.5-flash-lite",
                  "gemini-2.5-flash", "some-other-model"):
        cfg = _cfg()
        assert _UL.modernize_config(model, cfg) is cfg


def test_newer_models_lose_sampling_and_get_a_level():
    assert _UL.modernize_config("gemini-flash-latest", _cfg()).temperature is None
    out = _UL.modernize_config("gemini-3.8-flash", _cfg())
    assert out.temperature is None and out.top_p is None
    assert out.max_output_tokens == 500 and out.system_instruction == "sys"
    assert out.thinking_config.thinking_budget is None
    assert out.thinking_config.thinking_level == _t.ThinkingLevel.MEDIUM


def test_dict_configs_are_adapted_too():
    out = _UL.modernize_config("gemini-4.0-flash", {
        "temperature": 0.7, "thinking_config": {"thinking_budget": 256}, "max_output_tokens": 9})
    assert out == {"thinking_config": {"thinking_level": "LOW"}, "max_output_tokens": 9}


def test_budget_to_level():
    assert [_UL.level_for_budget(b) for b in (None, -1, 0, 256, 1024, 2000, 9000)] == \
        [None, None, "MINIMAL", "LOW", "LOW", "MEDIUM", "HIGH"]


def test_an_existing_level_is_kept():
    cfg = _t.GenerateContentConfig(temperature=0.1,
                                   thinking_config=_t.ThinkingConfig(thinking_level="MINIMAL"))
    out = _UL.modernize_config("gemini-3.8-flash", cfg)
    assert out.temperature is None
    assert out.thinking_config.thinking_level == _t.ThinkingLevel.MINIMAL


def test_the_wrapper_adapts_what_it_sends():
    import asyncio
    from types import SimpleNamespace
    sent = []

    async def agen(*a, **kw):
        sent.append(kw["config"])
        return SimpleNamespace(usage_metadata=None)

    def gen(*a, **kw):
        sent.append(a[2])
        return SimpleNamespace(usage_metadata=None)

    client = SimpleNamespace(models=SimpleNamespace(generate_content=gen),
                             aio=SimpleNamespace(models=SimpleNamespace(generate_content=agen)))
    _UL.instrument(client, "test")
    asyncio.run(client.aio.models.generate_content(model="gemini-3.8-flash", contents="x", config=_cfg()))
    client.models.generate_content("gemini-3.5-flash-lite", "x", _cfg())
    assert sent[0].temperature is None and sent[0].thinking_config.thinking_level is not None
    assert sent[1].temperature == 0.3 and sent[1].thinking_config.thinking_budget == 2000
