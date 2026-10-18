"""Each starter still to play carries the day his game is played.

2026-10-18 audit F2: the bot put Mahomes on Monday night (KC played Sunday
10/18) and JSN on Monday (SEA played Thursday 10/15); BK: "Jsn is playing
tonight nigga not Monday". The payload had no game day to read.
"""
import json
from unittest.mock import patch

from report import sleeper_data as S

SCHEDULE = [
    {"status": "complete", "date": "2026-10-15", "home": "DEN", "away": "SEA", "week": 6},
    {"status": "pre_game", "date": "2026-10-18", "home": "KC", "away": "LAC", "week": 6},
    {"status": "pre_game", "date": "2026-10-19", "home": "SF", "away": "WAS", "week": 6},
]


class _Resp:
    def __init__(self, data):
        self._b = json.dumps(data).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_game_days_come_from_the_schedule():
    S._STATUS_CACHE.clear()
    S._DAYS_CACHE.clear()
    with patch("urllib.request.urlopen", return_value=_Resp(SCHEDULE)):
        days = S.game_days("2026", 6)
    assert days["KC"] == "Sun Oct 18" and days["SEA"] == "Thu Oct 15" and days["SF"] == "Mon Oct 19"


def test_schedule_down_gives_no_days():
    S._STATUS_CACHE.clear()
    S._DAYS_CACHE.clear()
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert S.game_days("2026", 6) == {}
