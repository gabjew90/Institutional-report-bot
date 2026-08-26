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

# Request parameters, mirrored from the production /ask call in
# discord_bot/bot.py (the generate_content config around line 6142).
# These are NOT free harness choices: a different token cap or
# temperature is a different experiment. The old values (1200 / 0.2)
# were the source of the "empty answer after N tool calls" failures.
HARNESS_MAX_OUTPUT_TOKENS = 5000
HARNESS_TEMPERATURE = 0.3
# Sixth divergence, found 2026-08-26: production sets a 2000-token
# thinking budget and the harness set none. Thinking budget drives
# tool-use decisions, so every grounding number measured without it
# described a model reasoning less than the deployed one does.
HARNESS_THINKING_BUDGET = 2000

# ---------------------------------------------------------------------
# PRODUCTION CONFIG SNAPSHOT
# ---------------------------------------------------------------------
# Three times a harness result was corrupted before anyone noticed,
# always the same way: the local process resolved a different config than
# the deployed worker, and nothing in the output said so.
#
#   1. ASK_GEMINI_MODEL unset locally -> resolution fell through to
#      GEMINI_MODEL, so two baselines measured a model production never
#      runs.
#   2. GEMINI_MODEL pinned to a "-preview" alias locally -> the alias
#      moved server-side between runs and looked like a prompt
#      regression.
#   3. SLEEPER_LEAGUE_ID unset locally -> lookup_fantasy_league was
#      never declared, so fixture 27 asserted a tool that did not exist
#      in the request and could not pass for any prompt.
#
# Every one of those was invisible until someone went looking. This
# table makes them loud. Values are a snapshot of the Railway `worker`
# service; update it deliberately when production changes, never to
# silence a diff.
PRODUCTION_CONFIG = {
    # what model answers an /ask turn
    "ask_model": "gemini-3.5-flash-lite",
    # whether lookup_fantasy_league is in the declared tool set
    "fantasy_tool_registered": True,
    # generation config on the /ask call
    "max_output_tokens": 5000,
    "temperature": 0.3,
    "thinking_budget": 2000,
    "include_server_side_tool_invocations": True,
    "safety_all_block_none": True,
    # how many declared function tools ride alongside google_search
    "function_tool_count": 10,
    # production declares google_search on every turn
    "search_on_every_fixture": True,
}

# Differences that are DELIBERATE. Each needs a reason, and the reason is
# printed, so an unexplained entry is visible in review.
ALLOWED_CONFIG_DIFFS = {
    # google_search is withheld from fixtures that do not test grounding.
    # Production always declares it; this saves the grounding quota that
    # is the suite's binding cost. grounding_required fixtures and any
    # fixture with `search_tool: true` still get it, so nothing that
    # MEASURES grounding is affected. Override with --always-search.
    "search_on_every_fixture": (
        "withheld from fixtures that are neither grounding_required nor "
        "search_tool:true — saves grounding quota without changing what "
        "any grounding test measures"),
}


def resolve_harness_config(tool_list=None, model=None) -> dict:
    """What THIS process will actually send, resolved the same way the
    request builder resolves it."""
    cfg = {
        "ask_model": model or HARNESS_MODEL,
        "max_output_tokens": HARNESS_MAX_OUTPUT_TOKENS,
        "temperature": HARNESS_TEMPERATURE,
        "thinking_budget": HARNESS_THINKING_BUDGET,
        "include_server_side_tool_invocations": True,
        "safety_all_block_none": True,
    }
    if tool_list is not None:
        names = []
        for t in tool_list:
            for fd in (getattr(t, "function_declarations", None) or []):
                names.append(fd.name)
        cfg["fantasy_tool_registered"] = "lookup_fantasy_league" in names
        cfg["function_tool_count"] = len(names)
    cfg["search_on_every_fixture"] = False
    return cfg


def config_guard(cfg: dict) -> list[str]:
    """Readable diff of harness config against production. Runs BEFORE
    any API call, so a mismatch costs nothing but a message."""
    blockers = []
    for key, want in PRODUCTION_CONFIG.items():
        if key not in cfg:
            continue
        got = cfg[key]
        if got == want:
            continue
        if key in ALLOWED_CONFIG_DIFFS:
            print(f"  config diff ALLOWED  {key}: harness={got!r} "
                  f"production={want!r}  ({ALLOWED_CONFIG_DIFFS[key]})")
            continue
        blockers.append(
            f"{key}: harness={got!r} production={want!r}")
    return blockers


def config_fingerprint(cfg: dict) -> str:
    """Hash of the resolved config, recorded next to suite_fingerprint.

    A baseline measured under a different config is not comparable, for
    the same reason a baseline measured under different assertions is
    not comparable.
    """
    keys = sorted(set(PRODUCTION_CONFIG) | set(cfg))
    blob = json.dumps([(k, cfg.get(k)) for k in keys], default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]



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
    # Server-side URL fetch result, mirroring _maybe_fetch_user_urls.
    # Its ABSENCE is the whole point of the blocked-domain fixtures: a
    # blocked host returns "", so the model sees a bare link and no
    # content, with nothing telling it the content is missing.
    if ctx.get("fetched_urls"):
        parts.append(ctx["fetched_urls"])
    parts.append("--- the asker is asking: ---\n" + fx.get("question", ""))
    return "\n\n".join(parts)


def run_fixture(fx: dict, client, model, tools, safety,
                skip_search: bool = True) -> dict:
    """One live turn: model -> (optional stubbed tool round) -> answer."""
    from google.genai import types
    from discord_bot.bot import _build_runtime_system_instruction

    # GROUNDING QUOTA. google_search is billed separately from tokens
    # (free tier 5,000 grounded prompts/month, then ~$14/1000) and is the
    # binding cost of running this suite. A fixture that does not test
    # grounding does not need the tool declared, so it is withheld unless
    # the fixture is `grounding_required` or opts in with
    # `search_tool: true`.
    #
    # This IS a deliberate divergence from production, which always
    # declares google_search — recorded in ALLOWED_CONFIG_DIFFS rather
    # than left silent. The opt-in exists because a fixture can depend on
    # search being AVAILABLE without being grounding_required: fixture
    # 40's whole failure mode is confabulating FROM search results, and
    # withholding the tool would hide the behaviour it exists to catch.
    if (skip_search and not fx.get("grounding_required")
            and not fx.get("search_tool")):
        tools = [t for t in tools
                 if not getattr(t, "google_search", None)]

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
        safety_settings=safety,
        max_output_tokens=HARNESS_MAX_OUTPUT_TOKENS,
        temperature=HARNESS_TEMPERATURE,
        thinking_config=types.ThinkingConfig(
            thinking_budget=HARNESS_THINKING_BUDGET),
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

    # ROUND-CAP FINAL ANSWER — seventh config divergence, found
    # 2026-08-26. When the tool loop hits its cap with a call still
    # pending, production does NOT ship an empty answer: it makes one
    # more call with the data-fetching tools withheld (code execution
    # kept, per bot.py's 2026-07-29 note) to force text out. The harness
    # returned "" instead, which is where every "empty answer after N
    # tool call(s)" failure came from. Those were never behaviour
    # failures — they were the harness missing a production rung, and
    # they were written off as an unavoidable harness limitation for
    # weeks.
    # Fires whenever the loop produced tool calls but no text, not
    # only when calls are still pending at the cap: a model can end
    # a round with neither text nor a new call, and that shipped an
    # empty answer too.
    if not answer and tools_called:
        try:
            cap_cfg = types.GenerateContentConfig(
                system_instruction=sysinst,
                tools=[types.Tool(
                    code_execution=types.ToolCodeExecution())],
                safety_settings=safety,
                max_output_tokens=HARNESS_MAX_OUTPUT_TOKENS,
                temperature=HARNESS_TEMPERATURE,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=HARNESS_THINKING_BUDGET),
            )
            # Production does NOT resend `contents` unchanged — it
            # appends an [ANSWER NOW] user turn telling the model its
            # tool budget is spent. Without it the model simply requests
            # another tool and returns no text again, which is why the
            # rung looked fixed on an isolated fixture and still left
            # empty answers across a full run.
            cap_contents = list(contents) + [types.Content(
                role="user",
                parts=[types.Part.from_text(text=(
                    "[ANSWER NOW] You've used your tool budget. "
                    "Do NOT request more data. Write the answer "
                    "from what you already retrieved above. If "
                    "some piece is genuinely missing, answer "
                    "with what you have and say plainly what "
                    "you couldn't get."
                ))],
            )]
            cap_resp = client.models.generate_content(
                model=model, contents=cap_contents, config=cap_cfg)
            answer = (cap_resp.text or "").strip()
        except Exception:
            pass

    # Score what the USER sees, not what the model first emitted.
    # 07b asserts a user never sees plumbing; since 2026-08-26 that rule
    # lives in ask_response_validate, and production runs the same guard
    # ladder before sending. Scoring the raw draft measured a stage the
    # rule no longer lives at, so the harness runs the same ladder:
    # regenerate once, then strip, via the SHARED decision function.
    from scripts.ask_response_validate import (
        validate as _validate, resolve_violations as _resolve)
    from discord_bot.bot import _strip_sentences

    # Same context production passes: the question and whatever the
    # server-side fetcher actually retrieved.
    _vctx = {"question": fx.get("question", ""),
             "fetched": (fx.get("context") or {}).get("fetched_urls")}
    guard_outcome = "clean"
    if answer and _validate(answer, tools_called, **_vctx):
        retry_answer = ""
        try:
            retry_cfg = types.GenerateContentConfig(
                system_instruction=sysinst,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                safety_settings=safety,
                max_output_tokens=HARNESS_MAX_OUTPUT_TOKENS,
                temperature=0.7,   # mirrors the bot's retry temperature
                thinking_config=types.ThinkingConfig(
                    thinking_budget=HARNESS_THINKING_BUDGET),
            )
            retry_resp = client.models.generate_content(
                model=model, contents=contents, config=retry_cfg)
            retry_answer = (retry_resp.text or "").strip()
        except Exception:
            retry_answer = ""
        answer, guard_outcome = _resolve(
            answer, retry_answer, tools_called, _strip_sentences,
            **_vctx)

    return {"answer": answer, "tools_called": tools_called,
            "grounded": grounded, "model_version": model_version,
            "guard_outcome": guard_outcome}


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
                safety_settings=safety,
                max_output_tokens=HARNESS_MAX_OUTPUT_TOKENS,
                temperature=HARNESS_TEMPERATURE,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=HARNESS_THINKING_BUDGET))
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


def baseline_guard(path: str, model: str, allow_model: bool,
                   allow_suite: bool, allow_config: bool = False,
                   resolved_cfg: dict | None = None) -> tuple[dict, list]:
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

    bc = base.get("config_fingerprint")
    # MUST be the same resolved config the run will send, tool list
    # included. Rebuilding a partial one here made every comparison
    # a false mismatch.
    cc = config_fingerprint(
        resolved_cfg if resolved_cfg is not None
        else resolve_harness_config(model=model))
    if bc is None:
        blockers.append(
            "baseline predates the `config_fingerprint` field, so the "
            "config it measured cannot be established "
            "(--allow-config-change to proceed anyway)"
            if not allow_config else "")
    elif bc != cc and not allow_config:
        blockers.append(
            f"config mismatch: baseline fingerprint {bc}, current {cc}. "
            f"A different model, tool set, token cap or temperature is a "
            f"different experiment. Pass --allow-config-change if the "
            f"config change IS what you are measuring.")

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
    ap_.add_argument("--allow-config-change", dest="allow_config_change",
                     action="store_true",
                     help="permit comparing against a baseline recorded "
                          "under a different resolved config")
    ap_.add_argument("--allow-suite-change", dest="allow_suite_change",
                     action="store_true",
                     help="permit comparing against a baseline recorded "
                          "with different assertions")
    ap_.add_argument("--always-search", dest="always_search",
                     action="store_true",
                     help="declare google_search on every fixture, not "
                          "just grounding_required ones (costs grounding "
                          "quota; production always declares it)")
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

    # NOTE: the baseline comparison happens AFTER the tool list is built,
    # further down. It needs the fully resolved config (tool set
    # included) to fingerprint, and rebuilding a partial one here made
    # every comparison a false mismatch.
    baseline = {}

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

    # Config assertion. Runs after the tool list exists (it is part of
    # the config) but before the first API call, so a mismatch costs a
    # message rather than a corrupted run.
    resolved_cfg = resolve_harness_config(tool_list, model)
    cfg_blockers = config_guard(resolved_cfg)
    if cfg_blockers:
        print("HARNESS CONFIG DOES NOT MATCH PRODUCTION — refusing to run.")
        print("A result measured under a different config is evidence "
              "about a system nobody deploys.\n")
        for b in cfg_blockers:
            print("  " + b)
        print("\nFix the harness to match, or add a reasoned entry to "
              "ALLOWED_CONFIG_DIFFS. Do not update PRODUCTION_CONFIG "
              "unless production actually changed.")
        return 2
    print(f"config OK — matches production on "
          f"{len(PRODUCTION_CONFIG)} checked keys "
          f"(fingerprint {config_fingerprint(resolved_cfg)})\n")

    if args.baseline:
        baseline, blockers = baseline_guard(
            args.baseline, model, args.allow_model_change,
            args.allow_suite_change, args.allow_config_change,
            resolved_cfg)
        if blockers:
            print(f"REFUSING TO COMPARE against {args.baseline}")
            for b in blockers:
                print("  - " + b)
            return 2

    records = []
    model_versions: set[str] = set()
    passed = 0
    ground_turns = 0
    ground_satisfied = 0

    for fx in fixtures:
        attempts = []
        for _ in range(max(1, args.repeat)):
            try:
                res = run_fixture(fx, client, model, tool_list, safety,
                                  skip_search=not args.always_search)
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
                             "grounded": r.get("grounded"),
                             "guard_outcome": r.get("guard_outcome")}
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
            "config_fingerprint": config_fingerprint(
                resolve_harness_config(tool_list, model)),
            "config": resolve_harness_config(tool_list, model),
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
