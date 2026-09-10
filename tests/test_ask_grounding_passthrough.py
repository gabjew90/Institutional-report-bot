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
    resp_sentinel = NS(candidates=[NS(finish_reason="STOP")])
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
        response=resp_sentinel,
        safety_settings=None,
        types=types,
        user_content="",
    ))
    assert out_gm is sentinel and out_answer == answer
    assert _resp is resp_sentinel, (
        "phase 8 replaced the response on a turn that regenerated nothing; "
        "phase 9 then cannot read finish_reason or the block flags")


def _phase_return_names(fn) -> list[str]:
    import inspect
    import re
    src = inspect.getsource(fn)
    m = re.search(r"^    return \((.*)\)\s*$", src, re.M)
    if not m:
        return []
    return [n.strip() for n in m.group(1).split(",") if n.strip()]


def _binding_sites(fn) -> dict[str, list[tuple[bool, bool]]]:
    """Every binding of a name in a phase body as (is_none_literal,
    is_conditional). A binding is conditional when it sits under an
    if/for/while/with somewhere inside the phase; a top-level try body
    counts as unconditional because the except arm is the deliberate
    None path."""
    import inspect
    return _binding_sites_from_source(inspect.getsource(fn))


def _binding_sites_from_source(src: str) -> dict[str, list[tuple[bool, bool]]]:
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(src))
    fdef = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef | ast.FunctionDef))
    out: dict[str, list[tuple[bool, bool]]] = {}

    def _names(target):
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, ast.Tuple | ast.List):
            return [n for e in target.elts for n in _names(e)]
        return []

    def visit(stmts, conditional, out):
        for s in stmts:
            if isinstance(s, ast.Assign | ast.AnnAssign | ast.AugAssign):
                targets = s.targets if isinstance(s, ast.Assign) else [s.target]
                is_none = isinstance(getattr(s, "value", None), ast.Constant) and s.value.value is None
                for t in targets:
                    for n in _names(t):
                        out.setdefault(n, []).append((is_none, conditional))
            elif isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef):
                out.setdefault(s.name, []).append((False, conditional))
            if isinstance(s, ast.If) and s.orelse:
                # an if/else that binds the name in BOTH arms is exhaustive
                a, b = {}, {}
                visit(s.body, True, a)
                visit(s.orelse, True, b)
                for n in set(a) | set(b):
                    real_a = any(not none for none, _c in a.get(n, []))
                    real_b = any(not none for none, _c in b.get(n, []))
                    if real_a and real_b:
                        out.setdefault(n, []).append((False, conditional))
                    else:
                        out.setdefault(n, []).extend(a.get(n, []) + b.get(n, []))
                continue
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(s, field, None)
                if isinstance(inner, list):
                    nested = conditional or isinstance(s, ast.If | ast.For | ast.While | ast.With | ast.AsyncFor | ast.AsyncWith)
                    visit(inner, nested, out)
            for h in getattr(s, "handlers", []) or []:
                visit(h.body, True, out)

    visit(fdef.body, False, out)
    return out


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
        if "grounding_metadata" not in _phase_return_names(fn):
            continue
        params = inspect.signature(fn).parameters
        builds_it = "response.candidates[0].grounding_metadata" in src
        if "grounding_metadata" not in params and not builds_it:
            offenders.append(fn.__name__)
    assert offenders == [], offenders


def test_no_phase_returns_a_name_it_only_assigns_on_some_paths():
    """The same rule for EVERY returned name, not just grounding
    (2026-09-09): phase 8 None-initialised `response`, never received
    it, assigned it only inside the TA-regen branch, and returned it,
    so phase 9 read None on every non-TA turn. A returned name that is
    not a parameter must have at least one unconditional non-None
    binding in the phase body; a name whose only real bindings sit
    under an `if` is a clobber waiting for the path that skips it."""
    import inspect
    import re
    phases = [getattr(B, n) for n in dir(B) if re.match(r"_ask_\d\d_", n)]
    assert len(phases) >= 11
    # Phase 2 binds `response` and `um` inside the tool loop, whose first
    # iteration always runs; static analysis cannot see that, so they
    # are the one sanctioned exception.
    allowed = {"_ask_02_call_model_with_tools": {"response", "um"}}
    offenders = []
    for fn in phases:
        params = set(inspect.signature(fn).parameters)
        sites = _binding_sites(fn)
        for name in _phase_return_names(fn):
            if name in params or name in allowed.get(fn.__name__, set()):
                continue
            real = [s for s in sites.get(name, []) if not s[0]]
            if real and all(cond for _none, cond in real):
                offenders.append(f"{fn.__name__}: {name} is assigned only on some paths")
    assert offenders == [], offenders


def test_the_clobber_detector_would_have_caught_phase_8():
    """Guard the guard: a synthetic phase in the 09-01..09-09 shape
    must trip the binding analysis."""
    import textwrap
    src = textwrap.dedent('''
        async def _ask_99_fake(answer, question):
            """doc"""
            response = None
            if answer:
                try:
                    response = object()
                except Exception:
                    pass
            return (answer, response)
    ''')
    sites = _binding_sites_from_source(src)
    real = [s for s in sites["response"] if not s[0]]
    assert real and all(cond for _none, cond in real)
    # and the fixed shape (a parameter) is not a binding at all
    fixed = src.replace("(answer, question)", "(answer, question, response)").replace(
        "    response = None\n", "")
    assert all(cond for _n, cond in _binding_sites_from_source(fixed).get("response", []))


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
