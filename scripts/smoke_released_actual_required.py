"""Smoke: a released macro event the pulse discusses must carry its print.

2026-08-12. CPI printed at 8:30 ET and the pulse fired at 10:09 giving
Goldman's, JPMorgan's and Deutsche Bank's FORECASTS and nothing else. A
reader learned what three desks expected and never learned what the
print was, under the assertion that it "landed close to what the desks
had penciled in".

Two separate faults, worth keeping straight:

  DATA — the calendar carried no actual at pulse time. Finnhub's
  economic calendar 403s (paid tier since 2026-06-11) and the
  ForexFactory fallback had not populated actuals yet. The validator's
  own output proves it: the CPI row values it saw were
  [-0.42, -0.4, -0.02, 0.0, 0.3, 2.6, 2.86, 3.5, 3.88], all prevs from
  older releases, and it scored 0.22 as matches_estimate=True. Had the
  actual been present, 0.22 would have matched it instead.

  VALIDATION — unreconciled-release-figure fired three times and was
  SOFT, so the pulse shipped anyway. And no check required the print to
  be PRESENT at all: a bullet that discusses CPI and never states the
  result tripped nothing.

This file covers the validation half. The estimate-shipped-as-print
shape is now hard, and released-actual-missing is a new hard check that
fires when the feed does carry the actual and the pulse omits it. Neither
can manufacture a number the feed never delivered — that is the data
half, and it is the Finnhub entitlement block.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


CAL = (
    "ECONOMIC EVENTS ALREADY RELEASED (belongs in RECAP, NEVER in WHAT TO WATCH):\n"
    "  2026-08-12 08:30 EDT | [US] | Core CPI m/m | impact=high | "
    "ACTUAL=0.22% (for 2026-07) | est=0.2% | prev=0.0%\n"
    "  2026-08-12 08:30 EDT | [US] | Core CPI y/y | impact=high | "
    "ACTUAL=2.67% (for 2026-07) | est=2.5% | prev=2.6%\n"
    "ECONOMIC EVENTS STILL UPCOMING (belongs in WHAT TO WATCH):\n"
    "  2026-08-13 08:30 EDT | [US] | PPI m/m | impact=high | est=0.2%\n"
)
CTX = {"economic_calendar": CAL}

# The failing shape: discusses a released event, ships only desk
# forecasts, never states the print.
#
# This is the 2026-08-12 bullet with one change — its "+0.22% core" is
# dropped. That figure was JPMorgan's forecast in the shipped text and
# also happens to equal the m/m actual, so leaving it in would satisfy a
# presence check by coincidence. The y/y actual (2.67%) appears nowhere
# in the real bullet, which is the omission this check exists for.
SHIPPED = (
    "## 1. RECAP\n\n"
    "- **July CPI, 8:30 AM ET.** The July report came out at 8:30 and "
    "landed close to what the desks had penciled in. Goldman Sachs had "
    "estimated core at +0.19% on the year, JPMorgan at +0.21% core, "
    "and Deutsche Bank at +0.26% core.\n"
)

FIXED = (
    "## 1. RECAP\n\n"
    "- **July CPI, 8:30 AM ET.** Core prices rose 2.67% on the year "
    "against the 2.5% desks expected. Hotter than the street.\n"
)


def _kinds(md, ctx=None):
    from scripts.pulse_draft_validate import validate
    return [v["kind"] for v in validate(md, ctx if ctx is not None else CTX)]


def _by_kind(md, kind, ctx=None):
    from scripts.pulse_draft_validate import validate
    return [v for v in validate(md, ctx if ctx is not None else CTX)
            if v["kind"] == kind]


def test_the_shipped_bullet_is_caught():
    v = _by_kind(SHIPPED, "released-actual-missing")
    if not v:
        _fail("the 2026-08-12 CPI bullet still passes — it discusses a "
              "released event and never states the print")
    if v[0]["severity"] != "hard":
        _fail(f"missing print must be hard, got {v[0]['severity']}")
    if 2.67 not in v[0]["actual"]:
        _fail(f"violation does not carry the actual: {v[0]}")
    _ok("the shipped CPI bullet is caught, hard")


def test_estimate_shipped_as_print_is_now_hard():
    """The live 2026-08-12 shape: the row has printed, the pulse states a
    number that matches the CONSENSUS rather than the actual. That is the
    signature of publishing a forecast as the result, and it has now
    shipped twice (2026-07-15 and 2026-08-12), soft both times."""
    cal = (
        "ECONOMIC EVENTS ALREADY RELEASED:\n"
        "  2026-08-12 08:30 EDT | [US] | CPI y/y | impact=high | "
        "ACTUAL=3.52% (for 2026-07) | est=3.4% | prev=3.5%\n"
    )
    md = ("## 1. RECAP\n\n"
          "- **July CPI.** Headline inflation ran at 3.4% on the year.\n")
    v = _by_kind(md, "unreconciled-release-figure",
                 ctx={"economic_calendar": cal})
    est_hits = [x for x in v if x.get("matches_estimate")]
    if not est_hits:
        _fail(f"consensus-as-print was not flagged: {v}")
    if any(x["severity"] != "hard" for x in est_hits):
        _fail(f"estimate-as-print must be hard now: {est_hits}")
    _ok("a figure matching consensus on a printed row is hard")


def test_the_corrected_bullet_passes():
    kinds = _kinds(FIXED)
    for bad in ("released-actual-missing", "unreconciled-release-figure"):
        if bad in kinds:
            _fail(f"a bullet carrying the real print still flags {bad}: "
                  f"{_by_kind(FIXED, bad)}")
    _ok("a bullet stating the actual print passes both checks")


def test_silence_about_an_event_is_not_flagged():
    """The requirement is conditional: if we DISCUSS it, state the print.
    A pulse that never mentions CPI owes nothing."""
    md = "## 1. RECAP\n\n- Gold is the biggest mover, $GLD +1.32%.\n"
    if "released-actual-missing" in _kinds(md):
        _fail("flagged an event the pulse never discusses")
    _ok("an event the pulse ignores is not required to be reported")


def test_unreleased_event_is_not_required():
    """PPI is upcoming, not printed. Nothing to state."""
    md = "## 1. RECAP\n\n- PPI lands Thursday at 8:30 AM ET.\n"
    if "released-actual-missing" in _kinds(md):
        _fail("required a print from an event that has not printed")
    _ok("an upcoming event is not required to carry an actual")


def test_no_calendar_is_a_no_op():
    if _kinds(SHIPPED, ctx={}):
        got = _kinds(SHIPPED, ctx={})
        if "released-actual-missing" in got:
            _fail("flagged with no calendar to check against")
    _ok("no calendar in context is a clean no-op")


def test_hard_kind_is_registered():
    from scripts.pulse_draft_validate import HARD_VIOLATION_KINDS
    if "released-actual-missing" not in HARD_VIOLATION_KINDS:
        _fail("released-actual-missing is not in HARD_VIOLATION_KINDS, so "
              "it would not drive the exit code")
    _ok("released-actual-missing drives the hard exit code")


if __name__ == "__main__":
    test_the_shipped_bullet_is_caught()
    test_estimate_shipped_as_print_is_now_hard()
    test_the_corrected_bullet_passes()
    test_silence_about_an_event_is_not_flagged()
    test_unreleased_event_is_not_required()
    test_no_calendar_is_a_no_op()
    test_hard_kind_is_registered()
    print("\nAll released-actual smoke tests passed.")
