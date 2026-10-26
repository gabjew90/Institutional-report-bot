"""Standings are labelled by the last completed week (2026-10-26 audit F3)."""
from report import sleeper_data as S


def test_records_lag_the_current_week():
    out = {"week": 6}
    S._records_through(out, [{"settings": {"wins": 3, "losses": 2}}, {"settings": {"wins": 1, "losses": 4}}])
    assert out["records_through_week"] == 5 and "weeks 1-5 only" in out["records_note"]


def test_final_week_has_no_note():
    out = {"week": 5}
    S._records_through(out, [{"settings": {"wins": 3, "losses": 2}}])
    assert out["records_through_week"] == 5 and "records_note" not in out
