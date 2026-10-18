"""The Gemini backup pulse is cleaned in code before it posts.

2026-10-18 audit R3: the 10/14 backup shipped house trade calls, semicolons,
a RECAP with no header, one INSIGHTS block, and "Thursday, Oct 15: ASML
earnings" when ASML reported 10/14. Text below is from that pulse.
"""
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from report.backup_cleanup import (  # noqa: E402
    clean_backup_pulse, strip_house_calls, subject_on_date)

PULSE = """# Memory meltdown triggers rotation

Markets traded with a risk-off bias Wednesday as a hot inflation print collided with shocks.

## 1. INSIGHTS & ALPHA

### Semiconductor sector de-rating

Broadcom and Intel remain the best-positioned beneficiaries of this scarcity; ASML and TSMC will report. Long $AVGO.

### Tech rotation exhaustion

The rotation is approaching exhaustion. Long $RSP, paired with long $XLU.

### French sovereign debt stress

Spreads exceed 140 basis points. Short $GALP.

## 2. WHAT TO WATCH

### This Week
- **Thursday, Oct 15:** ASML earnings; focus on spare parts pricing.
- **Thursday, Oct 15:** TSMC earnings; focus on foundry capacity.
- **Thursday, Oct 15:** US Retail Sales; consumer resilience.
"""

NASDAQ_1015 = {"TSM": {"name": "Taiwan Semiconductor Manufacturing Co Ltd", "hour": "bmo"},
               "SCHW": {"name": "Charles Schwab Corp", "hour": "bmo"}}


def _clean():
    return clean_backup_pulse(PULSE, today=date(2026, 10, 14),
                              fetch=lambda iso: NASDAQ_1015 if iso == "2026-10-15" else {})


def test_house_calls_are_dropped():
    md, notes = _clean()
    for call in ("Long $AVGO", "Long $RSP", "Short $GALP"):
        assert call not in md
    assert len(notes["house_calls_dropped"]) == 3
    assert "Broadcom and Intel remain" in md


def test_a_desk_attributed_call_stays():
    md, dropped = strip_house_calls("Long $NVDA into the print, Goldman says.")
    assert "Long $NVDA" in md and not dropped


def test_no_semicolons_reach_prose():
    md, _ = _clean()
    body = "\n".join(l for l in md.split("\n") if not l.startswith("#"))
    assert ";" not in body and "—" not in body


def test_recap_header_and_main_event_split():
    md, _ = _clean()
    heads = [l for l in md.split("\n") if l.startswith("## ")]
    assert heads[:3] == ["## RECAP", "## THE MAIN EVENT", "## BRIEFS"]
    assert md.index("### Semiconductor") < md.index("## BRIEFS") < md.index("### Tech rotation")


def test_misdated_earnings_line_is_dropped_and_right_one_kept():
    md, notes = _clean()
    assert "ASML earnings" not in md
    assert "TSMC earnings" in md          # TSM by its initials
    assert "Retail Sales" in md           # not an earnings line
    assert len(notes["misdated_earnings_dropped"]) == 1


def test_nasdaq_down_keeps_the_line():
    md, notes = clean_backup_pulse(PULSE, today=date(2026, 10, 14), fetch=lambda iso: None)
    assert "ASML earnings" in md and not notes["misdated_earnings_dropped"]


def test_subject_matching():
    assert subject_on_date("$SCHW", NASDAQ_1015)
    assert subject_on_date("Charles Schwab", NASDAQ_1015)
    assert not subject_on_date("ASML", NASDAQ_1015)
