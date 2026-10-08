"""The Omnipulse retirement streak: no-research light days are neutral
(owner, 2026-10-08)."""
from scripts import omnipulse_streak as S


def test_neutral_days_neither_count_nor_break():
    days = [("d1", "omnipulse"), ("d2", "neutral"), ("d3", "omnipulse")]
    assert S.streak(days) == 2
    assert S.streak(days + [("d4", "miss")]) == 0
    assert S.streak([("d1", "miss"), ("d2", "omnipulse"), ("d3", "neutral")]) == 1


def test_only_the_no_research_reason_is_neutral():
    assert S.classify({"body_source": "light",
                       "light_reason": "no new bank research since the last pulse"}) == "neutral"
    assert S.classify({"body_source": "light", "light_reason": "not published after 600s"}) == "miss"
    assert S.classify({"body_source": "omnipulse"}) == "omnipulse"
    assert S.classify({}) == "miss"


def test_market_days_skip_weekends():
    assert S.market_days("2026-10-02", "2026-10-06") == ["2026-10-02", "2026-10-05", "2026-10-06"]
