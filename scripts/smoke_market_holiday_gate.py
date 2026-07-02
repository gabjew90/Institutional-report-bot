"""Smoke: market-holiday gate (2026-07-02) — pulse fires on open days only.

Three layers: the NYSE closure calendar lives in world_context.py (the
designated rot-prone-facts file), the bridge stamps `us_market_holiday`
into the context dump, and the routine's STEP 2.1 exits with a
self-documenting skip marker instead of synthesizing. Fail direction is
deliberate: an uncovered year (stale calendar) or a stale context
PROCEEDS — firing is the default, skipping is the rare action.
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


def test_holiday_calendar():
    from world_context import is_us_market_holiday, US_MARKET_HOLIDAYS
    # tomorrow's first live case + a sample of known closures
    assert is_us_market_holiday("2026-07-03") == "Independence Day (observed)"
    assert is_us_market_holiday("2026-11-26") == "Thanksgiving Day"
    assert is_us_market_holiday("2027-07-05") == "Independence Day (observed)"
    # observed-date convention: the Saturday/Sunday themselves are NOT
    # closures (markets already closed on weekends; cron never fires)
    assert is_us_market_holiday("2026-07-04") is False
    # regular trading days -> False (covered year, not a holiday)
    assert is_us_market_holiday("2026-07-06") is False
    assert is_us_market_holiday("2026-07-02") is False
    # uncovered year -> None (stale calendar must NOT cause skips)
    assert is_us_market_holiday("2028-01-17") is None
    assert is_us_market_holiday("2025-12-25") is None
    # malformed -> None
    assert is_us_market_holiday("") is None
    assert is_us_market_holiday("garbage") is None
    # both covered years carry the full 10 NYSE closures
    assert sum(1 for d in US_MARKET_HOLIDAYS if d.startswith("2026")) == 10
    assert sum(1 for d in US_MARKET_HOLIDAYS if d.startswith("2027")) == 10
    _ok("calendar: holidays named, open days False, uncovered years None")


def test_bridge_stamps_flag():
    import github_bridge.jobs as jobs
    src = inspect.getsource(jobs._dump_context_job_inner)
    assert 'ctx["us_market_holiday"]' in src, "bridge must stamp the flag"
    assert "is_us_market_holiday" in src, "must use the world_context calendar"
    # stamp failure must be non-fatal and fail toward firing (None)
    window = src.split('ctx["us_market_holiday"]', 1)[1][:400]
    assert "None" in window, "stamp failure must default to None (proceed)"
    _ok("bridge: dump stamps us_market_holiday, failure defaults to proceed")


def test_routine_gate_wired():
    md = open("docs/superpowers/routines/synthesis-routine.md",
              encoding="utf-8").read()
    assert "STEP 2.1 — Market-holiday gate" in md, "gate step missing"
    assert md.count("holiday_skip.txt") >= 2, \
        "gate must write the skip file AND the stop-rule must read it"
    assert "flag and ctx_today == utc_today" in md, \
        "skip requires a fresh, date-matching, truthy stamp"
    assert "Pulse skipped — US market holiday" in md, \
        "the skip must commit a self-documenting marker"
    assert "Do NOT run STEP 2.5" in md, "the stop rule must halt the routine"
    _ok("routine: STEP 2.1 gate + skip marker + hard stop wired")


if __name__ == "__main__":
    print("=== market-holiday gate smoke ===")
    test_holiday_calendar()
    test_bridge_stamps_flag()
    test_routine_gate_wired()
    print("\nALL MARKET-HOLIDAY GATE SMOKE TESTS PASS")
