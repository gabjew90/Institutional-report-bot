"""Tech share of the S&P computed in code (owner, 2026-10-10): the model's
first answer gave the broad share as 40.2% beside a 38.9% sector."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from report.index_weights import compute  # noqa: E402
from discord_bot import ask_router as R  # noqa: E402

SECTORS = {"technology": 0.4045, "communication_services": 0.0995}
HOLD = {"NVDA": 0.0836, "AMZN": 0.0370, "GOOGL": 0.030546, "GOOG": 0.024504, "META": 0.024194}


def test_broad_share_adds_the_three_companies():
    d = compute(SECTORS, HOLD)
    assert d["tech"] == 40.5 and d["adds"] == {"Alphabet": 5.5, "Amazon": 3.7, "Meta": 2.4}
    assert d["broad"] == 52.1


def test_missing_data_gives_none():
    assert compute({}, HOLD) is None
    assert compute(SECTORS, {k: v for k, v in HOLD.items() if k != "META"}) is None


def test_the_note_carries_the_computed_figures():
    note = R.tech_share_note("what percentage of s&p500 is tech", compute(SECTORS, HOLD))
    assert "52.1%" in note and "40.5%" in note and "Alphabet 5.5%" in note
    # without data it falls back to the search instruction
    assert "from a search" in R.tech_share_note("how much of the S&P is tech")


def test_ex_top_return_backs_out_start_weights_2026_10_11():
    """"What would the S&P gain for the year be if you removed the top 10"
    got an equal-weight range and no number."""
    from report.index_weights import ex_top_return
    # one holding at 20% today that doubled while the index rose 20%:
    # start weight 0.2 * 1.2 / 2.0 = 0.12, contribution 12 points,
    # the rest (20 - 12) / 0.88 = 9.1%
    d = ex_top_return(0.20, {"X": 0.20}, {"X": 1.0})
    assert d["top_contribution_pts"] == 12.0 and d["ex_top_ytd_pct"] == 9.1
    assert ex_top_return(0.2, {"X": 0.2}, {}) is None


def test_ex_top_routing_and_note():
    q = "What would the sp500 gain for the year be if you removed the top 10 stocks"
    assert R.is_ex_top_question(q) and R.classify(q).shape == R.HISTORICAL_STAT
    assert R.is_ex_top_question("spx ytd excluding the top 10?")
    assert not R.is_ex_top_question("what are the top 10 stocks in the s&p")
    data = {"spx_ytd_pct": 15.1, "top_contribution_pts": 6.9, "ex_top_ytd_pct": 13.1,
            "as_of": "2026-10-09", "top_holdings": [{"symbol": "NVDA", "ytd_pct": 23.2,
                                                     "weight_pct": 8.4}]}
    note = R.ex_top_note(q, data)
    assert "+13.1%" in note and "6.9 points" in note and "NVDA +23.2%" in note
    assert "do not give an equal-weight" in R.ex_top_note(q, None)
