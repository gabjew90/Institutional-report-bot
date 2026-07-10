"""Smoke: FACT-register batch (2026-07-09).

Three structural fixes from the 07-08 ask-log QC:

#1 FACT/BANTER register — the router's single call now emits TWO
   signals (route + register). A sincere informational question
   ("is warsh speaking today") got a correct answer wrapped in an
   invented premise ("you're confusing a document release with a press
   conference" — the asker never said that) plus a "stop looking" jab.
   Fix: classification-gated composition (the straight-answer directive
   only exists on FACT requests) + a code-level asker-mockery guard
   (detect→rewrite→strip), both gated on the router's FACT verdict so
   banter register is untouched.

#2 Bare probe — the 07-08 hedge batch (Toy Story / market-down /
   Netflix) proved even SEARCH-ONLY passes skip the discretionary
   search when the request carries the full room prompt + persona: the
   model answers from that context and priors. Stage-2 probe strips
   EVERYTHING but the question, so searching becomes the path of least
   resistance. Hedge only when the probe too comes back ungrounded.

#3 Audit stamp — ask-log entries now carry route/register/grounded/
   retry/guards, so QC reads decisions instead of inferring them from
   the presence of Sources blocks (Railway logs rotate away in ~1h).
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_mockery_detector_fires_on_the_warsh_shapes():
    import discord_bot.bot as bot
    # the exact 2026-07-08 shipped sentences
    for s in [
        "You’re confusing a document release with a press conference, "
        "the market’s already digested the notes.",
        "Stop looking for a speech that isn't happening and look at the "
        "actual minutes.",
        "That's just you coping with the red day.",
        "Just because you want it to moon doesn't make it a catalyst.",
    ]:
        v = bot._asker_mockery_violations(s)
        assert len(v) == 1, f"mockery shape must flag: {s!r} -> {v}"
    _ok("detector: invented-premise + stop-looking + coping shapes flagged")


def test_mockery_detector_leaves_straight_facts_alone():
    import discord_bot.bot as bot
    for s in [
        "Warsh isn't speaking today, the Fed dropped the June minutes "
        "at 2:00 PM ET.",
        "The committee is split on whether inflation is cooling.",
        "ASML reports Wednesday July 15 before the open.",
        # direct jabs WITHOUT invented premise are a register question,
        # not this guard's job
        "your last five trades were paperhanded exits.",
    ]:
        assert bot._asker_mockery_violations(s) == [], f"clean: {s!r}"
    _ok("detector: straight facts + premise-free jabs left alone")


def test_mockery_guard_wired_and_fact_gated():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # detection is gated on the router's FACT verdict
    assert "_route_is_factual and _asker_mockery_violations(answer)" in src, \
        "mockery detection must be FACT-gated"
    # feeds the register rewrite with a dedicated directive
    assert '"asker-mockery"' in src and "_am_directive" in src, \
        "mockery must feed the register rewrite"
    # hard-strip fallback exists and is FACT-gated
    assert "_mock_residual" in src and "_strip_sentences(answer, _mock_residual)" in src, \
        "hard-strip fallback missing"
    _ok("wired: FACT-gated detect -> rewrite directive -> hard-strip fallback")


def test_fact_directive_threaded_both_routes():
    import discord_bot.bot as bot
    d = bot._ASK_FACT_DIRECTIVE
    low = d.lower()
    assert "real question" in low and "straight" in low
    assert "confusing" in low and "premise" in low, \
        "directive must name the invented-premise failure"
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_fact_extra = _ASK_FACT_DIRECTIVE if _route_is_factual" in src, \
        "directive must be classification-gated"
    # threaded into the WEB config AND patched into the LOCAL config
    web_branch = src.split("if needs_web:", 1)[1]
    assert "_build_runtime_system_instruction(" in web_branch
    assert "config.system_instruction = (" in src, \
        "LOCAL branch must patch the directive into the prebuilt config"
    # builder accepts the extra
    sig = inspect.signature(bot._build_runtime_system_instruction)
    assert "extra_directive" in sig.parameters, "builder must take the extra"
    _ok("FACT directive: gated + threaded into WEB and LOCAL configs")


def test_bare_probe_wired():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    window = src.split("BARE PROBE", 1)
    assert len(window) == 2, "bare probe stage missing from backstop"
    probe = window[1][:6500]
    # the probe must NOT carry the room prompt or the persona
    assert "fact-checking search agent" in probe, "probe persona missing"
    assert "_build_runtime_system_instruction" not in probe.split(
        "if not _probe_ok:", 1)[0], "probe must not reuse the room persona"
    assert "probe_q = question.strip()[-600:]" in probe, \
        "probe must send the bare question, not the full contents"
    # search-only tool config
    assert "google_search=types.GoogleSearch()" in probe
    # accepted only when actually grounded; mechanical clean before accept
    assert "_grounding_has_sources(probe_gm)" in probe
    assert "_clean_voice_violations(" in probe, \
        "probe answer must pass the mechanical cleaner (skipped the lint pass)"
    # hedge is the terminal fallback
    assert "Couldn't verify these specifics" in probe
    _ok("bare probe: no room context/persona, search-only, grounded-only accept")


def test_ask_log_meta_stamp():
    import inspect as _i
    import db
    sig = _i.signature(db.append_ask_interaction)
    assert "meta" in sig.parameters, "append_ask_interaction must take meta"
    src = _i.getsource(db.append_ask_interaction)
    assert "**Route:**" in src, "meta stamp line missing from log entry"
    for key in ("route", "kind", "grounded", "ground_retry", "guards"):
        assert key in src, f"meta stamp must render {key!r}"
    # bot side: meta accumulated and passed
    import discord_bot.bot as bot
    bsrc = _i.getsource(bot._answer_with_gemini)
    assert "_ask_meta: dict = {" in bsrc, "meta accumulator missing"
    assert "meta=_ask_meta," in bsrc, "meta not passed to the log call"
    # every guard family stamps itself
    for tag in ('"guards"].append("ta")', '"guards"].append("outcome")',
                '"guards"].append("rank-trajectory")',
                'f"register:{k}"', '"grounding:"',
                '"asker-mockery-strip"'):
        assert tag in bsrc, f"guard stamp missing: {tag}"
    _ok("audit stamp: db renders Route line; all guard families stamp meta")


def test_question_only_filter_rung():
    # 2026-07-09: 2pale's "wtf is prevailing wage" + "what is WRAP" died
    # on Voice-strip AND slur-mask (his profile carries trip density
    # outside **Voice.**). Fourth rung: question only, no profiles/chat.
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    window = src.split("Fourth-tier retry: QUESTION-ONLY", 1)
    assert len(window) == 2, "question-only rung missing"
    rung = window[1][:3200]
    assert "_mask_slur_tokens" in rung, "the bare question must be masked too"
    assert "profiles" not in rung.split("generate_content", 1)[1][:400], \
        "the rung's call must carry no profile content"
    assert '_ask_meta["filter_retry"] = "question-only"' in rung
    # all rungs stamp the audit line; terminal failure stamps too
    for stamp in ('"voice-strip"', '"slur-mask"', '"question-only"',
                  '"failed"'):
        assert f'_ask_meta["filter_retry"] = {stamp}' in src, \
            f"filter_retry stamp missing: {stamp}"
    import db as _db
    dsrc = inspect.getsource(_db.append_ask_interaction)
    assert "filter_retry" in dsrc, "db stamp must render filter-retry"
    _ok("filter ladder: question-only terminal rung + all rungs stamped")


def test_probe_diagnostics_and_forcing():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    probe = src.split("BARE PROBE", 1)[1][:7000]
    # 07-09: probe converted 1 of 4 — stamp now distinguishes ran-but-
    # didn't-search from call-died, and the forcing got stronger.
    assert "FIRST action MUST" in probe, "probe must demand search-first"
    assert "thinking_budget=1024" in probe, "probe thinking budget too low"
    assert '_probe_state = "no-ground"' in probe and \
        '_probe_state = "error"' in probe, "probe state tracking missing"
    assert 'f"hedged(probe:{_probe_state})"' in probe, \
        "hedge stamp must carry the probe outcome"
    _ok("probe: search-first forcing + outcome-diagnostic stamps")


def test_qc_parser_tolerates_meta_line():
    from ask_qc.parser import parse_ask_log
    txt = (
        "# /ask interactions — 2026-07-09\n\n"
        "## 2026-07-09 14:00:00 UTC\n\n"
        "**Asker:** SV (`sv77788`) in #stonks\n\n"
        "**Route:** `WEB/FACT` · grounded ✅ (2 sources) · guards: —\n\n"
        "**Q:** is warsh speaking today\n\n"
        "**A:**\n\n"
        "No, the Fed dropped the June minutes at 2 PM ET.\n\n"
        "---\n\n"
    )
    got = parse_ask_log(txt)
    assert len(got) == 1, f"parser must survive the Route line: {got}"
    assert got[0].question == "is warsh speaking today"
    assert "minutes" in got[0].answer
    _ok("QC parser: Route stamp line doesn't break parsing")


if __name__ == "__main__":
    print("=== /ask FACT-register + bare-probe + audit-stamp smoke ===")
    test_mockery_detector_fires_on_the_warsh_shapes()
    test_mockery_detector_leaves_straight_facts_alone()
    test_mockery_guard_wired_and_fact_gated()
    test_fact_directive_threaded_both_routes()
    test_bare_probe_wired()
    test_ask_log_meta_stamp()
    test_question_only_filter_rung()
    test_probe_diagnostics_and_forcing()
    test_qc_parser_tolerates_meta_line()
    print("\nALL FACT-REGISTER SMOKE TESTS PASS")
