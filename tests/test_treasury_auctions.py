"""Treasury auction results (report/treasury_auctions.py, 2026-10-08 audit)."""
from discord_bot import ask_router as R
from report import treasury_auctions as T

NOTE = {"securityType": "Note", "securityTerm": "9-Year 10-Month", "term": "10-Year",
        "originalSecurityTerm": "10-Year", "auctionDate": "2026-10-07T00:00:00",
        "highYield": "5.3000", "bidToCoverRatio": "2.770000", "offeringAmount": "39000000000",
        "totalAccepted": "40000000000", "indirectBidderAccepted": "31000000000",
        "directBidderAccepted": "6600000000", "primaryDealerAccepted": "1000000000",
        "reopening": "Yes", "closingTimeCompetitive": "01:00 PM", "tips": "No", "floatingRate": "No"}


def test_a_reopening_is_its_original_tenor_and_tips_is_separate():
    assert T.tenor(NOTE) == "10-Year"
    assert T.tenor({**NOTE, "tips": "Yes"}) == "10-Year TIPS"
    assert T.tenor({"securityType": "Bill", "securityTerm": "13-Week"}) == "13-Week"


def test_summary_carries_where_it_cleared_and_who_bought():
    s = T.summarize(NOTE)
    assert (s["security"], s["high_yield_pct"], s["bid_to_cover"], s["offering_usd_bn"]) == \
        ("10-Year Note", 5.3, 2.77, 39.0)
    assert s["indirect_pct"] == 77.5 and s["reopening"] is True


def test_auction_questions_route_to_treasurydirect():
    for q, term in (("how did the 10 year auction go", "10-Year"), ("did the 30y auction tail", "30-Year"),
                    ("when are the 13 week bill auctions", "13-Week")):
        r = R.classify(q)
        assert r.prefetch == [(R.T_AUCTION, {"term": term})], q
    assert R.classify("when is CPI").prefetch == [(R.T_ECON, {"days": 7})]


def test_only_treasury_auctions_route_and_tips_needs_asking():
    assert R.classify("how much did the Christie's auction make").shape != R.ECON_CALENDAR
    assert R.classify("did the bond auction tail").prefetch == [(R.T_AUCTION, {"term": ""})]
    assert not T._matches({**NOTE, "tips": "Yes"}, "10-Year")
    assert T._matches({**NOTE, "tips": "Yes"}, "10-Year TIPS")
