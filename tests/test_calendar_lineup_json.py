"""report.calendar_data.lineup_json: the posted sheet, kept for the audit (2026-10-02)."""
import json

from report.calendar_data import CalendarDay, EarnRow, EconRow, lineup_json


def test_lineup_json_keeps_rows_and_moves_and_drops_logo_bytes():
    day = CalendarDay(date_iso="2026-10-13", weekday_label="TUESDAY 10/13", is_holiday=False)
    day.bmo = [EarnRow(symbol="JPM", name="JPMorgan", cap_musd=700_000.0,
                       implied_move=3.4, logo=b"\x89PNG...", session_confirmed=True)]
    day.econ = [EconRow(time_et="8:30", event="CPI m/m", impact="high", important=True)]
    data = json.loads(lineup_json(day))
    row = data["bmo"][0]
    assert row["symbol"] == "JPM" and row["implied_move"] == 3.4 and row["session_confirmed"]
    assert "logo" not in row
    assert data["econ"][0]["event"] == "CPI m/m"
    assert data["earnings_available"] is True and data["econ_available"] is True
