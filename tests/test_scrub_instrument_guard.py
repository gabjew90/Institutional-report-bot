"""SCRUB may not add instruments (2026-09-28: it swapped a theme's $VIXY
hedge for "puts on Goldman's own momentum index (GSP1MOMO)", a ticker in
no source, and it shipped)."""
import json

from scripts import pulse_driver as PD

PRE = (
    "# H\n\n## 1. RECAP\n\nStocks fell, the dash — here.\n\n"
    "## 2. INSIGHTS & ALPHA\n\n### Breadth\n\n"
    "Breadth is thin. Long $RSP over $SPY, paired with $VIXY as a hedge "
    "while this week's shock is still live.\n\n"
    "### Oil\n\nOil rose; the curve steepened.\n"
)
POST = (
    "# H\n\n## 1. RECAP\n\nStocks fell, the dash is gone here.\n\n"
    "## 2. INSIGHTS & ALPHA\n\n### Breadth\n\n"
    "Breadth is thin. Long $RSP over $SPY, paired with puts on Goldman's "
    "own momentum index (GSP1MOMO) as the hedge while this week's shock "
    "is still live.\n\n"
    "### Oil\n\nOil rose, and the curve steepened.\n"
)


def test_the_incident_paragraph_is_restored_and_other_fixes_kept():
    out, added = PD.revert_new_instruments(PRE, POST)
    assert added == ["GSP1MOMO"]
    assert "paired with $VIXY as a hedge" in out and "GSP1MOMO" not in out
    # SCRUB's voice fixes elsewhere survive
    assert "the dash is gone here" in out and "Oil rose, and the curve" in out


def test_a_new_cashtag_is_caught_too():
    out, added = PD.revert_new_instruments(PRE, POST.replace("GSP1MOMO", "$MTUM"))
    assert added == ["$MTUM"] and "$VIXY" in out


def test_no_new_instrument_is_a_no_op():
    post = POST.replace(
        "puts on Goldman's own momentum index (GSP1MOMO) as the hedge",
        "$VIXY as the hedge")
    out, added = PD.revert_new_instruments(PRE, post)
    assert added == [] and out == post


def test_plain_acronyms_periods_and_levels_are_not_instruments():
    toks = PD._instrument_tokens(
        "The Fed (FOMC) and core PCE (PCE), 10Y at 5.25%, Q3, FY26, 1H27, "
        "COVID19, the S&P 500, but GSP1MOMO and SPXW7700C are.")
    assert toks == {"GSP1MOMO", "SPXW7700C"}


def test_a_gloss_scrub_added_is_kept():
    post = POST.replace(
        "puts on Goldman's own momentum index (GSP1MOMO) as the hedge",
        "$VIXY (a volatility fund) as the hedge")
    out, added = PD.revert_new_instruments(PRE, post)
    assert added == [] and out == post


def test_a_merged_paragraph_loses_nothing():
    """SCRUB merged the theme's two paragraphs and added a ticker: the
    whole theme block comes back, so the second paragraph survives."""
    pre = PRE.replace("### Breadth\n\n", "### Breadth\n\nFirst paragraph.\n\n")
    post = POST.replace("### Breadth\n\n", "### Breadth\n\nFirst paragraph. ")
    out, added = PD.revert_new_instruments(pre, post)
    assert added == ["GSP1MOMO"]
    assert "First paragraph.\n\nBreadth is thin" in out and "$VIXY" in out
    assert out.count("Breadth is thin") == 1
    # untouched blocks keep SCRUB's edits
    assert "Oil rose, and the curve" in out and "the dash is gone here" in out


def test_the_relint_gate_reverts_before_linting(tmp_path):
    (tmp_path / "pre_scrub_final.md").write_text(PRE, encoding="utf-8")
    (tmp_path / "final.md").write_text(POST, encoding="utf-8")
    (tmp_path / "ctx.json").write_text(json.dumps({"today": "2026-09-28"}), encoding="utf-8")
    d = PD.Driver(tmp_path)
    assert d._revert_scrub_instruments() == ["GSP1MOMO"]
    assert "$VIXY" in (tmp_path / "final.md").read_text(encoding="utf-8")
    assert any(h.get("record") == "scrub_instrument_revert" for h in d.state["history"])
    # second call: nothing left to revert
    assert d._revert_scrub_instruments() == []
