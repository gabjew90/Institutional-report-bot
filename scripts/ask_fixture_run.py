#!/usr/bin/env python3
"""Regression harness for the /ask system prompt.

WHY THIS EXISTS
================
CLAUDE.md's prompt-enforcement policy requires prompt text to be
DELETED whenever a rule moves into code, and it makes raising
_SIZE_CEILING owner-only "with evidence from scripts/ask_fixture_run.py".
This is that evidence. Without it, nobody can tell the difference
between text that is load-bearing and text that is merely old, so the
prompt only ever grows.

The harness runs every fixture in tests/ask_fixtures/ against the
CURRENT prompt and reports what behavior survives. Delete a paragraph,
re-run, and the fixtures for the incidents that paragraph prevents are
the ones that fail.

WHAT A FIXTURE IS
=================
One recorded incident from the ask_prompt.py INCIDENT LEDGER (or a
later one), as JSON:
  question       — the user's turn, reply-quote block included
  context        — the injected blocks (profiles / chat / prior answers)
  tool_stubs     — canned tool results, so a run needs no live APIs and
                   two runs of the same fixture see the same data
  grounding_required — counts this turn in the tool-call-rate metric
  expect         — the assertions that define correct behavior

Tool STUBS rather than live tools is deliberate: the prompt governs
which tool the model reaches for, not what the tool returns. Stubbing
makes the harness deterministic and free.

USAGE
=====
  python scripts/ask_fixture_run.py                # run everything
  python scripts/ask_fixture_run.py --only 05 28   # substring match on id
  python scripts/ask_fixture_run.py --repeat 3     # flakiness check
  python scripts/ask_fixture_run.py --offline      # validate fixtures only
  python scripts/ask_fixture_run.py --self-test    # validate the ASSERTIONS
  python scripts/ask_fixture_run.py --json out.json
  python scripts/ask_fixture_run.py --model gemini-3.1-flash-lite
  python scripts/ask_fixture_run.py --baseline docs/prior.json
  python scripts/ask_fixture_run.py --two-condition   # prompt vs no prompt

Exit codes: 0 all passed, 1 one or more failed, 2 harness error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURE_DIR = REPO / "tests" / "ask_fixtures"

# The model under test, pinned HERE and deliberately NOT read from the
# environment. Both earlier baselines were measured against
# gemini-3.1-flash-lite-preview because local .env fell through to
# GEMINI_MODEL while Railway sets ASK_GEMINI_MODEL, and nothing in the
# output made that visible. A baseline is only evidence about the model
# production actually runs, so the runner names it rather than inheriting
# whatever the shell happens to hold. Override with --model.
HARNESS_MODEL = "gemini-3.5-flash-lite"


# ---------------------------------------------------------------- helpers
def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", (text or "").strip()) if w]


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s]


def _repeated_phrase(text: str, n: int) -> str | None:
    """Any n-word phrase occurring more than once — the repetition-glitch
    signature (2026-05-30, 2026-07-22)."""
    clean = re.sub(r"(?m)^\s*(?:[-*→>]|\d+[.)])\s*", " ", text or "")
    clean = clean.replace("**", " ")
    ws = [w.lower().strip(".,!?*_`") for w in _words(clean)]
    seen: dict[tuple, int] = {}
    for i in range(len(ws) - n + 1):
        key = tuple(ws[i:i + n])
        if len(" ".join(key)) < 12:
            continue
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            return " ".join(key)
    return None


# ------------------------------------------------------------ assertions
def evaluate(fx: dict, result: dict) -> list[str]:
    """Return a list of failure strings; empty means the fixture passed."""
    exp = fx.get("expect") or {}
    ans = result.get("answer") or ""
    tools = result.get("tools_called") or []
    grounded = bool(result.get("grounded"))
    fails: list[str] = []

    # An empty answer means the model ended its turn on a tool call the
    # harness ran out of rounds for. Report THAT rather than letting every
    # content assertion fail with a misleading message.
    if not ans.strip():
        return [f"empty answer after {len(tools)} tool call(s) "
                f"({tools}) — model never produced text"]

    want = exp.get("must_call_tools") or []
    if want:
        if exp.get("any_of_tools"):
            if not any(t in tools for t in want):
                fails.append(f"called none of {want} (called {tools or 'nothing'})")
        else:
            missing = [t for t in want if t not in tools]
            if missing:
                fails.append(f"did not call {missing} (called {tools or 'nothing'})")

    for t in exp.get("must_not_call_tools") or []:
        if t in tools:
            fails.append(f"called forbidden tool {t}")

    if exp.get("require_grounding_or_tool") and not (tools or grounded):
        fails.append("answered with no tool call and no web grounding")

    for pat in exp.get("answer_must_match") or []:
        if not re.search(pat, ans):
            fails.append(f"answer missing required pattern {pat!r}")

    any_pats = exp.get("answer_must_match_any") or []
    if any_pats and not any(re.search(p, ans) for p in any_pats):
        fails.append(f"answer matched none of {any_pats}")

    for pat in exp.get("answer_must_not_match") or []:
        m = re.search(pat, ans)
        if m:
            fails.append(f"answer contains banned {pat!r} -> {m.group(0)!r}")

    if "max_words" in exp and len(_words(ans)) > exp["max_words"]:
        fails.append(f"{len(_words(ans))} words > max {exp['max_words']}")
    if "min_words" in exp and len(_words(ans)) < exp["min_words"]:
        fails.append(f"{len(_words(ans))} words < min {exp['min_words']}")
    if "max_sentences" in exp and len(_sentences(ans)) > exp["max_sentences"]:
        fails.append(
            f"{len(_sentences(ans))} sentences > max {exp['max_sentences']}")

    if "no_repeated_phrase" in exp:
        dup = _repeated_phrase(ans, int(exp["no_repeated_phrase"]))
        if dup:
            fails.append(f"repeated phrase: {dup!r}")

    if "min_distinct_names" in exp:
        # Candidates come from the context AND the tool stubs (a group
        # roster lives in the stub, not the profile block), and handles
        # are often 2 chars — "BK" and "Ry" never counted before.
        pool = " ".join((fx.get("context") or {}).values())
        pool += " " + json.dumps(fx.get("tool_stubs") or {})
        names = {n for n in re.findall(r"\b([A-Z][A-Za-z]{1,})\b", pool)
                 if re.search(rf"\b{re.escape(n)}\b", ans)}
        if len(names) < exp["min_distinct_names"]:
            fails.append(
                f"named {len(names)} subjects, need {exp['min_distinct_names']}"
                f" (group-scope answer covered too few)")

    cond = exp.get("if_mentions_then_qualifies")
    if cond and re.search(cond["term"], ans):
        if not re.search(cond["qualifier"], ans):
            fails.append(
                f"mentioned {cond['term']!r} without the required "
                f"qualifier {cond['qualifier']!r}")

    if exp.get("no_invented_details_vs_context"):
        try:
            from discord_bot.bot import _invented_personal_details
            ctx = " ".join((fx.get("context") or {}).values())
            inv = _invented_personal_details(ans, ctx, fx.get("question", ""))
            if inv:
                fails.append(f"invented details not in context: {inv[:5]}")
        except Exception as e:      # detector unavailable -> don't fake a pass
            fails.append(f"invented-detail check unavailable: {e}")

    if exp.get("answer_must_not_match_unsourced_pct"):
        if not (tools or grounded) and re.search(r"\d+(\.\d+)?\s?%", ans):
            fails.append("stated a percentage with no tool and no grounding")

    return fails


# ------------------------------------------------------------- execution
def build_user_content(fx: dict) -> str:
    ctx = fx.get("context") or {}
    parts = []
    if ctx.get("profiles"):
        parts.append("WHO'S TALKING (background on people active in "
                     "this conversation):\n" + ctx["profiles"])
    if ctx.get("callers"):
        parts.append(ctx["callers"])
    if ctx.get("prior_answers"):
        parts.append(
            "[YOUR RECENT /ASK ANSWERS TO THIS ASKER — anti-recycling "
            "guard]\n" + ctx["prior_answers"])
    if ctx.get("chat"):
        parts.append(ctx["chat"])
    parts.append("--- the asker is asking: ---\n" + fx.get("question", ""))
    return "\n\n".join(parts)


def run_fixture(fx: dict, client, model, tools, safety) -> dict:
    """One live turn: model -> (optional stubbed tool round) -> answer."""
    from google.genai import types
    from discord_bot.bot import _build_runtime_system_instruction

    sysinst = _build_runtime_system_instruction("")
    contents = [types.Content(
        role="user",
        parts=[types.Part.from_text(text=build_user_content(fx))])]
    cfg = types.GenerateContentConfig(
        system_instruction=sysinst, tools=tools,
        # Mirrors production exactly: google_search rides alongside the
        # function tools, which the API only allows with this flag.
        tool_config=types.ToolConfig(
            include_server_side_tool_invocations=True),
        safety_settings=safety, max_output_tokens=1200, temperature=0.2,
    )
    resp = client.models.generate_content(
        model=model, contents=contents, config=cfg)

    tools_called: list[str] = []
    grounded = False
    # The requested string is an alias and can move server-side without
    # notice (that is how a "-preview" build silently replaced the model
    # two baselines were measured on). Record what the server says it ran.
    model_version = getattr(resp, "model_version", None)

    def _scan(r):
        nonlocal grounded
        try:
            cand = r.candidates[0]
        except (AttributeError, IndexError):
            return []
        gm = getattr(cand, "grounding_metadata", None)
        if getattr(gm, "grounding_chunks", None):
            grounded = True
        calls = []
        for p in (getattr(cand.content, "parts", None) or []):
            fc = getattr(p, "function_call", None)
            if fc and getattr(fc, "name", None):
                calls.append(fc)
        return calls

    calls = _scan(resp)
    for c in calls:
        if c.name not in tools_called:
            tools_called.append(c.name)

    # Stubbed tool rounds, mirroring production's loop shape. FOUR rounds:
    # a model that chains tool calls is normal (6 observed), and stopping
    # early leaves the answer empty, a content failure it is not.
    stubs = fx.get("tool_stubs") or {}
    for _round in range(6):
        if not calls:
            break
        contents.append(resp.candidates[0].content)
        parts = []
        for c in calls:
            payload = stubs.get(c.name, {
                "status": "empty",
                "note": ("No fixture stub for this tool. Answer from what "
                         "you have; do not invent data."),
            })
            parts.append(types.Part.from_function_response(
                name=c.name, response={"result": payload}))
        contents.append(types.Content(role="user", parts=parts))
        resp = client.models.generate_content(
            model=model, contents=contents, config=cfg)
        calls = _scan(resp)
        for c in calls:
            if c.name not in tools_called:
                tools_called.append(c.name)

    try:
        answer = (resp.text or "").strip()
    except Exception:
        answer = ""
    return {"answer": answer, "tools_called": tools_called,
            "grounded": grounded, "model_version": model_version}


def _expect_hash(fx: dict) -> str:
    """Stable hash of one fixture's assertions."""
    blob = json.dumps(fx.get("expect") or {}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def suite_fingerprint(fixtures: list[dict] | None = None) -> str:
    """Hash of every fixture's id + assertions.

    A baseline is only comparable to a run of the same assertions.
    Tightening one fixture changes this, which turns a silent mismatch
    into a visible one.
    """
    # ALWAYS the full suite on disk, never the --only subset: a filtered
    # run must still be comparable to a full baseline, and fingerprinting
    # the subset made every partial run look like an assertion change.
    fixtures = load_fixtures(None) if fixtures is None else fixtures
    blob = json.dumps(
        sorted((fx.get("id"), _expect_hash(fx)) for fx in fixtures))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def run_two_condition(fixtures: list[dict], client, model, tools,
                      safety) -> list[dict]:
    """Every fixture twice: with the system prompt, and with none.

    Measures ONE thing — whether the prompt changes which source the model
    reaches for on the FIRST turn. Deliberately a single call per arm with
    no stubbed tool rounds: routing is decided on turn one, and running
    the loop would let a later round mask the initial choice.

    A prompt that suppresses grounding is not hypothetical. On
    gemini-3.1-flash-lite-preview the same fixtures went 0/3 grounded with
    the prompt and 3/3 without it. This asks whether the production model
    shows a smaller version of the same effect.
    """
    from google.genai import types
    from discord_bot.bot import _build_runtime_system_instruction

    sysinst = _build_runtime_system_instruction("")
    out = []
    for fx in fixtures:
        row = {"id": fx["id"],
               "grounding_required": bool(fx.get("grounding_required"))}
        for arm, si in (("with_prompt", sysinst), ("no_prompt", None)):
            cfg_kw = dict(
                tools=tools,
                tool_config=types.ToolConfig(
                    include_server_side_tool_invocations=True),
                safety_settings=safety, max_output_tokens=1200,
                temperature=0.2)
            if si:
                cfg_kw["system_instruction"] = si
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[types.Content(role="user", parts=[
                        types.Part.from_text(
                            text=build_user_content(fx))])],
                    config=types.GenerateContentConfig(**cfg_kw))
                cand = resp.candidates[0]
                gm = getattr(cand, "grounding_metadata", None)
                grounded = bool(getattr(gm, "grounding_chunks", None))
                calls = [pp.function_call.name
                         for pp in (getattr(cand.content, "parts", None) or [])
                         if getattr(pp, "function_call", None)]
                row[arm] = {"grounded": grounded, "tools_called": calls,
                            "sourced": bool(grounded or calls), "error": None}
            except Exception as e:
                row[arm] = {"grounded": False, "tools_called": [],
                            "sourced": None,
                            "error": f"{type(e).__name__}: {str(e)[:100]}"}
        w, n = row["with_prompt"], row["no_prompt"]
        if w["sourced"] is None or n["sourced"] is None:
            row["delta"] = "error"
        elif w["sourced"] == n["sourced"]:
            row["delta"] = "same"
        elif n["sourced"] and not w["sourced"]:
            row["delta"] = "prompt_suppressed"
        else:
            row["delta"] = "prompt_induced"
        print(f"  {row['id']:<34} with={w['sourced']!s:<5} "
              f"without={n['sourced']!s:<5} {row['delta']}")
        out.append(row)
    return out


def baseline_guard(path: str, model: str,
                   allow_model: bool, allow_suite: bool) -> tuple[dict, list]:
    """Refuse to compare a run against a baseline it is not comparable to.

    A baseline answers "did this change break something". That only holds
    if the two runs differ in the ONE thing under test. Two ways it
    silently stops holding, both of which have already happened here:

      model   both prior baselines were recorded on
              gemini-3.1-flash-lite-preview while production runs
              gemini-3.5-flash-lite, so their numbers described a model
              no user reaches.
      suite   ask-baseline-01f124a was recorded before four fixtures had
              their assertions tightened, so it measured a different
              suite than any later run.

    Neither was detectable from the output at the time. Both are now.
    """
    blockers = []
    try:
        base = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        return {}, [f"cannot read baseline {path}: {e}"]

    if base.get("invalid_for_deletion_evidence"):
        blockers.append(
            f"baseline is marked INVALID: "
            f"{base.get('invalid_reason', '(no reason recorded)')}")

    bm = base.get("model")
    if bm is None:
        blockers.append(
            "baseline predates the `model` field, so the model it "
            "measured cannot be established (--allow-model-change to "
            "proceed anyway)" if not allow_model else "")
    elif bm != model and not allow_model:
        blockers.append(
            f"model mismatch: baseline ran {bm!r}, this run is {model!r}. "
            f"Pass --allow-model-change if the model change IS what you "
            f"are measuring.")

    bf = base.get("suite_fingerprint")
    cf = suite_fingerprint()
    if bf and bf != cf and not allow_suite:
        blockers.append(
            f"suite mismatch: baseline fingerprint {bf}, current {cf}. "
            f"The assertions changed, so a pass-rate delta mixes a "
            f"behavior change with an assertion change. Pass "
            f"--allow-suite-change to proceed.")

    return base, [b for b in blockers if b]


def compare_to_baseline(base: dict, records: list[dict]) -> None:
    """Print per-fixture transitions against a baseline."""
    prior = {f["id"]: f for f in base.get("fixtures") or []}
    moved = []
    for r in records:
        b = prior.get(r["id"])
        if not b:
            moved.append((r["id"], "(new)", r["status"], "", ""))
            continue
        if b.get("status") != r["status"]:
            moved.append((r["id"], b.get("status"), r["status"],
                          f"{b.get('attempts_passed','?')}/"
                          f"{b.get('attempts','?')}",
                          f"{r['attempts_passed']}/{r['attempts']}"))
    print("\n" + "-" * 64)
    print(f"VS BASELINE  model={base.get('model')} "
          f"suite={base.get('suite_fingerprint')} "
          f"pass={base.get('passed')}/{base.get('total')}")
    if not moved:
        print("  no fixture changed status")
    for fid, was, now, wa, na in moved:
        print(f"  {fid:<34} {was:>5} -> {now:<5} {wa:>5} -> {na}")


def load_fixtures(only: list[str] | None) -> list[dict]:
    out = []
    for p in sorted(FIXTURE_DIR.glob("*.json")):
        fx = json.loads(p.read_text(encoding="utf-8"))
        fx["_file"] = p.name
        if only and not any(o in fx["id"] for o in only):
            continue
        out.append(fx)
    return out


def validate_fixtures(fixtures: list[dict]) -> list[str]:
    """Structural checks that need no API — also the --offline mode."""
    problems = []
    seen = set()
    for fx in fixtures:
        fid = fx.get("id")
        if not fid:
            problems.append(f"{fx['_file']}: missing id")
        if fid in seen:
            problems.append(f"duplicate id {fid}")
        seen.add(fid)
        for key in ("ledger", "title", "why", "question", "expect"):
            if not fx.get(key):
                problems.append(f"{fid}: missing {key}")
        for pat in ((fx.get("expect") or {}).get("answer_must_match") or []) + \
                ((fx.get("expect") or {}).get("answer_must_not_match") or []):
            try:
                re.compile(pat)
            except re.error as e:
                problems.append(f"{fid}: bad regex {pat!r} ({e})")
        # A fixture with no synthetic pair has never been shown to tell a
        # correct answer from a wrong one, so its live result carries no
        # evidentiary weight. Required, not optional.
        st = fx.get("self_test") or {}
        for side in ("good", "bad"):
            if not (st.get(side) or {}).get("answer"):
                problems.append(
                    f"{fid}: missing self_test.{side}.answer — run "
                    f"--self-test; every fixture needs a hand-written "
                    f"answer that passes and one that fails")
    return problems


def ledger_coverage() -> tuple[set, set]:
    """Ledger dates in the prompt docstring vs dates the fixtures cover."""
    import discord_bot.ask_prompt as ap
    doc = ap.__doc__ or ""
    ledger = set(re.findall(r"^\s{2}(2026-\d{2}-\d{2})", doc, re.M))
    covered = set()
    for p in FIXTURE_DIR.glob("*.json"):
        covered.add(json.loads(p.read_text(encoding="utf-8")).get("ledger"))
    return ledger, covered


def run_self_test(fixtures: list[dict]) -> int:
    """Validate the assertions themselves, with no model in the loop.

    Each fixture carries two hand-written synthetic results: a `good` one
    built to satisfy every assertion, and a `bad` one built to violate at
    least one. A usable fixture passes the good result and fails the bad
    one. Anything else is a defect in the fixture, not in the prompt:

      TOO WEAK  passes both — the assertion cannot see the regression it
                exists to catch (this is the shape fixture 27's
                name-length bug had: a correct answer and a wrong answer
                scored identically).
      BROKEN    fails both — the assertion rejects even a correct answer,
                so any live failure it reports is uninformative.
      MISSING   no synthetic pair, so the fixture is unvalidated.

    Returns a process exit code.
    """
    print("SELF-TEST — assertions vs. hand-written good/bad answers")
    print("(no model calls; this validates the harness, not the prompt)\n")

    ok = weak = broken = missing = 0
    for fx in fixtures:
        fid = fx["id"]
        st = fx.get("self_test") or {}
        good, bad = st.get("good"), st.get("bad")
        if not good or not bad:
            print(f"MISSING   {fid}")
            print("          no self_test.good / self_test.bad pair\n")
            missing += 1
            continue

        gf = evaluate(fx, good)
        bf = evaluate(fx, bad)

        if not gf and bf:
            ok += 1
            print(f"OK        {fid}")
            print(f"          catches: {bf[0]}")
            continue

        if not gf and not bf:
            weak += 1
            print(f"TOO WEAK  {fid}")
            print("          the bad answer passed every assertion — this "
                  "fixture cannot detect a regression")
            print(f"          bad answer: {(bad.get('answer') or '')[:160]!r}")
        elif gf and bf:
            broken += 1
            print(f"BROKEN    {fid}")
            print("          the good answer was rejected too, so live "
                  "failures from this fixture mean nothing")
            for f in gf:
                print(f"          good-answer failure: {f}")
        else:   # gf and not bf — inverted
            broken += 1
            print(f"BROKEN    {fid}")
            print("          INVERTED: rejects the good answer, accepts "
                  "the bad one")
            for f in gf:
                print(f"          good-answer failure: {f}")
        print()

    total = len(fixtures)
    print("-" * 62)
    print(f"OK {ok}/{total} | TOO WEAK {weak} | BROKEN {broken} | "
          f"MISSING {missing}")
    if weak or broken or missing:
        print("\nThe harness is NOT cleared to authorize prompt deletion "
              "until every fixture is OK.")
        return 1
    print("\nEvery fixture separates a correct answer from a wrong one. "
          "Assertions are cleared for use as deletion evidence.")
    return 0


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--only", nargs="*", default=None)
    ap_.add_argument("--repeat", type=int, default=1)
    ap_.add_argument("--offline", action="store_true")
    ap_.add_argument("--self-test", dest="self_test", action="store_true",
                     help="validate each fixture's assertions against a "
                          "hand-written good and bad answer; no model calls")
    ap_.add_argument("--model", default=HARNESS_MODEL,
                     help=f"model under test (default {HARNESS_MODEL}); "
                          f"NOT read from the environment")
    ap_.add_argument("--baseline", default=None,
                     help="prior baseline JSON to compare this run against")
    ap_.add_argument("--allow-model-change", dest="allow_model_change",
                     action="store_true",
                     help="permit comparing against a baseline recorded on "
                          "a different model")
    ap_.add_argument("--allow-suite-change", dest="allow_suite_change",
                     action="store_true",
                     help="permit comparing against a baseline recorded "
                          "with different assertions")
    ap_.add_argument("--two-condition", dest="two_condition",
                     action="store_true",
                     help="also run every fixture with NO system prompt and "
                          "record the grounding/tool-routing delta")
    ap_.add_argument("--json", dest="json_out", default=None)
    args = ap_.parse_args()

    fixtures = load_fixtures(args.only)
    if not fixtures:
        print("no fixtures matched")
        return 2

    problems = validate_fixtures(fixtures)
    if problems:
        print("FIXTURE VALIDATION FAILED")
        for p in problems:
            print("  " + p)
        return 2

    ledger, covered = ledger_coverage()
    missing = sorted(ledger - covered)
    print(f"prompt size: ", end="")
    try:
        import discord_bot.ask_prompt as ap_mod
        print(f"{len(ap_mod._ASK_SYSTEM_INSTRUCTION):,} chars")
    except Exception:
        print("(unavailable)")
    print(f"ledger incidents: {len(ledger)} | covered by fixtures: "
          f"{len(ledger & covered)}"
          + (f" | UNCOVERED: {missing}" if missing else ""))
    print(f"fixtures: {len(fixtures)}\n")

    if args.self_test:
        return run_self_test(fixtures)

    if args.offline:
        print("offline mode — fixtures structurally valid, no model run")
        return 0

    try:
        from google import genai
        from google.genai import types
        from config import settings
        from discord_bot.bot import (
            _build_chat_search_tool, _build_user_profile_tool,
            _build_trade_log_tool, _build_market_price_tool,
            _build_options_chain_tool, _build_economic_calendar_tool,
            _build_earnings_date_tool, _build_query_data_tool,
            _build_price_history_tool, _build_fantasy_league_tool,
        )
    except Exception as e:
        print(f"HARNESS ERROR: cannot import bot surface ({e})")
        return 2

    key = settings.google_ask_api_key or settings.google_api_key
    if not key:
        print("HARNESS ERROR: no GOOGLE_API_KEY / GOOGLE_ASK_API_KEY set")
        return 2
    client = genai.Client(api_key=key)
    model = args.model
    print(f"model under test: {model}  (pinned in the runner; "
          f"env ASK_GEMINI_MODEL/GEMINI_MODEL are ignored)\n")

    baseline = {}
    if args.baseline:
        baseline, blockers = baseline_guard(
            args.baseline, model,
            args.allow_model_change, args.allow_suite_change)
        if blockers:
            print(f"REFUSING TO COMPARE against {args.baseline}")
            for b in blockers:
                print("  - " + b)
            return 2

    tool_list = [
        types.Tool(google_search=types.GoogleSearch()),
        _build_chat_search_tool(), _build_user_profile_tool(),
        _build_trade_log_tool(), _build_market_price_tool(),
        _build_options_chain_tool(), _build_economic_calendar_tool(),
        _build_earnings_date_tool(), _build_query_data_tool(),
        _build_price_history_tool(),
        # Registered unconditionally, NOT gated on settings like
        # production gates it. Railway has SLEEPER_LEAGUE_ID set, so the
        # deployed bot always sees this tool; a local run without the env
        # var saw a DIFFERENT tool list and fixture 27 was asserting a
        # tool that was never declared — unpassable for a reason that had
        # nothing to do with the prompt. Same failure shape as the model
        # falling through to GEMINI_MODEL: the harness must mirror the
        # deployed tool set, not the local shell. Fixtures stub the tool,
        # so no live Sleeper access is involved.
        _build_fantasy_league_tool(),
    ]
    safety = [
        types.SafetySetting(category=c, threshold="BLOCK_NONE")
        for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                  "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                  "HARM_CATEGORY_DANGEROUS_CONTENT")
    ]

    records = []
    model_versions: set[str] = set()
    passed = 0
    ground_turns = 0
    ground_satisfied = 0

    for fx in fixtures:
        attempts = []
        for _ in range(max(1, args.repeat)):
            try:
                res = run_fixture(fx, client, model, tool_list, safety)
                fails = evaluate(fx, res)
            except Exception as e:
                res = {"answer": "", "tools_called": [], "grounded": False}
                fails = [f"RUN ERROR: {type(e).__name__}: {str(e)[:140]}"]
            attempts.append((res, fails))

        ok = all(not f for _r, f in attempts)
        flaky = (not ok) and any(not f for _r, f in attempts)
        res0, fails0 = next(((r, f) for r, f in attempts if f), attempts[0])
        n_ok = sum(1 for _r, f in attempts if not f)
        for _r, _f in attempts:
            mv = _r.get("model_version")
            if mv:
                model_versions.add(mv)

        if fx.get("grounding_required"):
            ground_turns += 1
            if res0.get("tools_called") or res0.get("grounded"):
                ground_satisfied += 1

        status = "PASS" if ok else ("FLAKY" if flaky else "FAIL")
        passed += 1 if ok else 0
        mark = {"PASS": "PASS ", "FLAKY": "FLAKY", "FAIL": "FAIL "}[status]
        print(f"{mark} {n_ok}/{len(attempts)} {fx['id']:<34} "
              f"[{fx['ledger']}] {fx['title']}")
        if not ok:
            for f in fails0[:4]:
                print(f"        - {f}")
            print(f"        answer: {(res0.get('answer') or '')[:150]!r}")
            print(f"        tools: {res0.get('tools_called')} "
                  f"grounded={res0.get('grounded')}")
        # Per-attempt detail, not just the rolled-up status: the signal
        # that separates a regression from noise is 3/3 dropping to 2/3
        # on one fixture, and an aggregate cannot show that.
        records.append({"id": fx["id"], "ledger": fx["ledger"],
                        "status": status,
                        "attempts": len(attempts),
                        "attempts_passed": n_ok,
                        "expect_hash": _expect_hash(fx),
                        "per_attempt": [
                            {"passed": not f, "failures": f,
                             "tools_called": r.get("tools_called"),
                             "grounded": r.get("grounded")}
                            for r, f in attempts],
                        "failures": fails0,
                        "tools_called": res0.get("tools_called"),
                        "grounded": res0.get("grounded"),
                        "answer": res0.get("answer")})

    total = len(fixtures)
    rate = 100.0 * passed / max(total, 1)
    grate = 100.0 * ground_satisfied / max(ground_turns, 1)
    print("\n" + "=" * 64)
    print(f"OVERALL PASS RATE          {passed}/{total}  ({rate:.0f}%)")
    print(f"TOOL-CALL RATE (grounding) {ground_satisfied}/{ground_turns}"
          f"  ({grate:.0f}%)   <- turns marked grounding_required that "
          f"called a tool or grounded")
    print("=" * 64)

    if baseline:
        compare_to_baseline(baseline, records)

    two_cond = None
    if args.two_condition:
        print("\n" + "=" * 64)
        print("TWO-CONDITION: every fixture with the prompt and without it")
        print("(one call per arm, first-turn routing only)")
        print("=" * 64)
        two_cond = run_two_condition(fixtures, client, model, tool_list,
                                     safety)
        supp = [r["id"] for r in two_cond if r["delta"] == "prompt_suppressed"]
        ind = [r["id"] for r in two_cond if r["delta"] == "prompt_induced"]
        same = [r for r in two_cond if r["delta"] == "same"]
        errs = [r["id"] for r in two_cond if r["delta"] == "error"]
        print("\n" + "-" * 64)
        print(f"TWO-CONDITION DELTA   same {len(same)} | "
              f"prompt suppressed sourcing {len(supp)} | "
              f"prompt induced sourcing {len(ind)}"
              + (f" | errors {len(errs)}" if errs else ""))
        if supp:
            print(f"  suppressed: {supp}")
        if ind:
            print(f"  induced:    {ind}")
        gr = [r for r in two_cond if r["grounding_required"]]
        gw = sum(1 for r in gr if r["with_prompt"]["sourced"])
        gn = sum(1 for r in gr if r["no_prompt"]["sourced"])
        print(f"  grounding-required turns sourced: "
              f"with prompt {gw}/{len(gr)} | without {gn}/{len(gr)}")

    if args.json_out:
        try:
            import discord_bot.ask_prompt as _ap
            prompt_chars = len(_ap._ASK_SYSTEM_INSTRUCTION)
        except Exception:
            prompt_chars = None
        Path(args.json_out).write_text(json.dumps({
            "suite_fingerprint": suite_fingerprint(),
            "ran_subset": bool(args.only),
            "prompt_chars": prompt_chars,
            "repeat": max(1, args.repeat),
            "model": model,
            "model_pinned_in_runner": model == HARNESS_MODEL,
            "model_versions_seen": sorted(model_versions),
            "compared_against": args.baseline,
            "pass_rate": rate, "passed": passed, "total": total,
            "tool_call_rate_grounding": grate,
            "grounding_turns": ground_turns,
            "grounding_satisfied": ground_satisfied,
            "fixtures": records,
            "two_condition": two_cond,
        }, indent=1), encoding="utf-8")
        print(f"wrote {args.json_out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
