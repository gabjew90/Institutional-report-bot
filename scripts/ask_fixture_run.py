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
  python scripts/ask_fixture_run.py --json out.json

Exit codes: 0 all passed, 1 one or more failed, 2 harness error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

FIXTURE_DIR = REPO / "tests" / "ask_fixtures"


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
        ctx = " ".join((fx.get("context") or {}).values())
        names = {n for n in re.findall(r"\b([A-Z][a-zA-Z]{2,})\b", ctx)
                 if n in ans}
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
    # a model that chains tool calls is normal, and stopping early leaves
    # the answer empty, which reads as a content failure it is not.
    stubs = fx.get("tool_stubs") or {}
    for _round in range(4):
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
            "grounded": grounded}


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


def main() -> int:
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--only", nargs="*", default=None)
    ap_.add_argument("--repeat", type=int, default=1)
    ap_.add_argument("--offline", action="store_true")
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
    model = settings.ask_gemini_model or settings.gemini_model

    tool_list = [
        types.Tool(google_search=types.GoogleSearch()),
        _build_chat_search_tool(), _build_user_profile_tool(),
        _build_trade_log_tool(), _build_market_price_tool(),
        _build_options_chain_tool(), _build_economic_calendar_tool(),
        _build_earnings_date_tool(), _build_query_data_tool(),
        _build_price_history_tool(),
    ]
    if (settings.sleeper_league_id or "").strip():
        tool_list.append(_build_fantasy_league_tool())
    safety = [
        types.SafetySetting(category=c, threshold="BLOCK_NONE")
        for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                  "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                  "HARM_CATEGORY_DANGEROUS_CONTENT")
    ]

    records = []
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

        if fx.get("grounding_required"):
            ground_turns += 1
            if res0.get("tools_called") or res0.get("grounded"):
                ground_satisfied += 1

        status = "PASS" if ok else ("FLAKY" if flaky else "FAIL")
        passed += 1 if ok else 0
        mark = {"PASS": "PASS ", "FLAKY": "FLAKY", "FAIL": "FAIL "}[status]
        print(f"{mark} {fx['id']:<34} [{fx['ledger']}] {fx['title']}")
        if not ok:
            for f in fails0[:4]:
                print(f"        - {f}")
            print(f"        answer: {(res0.get('answer') or '')[:150]!r}")
            print(f"        tools: {res0.get('tools_called')} "
                  f"grounded={res0.get('grounded')}")
        records.append({"id": fx["id"], "ledger": fx["ledger"],
                        "status": status, "failures": fails0,
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

    if args.json_out:
        Path(args.json_out).write_text(json.dumps({
            "pass_rate": rate, "passed": passed, "total": total,
            "tool_call_rate_grounding": grate,
            "grounding_turns": ground_turns,
            "grounding_satisfied": ground_satisfied,
            "fixtures": records,
        }, indent=1), encoding="utf-8")
        print(f"wrote {args.json_out}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
