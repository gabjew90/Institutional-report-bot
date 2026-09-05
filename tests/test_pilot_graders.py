"""Grader machinery (pilot piece 6 and 7, 2026-09-01): deterministic
inputs, grade validation, the separation gate, and the scoreboard."""
import json
import os
import sys
import tempfile

from scripts.pilot_finalize_grade import validate
from scripts.pilot_grader_gate import DIMS, build_inputs, grade_says_fail, judge, load_fixtures
from scripts.pilot_grader_inputs import choose_briefs, main_event, sample_sentences, sentences_of
from scripts.pilot_scoreboard import day_row, render

SHADOW = ("# Head\n\n## 2. THE MAIN EVENT\n\n### Rates do the work\n\n"
          + " ".join(f"Sentence number {i} says something about the tape with a figure of {i}bp [c{i}]." for i in range(1, 30))
          + "\n\n## 3. BRIEFS\n\n### Memory\n\nMemory pricing keeps getting revised up [d2].\n\n"
          "## _LEANS\n\n- long | $NVDA | x\n")


def test_sampling_is_deterministic_per_date_and_excludes_leans_and_headers():
    a = sample_sentences(SHADOW, "2026-09-02")
    b = sample_sentences(SHADOW, "2026-09-02")
    assert a == b and len(a) == 15
    assert all("_LEANS" not in s["text"] and not s["text"].startswith("#") for s in a)
    assert all("[c" not in s["text"] for s in a), "markers stripped for the grader"
    assert sample_sentences(SHADOW, "2026-09-03") != a


def test_sampling_is_limited_to_the_sections_both_pulses_contain():
    """Shakedown 2026-09-02: production's RECAP (live prices) and WHAT TO
    WATCH were sampled and graded unsupported. Only THE MAIN EVENT and
    BRIEFS are shared with the shadow pulse."""
    from scripts.pilot_grader_inputs import shared_sections
    prod = ("---\nx: y\n---\n\n# Head\n\n## 1. RECAP\n\nS&P futures are down 0.3% before the open this morning.\n\n"
            "## 2. THE MAIN EVENT\n\n### T\n\nThe main event sentence has enough words to count here.\n\n"
            "## 3. BRIEFS\n\n### B\n\nA brief sentence that also has enough words to be sampled.\n\n"
            "## TRADE BOARD\n\n- x\n\n## 4. WHAT TO WATCH\n\n### Today\n\nCPI at 8:30 ET is the print to watch today.\n")
    sec = shared_sections(prod)
    assert "RECAP" not in sec and "WHAT TO WATCH" not in sec and "TRADE BOARD" not in sec
    sents = sentences_of(prod)
    assert all("futures" not in s and "CPI" not in s for s in sents), sents
    assert len(sents) == 2


def test_non_material_share_counts_briefs_not_distortions():
    d = _grades()
    for ag in ("a", "b"):
        d["grades"]["brief_fidelity"][ag]["briefs"] = [
            {"id": "d1", "tier": "top", "material_count": 0, "non_material_count": 3},
            {"id": "d2", "tier": "top", "material_count": 0, "non_material_count": 0},
            {"id": "d3", "tier": "rest", "material_count": 0, "non_material_count": 0},
            {"id": "d4", "tier": "rest", "material_count": 0, "non_material_count": 0}]
    r = day_row("2026-09-02", d)
    assert r["m2a"]["non_material_share"] == 0.25


def test_main_event_extracts_only_section_two():
    me = main_event(SHADOW)
    assert me.startswith("### Rates do the work") and "Memory pricing" not in me


def test_short_document_yields_fewer_sentences_not_a_crash():
    assert len(sentences_of("# H\n\n## 2. THE MAIN EVENT\n\nOne single sentence of seven words here.\n")) == 1
    assert sentences_of("# H\n\n## 2. THE MAIN EVENT\n\nToo short.\n") == []


def test_choose_briefs_prefers_main_event_cites_and_covers_every_tier():
    pack = {"docs": {f"d{i}": {"tier": "top" if i < 4 else "rest", "brief": "x"} for i in range(1, 8)}}
    picked = choose_briefs(pack, ["d2", "d3"], "2026-09-02", k=4)
    ids = [b["id"] for b in picked]
    assert ids[:2] == ["d2", "d3"]
    assert {b["tier"] for b in picked} == {"top", "rest"}
    assert 3 <= len(picked) <= 5


def test_validate_catches_count_mismatch_and_computes_grouping_pass():
    doc = {"sentences": [{"id": "s1"}, {"id": "s2"}], "faithful": 1, "distorted": 0, "unsupported": 0}
    assert "counts do not match the sentence list" in validate("fidelity", doc)
    g = {"fragmented_mass_share": 0.05, "mis_merges": [{"would_change_theme_selection": False}]}
    assert validate("grouping", g) == [] and g["pass"] is True
    g2 = {"fragmented_mass_share": 0.05, "mis_merges": [{"would_change_theme_selection": True}]}
    validate("grouping", g2)
    assert g2["pass"] is False


def test_gate_fixture_inputs_build_for_every_dimension():
    fx = load_fixtures()
    assert fx["brief_bad"] != fx["brief_clean"]
    with tempfile.TemporaryDirectory() as td:
        build_inputs(td)
        for dim in DIMS:
            for case in ("bad", "clean"):
                assert os.path.exists(os.path.join(td, f"{dim}-{case}.md")), (dim, case)


def _write(td, name, doc):
    with open(os.path.join(td, name + ".json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def test_gate_separates_only_when_bad_fails_and_clean_passes():
    assert grade_says_fail("brief_fidelity", {"material_total": 1}) is True
    assert grade_says_fail("brief_fidelity", {"material_total": 0}) is False
    assert grade_says_fail("mechanism", {"preserved": True}) is False
    assert grade_says_fail("fidelity", {"failed": True}) is None
    with tempfile.TemporaryDirectory() as td:
        _write(td, "grouping-bad", {"fragmented_mass_share": 0.5, "mis_merges": []})
        _write(td, "grouping-clean", {"fragmented_mass_share": 0.0, "mis_merges": []})
        _write(td, "fidelity-bad", {"unsupported": 1, "distorted": 1})
        _write(td, "fidelity-clean", {"unsupported": 0, "distorted": 0})
        _write(td, "brief_fidelity-bad", {"material_total": 0})   # TOO WEAK
        _write(td, "brief_fidelity-clean", {"material_total": 0})
        _write(td, "mechanism-bad", {"preserved": False})
        _write(td, "mechanism-clean", {"preserved": False})       # TOO HARSH
        v = judge(td)
    assert v["dimensions"]["grouping"]["status"] == "separates"
    assert v["dimensions"]["fidelity"]["status"] == "separates"
    assert v["dimensions"]["brief_fidelity"]["status"].startswith("TOO WEAK")
    assert v["dimensions"]["mechanism"]["status"].startswith("TOO HARSH")
    assert v["all_separate"] is False


def _grades(shadow_rate=0.9, prod_rate=0.8, unsup=0, material=0, preserved=(True, True), b_rate=None):
    g = {
        "grouping": {"a": {"fragmented_mass_share": 0.05, "mis_merges": []},
                     "b": {"fragmented_mass_share": 0.06, "mis_merges": []}},
        "fidelity-shadow": {"a": {"faithful_rate": shadow_rate, "unsupported": unsup},
                            "b": {"faithful_rate": b_rate if b_rate is not None else shadow_rate, "unsupported": unsup}},
        "fidelity-production": {"a": {"faithful_rate": prod_rate, "unsupported": 0},
                                "b": {"faithful_rate": prod_rate, "unsupported": 0}},
        "brief_fidelity": {"a": {"material_total": material, "briefs": [{"tier": "top", "material_count": material, "non_material_count": 1}]},
                           "b": {"material_total": material, "briefs": [{"tier": "rest", "material_count": material, "non_material_count": 0}]}},
        "mechanism-shadow": {"a": {"preserved": preserved[0]}, "b": {"preserved": preserved[0]}},
        "mechanism-production": {"a": {"preserved": preserved[1]}, "b": {"preserved": preserved[1]}},
    }
    return {"grades": g, "shadow_meta": {"citations": {"edge_quintile_share": 0.3, "metric4_flag": False, "failures": []},
                                         "unread_source_files_at_edit": 2},
            "ops": {"reader_failure_rate": 0.0, "collided_with_pulse_window": False}}


def test_day_row_passes_and_flags_disagreement():
    r = day_row("2026-09-02", _grades())
    assert r["m1"]["pass"] and r["m2_pass"] and r["m2a"]["pass"] and r["m5"]["pass"]
    assert r["m2a"]["tiers"]["top"]["audited"] == 1 and r["m2a"]["tiers"]["rest"]["audited"] == 1
    r2 = day_row("2026-09-02", _grades(shadow_rate=0.9, b_rate=0.4))
    assert "m2_shadow" in r2["disagreements"]


def test_source_files_are_restricted_to_the_window():
    from scripts.pilot_grader_inputs import source_files
    with tempfile.TemporaryDirectory() as td:
        for d in ("2026-08-20", "2026-09-01", "2026-09-02"):
            os.makedirs(os.path.join(td, d))
            with open(os.path.join(td, d, "1__x.txt"), "w") as fh:
                fh.write("x")
        got = source_files(td, "2026-09-02", 1)
    assert [os.path.basename(os.path.dirname(p)) for p in got] == ["2026-09-01", "2026-09-02"]


def test_choose_briefs_returns_nothing_when_no_brief_has_text():
    assert choose_briefs({"docs": {"d1": {"tier": "top", "brief": ""}}}, [], "2026-09-02") == []


def test_2a_counts_each_brief_once_across_both_agents():
    d = _grades()
    for ag in ("a", "b"):
        d["grades"]["brief_fidelity"][ag]["briefs"] = [
            {"id": "d1", "tier": "top", "material_count": 0, "non_material_count": 1},
            {"id": "d2", "tier": "rest", "material_count": 0, "non_material_count": 0}]
    r = day_row("2026-09-02", d)
    assert r["m2a"]["tiers"]["top"]["audited"] == 1 and r["m2a"]["tiers"]["rest"]["audited"] == 1
    assert r["m2a"]["non_material_share"] == 0.5


def test_owner_tiebreak_replaces_both_agents():
    d = _grades(shadow_rate=0.9, b_rate=0.4)
    assert "m2_shadow" in day_row("2026-09-02", d)["disagreements"]
    d["grades"]["fidelity-shadow"]["tiebreak"] = {"faithful_rate": 0.9, "unsupported": 0}
    r = day_row("2026-09-02", d)
    assert r["disagreements"] == [] and r["m2_shadow"]["rate"] == 0.9


def test_tiebreak_stems_name_disagreements_and_half_missing_dims():
    """2026-09-05: seven disagreements sat unresolved over three shakedown
    days. The graders workflow asks this for the stems that need a third
    agent; it must use the scoreboard's own agreement rule."""
    from scripts.pilot_scoreboard import tiebreak_stems
    d = _grades(shadow_rate=0.9, b_rate=0.4)
    assert tiebreak_stems(d["grades"]) == ["fidelity-shadow"]
    # one usable grader and one failed grade: a third settles it
    d["grades"]["mechanism-production"]["b"] = {"failed": True}
    assert tiebreak_stems(d["grades"]) == ["fidelity-shadow", "mechanism-production"]
    # both failed: nothing a third grader can settle
    d["grades"]["mechanism-production"]["a"] = {"failed": True}
    assert tiebreak_stems(d["grades"]) == ["fidelity-shadow"]
    # a recorded tiebreak clears the stem
    d["grades"]["fidelity-shadow"]["tiebreak"] = {"faithful_rate": 0.9, "unsupported": 0}
    assert tiebreak_stems(d["grades"]) == []
    # grouping: same share but only one grader sees a theme-changing merge
    g = _grades()
    g["grades"]["grouping"]["b"]["mis_merges"] = [{"label": "x", "would_change_theme_selection": True}]
    assert tiebreak_stems(g["grades"]) == ["grouping"]


def test_owner_grade_beats_the_agent_tiebreak_and_unusable_tiebreaks_are_ignored():
    d = _grades(shadow_rate=0.9, b_rate=0.4)
    d["grades"]["fidelity-shadow"]["tiebreak"] = {"faithful_rate": 0.4, "unsupported": 0}
    d["grades"]["fidelity-shadow"]["owner"] = {"faithful_rate": 0.9, "unsupported": 0}
    assert day_row("2026-09-02", d)["m2_shadow"]["rate"] == 0.9
    d2 = _grades(shadow_rate=0.9, b_rate=0.4)
    d2["grades"]["fidelity-shadow"]["tiebreak"] = {"failed": True}
    assert "m2_shadow" in day_row("2026-09-02", d2)["disagreements"]


def test_scoreboard_counts_only_days_after_day1():
    with tempfile.TemporaryDirectory() as td:
        for date, ok in (("2026-09-02", True), ("2026-09-03", True), ("2026-09-04", False)):
            gd = os.path.join(td, "grades", date)
            os.makedirs(gd)
            g = _grades(material=0 if ok else 2)["grades"]
            for dim, agents in g.items():
                for ag, doc in agents.items():
                    _write(gd, f"{dim}-{ag}", doc)
        os.makedirs(os.path.join(td, "shadow"))
        with open(os.path.join(td, "DAY1"), "w", encoding="utf-8") as fh:
            fh.write("2026-09-04\n")
        text = render(td)
    assert "Counted days: 1 of 10" in text
    assert "| 2026-09-02 | shakedown |" in text
    assert "| 2026-09-04 | yes |" in text
    assert "metric 2a brief fidelity: FAIL so far" in text, text
    assert "Scope limit" in text



# 2026-09-03: the editor packed the previous day's cards while 25 source
# files sat unread, and the day scored 70% shadow fidelity — a number
# that measured the staleness, not the writer. Such a day is void.
def test_a_day_with_unread_sources_at_edit_is_void():
    d = _grades()                       # fixture carries unread_source_files_at_edit = 2
    assert day_row("2026-09-02", d)["void"] is True
    d["shadow_meta"]["unread_source_files_at_edit"] = 0
    assert day_row("2026-09-02", d)["void"] is False
    # missing meta is not a void claim either way
    d["shadow_meta"] = {}
    assert day_row("2026-09-02", d)["void"] is False


def test_render_marks_void_days_and_drops_them_from_the_verdict(tmp_path=None):
    import json as _json
    import os
    import tempfile
    root = tempfile.mkdtemp()

    def _write(day, unread):
        gd = os.path.join(root, "grades", day)
        os.makedirs(gd, exist_ok=True)
        g = _grades()
        for dim, per in g["grades"].items():
            for ag, payload in per.items():
                with open(os.path.join(gd, f"{dim}-{ag}.json"), "w", encoding="utf-8") as fh:
                    _json.dump(payload, fh)
        sd = os.path.join(root, "shadow")
        os.makedirs(sd, exist_ok=True)
        meta = dict(g["shadow_meta"]); meta["unread_source_files_at_edit"] = unread
        with open(os.path.join(sd, f"{day}.meta.json"), "w", encoding="utf-8") as fh:
            _json.dump(meta, fh)

    _write("2026-09-02", 0)
    _write("2026-09-03", 25)
    with open(os.path.join(root, "DAY1"), "w", encoding="utf-8") as fh:
        fh.write("2026-09-02\n")
    text = render(root)
    assert "VOID (stale cards)" in text
    assert "**Void days (1):** 2026-09-03" in text
    # the clean day still counts; the void day does not
    assert "Counted days: 1 of 10" in text


# 2026-09-03 review: an unusable grade was scored as if it were real.
def test_an_unusable_grade_counts_as_a_missing_agent():
    from scripts.pilot_scoreboard import usable
    assert usable({"faithful_rate": 0.9}) is not None
    assert usable({"failed": True, "faithful_rate": 0.0}) is None
    assert usable(None) is None and usable("nope") is None
    d = _grades()
    d["grades"]["fidelity-shadow"]["a"] = {"failed": True, "faithful_rate": 0.0}
    r = day_row("2026-09-02", d)
    assert r["m2_shadow"]["rate"] is None and "unusable" in r["m2_shadow"]["note"]
    assert r["m2_pass"] is False


def test_fidelity_agreement_tolerance_is_tight():
    # 0.90 vs 0.77 is a disagreement for the owner, not a 0.835 average.
    d = _grades(shadow_rate=0.90, b_rate=0.77)
    r = day_row("2026-09-02", d)
    assert r["m2_shadow"]["rate"] is None, r["m2_shadow"]
    assert "m2_shadow" in r["disagreements"]
    # a real agreement still averages
    r2 = day_row("2026-09-02", _grades(shadow_rate=0.90, b_rate=0.88))
    assert r2["m2_shadow"]["rate"] == 0.89


def test_an_empty_sentence_list_is_a_failed_grade_not_a_zero_rate():
    import json as _json
    import os
    import subprocess
    import sys as _sys
    import tempfile
    d = tempfile.mkdtemp()
    raw = os.path.join(d, "raw.txt"); out = os.path.join(d, "g.json")
    with open(raw, "w", encoding="utf-8") as fh:
        _json.dump({"sentences": [], "faithful": 0, "distorted": 0,
                    "unsupported": 0, "artifact": "shadow"}, fh)
    r = subprocess.run(
        [_sys.executable, "scripts/pilot_finalize_grade.py", "--raw", raw, "--out", out,
         "--dim", "fidelity", "--agent", "a", "--model", "m",
         "--prompt", "docs/superpowers/routines/pilot/graders/fidelity.md"],
        capture_output=True, text=True)
    doc = _json.load(open(out, encoding="utf-8"))
    assert doc["failed"] is True, doc
    assert any("empty sentence list" in p for p in doc["problems"]), doc["problems"]
    assert "faithful_rate" not in doc, "an empty grade must not carry a 0.0 rate"
    assert r.returncode == 1

if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
