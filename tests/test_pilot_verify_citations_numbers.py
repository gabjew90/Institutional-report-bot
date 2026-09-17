"""Figure matching in the pilot citation verifier.

Plan-4.3 stress run (2026-09-17): 18 of 20 citation failures were the
same figure written two ways, "40k" in the card and "40,000" in the
pulse (the editor writes figures out by contract), and one more was
"September 16 meeting" read as the figure "16m"."""
import importlib.util
import pathlib

_P = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "pilot_verify_citations.py"
_spec = importlib.util.spec_from_file_location("pilot_verify_citations", _P)
V = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(V)


def nums(s):
    return sorted(V._numbers(s))


def test_shorthand_and_written_out_figures_match():
    assert nums("forecast 40k net payroll additions") == ["40000"]
    assert nums("Goldman estimated 40,000 against a consensus of 55,000") == ["40000", "55000"]
    assert nums("$115bn") == nums("$115 billion in fiscal 2027") == ["115000000000"]
    assert nums("1.5m barrels") == nums("1.5 million barrels") == ["1500000"]
    assert nums("$1.2T") == nums("$1.2 trillion") == ["1200000000000"]


def test_a_month_day_before_a_word_is_not_a_magnitude():
    assert nums("That splits the Tier-1 banks on the September 16 meeting.") == []
    assert nums("the 3 biggest names") == []


def test_percent_and_basis_points_are_one_figure():
    assert "5.36%" in nums("gained 536 basis points") and "536bp" in nums("rose 5.36%")
    assert "0.25%" in nums("+25bps") and "100bp" in nums("close to 100 basis points")
    assert "3bp" in nums("10-year yields fell 3bp to 4.75%")


def test_range_shorthand_yen_and_multiples():
    assert "3.5%" in nums("unchanged at 3.5-3.75% through 2026")
    assert "7%" in nums("coincided with ~7-10% drawdowns")
    assert nums("¥143.1tn") == nums("143.1 trillion yen") == ["143100000000000"]
    assert nums("14.5x P/E") == nums("14.5 times earnings") == ["14.5x"]
    assert "1.59x" in nums("net leverage at 47.6% and a 1.59x ratio")


def test_verify_accepts_the_stress_run_sentence():
    pack = {"cards": {"c1": {"claim": "Goldman Sachs economists forecast 40k net non-farm payroll additions for August.",
                             "anchor": "forecast 40k", "bank": "Goldman Sachs"}},
            "docs": {}, "card_count": 1}
    md = "# T\n\n## 2. THE MAIN EVENT\n\nGoldman estimated 40,000 for August [c1].\n"
    out = V.verify(md, pack)
    assert out["failures"] == [], out["failures"]
