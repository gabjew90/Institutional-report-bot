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


def test_one_consensus_when_both_feeds_list_the_report():
    """2026-11-17 (audit run 13 A8): NVDA's consensus was given as $2.53 on
    $111.28B from the calendar feed and, two minutes later, as $2.47 on
    $108.7B from Yahoo. The two were 2% apart, inside the old tolerance."""
    e = {"next": {"date": "2026-12-17", "eps_estimate": 38.3, "revenue_estimate": 61.7e9}}
    assert rec(e, _snap())
    assert e["next"]["eps_estimate"] == 38.28 and e["next"]["revenue_estimate"] == 61.75e9
    assert e["next"]["consensus_source"] == "Yahoo analyst consensus"
    assert e["next"]["date_status"] == "listed by both the calendar feed and Yahoo"
    # the same figures: nothing to change
    same = {"next": {"date": "2026-12-17", "eps_estimate": 38.28, "revenue_estimate": 61.75e9}}
    assert not rec(same, _snap()) and "consensus_source" not in same["next"]
    # a figure Yahoo lacks keeps the calendar's
    part = {"next": {"date": "2026-12-17", "eps_estimate": 38.3, "revenue_estimate": 61.7e9}}
    assert rec(part, _snap(rev=None)) and part["next"]["revenue_estimate"] == 61.7e9


def test_the_models_own_lookup_is_reconciled_too(monkeypatch):
    import asyncio
    from discord_bot import ask_tools as A
    from report import ticker_snapshot as ts

    async def fake(args):
        return {"status": "ok", "symbol": "MU",
                "next": {"date": "2026-12-17", "eps_estimate": 38.9, "revenue_estimate": 62.5e9}}
    monkeypatch.setattr(A, "_execute_earnings_date", fake)
    monkeypatch.setattr(ts, "fetch", lambda sym: _snap())
    out = asyncio.run(A._execute_earnings_date_one_consensus({"symbol": "MU"}))
    assert out["next"]["eps_estimate"] == 38.28 and out["next"]["revenue_estimate"] == 61.75e9
    # a failed snapshot read leaves the calendar's figures
    monkeypatch.setattr(ts, "fetch", lambda sym: (_ for _ in ()).throw(RuntimeError("yahoo down")))
    out = asyncio.run(A._execute_earnings_date_one_consensus({"symbol": "MU"}))
    assert out["next"]["eps_estimate"] == 38.9


def test_no_snapshot_growth_changes_nothing():
    e = {"next": {"date": "2026-12-15", "eps_estimate": 43.39}}
    assert not rec(e, {"status": "ok"}) and "date_status" not in e["next"]


def test_a_different_quarter_is_not_reconciled():
    """In the hours after a report the calendar lists the next quarter and
    Yahoo still lists the one just reported."""
    e = {"next": {"date": "2027-03-18", "eps_estimate": 43.39, "revenue_estimate": 69.3e9}}
    rec(e, _snap())
    assert e["next"]["eps_estimate"] == 43.39 and e["next"]["revenue_estimate"] == 69.3e9
