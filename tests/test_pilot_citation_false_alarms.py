"""The citation check's false alarms on published days (2026-11-23).

The Omnipulse body was published on 14 days from 2026-09-29 to 11-20 and
the check recorded 47 failed sentences on them. Read one by one against
the cards they cite, 26 were the check misreading a sentence the cards
support, 14 were a sentence that opens or closes a bank's argument with no
citation of its own, and 6 carried a figure no cited card has. Every case
below is one of those sentences. The same replay found the 2026-10-05
pulse published with seven grouped markers ("[c443, c444]") in its text."""
from scripts import omnipulse_body as O
from scripts.pilot_verify_citations import _numbers, cites_in, strip_markers, verify


def _pack(*cards, docs=None):
    """cards: (claim, bank) in id order c1, c2, ..."""
    return {"cards": {f"c{i}": {"claim": c, "anchor": c, "bank": b} for i, (c, b) in enumerate(cards, 1)},
            "docs": docs or {}, "card_count": len(cards)}


def _md(*sentences, theme="### One theme"):
    return "# T\n\n## 2. THE MAIN EVENT\n\n" + theme + "\n\n" + " ".join(sentences) + "\n"


def _reasons(md, pack):
    return [f["reason"] for f in verify(md, pack)["failures"]]


# ----------------------------------------------------------- grouped markers

def test_a_grouped_marker_is_read_as_its_citations():
    assert cites_in("Payrolls rose 29,000 [c1197, c1198, c1199].") == [("c", 1197), ("c", 1198), ("c", 1199)]
    assert cites_in("x [c713, c714, d2] y [c9]") == [("c", 713), ("c", 714), ("d", 2), ("c", 9)]
    md = _md("September payrolls rose 29,000 against a 90,000 estimate [c1, c2].")
    pack = _pack(("Payrolls rose 29,000 in September.", "UBS"), ("The estimate was 90,000.", "UBS"))
    assert _reasons(md, pack) == []


def test_the_clean_step_removes_a_grouped_marker_and_the_body_check_sees_one():
    assert strip_markers("A auction cleared at 5.033% [c443, c444].") == "A auction cleared at 5.033%."
    assert strip_markers("Capex rose [c713, c714, d2] again [c9].") == "Capex rose again."
    body = ("# Headline\n\n## 2. THE MAIN EVENT\n\n### A\n\nText [c443, c444].\n\n"
            "## 3. BRIEFS\n\n### B\n\nText.\n\n### C\n\nText.\n")
    assert "citation markers survived the clean step" in O.problems(body, {})
    assert "citation markers survived the clean step" not in O.problems(strip_markers(body), {})
    # a bracket that is not a marker is prose
    assert "citation markers survived the clean step" not in O.problems(
        body.replace("[c443, c444]", "[sic]"), {})


# ------------------------------------------------------- one value, one form

def test_trailing_zeros_do_not_make_a_new_figure():
    assert _numbers("near the government's 5% target") == _numbers("objective of 5.0%")
    assert _numbers("63.3% of its members") == _numbers("63.30% of SOX constituents")
    assert _numbers("keeps a $300 target") == _numbers("TARGET: US$300.00")
    assert _numbers("raising cash to 14%") == _numbers("allocation to 14.0%")
    # a real difference still differs
    assert _numbers("5.1%") != _numbers("5%")


def test_a_percentage_followed_by_a_ratio_is_the_percentage():
    assert _numbers("core CPI at 0.24% m/m in September") == {"0.24%"}
    assert _numbers("retail sales rose 0.3%m/m, ex autos up 0.5%m/m") == {"0.3%", "0.5%"}
    assert _numbers("hours worked fell 0.2%m/m") == {"0.2%"}
    assert _reasons(_md("Morgan Stanley forecasts 0.24% on the month [c1]."),
                    _pack(("We forecast core CPI at 0.24% m/m in September.", "Morgan Stanley"))) == []


def test_percent_written_out_is_the_percent_sign():
    assert _numbers("due at 0.3 percent on the month") == {"0.3%"}
    assert _numbers("carrying 44.5 percent of market value") == _numbers("44.5% of S&P 500 market cap")
    assert _numbers("growth of 46 to 47 percent") == {"46%", "47%"}
    # "percentage points" is a different unit and stays a bare number
    assert _numbers("cut by 3.5 percentage points") == {"3.5"}


def test_a_vote_count_before_a_rate_is_not_a_range():
    assert _numbers("the board voting 6-3 to 4.85%") == {"4.85%"}
    assert "3.5%" in _numbers("unchanged at 3.5-3.75% through 2026")       # a real range still is


def test_a_unit_named_once_covers_the_figures_that_follow():
    md = _md("Its forecast has investment-grade spreads widening from 88 to 105 basis points [c1], "
             "and high-yield from 320 to 410 [c2].")
    pack = _pack(("Goldman forecasts US IG spreads widening from 88bp to 105bp in 2027.", "Goldman Sachs"),
                 ("Goldman forecasts US HY spreads widening from 320bp to 410bp in 2027.", "Goldman Sachs"))
    assert _reasons(md, pack) == []


def test_barrels_a_day_shorthand():
    assert _numbers("exports recovered to 23.6mb/d", card_side=True) >= _numbers("23.6 million barrels a day")


# --------------------------------------------- a bank named with no citation

def test_a_sentence_that_opens_a_banks_argument_is_checked_against_its_theme():
    pack = _pack(("Goldman's desk bought the dip in semiconductors on 11 November.", "Goldman Sachs"),
                 ("UBS says the rebound lacks breadth.", "UBS"))
    md = _md("Goldman is buying the rebound.", "Its desk bought semiconductors on the dip [c1].",
             "UBS will not follow.", "It says the rebound lacks breadth [c2].")
    assert _reasons(md, pack) == []


def test_a_bank_the_theme_never_cites_still_fails():
    """2026-09-24, the case the check was built for: the editor staged a
    second bank's view that no card carried."""
    pack = _pack(("Goldman sees the level holding.", "Goldman Sachs"))
    md = _md("Goldman sees the level holding [c1].", "Morgan Stanley takes the other side of the level.")
    assert any("morgan stanley" in r for r in _reasons(md, pack))


def test_a_bank_cited_only_in_another_theme_does_not_count():
    pack = _pack(("Goldman sees the level holding.", "Goldman Sachs"),
                 ("Morgan Stanley expects a 2027 cut.", "Morgan Stanley"))
    md = ("# T\n\n## 2. THE MAIN EVENT\n\n### First\n\nGoldman sees the level holding [c1]. "
          "Morgan Stanley takes the other side.\n\n## 3. BRIEFS\n\n### Second\n\n"
          "Morgan Stanley expects a cut next year [c2].\n")
    assert any("morgan stanley" in r for r in _reasons(md, pack))


def test_a_cited_sentence_still_needs_its_own_bank():
    pack = _pack(("Zero Hedge notes Brent above $102.", "Zero Hedge"),
                 ("Goldman sees the market balanced.", "Goldman Sachs"))
    md = _md("Goldman sees the market balanced [c2].", "Goldman says Brent traded above $102 [c1].")
    assert any("goldman" in r for r in _reasons(md, pack))


# ------------------------------------------ what must still fail: real cases

def test_a_figure_no_cited_card_carries_still_fails():
    pack = _pack(("Bitcoin's selling accelerated after BTC broke below its 200-day moving average "
                  "around $73,000.", "The Market Ear"))
    md = _md("The weekend flush triggered nearly $2 billion of forced closures after the price broke "
             "below its 200-day average around $73,000 [c1].")
    assert _reasons(md, pack) == ["figures not in cited card(s): ['2000000000']"]
    pack = _pack(("Global Systematic L/S managers were down 3.7% MTD, the first negative month of the "
                  "year, while still up 16% YTD.", "Goldman Sachs"))
    md = _md("Systematic managers slipped 0.7% to end October down 3.7%, still up 16% year to date [c1].")
    assert _reasons(md, pack) == ["figures not in cited card(s): ['0.7%']"]


def test_a_figure_that_cites_only_a_brief_still_fails():
    md = _md("The question is whether Europe, growing near 1%, can carry the cost [d1].")
    assert _reasons(md, _pack(docs={"d1": {"bank": "Goldman Sachs"}})) == [
        "figures with no card citation: ['1%']"]
