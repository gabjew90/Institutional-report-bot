"""Smoke: thesis-level FLIP detection on the TRADE BOARD.

2026-07-29 pulse feedback: "Monday's Fed lean (hike risk underpriced ->
Long $UUP) becomes Tuesday's 'clean hold base case -> Long $TLT' — a
near-reversal in the Fed view within a day, labeled NEW rather than
FLIP, which is exactly what the FLIP tag exists to catch."

Existing FLIP detection (db.upsert_pulse_leans) only catches SAME-ticker
direction reversals, so a thesis reversed through a DIFFERENT instrument
is invisible. Fix: a curated map of macro instruments to (axis, stance)
— Long UUP is hawkish, Long TLT is dovish, both on the "fed" axis — so
an opposing stance on the same axis across consecutive boards flags as
a FLIP.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_lean_thesis_mapping():
    from report.pulse_sections import _lean_thesis
    assert _lean_thesis("UUP", "long") == ("fed", "hawkish")
    assert _lean_thesis("TLT", "long") == ("fed", "dovish")
    # shorting inverts the stance
    assert _lean_thesis("TLT", "short") == ("fed", "hawkish")
    assert _lean_thesis("SPY", "long") == ("risk", "on")
    assert _lean_thesis("VIX", "long") == ("risk", "off")
    # unmapped single names carry no macro thesis
    assert _lean_thesis("NVDA", "long") is None
    _ok("instrument -> (axis, stance) mapping, short inverts")


def _rows(prev, today):
    out = []
    for tk, d in prev:
        out.append({"instrument": tk, "direction": d,
                    "first_seen_date": "2026-07-27",
                    "last_seen_date": "2026-07-27"})
    for tk, d in today:
        out.append({"instrument": tk, "direction": d,
                    "first_seen_date": "2026-07-28",
                    "last_seen_date": "2026-07-28"})
    return out


def test_cross_instrument_fed_reversal_flagged():
    from report.pulse_sections import detect_thesis_flips
    rows = _rows([("UUP", "long")], [("TLT", "long")])
    flips = detect_thesis_flips(rows, "2026-07-28", "2026-07-27")
    assert "TLT" in flips, f"UUP(hawkish) -> TLT(dovish) must FLIP: {flips}"
    _ok("Long $UUP -> Long $TLT flags as a thesis FLIP (the live case)")


def test_risk_axis_reversal_flagged():
    from report.pulse_sections import detect_thesis_flips
    rows = _rows([("SPY", "long")], [("VIX", "long")])
    flips = detect_thesis_flips(rows, "2026-07-28", "2026-07-27")
    assert "VIX" in flips, f"risk-on -> risk-off must FLIP: {flips}"
    _ok("Long $SPY -> Long $VIX flags as a thesis FLIP")


def test_same_stance_is_not_a_flip():
    from report.pulse_sections import detect_thesis_flips
    # dovish held, expressed through a different instrument
    rows = _rows([("TLT", "long")], [("IEF", "long")])
    flips = detect_thesis_flips(rows, "2026-07-28", "2026-07-27")
    assert not flips, f"same stance on the axis is NOT a flip: {flips}"
    _ok("same-axis SAME-stance (TLT -> IEF) is not a flip")


def test_unmapped_tickers_never_flip():
    from report.pulse_sections import detect_thesis_flips
    rows = _rows([("NVDA", "long")], [("AMD", "short")])
    assert not detect_thesis_flips(rows, "2026-07-28", "2026-07-27")
    _ok("single names with no macro axis never produce thesis flips")


def test_reversed_lean_still_renders_as_a_call():
    """2026-08-12 (owner decision): the board dropped every status label,
    FLIP included. Detection is unchanged and still covered by the tests
    above — detect_thesis_flips is what a future consumer would use. What
    matters at the render layer now is only that a reversed lean is still
    ON the board as one of today's calls, unlabelled."""
    from report.pulse_sections import render_trade_board
    rows = _rows([("UUP", "long")], [("TLT", "long")])
    out = render_trade_board(rows, "2026-07-28",
                             prev_board_date="2026-07-27")
    tlt_line = [ln for ln in out.splitlines() if "TLT" in ln]
    assert tlt_line, out
    assert "FLIP" not in out, f"FLIP label should be gone:\n{out}"
    assert "UUP" not in out, (
        f"yesterday's opposing lean must not render:\n{out}")
    _ok("reversed lean renders as a plain call; FLIP label retired")


if __name__ == "__main__":
    print("=== thesis FLIP smoke ===")
    test_lean_thesis_mapping()
    test_cross_instrument_fed_reversal_flagged()
    test_risk_axis_reversal_flagged()
    test_same_stance_is_not_a_flip()
    test_unmapped_tickers_never_flip()
    test_reversed_lean_still_renders_as_a_call()
    print("\nALL THESIS FLIP SMOKE TESTS PASS")
