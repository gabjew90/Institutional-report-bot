"""2026-10-12 pulse fixes. The validator names an economic event only as a
whole word ("Hormuz shipping" read as PPI and "SpaceX" as PCE, failing the
Omnipulse draft twice), and STEP 6 pushes every artifact in one list."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.pulse_draft_validate import _names_event  # noqa: E402


def test_event_names_are_whole_words():
    assert not _names_event("HORMUZ SHIPPING FLOWS", "PPI")
    assert not _names_event("SPACEX RAISED $40BN", "PCE")
    assert not _names_event("THE MECHANISM IS PESSIMISM", "ISM")
    assert _names_event("SEPTEMBER PPI ROSE 0.1%", "PPI")
    assert _names_event("CORE PCE, 3.0% Y/Y", "PCE")
    assert _names_event("ISM SERVICES 54.5", "ISM")
    assert _names_event("RETAIL SALES ROSE", "RETAIL SALES")


def test_push_list_carries_every_artifact_present(tmp_path, monkeypatch):
    """2026-10-12: the routine pushed only the pulse; the driver trail,
    lint report and adversarial verdict never reached the branch."""
    import json
    from scripts import pulse_driver as PD
    files = {"pulse_ts.txt": "2026-10-12T14-08-26Z", "final_with_frontmatter.md": "# pulse",
             "adjudication.json": "{}", "driver_state.json": '{"gates": []}',
             "lint_report.json": "[]", "adversarial_verdict.json": '{"findings": []}'}
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    monkeypatch.setattr(PD, "Path", lambda p: tmp_path / str(p).replace("/tmp/", ""))
    out = tmp_path / "push.json"
    assert PD.push_list(str(out)) == 0
    paths = [f["path"] for f in json.loads(out.read_text(encoding="utf-8"))]
    assert "pulse-output/pending/2026-10-12T14-08-26Z.md" in paths
    assert "pulse-output/driver/2026-10-12T14-08-26Z.json" in paths
    assert "pulse-output/adversarial/2026-10-12T14-08-26Z.json" in paths
    (tmp_path / "final_with_frontmatter.md").unlink()
    assert PD.push_list(str(out)) == 3          # no pulse, no push
