"""Valuation multiples computed in code (2026-10-26 audit A12/A13: NBIS
forward P/E answered "N/A", then invented as 68-72x against negative EPS)."""
from discord_bot import ask_router as R
from report import ticker_snapshot as S


def test_pe_and_price_to_sales_are_computed():
    g = {"fiscal_year": {"eps": 4.0, "revenue": 50e9}, "next_fiscal_year": {"eps": 5.0, "revenue": 60e9}}
    v = S.valuation(100.0, 600e9, 3.2, g)
    assert v == {"pe_trailing": 31.2, "pe_this_fiscal_year": 25.0, "pe_next_fiscal_year": 20.0,
                 "price_to_sales_this_fiscal_year": 12.0, "price_to_sales_next_fiscal_year": 10.0}
    line = S.render_valuation(v)
    assert "forward P/E on next fiscal year's consensus EPS 20.0x" in line


def test_a_loss_has_no_pe():
    v = S.valuation(157.0, 40e9, -1.9, {"next_fiscal_year": {"eps": -0.5, "revenue": 3e9}})
    assert v["pe_trailing"] == "none (EPS -$1.90, a loss)"
    assert v["pe_next_fiscal_year"] == "none (EPS -$0.50, a loss)"
    assert "P/E on the last 12 months' EPS: none (EPS -$1.90, a loss)" in S.render_valuation(v)
    assert v["price_to_sales_next_fiscal_year"] == 13.3


def test_next_fiscal_year_consensus_is_kept():
    g = S.growth_block({"+1y": {"avg": 6e9, "yearAgoRevenue": 3e9, "growth": 1.0}},
                       {"+1y": {"avg": 1.2, "yearAgoEps": 0.4, "growth": 2.0}}, [], [])
    assert g["next_fiscal_year"]["eps"] == 1.2
    assert any("next fiscal year" in ln for ln in S.render_growth(g))


def test_valuation_questions_fetch_the_snapshot():
    for q in ("what is NBIS forward pe", "NBIS forward p/e?", "whats the pe on nbis",
              "what multiple is AAPL trading at"):
        r = R.classify(q)
        assert r.shape == R.PRICE and R.T_SNAPSHOT in [p[0] for p in r.prefetch], q
    assert R.classify("the pe is crazy").shape == R.UNKNOWN
