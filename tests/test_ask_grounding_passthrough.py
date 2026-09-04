"""Grounding must survive the /ask phase chain (2026-09-04).

The 2026-09-01 split gave phase 9 a `grounding_metadata = None` local
that it returned on every non-regenerating turn, and the caller
unpacked that into the real variable. Sources footers per day: 7, 1,
13 up to 09-01, then 0, 0, 0. Grounded answers were stamped
`ungrounded`, and the figure-provenance guard, which runs in phase 7,
skipped answers as grounded that phase 10 then rendered without a
single source.

This runs the REAL phase 9 with a sentinel grounding object and a
client that fails the test if it is ever called, so a benign answer
must come back with the sentinel intact.
"""
import asyncio
import sys
from types import SimpleNamespace as NS

from google.genai import types

from discord_bot import bot as B


class _NoCallClient:
    """Any model call from a guard that should not have fired is a
    test failure, not a silent regeneration."""
    class _Models:
        async def generate_content(self, **kw):
            raise AssertionError(f"phase 9 called the model on a benign answer: {kw.get('contents')!r}"[:300])

    class _Aio:
        def __init__(self):
            self.models = _NoCallClient._Models()

    def __init__(self):
        self.aio = _NoCallClient._Aio()


def _run_phase_9(answer: str, gm):
    return asyncio.run(B._ask_09_rank_and_regen_guards(
        _ask_meta={"guards": [], "kind": "FACT", "route": "WEB"},
        _tally_retry_usage=lambda *_a, **_k: None,
        answer=answer,
        ask_model="m",
        chat_context="",
        client=_NoCallClient(),
        config=NS(),
        contents=[],
        cross_window_block="",
        fetched_urls="",
        grounding_metadata=gm,
        images=None,
        profiles_for_prompt="",
        question="what time are S&P inclusions announced",
        response=NS(candidates=[]),
        safety_settings=None,
        separator="",
        types=types,
    ))


def test_phase_9_returns_the_grounding_it_was_handed():
    sentinel = NS(grounding_chunks=[NS(web=NS(uri="https://spglobal.com/x", title="spglobal.com"))])
    answer = "→ **After market close** on the second Friday of the last month of each quarter."
    out_answer, out_gm = _run_phase_9(answer, sentinel)
    assert out_gm is sentinel, "phase 9 replaced the grounding on a turn that regenerated nothing"
    assert out_answer == answer


def test_phase_9_takes_grounding_as_a_parameter_and_never_none_inits_it():
    import inspect
    sig = inspect.signature(B._ask_09_rank_and_regen_guards)
    assert "grounding_metadata" in sig.parameters
    src = inspect.getsource(B._ask_09_rank_and_regen_guards)
    body_top = src.split('"""')[2][:400] if src.count('"""') >= 2 else src[:400]
    assert "grounding_metadata = None" not in body_top, (
        "a None-init at the top of phase 9 is the 09-02..09-04 footer outage")


def test_caller_passes_grounding_into_phase_9():
    src = B._ask_pipeline_source()
    i = src.index("await _ask_09_rank_and_regen_guards(")
    call = src[i:i + 900]
    assert "grounding_metadata=grounding_metadata" in call
    # and the caller still unpacks it back, so a regen CAN replace it
    assert "(answer, grounding_metadata) = await _ask_09" in src

def test_phase_8_returns_the_grounding_it_was_handed():
    sentinel = NS(grounding_chunks=[NS(web=NS(uri="https://x", title="x"))])
    answer = "→ **After market close** on the second Friday of the last month of each quarter."
    out_answer, out_gm, _resp = asyncio.run(B._ask_08_technical_analysis_guard(
        _ask_meta={"guards": [], "kind": "FACT", "route": "WEB"},
        _ask_tool_trace=[],
        _prompt_extra="",
        _tally_retry_usage=lambda *_a, **_k: None,
        answer=answer,
        ask_model="m",
        client=_NoCallClient(),
        contents=[],
        grounding_metadata=sentinel,
        question="what time are S&P inclusions announced",
        safety_settings=None,
        types=types,
        user_content="",
    ))
    assert out_gm is sentinel and out_answer == answer


def test_every_phase_that_returns_grounding_either_receives_or_builds_it():
    """The structural rule the split broke: a phase may return
    grounding_metadata only if it took it as a parameter (pass-through)
    or derives it from `response` inside (phase 7 does). A bare
    `grounding_metadata = None` at the top of a phase that then returns
    it is the outage, whichever phase it lands in next."""
    import inspect
    import re
    phases = [getattr(B, n) for n in dir(B) if re.match(r"_ask_\d\d_", n)]
    assert len(phases) >= 11, [f.__name__ for f in phases]
    offenders = []
    for fn in phases:
        src = inspect.getsource(fn)
        m = re.search(r"^    return \((.*)\)\s*$", src, re.M)
        if not m or "grounding_metadata" not in m.group(1):
            continue
        params = inspect.signature(fn).parameters
        builds_it = "response.candidates[0].grounding_metadata" in src
        if "grounding_metadata" not in params and not builds_it:
            offenders.append(fn.__name__)
    assert offenders == [], offenders


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
