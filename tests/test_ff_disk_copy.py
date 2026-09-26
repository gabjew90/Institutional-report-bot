"""ForexFactory feed survives deploys and 429s (2026-09-26).

The Monday 9/28 sheet shipped its economic block as "unavailable
tonight": the feed answers 429 readily and the only cache was in memory,
which every deploy empties.
"""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from report import news_data as nd


def _week(start_offset_days=0, n=5):
    base = datetime.now(ZoneInfo("America/New_York")).replace(
        hour=8, minute=30, second=0, microsecond=0) + timedelta(days=start_offset_days)
    return [{"title": "CPI m/m", "country": "USD", "impact": "High",
             "date": (base + timedelta(days=i)).isoformat(),
             "forecast": "0.3%", "previous": "0.2%"} for i in range(n)]


def _reset():
    nd._FF_CACHE["at"] = None
    nd._FF_CACHE["rows"] = None


def test_a_good_fetch_is_saved_and_served_after_a_restart_and_a_429(tmp_path):
    _reset()
    with patch.object(nd, "_ff_disk_path", return_value=tmp_path / "ff.json"):
        with patch.object(nd, "_fetch_json", return_value=_week()):
            live = nd._fetch_ff_economic_events()
        _reset()                                  # a deploy empties memory
        with patch.object(nd, "_fetch_json", return_value=None):   # 429
            again = nd._fetch_ff_economic_events()
    assert live and again == live


def test_last_weeks_copy_is_not_served_as_this_week(tmp_path):
    """A stale week would answer today with [] and read as a quiet day."""
    _reset()
    with patch.object(nd, "_ff_disk_path", return_value=tmp_path / "ff.json"):
        with patch.object(nd, "_fetch_json", return_value=_week(start_offset_days=-9)):
            nd._fetch_ff_economic_events()
        _reset()
        with patch.object(nd, "_fetch_json", return_value=None):
            assert nd._fetch_ff_economic_events() == []


def test_the_warm_job_keeps_the_stale_fallback(tmp_path):
    _reset()
    with patch.object(nd, "_ff_disk_path", return_value=tmp_path / "missing" / "ff.json"):
        with patch.object(nd, "_fetch_json", return_value=_week()):
            nd._fetch_ff_economic_events()     # memory only (disk save fails)
        with patch.object(nd, "_fetch_json", return_value=None):
            assert nd.warm_ff_feed() == 5, "a failed warm still serves memory"
    _reset()


def test_feed_down_falls_back_to_fred_majors():
    from report import calendar_data as cd
    fred = [{"event": "PCE (Personal Income and Outlays)", "country": "US",
             "time": "2026-09-30T12:30:00", "impact": "high"}]
    with patch.object(cd.news_data, "fetch_us_econ_events_for_date", return_value=None), \
         patch.object(cd.news_data, "ff_feed_covers", return_value=None), \
         patch.object(cd.news_data, "fetch_us_major_releases_from_fred", return_value=fred), \
         patch.object(cd.news_data, "fetch_earnings_calendar_all", return_value=[]), \
         patch.object(cd.db, "conference_sessions_for_date", return_value=[]):
        day = cd.build_calendar_day("2026-09-30")
    assert day.econ_available is not False and day.econ_partial
    assert day.econ_partial_reason == "feed_down"
    assert [r.event for r in day.econ] == ["PCE (Personal Income and Outlays)"]


def test_a_feed_outage_is_not_described_as_next_week():
    """'full list posts Sunday' is true only past the feed's week; on a
    covered weekday the full list exists and could not be fetched."""
    from types import SimpleNamespace
    from report import calendar_render as R
    down = SimpleNamespace(econ_partial=True, econ_partial_reason="feed_down")
    nxt = SimpleNamespace(econ_partial=True, econ_partial_reason="next_week")
    assert R._econ_empty_text(down) == R.ECON_DOWN_EMPTY
    assert R._econ_partial_note(down) == R.ECON_DOWN_NOTE
    assert R._econ_empty_text(nxt) == R.ECON_PARTIAL_EMPTY
    assert "Sunday" not in R.ECON_DOWN_NOTE


def test_disk_saves_use_a_per_writer_temp_file(tmp_path):
    import threading
    _reset()
    target = tmp_path / "ff.json"
    with patch.object(nd, "_ff_disk_path", return_value=target):
        ts = [threading.Thread(target=nd._ff_save_disk, args=(_week(),)) for _ in range(8)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert nd._ff_load_disk() is not None
    assert not list(tmp_path.glob("*.tmp")), "no temp file left behind"
