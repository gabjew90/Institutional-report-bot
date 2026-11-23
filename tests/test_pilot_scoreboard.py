"""The edition scoreboard after model grading was retired (2026-11-23).

It reports only what the pipeline computes in code, per edition, and no
workflow, dispatch slot or script of the graders is left behind."""
import json
import pathlib

from github_bridge import workflow_dispatch as W
from scripts import pilot_scoreboard as SB

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _tree(tmp_path, metas, ops=None):
    (tmp_path / "shadow").mkdir()
    (tmp_path / "ops").mkdir()
    for date, meta in metas.items():
        (tmp_path / "shadow" / f"{date}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for date, rec in (ops or {}).items():
        (tmp_path / "ops" / f"{date}.json").write_text(json.dumps(rec), encoding="utf-8")
    return str(tmp_path)


def _meta(failures=0, unread=0, given_up=3, cards=100, briefs=10, edge=0.4, flag=False):
    return {"unread_source_files_at_edit": unread, "given_up_at_edit": given_up,
            "structural_problems": [],
            "citations": {"card_citations": cards, "brief_citations": briefs,
                          "edge_quintile_share": edge, "metric4_flag": flag,
                          "failures": [{"sentence": "s", "cite": "c1", "reason": "r"}] * failures}}


def test_a_row_per_edition_from_the_editors_own_numbers(tmp_path):
    root = _tree(tmp_path,
                 {"2026-11-18": _meta(failures=2), "2026-11-19": _meta(unread=25, edge=0.8, flag=True)},
                 ops={"2026-11-18": {"reader_failure_rate": 0.02, "collided_with_pulse_window": False},
                      "2026-11-19": {"reader_failure_rate": 0.25, "collided_with_pulse_window": False},
                      "2026-11-21": {"reader_failure_rate": 0.0}})
    text = SB.render(root)
    assert "| 2026-11-18 | 110 | 2 | 0 | 3 | 40% | yes |  |" in text
    assert "| 2026-11-19 | 110 | 0 | 25 | 3 | 80% FLAG | no | VOID (stale cards) |" in text
    # a day the readers ran and no edition was written is not a row
    assert "2026-11-21" not in text
    assert "Last 2 editions: 2 failed sentences against 220 citations, 1 void, 1 flagged" in text


def test_the_scoreboard_no_longer_reads_grades(tmp_path):
    root = _tree(tmp_path, {"2026-11-18": _meta()})
    grades = tmp_path / "grades" / "2026-11-18"
    grades.mkdir(parents=True)
    (grades / "fidelity-shadow-a.json").write_text('{"faithful_rate": 0.1}', encoding="utf-8")
    text = SB.render(root)
    assert "m2" not in text and "faithful" not in text and "Counted days" not in text
    assert "Model grading was retired on 2026-11-23" in text


def test_a_failed_edit_shows_as_one(tmp_path):
    meta = _meta()
    meta["structural_problems"] = ["missing ## 3. BRIEFS"]
    assert "failed edit: missing ## 3. BRIEFS" in SB.render(_tree(tmp_path, {"2026-11-18": meta}))


def test_no_grader_workflow_script_or_dispatch_slot_remains():
    wf = ROOT / ".github" / "workflows"
    assert not (wf / "pilot-graders.yml").exists() and not (wf / "pilot-grader-gate.yml").exists()
    assert not any("grader" in name for name in W.PILOT_WORKFLOWS)
    for name in ("pilot_finalize_grade.py", "pilot_grader_inputs.py", "pilot_grader_gate.py",
                 "pilot_tiebreaks_needed.py"):
        assert not (ROOT / "scripts" / name).exists(), name


def test_the_editor_workflow_renders_the_scoreboard_and_cannot_be_stopped_by_it():
    text = (wf_path := ROOT / ".github" / "workflows" / "pilot-editor.yml").read_text(encoding="utf-8")
    assert "scripts/pilot_scoreboard.py" in text, wf_path
    # a scoreboard fault must never cost the day's edition its commit
    step = text[text.index("- name: Scoreboard"):]
    step = step[:step.index("- name: Commit shadow pulse")]
    assert "continue-on-error: true" in step
    assert '[ -f "$PILOT_ROOT/scoreboard.md" ] && git add "$PILOT_ROOT/scoreboard.md"' in text
