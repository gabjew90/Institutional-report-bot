"""The calendar feed's consensus and date checked against Yahoo's in the
same turn (2026-10-26 audit A11, MU)."""
from discord_bot.ask_tools import reconcile_earnings_with_snapshot as rec


def _snap(eps=38.28, rev=61.75e9, date="2026-12-17"):
    return {"status": "ok", "growth": {"next_quarter": {"eps": eps, "revenue": rev},
                                       "next_report": {"date": date}}}


def test_yahoo_consensus_wins_and_the_date_is_unconfirmed():
    e = {"next": {"date": "2026-12-15", "eps_estimate": 43.39, "revenue_estimate": 69.3e9}}
    assert rec(e, _snap())
    n = e["next"]
    assert n["eps_estimate"] == 38.28 and n["revenue_estimate"] == 61.75e9
    assert n["date_status"].startswith("projected, not confirmed") and "2026-12-17" in n["date_status"]


def test_agreeing_feeds_are_left_alone():
    e = {"next": {"date": "2026-12-17", "eps_estimate": 38.3, "revenue_estimate": 61.7e9}}
    assert not rec(e, _snap())
    assert e["next"]["eps_estimate"] == 38.3
    assert e["next"]["date_status"] == "listed by both the calendar feed and Yahoo"


def test_no_snapshot_growth_changes_nothing():
    e = {"next": {"date": "2026-12-15", "eps_estimate": 43.39}}
    assert not rec(e, {"status": "ok"}) and "date_status" not in e["next"]
