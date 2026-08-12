"""Smoke: 2026-07-15 end-to-end accuracy batch.

Covers the six structural fixes from the pipeline review triggered by the
07-15 pulse QC (estimate-shipped-as-print CPI, mislabeled JPM EPS,
"Tuesday 7/22" on a Wednesday, "still propagating" jargon leak,
themes:[] in production latest.json, fragment-refresh dead end).
"""

import importlib.util
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
pdv = _load(os.path.join(_ROOT, "scripts", "pulse_draft_validate.py"), "pdv")

_CTX = {
    "today": "2026-07-15",
    "economic_calendar": (
        "ECONOMIC EVENTS ALREADY RELEASED (belongs in RECAP, NEVER in "
        "WHAT TO WATCH):\n"
        "  07-14 08:30 ET | [US] CPI m/m | ACTUAL=-0.4% (for 2026-06) | "
        "est=-0.1% | prev=0.3%\n"
        "  07-14 08:30 ET | [US] Core CPI m/m | ACTUAL=0.0% (for 2026-06) | "
        "est=0.2% | prev=0.2%\n"
    ),
}


# ---------------------------------------------------------------------------
# Task 1 — extraction schema carries number-status qualifiers
# ---------------------------------------------------------------------------

def test_extraction_schema_typed_numbers():
    from ai_analysis.models import MacroIndicator, KeyDataPoint
    from ai_analysis import prompts
    mi = MacroIndicator(indicator="CPI", reading="3.5%", interpretation="x")
    assert mi.status == "" and mi.period == "", "defaults must be empty (old rows)"
    kdp = KeyDataPoint(figure="3.5%", metric="CPI y/y", source_bank="GS")
    assert kdp.figure_status == "", "default must be empty (old rows)"
    p = prompts.ANALYSIS_SYSTEM_PROMPT
    assert '"status": "released | forecast | target' in p, \
        "macro_indicators.status definition missing"
    assert '"figure_status": "released | forecast | target | level' in p, \
        "key_data_points.figure_status definition missing"
    assert "Actual value or forecast" not in p, \
        "the untyped 'Actual value or forecast' definition must be gone"
    assert "NEVER strip a number's qualifier" in p, \
        "the qualifier rule must be in the extraction rules block"
    assert "reported / $6.14 ex-significant-items" in p.replace("'", "'") or \
        "$6.14 ex-significant-items" in p, \
        "earnings basis-labeling instruction missing"
    _ok("extraction schema: status/period/figure_status + qualifier rule")


def test_draft_and_audit_honor_labels():
    from ai_analysis.prompts import DRAFT_USER, AUDIT_SYSTEM
    assert "HONOR THE NUMBER-STATUS LABELS" in DRAFT_USER, \
        "DRAFT must carry the honor-the-labels rule"
    assert "the calendar wins" in DRAFT_USER, \
        "calendar-authority rule missing from DRAFT"
    assert "RELEASED-NUMBER RECONCILIATION" in AUDIT_SYSTEM, \
        "AUDIT must carry the reconciliation scan"
    assert "[REPORTED-BMO-today]" in AUDIT_SYSTEM, \
        "AUDIT timing check missing"
    _ok("DRAFT + AUDIT: honor-the-labels + reconciliation rules present")


# ---------------------------------------------------------------------------
# Task 2 — validator checks 8 and 9
# ---------------------------------------------------------------------------

def test_weekday_date_mismatch():
    md = ("### This Week\n"
          "- **Tuesday 7/22: Alphabet ($GOOGL), Tesla ($TSLA), after the "
          "close.**\n"
          "- **Thursday 7/16, 8:30 AM ET: Retail Sales.**\n"
          "- Wednesday, July 22: also fine as a spelled month.\n")
    v = pdv._weekday_date_violations(md, _CTX)
    assert len(v) == 1, f"exactly the wrong pairing must flag: {v}"
    assert v[0]["claimed"] == "Tuesday" and v[0]["actual"] == "Wednesday"
    assert v[0]["severity"] == "hard"
    assert "weekday-date-mismatch" in pdv.HARD_VIOLATION_KINDS
    # correct pairings never flag
    ok = pdv._weekday_date_violations(
        "Wednesday 7/22 and Thursday 7/16 are both right.", _CTX)
    assert ok == [], f"correct pairings must not flag: {ok}"
    _ok("CHECK 8: 'Tuesday 7/22' flags hard; correct weekdays pass")


def test_released_figure_reconciliation():
    # the exact 07-15 failure sentence shapes
    bad = ("Tuesday's June CPI came in cold. Headline prices fell 0.42% on "
           "the month against an estimate of just -0.1%, dragging the "
           "annual rate down to 3.88% from 4.2%.")
    v = pdv._released_figure_violations(bad, _CTX)
    figs = {x["figure"] for x in v}
    assert "3.88" in figs, f"estimate-as-print 3.88 must flag: {figs}"
    assert "0.42" not in figs, "magnitude match must clear 'fell 0.42%' vs ACTUAL=-0.4"
    assert "-0.1" not in figs, "figures labeled as estimates are exempt"
    # marker binds to its NEAREST figure only — 2.86 right after
    # "+0.2% estimate" must still flag
    bad2 = ("Core CPI dropped 0.02% on the month against a +0.2% estimate, "
            "landing at 2.86% year-over-year.")
    v2 = pdv._released_figure_violations(bad2, _CTX)
    assert "2.86" in {x["figure"] for x in v2}, \
        "figure after a neighboring estimate-label must still flag"
    # a correctly-written sentence is clean
    good = ("June CPI printed -0.4% on the month against a -0.1% estimate, "
            "and core CPI was flat at 0.0%.")
    assert pdv._released_figure_violations(good, _CTX) == []
    # matching the est value exactly gets the stronger message
    est_as_print = "June CPI came in at -0.1% on the month."
    v3 = pdv._released_figure_violations(est_as_print, _CTX)
    assert v3 and v3[0]["matches_estimate"] is True, \
        "est-value-as-print must be flagged as the estimate signature"
    # 2026-08-12: severity now splits. A figure matching the CONSENSUS on
    # a row that has already printed is the estimate-shipped-as-print
    # signature and is HARD — it shipped soft on 2026-07-15 and again on
    # 2026-08-12. The generic no-match case stays SOFT, because a stray
    # percentage in a long bullet is the noisy shape (three false
    # positives on 2026-08-07).
    for x in v + v2 + v3:
        want = "hard" if x["matches_estimate"] else "soft"
        assert x["severity"] == want, (
            f"{x['figure']} matches_estimate={x['matches_estimate']} "
            f"should be {want}, got {x['severity']}"
        )
    assert any(x["severity"] == "hard" for x in v3), \
        "the est-as-print case must be hard"
    _ok("CHECK 9: estimate-as-print flags HARD; generic mismatch stays soft; "
        "correct prints pass")


# ---------------------------------------------------------------------------
# Task 3 — web metadata themes + fragment refresh
# ---------------------------------------------------------------------------

def test_metadata_themes_new_headers():
    dash = _load(os.path.join(_ROOT, "scripts", "pulse_dashboard.py"), "dash")
    md = ("---\npdf_count: 5\ndumped_at_utc: 2026-07-15T13:53:41Z\n---\n\n"
          "# The hike is dead\n\n## 1. RECAP\n\nstuff\n\n"
          "## 2. THE MAIN EVENT\n\n### The bond market is right\n\nbody\n\n"
          "## 3. BRIEFS\n\n### AI chip demand is still the engine\n\nbody\n\n"
          "### The Middle East keeps a floor under oil\n\nbody\n\n"
          "## 4. WHAT TO WATCH\n\n### Today\n\n- bullet\n")
    meta = dash.extract_pulse_metadata(md, ts="2026-07-15T14-07-29Z")
    assert meta["themes"] == [
        "The bond market is right",
        "AI chip demand is still the engine",
        "The Middle East keeps a floor under oil",
    ], f"themes must come from MAIN EVENT + BRIEFS: {meta['themes']}"
    # WHAT TO WATCH's ### Today must NOT leak in
    assert "Today" not in meta["themes"]
    # legacy INSIGHTS & ALPHA headers still work
    md_old = md.replace("## 2. THE MAIN EVENT", "## 2. INSIGHTS & ALPHA") \
               .replace("## 3. BRIEFS", "## 3. SOMETHING ELSE")
    meta_old = dash.extract_pulse_metadata(md_old, ts="x")
    assert "The bond market is right" in meta_old["themes"]
    _ok("metadata: themes extracted from MAIN EVENT/BRIEFS + legacy headers")


def test_fragment_refresh_on_sha_change():
    import ast
    src = open(os.path.join(_ROOT, "github_bridge", "jobs.py"),
               encoding="utf-8").read()
    assert 'md_changed = bool(item_sha and cached_sha and item_sha != cached_sha)' in src, \
        "sha-change detection missing"
    assert "if not fragment_present or md_changed:" in src, \
        "re-render must fire on markdown change"
    assert 'entry["archive_sha"] = item_sha' in src or \
        '"archive_sha": item_sha' in src, "entry must stamp archive_sha"
    assert "and not md_changed)" in src, \
        "the cached-reuse branch must be bypassed when the markdown changed"
    _ok("fragment refresh: corrected archive markdown re-renders + repoints")


def test_fragment_has_disclaimer():
    dash = _load(os.path.join(_ROOT, "scripts", "pulse_dashboard.py"), "dash2")
    frag = dash.render_pulse_fragment(
        "# T\n\n## 1. RECAP\n\nbody\n")
    assert "pulse-disclaimer" in frag and "Not investment advice" in frag, \
        "web fragment must carry the disclaimer footer"
    _ok("web fragment: disclaimer footer present")


# ---------------------------------------------------------------------------
# Task 4 — truncation guard
# ---------------------------------------------------------------------------

def test_truncation_guard_wired():
    import ai_analysis.analyzer as az
    src = inspect.getsource(az.analyze_pdf_deep)
    assert "finish_reason" in src, "finish_reason check missing"
    assert "MAX_TOKENS" in src, "MAX_TOKENS detection missing"
    assert "refusing to parse a partial JSON" in src, \
        "truncation must raise, never parse a partial object"
    assert "retrying with" in src, "one raised-cap retry expected"
    _ok("truncation guard: detect -> retry raised cap -> raise (no partial JSON)")


# ---------------------------------------------------------------------------
# Task 5 — wording / clarity
# ---------------------------------------------------------------------------

def test_no_propagating_in_reader_phrasing():
    from ai_analysis.prompts import DRAFT_USER
    import re
    # every QUOTED suggested reader phrasing must be jargon-free — the
    # instruction may still NAME the banned words when telling the model
    # not to use them.
    assert "still propagating at press time" not in DRAFT_USER
    src = open(os.path.join(_ROOT, "report", "news_data.py"),
               encoding="utf-8").read()
    assert "number propagating" not in src, \
        "calendar PRINTED marker must not suggest 'propagating' phrasing"
    routine = open(os.path.join(
        _ROOT, "docs", "superpowers", "routines", "synthesis-routine.md"),
        encoding="utf-8").read()
    assert "number still propagating at press time" not in routine, \
        "routine press-time template must not suggest 'propagating'"
    _ok("press-time phrasing: pipeline jargon out of all suggested reader prose")


def test_desk_counting_meta_narration_lint():
    from ai_analysis.voice_rules import compose_lint_patterns
    import re
    pats = compose_lint_patterns()
    def hits(text):
        return [k for p, k in pats
                if re.search(p, text, re.IGNORECASE)]
    assert "meta-narration" in hits("Eleven desks flag it and none calls the direction."), \
        "'Eleven desks flag it' must lint as meta-narration"
    assert "meta-narration" in hits("the most heavily covered theme in the research"), \
        "'heavily covered theme' must lint"
    assert "meta-narration" in hits("a chunk of this conviction traces to one loud house"), \
        "'one loud house' must lint"
    assert "meta-narration" not in hits(
        "Goldman raised its target to $350 and JPMorgan disagrees."), \
        "named-bank attribution must NOT lint"
    _ok("lint: desk-counting + corpus-survey phrases flagged; attribution safe")


def test_ratings_legend():
    from report.pulse_sections import _render_hc_subsection, _norm_rating
    render_hc_subsection = _render_hc_subsection
    out = render_hc_subsection([
        {"source": "BofA", "ticker": "AMD", "rating": "Overweight",
         "pt": "$620", "rationale": "x", "action": ""},
    ])
    assert "OW = overweight" in out and "PT = price target" in out, \
        f"legend must decode the tokens used: {out}"
    out2 = render_hc_subsection([
        {"source": "GS", "ticker": "IBM", "rating": "Buy", "pt": "",
         "rationale": "x", "action": ""},
    ])
    assert "OW =" not in out2, "legend only shows tokens actually used"
    assert _norm_rating("equal weight", "") == "EW", \
        "space form of equal weight must normalize"
    _ok("HC section: ratings legend renders used tokens only; 'equal weight' maps")


def test_discord_footer_disclaimer():
    src = open(os.path.join(_ROOT, "report", "formatter.py"),
               encoding="utf-8").read()
    assert "Not investment advice" in src, \
        "Discord footer must carry the disclaimer"
    _ok("Discord footer: disclaimer present")


def test_cashtag_scrub_collisions_removed():
    stitch = _load(os.path.join(_ROOT, "scripts", "pulse_stitch.py"), "stitch")
    for tk in ("CCL", "ORA", "VOD", "BA"):
        assert tk not in stitch.FOREIGN_CASHTAGS, \
            f"${tk} is US-tradable — must not be in the unconditional scrub map"
    for tk in ("TSCO", "CNA", "BT", "RR"):
        assert tk in stitch.FOREIGN_CASHTAGS, \
            f"genuinely foreign ${tk} must stay mapped"
    new_md, fixes, _ = stitch.stitch("Long $CCL into the summer cruise ramp")
    assert "$CCL" in new_md, "US $CCL must survive the stitch pass"
    _ok("stitch: US-tradable tickers out of the scrub map; foreign ones kept")


def test_routine_volume_gate():
    """2026-07-16: the research feed ran ~14h late, the 10 AM fire found
    4 PDFs, and 36 landed six minutes after the snapshot. The routine
    must wait (re-fetching the ~15-min context dumps) until pdf_count
    >= 10 or ~90 minutes pass, then ship with a binding thin-corpus
    note if still short."""
    md = open(os.path.join(_ROOT, "docs", "superpowers", "routines",
                           "synthesis-routine.md"), encoding="utf-8").read()
    assert "STEP 2.2 — Corpus-volume gate" in md, "volume gate step missing"
    assert "MIN_PDFS = 10" in md, "10-PDF floor missing"
    assert "MAX_WAITS = 9" in md and "WAIT_SECS = 600" in md, \
        "wait budget must be bounded (~90 min)"
    assert "thin_corpus_note.txt" in md, "thin-corpus fallback note missing"
    assert md.count("thin_corpus_note.txt") >= 2, \
        "the note must be wired into the STEP 4 DRAFT dispatch"
    assert "if new_count >= count:" in md, \
        "re-fetched context must never go backward"
    # gate must run BEFORE the press-time check so freshness math sees
    # the FINAL context
    assert md.index("STEP 2.2 — Corpus-volume gate") < \
        md.index("STEP 2.5 — Press-time freshness check"), \
        "volume gate must precede the press-time check"
    _ok("routine: corpus-volume gate waits for >=10 PDFs, bounded, noted")


# ---------------------------------------------------------------------------
# Task 6 — multimodal docs/code alignment
# ---------------------------------------------------------------------------

def test_multimodal_prompt_alignment():
    from ai_analysis.prompts import ANALYSIS_SYSTEM_PROMPT as p
    assert "Image rendering was removed" not in p, \
        "stale removed-images claim must be gone"
    assert "When page images are attached" in p, \
        "dual-mode chart instruction missing"
    import ai_analysis.analyzer as az
    assert hasattr(az, "_should_run_multimodal"), \
        "selective multimodal path expected to exist (2026-05-07 decision)"
    claude_md = open(os.path.join(_ROOT, "CLAUDE.md"), encoding="utf-8").read()
    assert "multimodal carve-out" in claude_md, \
        "CLAUDE.md must describe the selective multimodal trigger"
    _ok("multimodal: prompt + CLAUDE.md aligned with the live selective path")


if __name__ == "__main__":
    print("=== 2026-07-15 pulse accuracy batch smoke ===")
    test_extraction_schema_typed_numbers()
    test_draft_and_audit_honor_labels()
    test_weekday_date_mismatch()
    test_released_figure_reconciliation()
    test_metadata_themes_new_headers()
    test_fragment_refresh_on_sha_change()
    test_fragment_has_disclaimer()
    test_truncation_guard_wired()
    test_no_propagating_in_reader_phrasing()
    test_desk_counting_meta_narration_lint()
    test_ratings_legend()
    test_discord_footer_disclaimer()
    test_cashtag_scrub_collisions_removed()
    test_routine_volume_gate()
    test_multimodal_prompt_alignment()
    print("\nALL PULSE ACCURACY BATCH SMOKE TESTS PASS")
