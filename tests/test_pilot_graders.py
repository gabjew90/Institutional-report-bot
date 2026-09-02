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


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
