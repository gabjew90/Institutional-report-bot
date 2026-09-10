"""Conference sessions from the corpus (spec 2026-09-09, approved
2026-09-10): extraction is verified against the text it came from,
admitted by the NDX list, ordered by market cap, and drawn beside the
economic column. Fixtures are the real Goldman notes of 2026-09-09:
15746 carries the Day 2 schedule as text, 15752 as an image (blank).
"""
import io
import json
import sys
from datetime import date
from pathlib import Path

from PIL import Image

from ai_analysis import conference_sessions as CS
from report import calendar_render as cr
from report.calendar_data import (CalendarDay, ConfRow, EconRow, NDX_TICKERS,
                                  build_conference_rows, lineup_signature)

FIX = Path("tests/fixtures")
TEXT = (FIX / "conference_schedule_15746.txt").read_text(encoding="utf-8")
IMAGE_TEXT = (FIX / "conference_image_schedule_15752.txt").read_text(encoding="utf-8")
REF = date(2026, 9, 9)
CONF = "GS Communacopia + Technology Conference"


def _raw_from_fixture() -> list[dict]:
    """What a faithful reader emits for 15746: one entry per schedule line."""
    import re
    out = []
    for line in TEXT.splitlines():
        if re.match(r"^\d{1,2}:\d{2} [AP]M: ", line):
            t, names = line.split(": ", 1)
            out.append({"conference": CONF, "date_iso": "2026-09-09", "time_local": t.strip(),
                        "tz": "PT", "tickers": [n.strip() for n in names.split(",")],
                        "anchor": line.strip()})
    return out


def test_the_real_schedule_yields_one_verified_session_per_slot_with_a_us_name():
    raw = _raw_from_fixture()
    assert len(raw) == 13
    sessions, stats = CS.resolve_sessions(raw, TEXT, REF, file_name="15746")
    # the 11:10 opening remarks slot names no ticker and is dropped
    assert stats["no_ticker"] == 1 and stats["anchor_missed"] == 0
    assert len(sessions) == 12 and stats["kept"] == 12
    msft = next(s for s in sessions if s.time_local == "12:30 PM")
    assert msft.tickers == ["MSFT"] and msft.tz == "PT" and msft.date_iso == "2026-09-09"
    slot = next(s for s in sessions if s.time_local == "1:10 PM")
    assert "100.HK" not in slot.tickers and "BKNG" in slot.tickers
    assert "ADYEN" not in " ".join(next(s for s in sessions if s.time_local == "11:50 AM").tickers)


def test_a_paraphrased_anchor_or_an_unprinted_date_drops_the_slot():
    good = _raw_from_fixture()[2]
    bad_anchor = {**good, "anchor": "Microsoft presents at 12:30 in the afternoon"}
    bad_date = {**good, "date_iso": "2026-09-10"}          # not printed anywhere
    day_label_only = {**good, "date_iso": "2026-09-09"}
    sessions, stats = CS.resolve_sessions([bad_anchor, bad_date], TEXT, REF)
    assert sessions == [] and stats["anchor_missed"] == 1 and stats["no_date"] == 1
    # the same slot against the IMAGE-form note: the anchor is not in the text
    sessions, stats = CS.resolve_sessions([day_label_only], IMAGE_TEXT, REF)
    assert sessions == [] and stats["anchor_missed"] == 1


def test_printed_dates_resolve_and_roll_forward_across_new_year():
    assert CS.resolve_date("2026-09-09", TEXT.lower(), REF) == "2026-09-09"
    assert CS.resolve_date("2026-09-09", "day 2 schedule (all times in pt)", REF) is None
    txt = "conference agenda: Tuesday, January 13th, 9:00 AM ET"
    assert CS.resolve_date("2025-01-13", txt, date(2025, 12, 15)) == "2026-01-13"
    assert CS.resolve_date("2026-01-13", txt, date(2026, 1, 10)) == "2026-01-13"
    assert CS.resolve_date("2026-13-40", txt, REF) is None
    assert CS.resolve_date("2026-01-13", "see 1/13 for the agenda", date(2026, 1, 5)) == "2026-01-13"


def test_local_times_convert_to_et_only_for_known_zones():
    assert CS.local_to_et_hhmm("12:30 PM", "PT", "2026-09-09") == "15:30"
    assert CS.local_to_et_hhmm("7:25 PM", "PT", "2026-09-09") == "22:25"
    assert CS.local_to_et_hhmm("9:00 AM", "CT", "2026-01-13") == "10:00"
    assert CS.local_to_et_hhmm("9:00 AM", "ET", "2026-01-13") == "9:00"
    assert CS.local_to_et_hhmm("9:00 AM", "GMT", "2026-01-13") is None
    assert CS.local_to_et_hhmm("", "PT", "2026-01-13") is None
    assert CS.local_to_et_hhmm("noon", "PT", "2026-01-13") is None


def test_ticker_cleaning_keeps_us_shapes_only():
    assert CS.clean_tickers(["Adyen NV", "CMCSA", "$PINS", "100.HK", "TNE.AU", "BRK.B", "cmcsa"]) \
        == ["CMCSA", "PINS", "BRK.B"]


# ------------------------------------------------------------ calendar

def _sessions():
    out, _ = CS.resolve_sessions(_raw_from_fixture(), TEXT, REF)
    return [{"conference": s.conference, "date_iso": s.date_iso, "time_local": s.time_local,
             "tz": s.tz, "tickers": s.tickers} for s in out]


def test_one_row_per_conference_admitted_by_ndx_and_ordered_by_market_cap():
    caps = {"MSFT": {"cap": 3_500_000}, "AMAT": {"cap": 180_000}, "CMCSA": {"cap": 120_000},
            "BKNG": {"cap": 170_000}, "PYPL": {"cap": 70_000}, "ADSK": {"cap": 60_000},
            "CDNS": {"cap": 90_000}, "CHTR": {"cap": 40_000}, "ROP": {"cap": 60_000}}
    rows = build_conference_rows(_sessions(), "2026-09-09", caps)
    assert len(rows) == 1
    r = rows[0]
    assert r.conference == CONF
    assert r.time_et == "14:50", "the first ADMITTED slot (11:50 AM PT) sets the start, not the opening remarks"
    assert r.tickers[:4] == ["MSFT", "AMAT", "BKNG", "CMCSA"]
    assert all(t in NDX_TICKERS for t in r.tickers)
    assert "HOOD" not in r.tickers and "DIS" not in r.tickers, "non-NDX names are dropped from the row"
    assert r.important, "MSFT is on the major-ticker list"
    # unknown caps sort last, alphabetically
    tail = [t for t in r.tickers if t not in caps]
    assert tail == sorted(tail) and r.tickers[-len(tail):] == tail


def test_a_conference_with_no_admitted_name_produces_no_row_and_duplicates_collapse():
    only_foreign = [{"conference": "Barclays Global Tech", "date_iso": "2026-09-09",
                     "time_local": "9:00 AM", "tz": "ET", "tickers": ["HOOD", "DIS"]}]
    assert build_conference_rows(only_foreign, "2026-09-09") == []
    twice = [{"conference": CONF, "date_iso": "2026-09-09", "time_local": "12:30 PM",
              "tz": "PT", "tickers": ["MSFT"]}] * 2 + \
            [{"conference": CONF.upper(), "date_iso": "2026-09-09", "time_local": "",
              "tz": "", "tickers": ["AMAT"]}]
    rows = build_conference_rows(twice, "2026-09-09")
    assert len(rows) == 1 and sorted(rows[0].tickers) == ["AMAT", "MSFT"] and rows[0].time_et == "15:30"
    dayless = [{"conference": "MS TMT", "date_iso": "2026-09-09", "time_local": "", "tz": "",
                "tickers": ["NVDA"]}]
    assert build_conference_rows(dayless, "2026-09-09")[0].time_et is None


def _day(conferences):
    return CalendarDay(
        date_iso="2026-09-09", weekday_label="WEDNESDAY 9/9", is_holiday=False,
        econ=[EconRow(time_et="8:30", event="Prelim Benchmark Payrolls Revision", impact="high",
                      important=True),
              EconRow(time_et="10:30", event="EIA Crude Oil Stocks Change", impact="low",
                      important=False)],
        conferences=conferences, earnings_available=False)


def test_the_industry_band_appears_only_with_a_conference_and_names_wrap():
    seen = []
    orig = cr._band

    def spy(d, label, x0, x1, y, f):
        seen.append(label)
        return orig(d, label, x0, x1, y, f)

    cr._band = spy
    try:
        png_plain = cr.render_calendar_png(_day([]))
        assert "Industry Events" not in seen
        seen.clear()
        row = ConfRow(conference=CONF, time_et="14:50",
                      tickers="MSFT AMAT BKNG CMCSA CDNS ROP PYPL ADSK CHTR CSGP".split(),
                      important=True, slots=9)
        png_conf = cr.render_calendar_png(_day([row]))
        assert seen[:2] == ["Economic", "Industry Events"]
    finally:
        cr._band = orig
    h_plain = Image.open(io.BytesIO(png_plain)).height
    h_conf = Image.open(io.BytesIO(png_conf)).height
    assert h_conf >= h_plain, "two columns beside each other never cost less height than a wrapped row"


def test_wrap_never_truncates():
    from PIL import ImageDraw
    d = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    f = cr._Fonts.get()
    text = "Prelim Benchmark Payrolls Revision for the twelve months to March"
    lines = cr._wrap(d, text, f["ev"], 300 * cr._S)
    assert " ".join(lines) == text and len(lines) >= 2
    assert all(d.textlength(l, font=f["ev"]) <= 300 * cr._S for l in lines)


def test_signature_sees_a_conference_row():
    a = lineup_signature(_day([]))
    row = ConfRow(conference=CONF, time_et="14:50", tickers=["MSFT"], important=True)
    b = lineup_signature(_day([row]))
    c = lineup_signature(_day([ConfRow(conference=CONF, time_et="14:50", tickers=["MSFT", "AMAT"])]))
    assert a != b != c and b == lineup_signature(_day([row]))


# --------------------------------------------------------------- wiring

def test_wiring_from_analyzer_to_sheet():
    import inspect
    import db
    from ai_analysis import analyzer
    from report import calendar_data
    assert callable(db.conference_sessions_for_date)
    assert "resolve_sessions(" in inspect.getsource(analyzer.analyze_pdf_deep)
    src = inspect.getsource(calendar_data.build_calendar_day)
    assert "db.conference_sessions_for_date(date_iso)" in src
    assert "build_conference_rows(" in src
    import ai_analysis.prompts as P
    psrc = inspect.getsource(P)
    assert '"conference_sessions": [' in psrc and "**For conference_sessions**" in psrc


def test_db_helper_dedupes_across_documents_and_filters_by_date(tmp_path=None):
    import sqlite3
    import db
    from unittest.mock import patch
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE pdf_files (id INTEGER PRIMARY KEY, file_name TEXT);
        CREATE TABLE pdf_analyses (id INTEGER PRIMARY KEY, pdf_file_id INTEGER,
            analysis_json TEXT, created_at TEXT DEFAULT (datetime('now')));
        INSERT INTO pdf_files VALUES (1, 'a.pdf'), (2, 'b.pdf');
    """)
    slot = {"conference": CONF, "date_iso": "2026-09-09", "time_local": "12:30 PM",
            "tz": "PT", "tickers": ["MSFT"], "anchor": "12:30 PM: MSFT"}
    other_day = {**slot, "date_iso": "2026-09-10", "tickers": ["AMAT"]}
    conn.execute("INSERT INTO pdf_analyses (pdf_file_id, analysis_json) VALUES (1, ?)",
                 (json.dumps({"conference_sessions": [slot, other_day]}),))
    conn.execute("INSERT INTO pdf_analyses (pdf_file_id, analysis_json) VALUES (2, ?)",
                 (json.dumps({"conference_sessions": [slot]}),))
    # a superseded analysis of doc 1 with a different slot must NOT count
    conn.execute("INSERT INTO pdf_analyses (pdf_file_id, analysis_json) VALUES (1, ?)",
                 (json.dumps({"conference_sessions": [slot]}),))
    with patch("db_parts.pdf._db.get_connection", return_value=conn):
        rows = db.conference_sessions_for_date("2026-09-09")
        assert len(rows) == 1 and rows[0]["tickers"] == ["MSFT"] and rows[0]["file_name"] in ("a.pdf", "b.pdf")
        assert db.conference_sessions_for_date("2026-09-10") == [], \
            "the superseded analysis carried 09-10; the latest one does not"
        assert db.conference_sessions_for_date("2026-09-11") == []


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
