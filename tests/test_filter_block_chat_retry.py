"""A filter block is recovered by trimming the chat window (2026-09-24).

Replaying all 10 turns that shipped "Gemini bounced this one" (Sep 4-24):
every one was refused 3/3 on resend, and in all 10 the recent-chat
window was a necessary ingredient. The trigger is not a word list: the
9/23 refusal reduced to "CRWV entry here possibly <@id>" plus "Damn
1dtes fcked me today" plus "what's abe glw play", and changing any one
of those pieces let it through. Every existing rung resent the chat
nearly verbatim. Cleaned and cut to the last 15 messages, the chat
rescued 10/10; dropped entirely, also 10/10.

Phase 9 runs for real; only the model client is a stub.
"""
import asyncio
from types import SimpleNamespace as NS

from google.genai import types

from discord_bot import bot as B
from tests.test_ask_empty_retry import _blocked_response, _cfg

CHAT = "\n".join(
    ["Recent channel chat (oldest → newest, for context only — the actual question follows after):"]
    + [f"m{i}: line {i}" for i in range(30)]
    + ["abe (abullish_xyz): CRWV entry here possibly <@811385796295655434>",
       "The Oracle (the_oracle_ish): Damn 1dtes fcked me today https://x.com/a/b"]
)


class _Client:
    """Refuses any payload still carrying the mention; answers otherwise."""
    def __init__(self, refuse=lambda t: "<@811385796295655434>" in t or not t):
        self.sent = []
        outer = self

        class _Models:
            async def generate_content(self, **kw):
                text = " ".join(p.text for c in kw.get("contents") or []
                                for p in (c.parts or []) if getattr(p, "text", None))
                outer.sent.append(text)
                if refuse(text):
                    return _blocked_response()
                cand = NS(finish_reason=NS(name="STOP"), safety_ratings=[],
                          grounding_metadata=None)
                return NS(text="→ **GLW** entry around the 50 day.", candidates=[cand],
                          prompt_feedback=None)

        self.aio = NS(models=_Models())


def _run(client, meta, chat=CHAT):
    return asyncio.run(B._ask_09_rank_and_regen_guards(
        _ask_meta=meta, _tally_retry_usage=lambda *_a, **_k: None,
        answer="", ask_model="m", chat_context=chat, client=client,
        config=_cfg(), contents=[], cross_window_block="", fetched_urls="",
        grounding_metadata=None, images=None, profiles_for_prompt="",
        question="what's abe glw play", response=_blocked_response(),
        safety_settings=None, separator="--- bulch (tulch) is asking: ---",
        types=types))


def test_the_trimmed_chat_rescues_a_block_that_every_old_rung_resent():
    meta = {"guards": [], "kind": "FACT", "route": "LOCAL"}
    c = _Client()
    answer, _ = _run(c, meta)
    assert "GLW" in answer
    assert meta["filter_retry"] == "chat-trim"
    trimmed = c.sent[-1]
    assert "<@" not in trimmed and "https://" not in trimmed
    assert "Damn 1dtes" in trimmed, "recent context is kept"
    assert "m0: line 0" not in trimmed, "only the last 15 messages ride"


def test_no_chat_is_the_fallback_when_the_trim_is_still_refused():
    meta = {"guards": [], "kind": "FACT", "route": "LOCAL"}
    c = _Client(refuse=lambda t: "Recent channel chat" in t or not t)
    answer, _ = _run(c, meta)
    assert "GLW" in answer and meta["filter_retry"] == "no-chat"


def test_the_helper_cleans_and_cuts():
    out = B._filter_safe_chat(CHAT)
    lines = out.splitlines()
    assert lines[0].startswith("Recent channel chat")
    assert len(lines) == 1 + B._FILTER_TRIM_LINES
    assert "<@" not in out and "(link)" in out
    assert B._filter_safe_chat("") == ""


def test_the_rungs_sit_after_the_identical_resend_and_before_voice_strip():
    src = B._ask_pipeline_source()
    t0 = src.find('"same-prompt"')
    trim = src.find('("chat-trim", _filter_safe_chat(chat_context))')
    voice = src.find("# Tier 1 — voice-strip")
    assert -1 < t0 < trim < voice


def test_a_nothing_generated_block_reaches_the_chat_rungs():
    """candidates=None, zero tokens, no prompt_feedback is a block too;
    the chat rungs must not be gated on prompt_block alone."""
    meta = {"guards": [], "kind": "FACT", "route": "LOCAL"}
    silent = NS(candidates=None, prompt_feedback=None,
                usage_metadata=NS(thoughts_token_count=0, candidates_token_count=0))
    c = _Client()
    answer = asyncio.run(B._ask_09_rank_and_regen_guards(
        _ask_meta=meta, _tally_retry_usage=lambda *_a, **_k: None,
        answer="", ask_model="m", chat_context=CHAT, client=c,
        config=_cfg(), contents=[], cross_window_block="", fetched_urls="",
        grounding_metadata=None, images=None, profiles_for_prompt="",
        question="what's abe glw play", response=silent,
        safety_settings=None, separator="--- q ---", types=types))[0]
    assert "GLW" in answer and meta["filter_retry"] == "chat-trim"


def test_both_rung_stamps_are_named_in_the_source():
    src = B._ask_pipeline_source()
    assert '("chat-trim", _filter_safe_chat(chat_context))' in src
    assert '("no-chat", "")' in src
    assert '_ask_meta["filter_retry"] = _rung' in src
