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
