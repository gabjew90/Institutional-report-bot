"""Smoke test for the bare-probe revoice acceptance gate.

Context (2026-07-23): the grounding backstop's bare probe is the only
stage that reliably searches, but its output is deliberately persona-
less prose + Sources — every probe answer this week failed QC voice/
format. The revoice pass rewrites the probe's verified facts into
arrow-bullet room voice; `_revoice_acceptable` is the gate that decides
whether the rewrite ships or the dry probe answer stays.

Covers:
  - faithful rewrite (facts drawn from probe answer + question) accepted
  - rewrite that invents substance (> novel-ratio cap) rejected
  - empty / whitespace rewrite rejected
  - rewrite carrying a repetition glitch rejected
"""

import sys


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_PROBE_ANSWER = (
    "Intel is scheduled to report its Q2 2026 earnings on Thursday, "
    "July 23, 2026, after the market closes. The company will host an "
    "earnings conference call at 2:00 PM PDT on that same day."
)
_QUESTION = "when are intc earnings"


def test_faithful_rewrite_accepted():
    from discord_bot.bot import _revoice_acceptable
    revoiced = (
        "-> $INTC reports Q2 earnings Thursday July 23 after the close.\n"
        "-> Conference call follows at 2:00 PM PDT, same day."
    )
    assert _revoice_acceptable(revoiced, _PROBE_ANSWER, _QUESTION), (
        "faithful rewrite was rejected"
    )
    _ok("faithful arrow-bullet rewrite accepted")


def test_inventing_rewrite_rejected():
    from discord_bot.bot import _revoice_acceptable
    revoiced = (
        "-> Intel prints Thursday, whisper numbers looking brutal after "
        "the foundry writedown fiasco and the Ohio fabrication delays.\n"
        "-> Positioning crowded short, gamma squeeze potential elevated, "
        "dealers hedging desperately into the event."
    )
    assert not _revoice_acceptable(revoiced, _PROBE_ANSWER, _QUESTION), (
        "rewrite full of invented substance was accepted"
    )
    _ok("substance-inventing rewrite rejected")


def test_empty_rewrite_rejected():
    from discord_bot.bot import _revoice_acceptable
    assert not _revoice_acceptable("", _PROBE_ANSWER, _QUESTION)
    assert not _revoice_acceptable("   \n", _PROBE_ANSWER, _QUESTION)
    _ok("empty rewrite rejected")


def test_glitchy_rewrite_rejected():
    from discord_bot.bot import _revoice_acceptable
    revoiced = (
        "-> $INTC reports Q2 earnings Thursday after the close reports "
        "earnings Thursday after the close reports earnings Thursday "
        "after the close conference call conference call conference call."
    )
    assert not _revoice_acceptable(revoiced, _PROBE_ANSWER, _QUESTION), (
        "repetition-glitched rewrite was accepted"
    )
    _ok("repetition-glitched rewrite rejected")


if __name__ == "__main__":
    print("=== probe revoice gate smoke ===")
    test_faithful_rewrite_accepted()
    test_inventing_rewrite_rejected()
    test_empty_rewrite_rejected()
    test_glitchy_rewrite_rejected()
    print("\nALL PROBE REVOICE SMOKE TESTS PASS")
