"""Smoke: the 2026-08-20 structural fixes from the pulse review.

  1. Consensus ledger: _render_prev_consensus_block extracts consensus
     lines from the previous pulse; empty/none paths are honest.
  2. consensus-amnesia validator: fires on the REAL 8/19 failure shapes
     ("no consensus" denial; unframed actual), stays quiet on a clean
     recap and on an empty ledger; registered HARD.
  3. section-length lint: soft kind, fires outside tolerance, quiet
     inside; DRAFT-stage markdown (INSIGHTS header) is a no-op.
  4. Prompt wiring: {prev_consensus_block} present in DRAFT_USER and
     AUDIT_USER, absent from AUDIT_SYSTEM (which is never formatted).
"""

import sys
from unittest.mock import patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_PREV_MD = """# Prior pulse

## 4. WHAT TO WATCH

- **Wednesday before the open: Target.** Consensus is $2.31 a share on $26.32bn of revenue.
- **Thursday before the open: Walmart.** Consensus is $0.75 a share on $188.79bn of revenue.
- **Jackson Hole.** No figures here.
"""

# The 8/19 phrasing that the old extractor missed: "expected", no
# "consensus" word. And a 2-pulses-back line the old lookback missed.
_PREV_MD_NEWEST = """# Newer pulse

## 4. WHAT TO WATCH

- **Thursday, August 20: Walmart reports before the open**, with $188.79 billion of revenue expected.
"""

_LEDGER_CTX = {
    "prev_consensus_block": (
        "[from your own pulse dated 2026-08-18]\n"
        "- Wednesday before the open: Target. Consensus is $2.31 a share "
        "on $26.32bn of revenue.\n"
        "- Thursday before the open: Walmart. Consensus is $0.75 a share "
        "on $188.79bn of revenue."
    )
}


def test_ledger_extraction():
    import db
    from report.synthesizer import _render_prev_consensus_block
    with patch.object(db, "get_last_daily_pulses",
                      return_value=[
                          {"report_date": "2026-08-19",
                           "report_markdown": _PREV_MD_NEWEST},
                          {"report_date": "2026-08-18",
                           "report_markdown": _PREV_MD},
                      ]):
        block = _render_prev_consensus_block()
    # "expected" phrasing (8/19) must be captured
    assert "$188.79 billion of revenue expected" in block, block
    # two-pulses-back consensus (8/18) must be captured
    assert "Target" in block and "$2.31" in block, block
    assert "$0.75" in block, block
    assert "Jackson Hole" not in block, "no-figure lines must not enter"
    assert "dated 2026-08-19" in block and "dated 2026-08-18" in block
    with patch.object(db, "get_last_daily_pulses", return_value=[]):
        assert _render_prev_consensus_block() == "(none)"
    _ok("ledger: 'expected' phrasing + 3-pulse lookback + empty path")


def test_lint_ignores_internal_blocks_for_voice():
    sys.path.insert(0, "scripts")
    import importlib
    pl = importlib.import_module("pulse_lint")
    dash = "—"
    md = ("## 1. RECAP\n\nclean prose here.\n\n"
          f"## _LEANS (internal {dash} TRADE BOARD source)\n"
          f"- long | $SPY | a; b {dash} c\n")
    kinds = [i["kind"] for i in pl.lint_markdown(md)]
    assert not any(k in ("em-dash", "semicolon") for k in kinds), kinds
    md2 = f"## 1. RECAP\n\nprose {dash} with a dash.\n"
    assert "em-dash" in [i["kind"] for i in pl.lint_markdown(md2)], \
        "reader prose must still be scanned"
    _ok("lint: internal ## _ blocks exempt from voice scan, prose not")


def _pdv():
    sys.path.insert(0, "scripts")
    import importlib
    return importlib.import_module("pulse_draft_validate")


def test_amnesia_fires_on_denial():
    pdv = _pdv()
    md = ("## 1. RECAP\n\nlede.\n\n"
          "- **Target reported this morning at $2.46 a share on $26.54 "
          "billion of revenue.** No consensus figures were posted for "
          "either.\n\n## 2. THE MAIN EVENT\nx\n")
    v = pdv._consensus_amnesia_violations(md, _LEDGER_CTX)
    assert v and v[0]["kind"] == "consensus-amnesia", v
    assert "no consensus" in v[0]["message"].lower(), v
    _ok("validator: fires on the 8/19 'no consensus' denial")


def test_amnesia_fires_on_unframed_actual():
    pdv = _pdv()
    md = ("## 1. RECAP\n\nlede.\n\n"
          "- **Walmart reported before the open, $0.81 a share on "
          "$190.1bn of revenue.** Retail is the direct test this week.\n"
          "\n## 2. THE MAIN EVENT\nx\n")
    v = pdv._consensus_amnesia_violations(md, _LEDGER_CTX)
    assert v and "frames it against" in v[0]["message"], v
    _ok("validator: fires on an actual reported with no beat/miss")


def test_amnesia_quiet_when_framed_or_no_ledger():
    pdv = _pdv()
    good = ("## 1. RECAP\n\nlede.\n\n"
            "- **Walmart reported $0.81 a share against the $0.75 "
            "consensus, a clean beat on $190.1bn of revenue.**\n\n"
            "## 2. THE MAIN EVENT\nx\n")
    assert pdv._consensus_amnesia_violations(good, _LEDGER_CTX) == []
    bad = ("## 1. RECAP\n\n- **Walmart reported $0.81 a share.** No "
           "consensus was posted.\n\n## 2. X\nx\n")
    for empty in ("(none)", "(unavailable)", ""):
        v = pdv._consensus_amnesia_violations(
            bad, {"prev_consensus_block": empty})
        assert v == [], f"must be quiet with ledger={empty!r}: {v}"
    _ok("validator: quiet on framed beat and on empty ledger")


def test_amnesia_registered_hard():
    pdv = _pdv()
    assert "consensus-amnesia" in pdv.HARD_VIOLATION_KINDS
    _ok("validator: consensus-amnesia is a HARD kind")


def test_section_length_lint():
    sys.path.insert(0, "scripts")
    import importlib
    pl = importlib.import_module("pulse_lint")
    assert "section-length" in pl.SOFT_ISSUE_KINDS, "must be soft"
    me_long = " ".join(["word"] * 560)
    brief_short = "tiny brief."
    md = (f"## 2. THE MAIN EVENT\n\n### Lead\n\n{me_long}\n\n"
          f"## 3. BRIEFS\n\n### One\n{brief_short}\n\n"
          f"## 4. WHAT TO WATCH\nx\n")
    kinds = [i["kind"] for i in pl._check_section_lengths(md)]
    assert kinds.count("section-length") == 2, kinds
    ok_md = ("## 2. THE MAIN EVENT\n\n" + " ".join(["w"] * 400)
             + "\n\n## 3. BRIEFS\n\n### One\n" + " ".join(["w"] * 150)
             + "\n\n## 4. WHAT TO WATCH\nx\n")
    assert pl._check_section_lengths(ok_md) == []
    draft_md = "## 2. INSIGHTS & ALPHA\n\n" + " ".join(["w"] * 900) + "\n\n## 4. X\nx\n"
    assert pl._check_section_lengths(draft_md) == [], \
        "DRAFT-stage headers must be a no-op"
    _ok("lint: section-length soft, fires outside band, quiet inside")


def test_prompt_wiring():
    import ai_analysis.prompts as p
    assert "{prev_consensus_block}" in p.DRAFT_USER
    assert "{prev_consensus_block}" in p.AUDIT_USER
    assert "{prev_consensus_block}" not in p.AUDIT_SYSTEM, \
        "AUDIT_SYSTEM is never .format()ed — placeholder would ship raw"
    assert "300-450" in p.DRAFT_USER and "110-180" in p.DRAFT_USER, \
        "length contract not reconciled"
    _ok("prompts: placeholder in USER templates only; lengths updated")


if __name__ == "__main__":
    print("=== consensus ledger + length contract smoke ===")
    test_ledger_extraction()
    test_amnesia_fires_on_denial()
    test_amnesia_fires_on_unframed_actual()
    test_amnesia_quiet_when_framed_or_no_ledger()
    test_amnesia_registered_hard()
    test_section_length_lint()
    test_lint_ignores_internal_blocks_for_voice()
    test_prompt_wiring()
    print("\nALL CONSENSUS-LEDGER SMOKE TESTS PASS")
