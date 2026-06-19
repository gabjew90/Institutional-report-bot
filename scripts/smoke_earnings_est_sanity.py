"""Smoke test: earnings-calendar estimate sanity guard (2026-06-18).

Finnhub's free-tier earnings estimates are sometimes garbage. On
2026-06-18 the MU row came back EPS est $20.6923 / Rev est $35.88B
(~10x / 4x too high for Micron) and the pulse relayed it verbatim into
WHAT TO WATCH. The formatter now drops estimates outside plausible
bounds (|EPS| <= 15, 0 < quarterly rev <= $200B) rather than ship a
wrong number a reader trades off.
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def _cal(rows):
    return {"earningsCalendar": rows}


def test_drops_implausible_keeps_good():
    from report import news_data
    from datetime import datetime, timedelta
    soon = (datetime.utcnow().date() + timedelta(days=3)).isoformat()
    rows = [
        # MU bad data — the 06-18 case
        {"symbol": "MU", "date": soon, "hour": "amc",
         "epsEstimate": 20.6923, "revenueEstimate": 35.88e9},
        # NVDA good data
        {"symbol": "NVDA", "date": soon, "hour": "amc",
         "epsEstimate": 1.05, "revenueEstimate": 42.0e9},
    ]
    with patch("report.news_data._fetch_json", return_value=_cal(rows)), \
         patch("config.settings.finnhub_api_key", "k"):
        text = news_data.fetch_earnings_calendar(days_ahead=7)
    # MU listed (date kept) but its garbage estimates dropped
    assert "MU" in text, text
    assert "20.69" not in text and "35.88" not in text, (
        f"implausible MU estimates must be dropped: {text}"
    )
    # NVDA's plausible estimates kept
    assert "1.05" in text and "42.00B" in text, text
    _ok("earnings calendar: drops implausible MU est, keeps good NVDA est")


def test_bounds_helpers_via_output():
    """A merely-high-but-real revenue (AAPL ~$95B/qtr) is kept; an
    absurd one (>$200B/qtr) is dropped."""
    from report import news_data
    from datetime import datetime, timedelta
    soon = (datetime.utcnow().date() + timedelta(days=2)).isoformat()
    rows = [
        {"symbol": "AAPL", "date": soon, "hour": "amc",
         "epsEstimate": 1.50, "revenueEstimate": 95.0e9},
        {"symbol": "TSM", "date": soon, "hour": "bmo",
         "epsEstimate": 2.0, "revenueEstimate": 900.0e9},  # absurd rev
    ]
    with patch("report.news_data._fetch_json", return_value=_cal(rows)), \
         patch("config.settings.finnhub_api_key", "k"):
        text = news_data.fetch_earnings_calendar(days_ahead=7)
    assert "95.00B" in text, "real ~$95B quarter must be kept"
    assert "900.00B" not in text, "absurd $900B quarter must be dropped"
    _ok("earnings calendar: keeps realistic high rev, drops absurd rev")


if __name__ == "__main__":
    print("=== earnings est sanity smoke ===")
    test_drops_implausible_keeps_good()
    test_bounds_helpers_via_output()
    print("\nALL EARNINGS EST SANITY SMOKE TESTS PASS")
