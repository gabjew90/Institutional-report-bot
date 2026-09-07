"""An empty answer is recoverable, not terminal (2026-09-07).

Four of six asks in one afternoon shipped "Thought myself in circles and
ran out of room". Two separate defects sat behind that one message and
this file pins both.

1. A genuine spent budget (finish_reason MAX_TOKENS with output tokens
   actually spent) had no retry at all, only the wrapper.
2. Those four particular asks were not a spent budget. Replayed against
   the live model they return candidates=None, no finish_reason, and
   usage showing ZERO thinking and ZERO output tokens, with
   prompt_feedback.block_reason=PROHIBITED_CONTENT. Nothing was
   generated, so nothing ran out. The recovery for that is the filter
   ladder that already existed, and the discriminator is the token
   count: a block generates nothing, a spent budget generates plenty.
   On replay, question-only recovered 4 of 4 and Voice-stripped 2 of 4.

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


def _empty_response(reason="MAX_TOKENS", thoughts=1800, out=3200):
    # reason=None is a candidate with NO finish_reason at all, which is
    # how the SDK reports it; an object whose .name is None is a
    # different (and not real) shape. Tokens default to a budget that
    # really was spent — the case the short-thinking retry is for.
    fr = NS(name=reason) if reason is not None else None
    return NS(candidates=[NS(finish_reason=fr, safety_ratings=[])],
              prompt_feedback=None,
              usage_metadata=NS(thoughts_token_count=thoughts,
                                candidates_token_count=out))


def _blocked_response():
    """What the live model actually returns for these: no candidates, no
    finish reason, nothing generated (verified 2026-09-07)."""
    return NS(candidates=None,
              prompt_feedback=NS(block_reason=NS(name="PROHIBITED_CONTENT")),
              usage_metadata=NS(thoughts_token_count=0,
                                candidates_token_count=0))


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


def _run(client, meta, reason="MAX_TOKENS", response=None):
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
        response=response if response is not None else _empty_response(reason),
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


def test_a_blocked_prompt_takes_the_filter_ladder_not_the_budget_wrapper():
    """The 2026-09-07 shape: nothing generated. It must reach the ladder,
    whose first tier resends the identical prompt, and the stamp must
    name the block rather than a budget."""
    meta = {"guards": [], "kind": "FACT", "route": "WEB"}
    client = _Client("→ **Georgia 65, TCU 7** in the January 2023 final.")
    answer, _gm = _run(client, meta, response=_blocked_response())
    assert "circles" not in answer, answer
    assert "65, TCU 7" in answer
    assert meta["empty"] == "PROHIBITED_CONTENT"
    assert meta["filter_retry"] == "same-prompt", meta
    assert meta.get("empty_retry") is None, "not a budget failure"


def test_nothing_generated_is_a_block_even_with_no_prompt_feedback():
    """Production's response object did not carry the block reason the
    replay showed, so the token count is the discriminator that has to
    stand on its own."""
    meta = {"guards": [], "kind": "FACT", "route": "WEB"}
    resp = NS(candidates=[NS(finish_reason=None, safety_ratings=[])],
              prompt_feedback=None,
              usage_metadata=NS(thoughts_token_count=0,
                                candidates_token_count=0))
    answer, _gm = _run(_Client("→ **An answer.**"), meta, response=resp)
    assert "circles" not in answer, answer
    assert meta["empty"] == "nothing-generated"


def test_a_spent_budget_is_not_mistaken_for_a_block():
    """The other side of the discriminator: tokens were spent, so this
    is the budget path, not the ladder."""
    meta = {"guards": [], "kind": "FACT", "route": "WEB"}
    _run(_Client("→ **An answer.**"), meta)
    assert meta["empty"] == "MAX_TOKENS"
    assert meta["empty_retry"] == "short-thinking"
    assert meta.get("filter_retry") is None


def test_the_audit_stamp_carries_the_reason_and_the_retry():
    import db_parts.summaries as S
    import inspect
    src = inspect.getsource(S)
    assert 'f"empty: {meta[\'empty\']}"' in src
    assert 'f"empty-retry: {meta[\'empty_retry\']}"' in src


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
