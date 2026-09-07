"""An empty answer on MAX_TOKENS is recoverable, not terminal (2026-09-07).

Four of six asks in one afternoon shipped "Thought myself in circles and
ran out of room". Every one was WEB/FACT and ungrounded; the two that
reached Google Search answered fine. The branch that handles a textless
response had a retry ladder for the safety filter and nothing at all for
a spent budget, so a recoverable turn cost the asker their answer and
their quota.

Phase 9 is run for real here; only the model client is a stub.
"""
import asyncio
import sys
from types import SimpleNamespace as NS

from google.genai import types

from discord_bot import bot as B


def _cfg():
    """A real config, because the retry builds its own with model_copy."""
    return types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        tool_config=types.ToolConfig(),
        max_output_tokens=5000,
        thinking_config=types.ThinkingConfig(thinking_budget=2000),
    )


def _empty_response(reason="MAX_TOKENS"):
    # reason=None is a candidate with NO finish_reason at all, which is
    # how the SDK reports it; an object whose .name is None is a
    # different (and not real) shape.
    fr = NS(name=reason) if reason is not None else None
    return NS(candidates=[NS(finish_reason=fr, safety_ratings=[])],
              prompt_feedback=None)


class _Client:
    """Records the config it was handed and replies with `text`."""
    def __init__(self, text, gm=None):
        self.calls = []
        outer = self

        class _Models:
            async def generate_content(self, **kw):
                outer.calls.append(kw)
                cand = NS(finish_reason=NS(name="STOP"), safety_ratings=[],
                          grounding_metadata=gm)
                return NS(text=text, candidates=[cand], prompt_feedback=None)

        self.aio = NS(models=_Models())


def _run(client, meta, reason="MAX_TOKENS"):
    return asyncio.run(B._ask_09_rank_and_regen_guards(
        _ask_meta=meta,
        _tally_retry_usage=lambda *_a, **_k: None,
        answer="",
        ask_model="m",
        chat_context="",
        client=client,
        config=_cfg(),
        contents=[],
        cross_window_block="",
        fetched_urls="",
        grounding_metadata=None,
        images=None,
        profiles_for_prompt="",
        question="was the tcu vs michigan game one of the best playoff games",
        response=_empty_response(reason),
        safety_settings=None,
        separator="",
        types=types,
    ))


def test_max_tokens_retries_and_ships_the_retry_answer():
    meta = {"guards": [], "kind": "FACT", "route": "WEB"}
    client = _Client("→ **51-45 Michigan-TCU**, the second-highest scoring CFP game.")
    answer, _gm = _run(client, meta)
    assert "circles" not in answer, answer
    assert "51-45" in answer
    assert meta["empty"] == "MAX_TOKENS"
    assert meta["empty_retry"] == "short-thinking"
    assert len(client.calls) == 1, client.calls


def test_the_retry_cuts_thinking_and_keeps_only_search():
    """The two documented causes — reasoning overrun and an automatic
    function-calling loop — are fixed by exactly these two changes."""
    client = _Client("→ **An answer.**")
    _run(client, {"guards": [], "kind": "FACT", "route": "WEB"})
    cfg = client.calls[0]["config"]
    assert cfg.thinking_config.thinking_budget == 512
    assert all(getattr(t, "google_search", None) is not None
               for t in (cfg.tools or [])), cfg.tools


def test_grounding_from_the_retry_reaches_the_caller():
    gm = NS(grounding_chunks=[NS(web=NS(uri="https://espn.com/x", title="espn.com"))])
    _answer, out_gm = _run(_Client("→ **An answer.**", gm=gm),
                           {"guards": [], "kind": "FACT", "route": "WEB"})
    assert out_gm is gm, "a grounded retry must not be stamped ungrounded"


def test_a_still_empty_retry_falls_back_and_says_so():
    meta = {"guards": [], "kind": "FACT", "route": "WEB"}
    answer, _gm = _run(_Client(""), meta)
    assert "Thought myself in circles" in answer
    assert meta["empty_retry"] == "failed"


def test_a_raising_client_still_falls_back_rather_than_erroring():
    class _Boom:
        def __init__(self):
            async def _gc(**kw):
                raise RuntimeError("503")
            self.aio = NS(models=NS(generate_content=_gc))
    meta = {"guards": [], "kind": "FACT", "route": "WEB"}
    answer, _gm = _run(_Boom(), meta)
    assert "Thought myself in circles" in answer
    assert meta["empty_retry"] == "failed"


def test_other_and_missing_finish_reasons_take_the_same_path():
    for reason in ("OTHER", None):
        meta = {"guards": [], "kind": "FACT", "route": "WEB"}
        answer, _gm = _run(_Client("→ **Answered.**"), meta, reason=reason)
        assert "Answered" in answer, reason
        assert meta["empty_retry"] == "short-thinking", reason


def test_the_audit_stamp_carries_the_reason_and_the_retry():
    import db_parts.summaries as S
    import inspect
    src = inspect.getsource(S)
    assert 'f"empty: {meta[\'empty\']}"' in src
    assert 'f"empty-retry: {meta[\'empty_retry\']}"' in src


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
