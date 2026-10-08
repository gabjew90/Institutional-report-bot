"""The context dump keeps live data fresh before the pulse even with no new
research (github_bridge/jobs.py, 2026-10-08 audit: the 10-06 pulse quoted
4:34 AM ET prices)."""
from datetime import datetime

from github_bridge.jobs import _before_scheduled_pulse as f


def test_the_window_covers_the_routine_in_summer_and_winter():
    assert f(datetime(2026, 10, 6, 13, 30))       # 9:30 AM EDT
    assert f(datetime(2026, 12, 8, 14, 45))       # 9:45 AM EST
    assert not f(datetime(2026, 10, 6, 8, 34))    # the 4:34 AM ET dump
    assert not f(datetime(2026, 10, 10, 13, 30))  # Saturday
