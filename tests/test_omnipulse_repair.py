"""Narrow repairs to the locked Omnipulse body (owner option C, 2026-10-06)."""
from scripts import omnipulse_repair as R

# the 2026-10-05 sentence that shipped
GS = ("France's budget calls for a record EUR340 billion of bond issuance next year. "
      "Goldman says the latest leg of the widening is driven by political and budget "
      "risk premium rather than fresh French fiscal deterioration, and the move is "
      "spilling into Italian bonds. BofA puts fair value for that spread at 100 to 110 "
      "basis points.")

# the 2026-09-29 diesel brief shape
DIESEL = ("### Diesel squeeze\n"
          "Diesel futures rose 91 cents to $6.52. The White House is preparing a 90-day ban "
          "on diesel exports. Refiners are running near capacity.\n")


def test_a_source_prefix_opener_keeps_the_bank_and_passes_lint():
    out, notes = R.fix_voice(GS)
    assert "Goldman says" not in out
    assert "In Goldman's view, the latest leg of the widening" in out
    assert notes and "Goldman" in notes[0]
    import re
    from ai_analysis.voice_rules import compose_lint_patterns
    kinds = [k for rx, k in compose_lint_patterns() if re.search(rx, out, re.I)]
    assert "source-prefix" not in kinds


def test_mid_sentence_prefix_uses_lower_case_and_plural_banks_get_an_apostrophe():
    out, _ = R.fix_voice("Rates fell, and Barclays notes the curve steepened.")
    assert out == "Rates fell, and in Barclays' view, the curve steepened."


def test_dashes_and_semicolons_become_commas_but_tables_are_left_alone():
    out, notes = R.fix_voice("Yields rose — sharply; stocks held.\n| a; b |")
    assert out.splitlines()[0] == "Yields rose, sharply, stocks held."
    assert out.splitlines()[1] == "| a; b |"
    assert len(notes) == 2


def test_a_fabricated_event_sentence_is_cut_and_nothing_else():
    findings = [{"kind": "fabricated-event",
                 "quote": "The White House is preparing a 90-day ban on diesel exports"},
                {"kind": "overstated-claim", "quote": "Refiners are running near capacity"}]
    out, cut = R.cut_findings(DIESEL, findings)
    assert cut == ["The White House is preparing a 90-day ban on diesel exports."]
    assert "preparing a 90-day ban" not in out
    assert "Refiners are running near capacity." in out, "judgment kinds are recorded, not cut"
    assert out.startswith("### Diesel squeeze\n")


def test_cuts_are_capped_and_need_the_quote_in_the_body():
    body = "One thing happened. Two things happened. Three things happened."
    fs = [{"kind": "fabricated-event", "quote": q} for q in
          ("One thing happened", "Two things happened", "Three things happened")]
    out, cut = R.cut_findings(body, fs)
    assert len(cut) == R.MAX_CUTS and out == "Three things happened."
    _, none = R.cut_findings(body, [{"kind": "invented-call", "quote": "a sentence not in the body"}])
    assert none == []


def test_a_heading_is_never_cut():
    out, cut = R.cut_findings("### The White House diesel ban\nText here.",
                              [{"kind": "fabricated-event", "quote": "The White House diesel ban"}])
    assert cut == [] and out.startswith("### The White House diesel ban")


def test_number_ranges_and_minus_signs_survive_the_dash_fix():
    out, _ = R.fix_voice("Core PCE rose 0.2–0.3% while GDP tracks –0.4%. Rates — finally — fell.")
    assert out == "Core PCE rose 0.2 to 0.3% while GDP tracks -0.4%. Rates, finally, fell."


def test_a_banned_publication_opener_is_left_for_lint():
    out, notes = R.fix_voice("TME notes dealers are short gamma.")
    assert out == "TME notes dealers are short gamma." and notes == []


def test_the_cut_limit_is_what_is_left_of_the_day():
    body = "One thing happened. Two things happened."
    fs = [{"kind": "fabricated-event", "quote": "One thing happened"},
          {"kind": "fabricated-event", "quote": "Two things happened"}]
    out, cut = R.cut_findings(body, fs, max_cuts=1)
    assert cut == ["One thing happened."] and out == "Two things happened."
    assert R.cut_findings(body, fs, max_cuts=0)[1] == []
