"""Smoke: numeric-scope-drift validator (2026-07-07).

The 07-07 pulse cited "119% Y/Y" — a MEMORY figure from the source
("119% Y/Y in May, with memory pricing improving materially") — but
rendered it as "May SEMICONDUCTOR INDUSTRY SALES still grew 119%".
Actual 2026 semi-industry growth was ~26%; the whole memory category
~39%. The number was real and present in the source, so a fabrication
check passes. What drifted was the number's SUBJECT: a narrow segment
figure re-scoped to the whole industry.

pulse_draft_validate.validate() now runs Check 7: for a stat present in
both draft and source with the same value, if the draft's subject words
and the source's subject words share NOTHING (after synonym-normalizing
true synonyms but NEVER collapsing memory->semiconductor), flag it soft.

This is structural, not a prompt line: it compares the draft against the
research corpus in ctx.json the same way the sibling/lean checks do.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts import pulse_draft_validate as v  # noqa: E402


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# The real source string, wrapped the way the context ships it (a big
# analyses_json blob of per-PDF JSON text). Note the source subject is
# "memory" — there is no "industry"/"semiconductor" anywhere near the
# 119% figure.
_SOURCE_119 = (
    '[{"source":"JPMorgan","key_insights":['
    '"119% Y/Y in May, with memory pricing improving materially.",'
    '"Global capital spending running 9.8% annualized in Q2."'
    ']}]'
)


def _ctx(analyses_text):
    return {"theme_map": {}, "analyses_json": analyses_text}


def _pulse(insights_body, recap_body="Live prices only."):
    return (
        "# Test pulse\n\n"
        "## 1. RECAP\n\n" + recap_body + "\n\n"
        "## 2. INSIGHTS & ALPHA\n\n"
        "### The crowded trade cracks\n\n" + insights_body + "\n\n"
        "## 3. WHAT TO WATCH\n\n### Today\nNothing.\n\n"
        "## _LEANS (internal)\n- short | $SMH | crowded semis\n"
    )


def test_fires_on_the_observed_drift():
    # draft attaches 119% to "semiconductor industry sales"; source uses
    # it for "memory".
    md = _pulse(
        "May semiconductor industry sales still grew 119% year over year "
        "(JPMorgan). The end market is not rolling over."
    )
    viols = v.validate(md, _ctx(_SOURCE_119))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert len(drift) == 1, f"expected 1 scope-drift, got {viols}"
    assert drift[0]["figure"].startswith("119"), drift[0]
    assert "semiconductor" in drift[0]["draft_subject"], drift[0]
    assert "memory" in drift[0]["source_subject"], drift[0]
    assert drift[0]["severity"] == "soft"  # never hard-blocks the pulse
    _ok("fires: 119% memory-figure re-scoped to 'semiconductor industry sales'")


def test_real_source_says_industry_so_it_passes():
    # THE ACTUAL 07-07 source string: it says "semiconductor industry
    # sales growth accelerated to +119%". So the pulse's "semiconductor
    # industry sales grew 119%" is GROUNDED, not a drift. This locks that
    # in — the guard must NOT flag a figure the source really did scope to
    # the industry. (An earlier QC pass misread this as a memory-only
    # figure; it never was.)
    real_src = (
        '[{"source":"JPMorgan","key_insights":['
        '"Semiconductor industry sales growth accelerated to +119% Y/Y in '
        'May, with memory pricing improving materially."]}]'
    )
    md = _pulse(
        "May semiconductor industry sales still grew 119% year over year "
        "(JPMorgan). The end market is not rolling over."
    )
    viols = v.validate(md, _ctx(real_src))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert drift == [], f"grounded industry figure must pass: {drift}"
    _ok("passes: source genuinely says 'industry' -> 119% is grounded")


def test_first_mention_decides_no_restatement_fp():
    # first mention is grounded; a later GENERIC restatement of the same
    # number must not re-fire (the 60% false positive on real data).
    src = ('[{"key_insights":["AI infrastructure stocks drive 60% of S&P '
           '500 EPS growth this quarter."]}]')
    md = _pulse(
        "AI infrastructure now drives 60% of index EPS growth this quarter. "
        "When 60% of the index rides on one trade, the whole tape is only as "
        "safe as that trade."
    )
    viols = v.validate(md, _ctx(src))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert drift == [], f"grounded first mention should retire the figure: {drift}"
    _ok("first-occurrence: grounded lead mention retires the figure, no restate FP")


def test_correct_scope_passes():
    # same figure, correct subject -> no flag.
    md = _pulse(
        "Memory sales grew 119% year over year, and memory pricing keeps "
        "improving."
    )
    viols = v.validate(md, _ctx(_SOURCE_119))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert drift == [], f"correct scope must pass: {drift}"
    _ok("passes: 119% kept on 'memory' matches the source subject")


def test_entity_synonym_passes():
    # source says 'chip', draft says 'semiconductor' — same entity, a
    # genuine synonym, must NOT flag. (Also exercises measure-word
    # stripping: 'revenue' vs 'sales' are both dropped, so the overlap
    # rides on the entity.)
    src = ('[{"key_insights":["Chip revenue rose 33% Y/Y across the group."]}]')
    md = _pulse("Semiconductor sales rose 33% on the year.")
    viols = v.validate(md, _ctx(src))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert drift == [], f"chip<->semiconductor synonym must pass: {drift}"
    _ok("passes: chip<->semiconductor normalized, no false drift flag")


def test_figure_absent_from_source_is_skipped():
    # a live-market observation the writer added in prose ("$QQQ down 2%")
    # whose value isn't a stat in the source -> skip, not fabrication-flag.
    md = _pulse("It is playing out live, with $QQQ down 2% on the session.")
    viols = v.validate(md, _ctx(_SOURCE_119))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert drift == [], f"absent figure must be skipped: {drift}"
    _ok("skips: figure not present in source as a stat -> no flag")


def test_recap_figures_are_not_scanned():
    # the SAME drifted sentence, but in RECAP (live-data section) -> not
    # scanned, so no flag. Guards against false positives on market prints.
    md = _pulse(
        "Nothing in insights.",
        recap_body="Semiconductor industry sales grew 119% year over year.",
    )
    viols = v.validate(md, _ctx(_SOURCE_119))
    drift = [x for x in viols if x["kind"] == "numeric-scope-drift"]
    assert drift == [], f"RECAP must not be scanned: {drift}"
    _ok("scopes to INSIGHTS: identical drift in RECAP is not flagged")


def test_soft_not_hard():
    # numeric-scope-drift is advisory — a fuzzy semantic check must never
    # hard-block the whole pulse on a false positive.
    assert "numeric-scope-drift" not in v.HARD_VIOLATION_KINDS
    _ok("severity: numeric-scope-drift is soft (never blocks the pulse)")


if __name__ == "__main__":
    print("=== pulse numeric-scope-drift smoke ===")
    test_fires_on_the_observed_drift()
    test_real_source_says_industry_so_it_passes()
    test_first_mention_decides_no_restatement_fp()
    test_correct_scope_passes()
    test_entity_synonym_passes()
    test_figure_absent_from_source_is_skipped()
    test_recap_figures_are_not_scanned()
    test_soft_not_hard()
    print("\nALL NUMERIC-SCOPE-DRIFT SMOKE TESTS PASS")
