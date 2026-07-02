"""Smoke: /ask retries once on transient Gemini failures (2026-07-02).

QC of the 07-02 ask logs: two of five morning asks died on
`ServerError: 500 INTERNAL` and surfaced "Google's hiccuping" to the
user — while the identical shape worked 21 seconds later. Transient 5xx
blips should get ONE silent in-place retry before the user sees an
error. Wiring checks (the retry lives inside _answer_with_gemini's
outer except, guarded by _transient_retry so it can't loop).
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


def test_retry_wired():
    import discord_bot.bot as bot
    sig = inspect.signature(bot._answer_with_gemini)
    assert "_transient_retry" in sig.parameters, "retry guard param missing"
    assert sig.parameters["_transient_retry"].default is False

    src = inspect.getsource(bot._answer_with_gemini)
    # the retry fires only for transient error classes...
    assert "_is_transient" in src and '"500"' in src and '"timeout"' in src, \
        "transient error classes must be matched"
    # ...only once (guarded), with a short backoff, re-calling itself
    assert "if _is_transient and not _transient_retry:" in src, \
        "retry must be guarded so it can't loop"
    assert "asyncio.sleep(2)" in src, "retry needs a short backoff"
    assert "_transient_retry=True" in src, "recursive call must set the guard"
    # the user-facing error path still exists for the second failure
    assert "Google's hiccuping" in src, "final-failure message must remain"
    _ok("transient 5xx -> one guarded retry, then normal error handling")


def test_retry_happens_before_failure_logging():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    # The retry decision must come BEFORE the failure is appended to the
    # ask-log, so a recovered blip logs one clean success, not a
    # failed-then-succeeded pair... the retry's own outcome logs itself.
    retry_pos = src.index("if _is_transient and not _transient_retry:")
    fail_log_pos = src.index('interaction_type="failed"')
    assert retry_pos < fail_log_pos, \
        "retry must be attempted before the failure is logged"
    _ok("retry attempted before the failure hits the ask-log")


if __name__ == "__main__":
    print("=== /ask transient-retry smoke ===")
    test_retry_wired()
    test_retry_happens_before_failure_logging()
    print("\nALL /ASK TRANSIENT-RETRY SMOKE TESTS PASS")
