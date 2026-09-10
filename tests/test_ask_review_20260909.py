"""The 2026-09-09 /ask code review: ten confirmed defects, every one a
value carried by hand between phases and dropped on the way. These
pin the fixes. The phase-8 `response` clobber itself is pinned in
tests/test_ask_grounding_passthrough.py.
"""
import asyncio
import sys
from types import SimpleNamespace as NS

from discord_bot import bot as B
from discord_bot import figure_provenance as FP


# --- finding 2: retry token tallies reach record_actual ---------------

def test_retry_tally_sums_on_the_object_and_the_caller_adds_it():
    t = B._RetryTally()
    t(NS(usage_metadata=NS(prompt_token_count=1000, candidates_token_count=50)))
    t(NS(usage_metadata=NS(prompt_token_count=None, candidates_token_count=7)))
    t(object())  # unreadable response contributes nothing and never raises
    assert t.total == 1057 and t.calls == 2
    src = B._ask_pipeline_source()
    assert "_ask_actual_total=_ask_actual_total + _tally_retry_usage.total" in src, (
        "phase 10 must be handed the loop total PLUS the retry tally")


# --- finding 3: the price executor speaks the shared status vocabulary --

def test_market_price_returns_a_status_on_every_path():
    from unittest.mock import patch
    from discord_bot import ask_tools as T
    r = asyncio.run(T._execute_market_price({"symbols": []}))
    assert r["status"] == "error" and "error" in r
    with patch("report.market_data._fetch_finnhub_quote", return_value=None), \
         patch("report.market_data._session_label", return_value=("OPEN", "note")), \
         patch.object(T, "_crypto_quote", new=_none_async):
        r = asyncio.run(T._execute_market_price({"symbols": ["ZZZZ"]}))
    assert r["status"] == "no_data", r
    with patch("report.market_data._fetch_finnhub_quote",
               return_value={"price": 598.42, "prev_close": 596.40, "change_pct": 0.34}), \
         patch("report.market_data._session_label", return_value=("OPEN", "note")):
        r = asyncio.run(T._execute_market_price({"symbols": ["SPY"]}))
    assert r["status"] == "ok", r


async def _none_async(_sym):
    return None


# --- finding 4: a failed or timed-out prefetch is not a source ---------

def test_a_trace_of_failed_calls_is_not_a_source():
    assert not B._trace_has_source([])
    assert not B._trace_has_source([{"tool": "lookup_market_price", "status": "timeout", "via": "prefetch"}])
    assert not B._trace_has_source([{"tool": "lookup_earnings_date", "status": "no_data", "via": "prefetch"}])
    assert B._trace_has_source([{"tool": "lookup_earnings_date", "status": "no_data"},
                                {"tool": "lookup_market_price", "status": "ok"}])
    assert B._trace_has_source([{"tool": "lookup_market_price", "status": "backstop-fetch"}])
    # the nets read it
    answer = "Goldman has a price target of $240 on $NVDA and consensus sits at 225.40 after the print."
    assert B._is_ungrounded_market_fact(answer, None, [])
    assert B._is_ungrounded_market_fact(answer, None, [{"tool": "lookup_market_price", "status": "timeout"}])
    assert not B._is_ungrounded_market_fact(answer, None, [{"tool": "lookup_market_price", "status": "ok"}])


def test_prefetch_trace_uses_the_executor_status_and_names_the_caller():
    src = B._ask_pipeline_source()
    assert '"status": "timeout", "via": "prefetch"' in src
    assert "prefetch:" not in src.split("_run_prefetch")[1][:2000], (
        "the prefetch:<status> string matched nothing downstream")
    from scripts import ask_response_validate as V
    assert "timeout" in V._FAILED_TOOL_STATUSES
    assert set(V._FAILED_TOOL_STATUSES) == set(B._FAILED_TOOL_STATUSES)


# --- finding 5: the hedge is detached before figure provenance ---------

def test_prose_with_a_single_arrow_splits_as_prose():
    prose = ("Nvidia reported revenue of 46.7B for the quarter. Data center was "
             "41.1B of that. Guidance was 54B.")
    assert len(FP._lines(prose)) == 3
    # with the hedge attached the body is still not swallowed into one
    # arrow "line" with the hedge: paragraph mode keeps them apart
    hedged = FP._lines(prose + B._UNVERIFIED_HEDGE)
    assert len(hedged) == 2 and hedged[0].startswith("Nvidia") and "Couldn't verify" in hedged[1]
    bullets = FP._lines("→ **A** 46.7B\n→ **B** 41.1B")
    assert len(bullets) == 2
    assert len(FP._lines("→ **Recovered.**")) == 1


def test_a_hedged_prose_answer_keeps_its_sourced_sentences():
    body = "Revenue was 46.7B. Data center was 41.1B. Guidance was 54B."
    rep = FP.check(body, "tool: revenue 46.7B, dc 41.1B")
    assert rep.action == "stripped"
    assert "46.7B" in rep.answer and "41.1B" in rep.answer and "54B" not in rep.answer
    src = B._ask_pipeline_source()
    assert "_fp_hedged = answer.endswith(_UNVERIFIED_HEDGE)" in src
    assert "_rep = _fp.check(_fp_body, _ev)" in src


# --- finding 6: dossiers and the model's own words are not evidence ----

def test_dossier_numbers_and_model_text_do_not_reach_the_evidence():
    from google.genai import types
    uc = ("WHO'S TALKING (background): humor:54/100, 348 msgs\n\n"
          "Recent channel chat (oldest → newest, for context only):\nbk: lulu implied is 8.1%\n\n"
          "---\nwhats the probability on kalshi")
    contents = [
        types.Content(role="user", parts=[types.Part.from_text(text=uc)]),
        types.Content(role="model", parts=[types.Part.from_text(text="I think it is 54% honestly")]),
        types.Content(role="user", parts=[types.Part.from_text(text="[LIVE PRICES] {\"NVDA\": 225.4}")]),
    ]
    ev = B._ask_evidence_text(contents, NS(candidates=[]), "whats the probability on kalshi", uc)
    assert "8.1%" in ev and "225.4" in ev
    assert "humor:54/100" not in ev and "348 msgs" not in ev
    assert "54% honestly" not in ev


# --- finding 7: a regenerated answer ships with its own response -------

def test_plumbing_regen_replaces_the_response_too():
    src = B._ask_pipeline_source()
    i = src.index('if _outcome == "regenerated":')
    assert "response = _plumb_resp" in src[i:i + 400]


# --- finding 8: the repetition retry derives from the routed config ----

def test_repetition_retry_uses_the_routed_config():
    import inspect
    src = inspect.getsource(B._ask_03_assemble_response)
    assert "config" in inspect.signature(B._ask_03_assemble_response).parameters
    i = src.index("_has_repetition_glitch(answer):")
    block = src[i:i + 1200]
    assert "config.model_copy(" in block
    assert "_build_chat_search_tool()" not in block, "hand-listed tools ignore TOOL_POLICY"


# --- finding 9: the transient retry carries out_meta -------------------

def test_transient_retry_passes_out_meta():
    src = B._ask_pipeline_source()
    i = src.index("_transient_retry=True,")
    assert "out_meta=out_meta" in src[i - 600:i + 200]


# --- finding 10: no synchronous db call on the event loop --------------

def test_no_bare_db_call_in_the_async_ask_phases_or_executors():
    """Every db.* call inside an async def in the /ask pipeline and the
    tool executors must go through asyncio.to_thread. A 30 s
    busy_timeout on a bare call parks the whole Discord loop."""
    import ast
    import inspect
    import re
    from discord_bot import ask_tools as T
    offenders = []
    targets = [getattr(B, n) for n in dir(B) if re.match(r"_ask_\d\d_", n)] + [B._answer_with_gemini]
    targets += [getattr(T, n) for n in dir(T) if n.startswith("_execute_")
                and inspect.iscoroutinefunction(getattr(T, n))]
    for fn in targets:
        try:
            tree = ast.parse(_dedent(inspect.getsource(fn)))
        except Exception:
            continue
        fdef = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef))

        def walk(node):
            # a db call inside a nested def or lambda is the thread body
            # handed to to_thread, which is the sanctioned shape
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Lambda | ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) \
                        and isinstance(child.func.value, ast.Name) and child.func.value.id == "db":
                    offenders.append(f"{fn.__name__}: db.{child.func.attr} at line {child.lineno}")
                walk(child)

        walk(fdef)
    assert offenders == [], offenders


def _dedent(src: str) -> str:
    import textwrap
    return textwrap.dedent(src)


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
