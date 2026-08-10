"""Smoke: interactions with no bot answer get their own bucket, not a grade.

2026-08-07..09: the filter-block wrapper is one fixed string, and the
judge graded that identical string CLEAN twice and FAIL twice. On
format_adherence it produced both "used the required arrow bullet format
even for the error message" and "failed to use the required arrow bullet
format". One FAIL called the accurate "hard filter" report a fabrication,
while the ask log's own route metadata recorded the block as real.

Grading voice, depth and fabrication on a canned system string is a
category error — there is no authored answer underneath. These now
short-circuit to INFRA from recorded system state, so CLEAN/FAIL means
"the bot answered well/badly" and the judge is never asked.

Coverage note: the drift guard below catches a RENAMED or DELETED wrapper
string in bot.py. It cannot catch a brand-new wrapper nobody added a
signature for — route_meta's `filter-retry: failed` covers the filter
path regardless, but a new non-filter wrapper would silently go back to
being graded. Add its signature when you add the wrapper.
"""

import asyncio
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


WRAPPER = (
    "→ Gemini bounced this one — its hard filter blocked the prompt. "
    "Try asking a different way or about a different subject."
)

# Verbatim from ask-logs/2026-08-07.md and 2026-08-09.md — the four asks
# that shipped the wrapper, with their real Route lines.
REAL_LOG = f"""# /ask interactions - 2026-08-07

## 2026-08-07 16:14:06 UTC

**Asker:** Grand Nagus Yeezy (`grandnagusyeezy`) in #stonks-yapping

**Route:** `LOCAL/BANTER` · grounded ✅ (4 sources) · guards: —

**Q:** top 3 gold etf tickers please

**A:**

→ $GLD is the big liquid one.

---

## 2026-08-07 16:24:21 UTC

**Asker:** bulch (`tulch`) in #stonks-yapping

**Route:** `LOCAL/BANTER` · ungrounded · filter-retry: failed · guards: —

**Q:** explain to these peasants what the ticker COHR is, and when earnings is

**A:**

{WRAPPER}

---

## 2026-08-07 19:04:45 UTC

**Asker:** Ryan (`rdd1414`) in #stonks-yapping

**Route:** `LOCAL/BANTER` · ungrounded · filter-retry: failed · guards: —

**Q:** how can I long platinum and palladium via public equity options?

**A:**

{WRAPPER}

---
"""


def test_parser_captures_route_meta():
    from ask_qc.parser import parse_ask_log
    ix = parse_ask_log(REAL_LOG)
    if len(ix) != 3:
        _fail(f"expected 3 interactions, parsed {len(ix)}")
    if not all(i.route_meta for i in ix):
        _fail("route_meta not captured — the grader's primary signal is "
              "the recorded system state, not the answer prose")
    if "filter-retry: failed" not in (ix[1].route_meta or ""):
        _fail("filter-retry marker lost in parsing")
    _ok("parser captures the Route line verbatim")


def test_blocked_asks_bucket_as_infra():
    from ask_qc.parser import parse_ask_log
    from ask_qc.grader import infra_failure_reason
    ix = parse_ask_log(REAL_LOG)
    reasons = [infra_failure_reason(i) for i in ix]
    if reasons[0] is not None:
        _fail(f"a real answered ask was misread as infra ({reasons[0]!r})")
    if reasons[1] != "filter-block" or reasons[2] != "filter-block":
        _fail(f"filter-blocked asks not bucketed: {reasons}")
    _ok("filter-blocked asks bucket as INFRA, real answers do not")


def test_judge_is_never_called_for_infra():
    """The whole point: no Gemini call, so no inconsistent verdict."""
    from ask_qc.parser import parse_ask_log
    from ask_qc import grader
    calls = []

    async def _boom(interaction):
        calls.append(interaction.ts_utc)
        raise AssertionError("judge was invoked for an infra failure")

    orig = grader._grade_interaction_with_retry
    grader._grade_interaction_with_retry = _boom
    try:
        blocked = [i for i in parse_ask_log(REAL_LOG)
                   if "filter-retry: failed" in (i.route_meta or "")]
        graded = asyncio.run(grader.grade_day(blocked))
    finally:
        grader._grade_interaction_with_retry = orig

    if calls:
        _fail(f"judge was called for {len(calls)} infra interaction(s)")
    if [g.overall_verdict for g in graded] != ["INFRA", "INFRA"]:
        _fail(f"verdicts were {[g.overall_verdict for g in graded]}")
    _ok("judge is never invoked for infra failures; verdict is INFRA")


def test_infra_is_deterministic():
    """Same input, same verdict — the defect was the same string getting
    CLEAN twice and FAIL twice."""
    from ask_qc.models import AskInteraction
    from ask_qc.grader import infra_failure_reason
    ix = AskInteraction(
        ts_utc="2026-08-09 22:35:22 UTC", asker_label="Ry", asker_username="r",
        channel="#stonks-yapping", question="why are money market volumes high",
        answer=WRAPPER, prompt_block=None,
        route_meta="`LOCAL/BANTER` · ungrounded · filter-retry: failed",
    )
    verdicts = {infra_failure_reason(ix) for _ in range(25)}
    if verdicts != {"filter-block"}:
        _fail(f"non-deterministic verdict: {verdicts}")
    _ok("verdict is deterministic across repeated evaluation")


def test_wrapper_signatures_still_match_bot():
    """Drift guard: if bot.py rewords a wrapper, this fails instead of
    the string quietly going back to the judge."""
    import discord_bot.bot as bot
    from ask_qc.grader import _INFRA_ANSWER_SIGNATURES
    src = inspect.getsource(bot)
    for needle, reason in _INFRA_ANSWER_SIGNATURES:
        if needle not in src:
            _fail(f"signature {needle!r} ({reason}) no longer appears in "
                  f"bot.py — the wrapper was reworded and infra failures "
                  f"will be graded as bad answers again")
    _ok(f"all {len(_INFRA_ANSWER_SIGNATURES)} wrapper signatures still "
        f"present in bot.py")


def test_report_separates_infra_from_graded():
    from ask_qc.aggregator import render_report
    from ask_qc.models import GradedInteraction, DimensionVerdict
    graded = [
        GradedInteraction(
            interaction_ts_utc="2026-08-07 16:14:06 UTC",
            dimensions={"voice": DimensionVerdict("PASS", "sharp")},
        ),
        GradedInteraction(interaction_ts_utc="2026-08-07 16:24:21 UTC",
                          infra_reason="filter-block"),
        GradedInteraction(interaction_ts_utc="2026-08-07 19:04:45 UTC",
                          infra_reason="filter-block"),
    ]
    md = render_report("2026-08-07", graded)
    if "1 CLEAN" not in md:
        _fail("graded tally wrong — infra should not inflate CLEAN")
    if "0 FAIL" not in md:
        _fail("infra leaked into the FAIL count")
    if "Not gradable:** 2 of 3 (67%)" not in md:
        _fail(f"not-gradable line missing or wrong:\n{md[:600]}")
    if "filter-block x2" not in md:
        _fail("infra reasons not broken out by cause")
    if "- INFRA" not in md:
        _fail("per-interaction INFRA verdict not rendered")
    _ok("report counts infra separately and names the cause")


if __name__ == '__main__':
    test_parser_captures_route_meta()
    test_blocked_asks_bucket_as_infra()
    test_judge_is_never_called_for_infra()
    test_infra_is_deterministic()
    test_wrapper_signatures_still_match_bot()
    test_report_separates_infra_from_graded()
    print("\nAll ask-QC infra-bucket smoke tests passed.")
