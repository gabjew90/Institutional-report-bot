"""Figure matching in the pilot citation verifier.

Plan-4.3 stress run (2026-09-17): 18 of 20 citation failures were the
same figure written two ways, "40k" in the card and "40,000" in the
pulse (the editor writes figures out by contract), and one more was
"September 16 meeting" read as the figure "16m". Review the same day:
the range regex minted "2026%" from "by 2026 to 3.5%", the bp/% alias
was symmetric (a card's 3.5% passed a pulse 350bp), and a failure was
reported in the alias form rather than the sentence's own."""
from scripts.pilot_verify_citations import _numbers, verify


def nums(s, **kw):
    return sorted(_numbers(s, **kw))


def _pack(claim, anchor="", bank="Goldman Sachs"):
    return {"cards": {"c1": {"claim": claim, "anchor": anchor or claim, "bank": bank}},
            "docs": {}, "card_count": 1}


def _md(sentence):
    return f"# T\n\n## 2. THE MAIN EVENT\n\n{sentence}\n"


def test_shorthand_and_written_out_figures_match():
    assert nums("forecast 40k net payroll additions") == ["40000"]
    assert nums("Goldman estimated 40,000 against a consensus of 55,000") == ["40000", "55000"]
    assert nums("$115bn") == nums("$115 billion in fiscal 2027") == ["115000000000"]
    assert nums("1.5m barrels") == nums("1.5 million barrels") == ["1500000"]
    assert nums("$1.2T") == nums("$1.2 trillion") == ["1200000000000"]


def test_a_month_day_before_a_word_is_not_a_magnitude():
    assert nums("That splits the Tier-1 banks on the September 16 meeting.") == []
    assert nums("the 3 biggest names") == []


def test_the_pulse_side_keeps_the_sentence_form_and_the_card_side_aliases_bp():
    assert nums("gained 536 basis points") == ["536bp"]
    assert nums("gained 536 basis points", card_side=True) == ["5.36%", "536bp"]
    assert nums("rose 5.36%", card_side=True) == ["5.36%"]
    assert "3bp" in nums("10-year yields fell 3bp to 4.75%")


def test_range_shorthand_yen_and_multiples():
    assert "3.5%" in nums("unchanged at 3.5-3.75% through 2026")
    assert "7%" in nums("coincided with ~7-10% drawdowns")
    assert nums("falling by 2026 to 3.5%") == ["3.5%"]
    assert nums("¥143.1tn") == nums("143.1 trillion yen") == ["143100000000000"]
    assert nums("14.5x P/E") == nums("14.5 times earnings") == ["14.5"]
    assert nums("raised guidance 3 times") == []


def test_verify_accepts_shorthand_cards_and_written_out_pulses():
    out = verify(_md("Goldman estimated 40,000 for August [c1]."),
                 _pack("Goldman Sachs economists forecast 40k net non-farm payroll additions for August."))
    assert out["failures"] == [], out["failures"]
    out = verify(_md("Goldman's AI software basket rose 5.36% on Thursday [c1]."),
                 _pack("Goldman's AI Software basket gained 536 basis points on Thursday."))
    assert out["failures"] == [], out["failures"]


def test_a_percent_card_does_not_pass_a_basis_point_pulse():
    out = verify(_md("Goldman says spreads widened 350bp [c1]."),
                 _pack("Goldman sees GDP growth of 3.5%."))
    assert [f["reason"] for f in out["failures"]] == ["figures not in cited card(s): ['350bp']"]


def test_a_failure_names_the_figure_as_the_sentence_wrote_it():
    out = verify(_md("Goldman expects a 25bp cut [c1]."), _pack("Goldman expects a 50bp cut."))
    assert [f["reason"] for f in out["failures"]] == ["figures not in cited card(s): ['25bp']"]


def test_a_year_before_a_range_word_is_not_a_bound():
    out = verify(_md("Goldman sees inflation falling by 2026 to 3.5% [c1]."),
                 _pack("Goldman: inflation falls to 3.5% by 2026."))
    assert out["failures"] == [], out["failures"]
