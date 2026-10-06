"""Pulse driver in omnipulse mode (spec 2026-09-26).

Uses the real 9/25 production DRAFT and the real 9/24 Omnipulse.
"""
import json
import os
from pathlib import Path
from unittest.mock import patch

from scripts import omnipulse_body as O
from scripts import pulse_driver as PD

FX = Path(os.path.dirname(__file__)) / "fixtures" / "omnipulse"


def _driver(tmp: Path) -> PD.Driver:
    (tmp / "ctx.json").write_text(json.dumps({"today": "2026-09-25", "theme_map": {}}),
                                  encoding="utf-8")
    (tmp / "draft.md").write_text((FX / "draft-2026-09-25.md").read_text(encoding="utf-8"),
                                  encoding="utf-8")
    return PD.Driver(tmp)


def _fake_fetch(driver, tmp):
    """_run that serves the 9/24 Omnipulse for the fetch step and runs
    every other script for real."""
    real = PD.Driver._run

    def run(self, args):
        if args[0].endswith("omnipulse_body.py") and args[1] == "fetch":
            md = (FX / "2026-09-24.clean.md").read_text(encoding="utf-8")
            head, body = O.to_insights(md)
            (tmp / "omnipulse_body.md").write_text(body, encoding="utf-8")
            (tmp / "omnipulse_headline.txt").write_text(head, encoding="utf-8")
            return 0, "omnipulse: 2026-09-24 ok, 9 themes"
        return real(self, args)
    return patch.object(PD.Driver, "_run", run)


def test_switch_off_is_the_classic_pulse(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", False):
        assert d.gate_omnipulse("2026-09-24") == "CLASSIC"
    assert not d.omnipulse_mode()


def test_no_usable_omnipulse_falls_back(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), \
         patch.object(PD.Driver, "_run", lambda self, a: (3, "none after 1200s")):
        assert d.gate_omnipulse("2026-09-24") == "CLASSIC"
    assert not d.omnipulse_mode()


def test_the_body_replaces_drafts_insights_and_survives_edit(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_fetch(d, tmp_path):
        assert d.gate_omnipulse("2026-09-24") == "OMNIPULSE"
        d.gate_draft_validate()
    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    assert draft.startswith("# The Five Percent Problem")
    assert "The bond market repriced growth, not inflation" in draft
    assert "The bond market's break higher isn't finished" not in draft
    assert "## 1. RECAP" in draft and "## _LEANS" in draft
    # EDIT rewrites a body paragraph; the lint gate puts the body back
    edited = draft.replace("Bonds sold off hard.", "Bonds crashed.")
    (tmp_path / "final.md").write_text(edited, encoding="utf-8")
    d.gate_lint()
    assert "Bonds sold off hard." in (tmp_path / "final.md").read_text(encoding="utf-8")


def test_the_theme_choice_checks_do_not_apply(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_fetch(d, tmp_path):
        d.gate_omnipulse("2026-09-24")
        d.gate_draft_validate()
    v = json.loads((tmp_path / "draft_validation.json").read_text(encoding="utf-8"))
    kinds = {x["kind"] for x in v["violations"]}
    from scripts.pulse_draft_validate import OMNIPULSE_EXEMPT_KINDS
    assert not kinds & OMNIPULSE_EXEMPT_KINDS


def test_the_fact_check_covers_recap_and_watch_not_the_body(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_fetch(d, tmp_path):
        d.gate_omnipulse("2026-09-24")
        d.gate_draft_validate()
    final = (tmp_path / "draft.md").read_text(encoding="utf-8")
    (tmp_path / "final.md").write_text(final, encoding="utf-8")
    body_quote = "Bonds sold off hard."
    recap_line = next(l for l in final.split("## 1. RECAP", 1)[1].splitlines() if len(l) > 40)
    verdict = {"findings": [
        {"kind": "unsupported-figure", "quote": body_quote, "why": "x", "fix": "y"}]}
    (tmp_path / "adversarial_verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    assert d.gate_adversarial() == "CONTINUE"
    assert (tmp_path / "adversarial_omnipulse_body.json").exists()
    verdict["findings"].append({"kind": "unsupported-figure", "quote": recap_line[:60],
                                "why": "x", "fix": "y"})
    (tmp_path / "adversarial_verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    d2 = PD.Driver(tmp_path)
    assert d2.gate_adversarial(recheck=True) in ("DISPATCH_REPAIR",)


def test_preflight_restores_a_broken_body_instead_of_blocking(tmp_path):
    """A blocked preflight means no pulse at all, so a theme a late pass
    broke is restored from the saved body."""
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_fetch(d, tmp_path):
        d.gate_omnipulse("2026-09-24")
        d.gate_draft_validate()
    final = (tmp_path / "draft.md").read_text(encoding="utf-8")
    cut = final.replace("### Oil is a policy problem now, not just a price", "### Oil")
    (tmp_path / "final.md").write_text(cut, encoding="utf-8")
    d.preflight()
    assert "### Oil is a policy problem now, not just a price" in (
        tmp_path / "final.md").read_text(encoding="utf-8")
    assert any(h.get("record") == "omnipulse_body_restored" for h in d.state["history"])


def test_preflight_blocks_when_the_saved_body_is_gone(tmp_path, capsys):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_fetch(d, tmp_path):
        d.gate_omnipulse("2026-09-24")
        d.gate_draft_validate()
    (tmp_path / "final.md").write_text((tmp_path / "draft.md").read_text(encoding="utf-8"),
                                       encoding="utf-8")
    (tmp_path / "omnipulse_body.md").unlink()
    assert d.preflight() == "BLOCK"
    assert "omnipulse body incomplete" in capsys.readouterr().out


def test_the_gate_passes_the_backup_directory_through(tmp_path):
    d = _driver(tmp_path)
    seen = {}

    def run(self, args):
        seen["args"] = args
        return 3, "none"
    with patch.object(O, "ENABLED", True), patch.object(PD.Driver, "_run", run):
        d.gate_omnipulse("2026-09-28", src_dir="/tmp/omnipulse_src")
    assert seen["args"][-2:] == ["--from", "/tmp/omnipulse_src"]


def _fake_light(tmp):
    """_run that reports a miss day under MISS_DAY='light'."""
    real = PD.Driver._run

    def run(self, args):
        if args[0].endswith("omnipulse_body.py") and args[1] == "fetch":
            (tmp / "omnipulse_body.md").write_text(O.light_body(), encoding="utf-8")
            (tmp / "omnipulse_headline.txt").write_text("", encoding="utf-8")
            # _run returns stdout + stderr, so the stderr retry line trails
            # the decision line and the last line is not the decision.
            return O.EXIT_LIGHT, ("omnipulse: none for 2026-09-30 (not published after 600s) -> light pulse\n"
                                  "omnipulse: fetch error (HTTP 500)\n")
        return real(self, args)
    return patch.object(PD.Driver, "_run", run)


def test_a_light_day_runs_the_omnipulse_path_around_the_note(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_light(tmp_path):
        assert d.gate_omnipulse("2026-09-30") == "LIGHT"
        assert d.omnipulse_mode()
        assert (tmp_path / "pulse_mode.txt").read_text(encoding="utf-8") == "light"
        assert (tmp_path / "light_reason.txt").read_text(encoding="utf-8") == "not published after 600s"
        d.gate_draft_validate()
    draft = (tmp_path / "draft.md").read_text(encoding="utf-8")
    fixture_h1 = (FX / "draft-2026-09-25.md").read_text(encoding="utf-8").splitlines()[0]
    assert draft.startswith(fixture_h1)                # DRAFT's own headline kept
    assert O.LIGHT_BODY_NOTE in draft
    assert "The bond market's break higher isn't finished" not in draft
    assert "## 1. RECAP" in draft and "## 3. WHAT TO WATCH" in draft and "## _LEANS" in draft


def test_preflight_accepts_a_light_body_and_restores_it_when_cut(tmp_path, capsys):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_light(tmp_path):
        d.gate_omnipulse("2026-09-30")
        d.gate_draft_validate()
    final = (tmp_path / "draft.md").read_text(encoding="utf-8")
    (tmp_path / "final.md").write_text(final, encoding="utf-8")
    # Check stdout, not the return value: preflight also blocks on gates
    # this test never consults, so its verdict says nothing about the body.
    d.preflight()
    assert "omnipulse body incomplete" not in capsys.readouterr().out
    cut = final.replace(O.LIGHT_BODY_NOTE, "Nothing here.")
    (tmp_path / "final.md").write_text(cut, encoding="utf-8")
    d.preflight()
    assert "omnipulse body incomplete" not in capsys.readouterr().out
    assert O.LIGHT_BODY_NOTE in (tmp_path / "final.md").read_text(encoding="utf-8")
    assert any(h.get("record") == "omnipulse_body_restored" for h in d.state["history"])


def test_body_markers_cover_a_heading_less_body(tmp_path):
    d = _driver(tmp_path)
    (tmp_path / "omnipulse_body.md").write_text(O.light_body(), encoding="utf-8")
    assert d._body_markers() == [O.LIGHT_BODY_NOTE]
    (tmp_path / "omnipulse_body.md").write_text(O.INSIGHTS_HEADER + "\n", encoding="utf-8")
    assert d._body_markers() == []


def test_a_stale_light_reason_is_cleared_by_a_later_run(tmp_path):
    d = _driver(tmp_path)
    (tmp_path / "light_reason.txt").write_text("old", encoding="utf-8")
    with patch.object(O, "ENABLED", True), \
         patch.object(PD.Driver, "_run", lambda self, a: (3, "none after 600s")):
        assert d.gate_omnipulse("2026-10-01") == "CLASSIC"
    assert not (tmp_path / "light_reason.txt").exists()


def test_the_env_override_still_forces_classic_on_a_light_day(tmp_path, monkeypatch):
    d = _driver(tmp_path)
    monkeypatch.setenv("OMNIPULSE_BODY", "off")
    with patch.object(O, "ENABLED", True), patch.object(O, "MISS_DAY", "light"), _fake_light(tmp_path):
        assert d.gate_omnipulse("2026-09-30") == "CLASSIC"
    assert not d.omnipulse_mode()


# --- owner option C, 2026-10-06: narrow repairs to the locked body ------
def _omni_day(tmp_path):
    d = _driver(tmp_path)
    with patch.object(O, "ENABLED", True), _fake_fetch(d, tmp_path):
        d.gate_omnipulse("2026-09-24")
        d.gate_draft_validate()
    (tmp_path / "final.md").write_text((tmp_path / "draft.md").read_text(encoding="utf-8"),
                                       encoding="utf-8")
    return d


def test_a_fabricated_claim_in_the_body_is_cut_everywhere(tmp_path):
    d = _omni_day(tmp_path)
    body = (tmp_path / "omnipulse_body.md").read_text(encoding="utf-8")
    line = next(l for l in body.splitlines() if l.strip() and not l.startswith("#") and ". " in l)
    target = line.split(". ")[0] + "."
    verdict = {"findings": [
        {"kind": "fabricated-event", "quote": target, "why": "x", "fix": "y"},
        {"kind": "overstated-claim", "quote": line.split(". ")[1][:50], "why": "x", "fix": "y"}]}
    (tmp_path / "adversarial_verdict.json").write_text(json.dumps(verdict), encoding="utf-8")
    assert d.gate_adversarial() == "CONTINUE"
    assert target not in (tmp_path / "final.md").read_text(encoding="utf-8")
    assert target not in (tmp_path / "omnipulse_body.md").read_text(encoding="utf-8")
    rec = json.loads((tmp_path / "adversarial_omnipulse_body.json").read_text(encoding="utf-8"))
    assert [f["cut"] for f in rec] == [True, False]
    assert any(h.get("record") == "omnipulse_body_cut" for h in d.state["history"])


def test_a_bank_says_opener_in_the_body_is_fixed_before_lint(tmp_path):
    d = _omni_day(tmp_path)
    bp = tmp_path / "omnipulse_body.md"
    body = bp.read_text(encoding="utf-8")
    line = next(l for l in body.splitlines() if l.strip() and not l.startswith("#"))
    bp.write_text(body.replace(line, line + " Goldman says the move has further to run.", 1),
                  encoding="utf-8")
    d.gate_lint()
    final = (tmp_path / "final.md").read_text(encoding="utf-8")
    assert "Goldman says" not in final
    assert "In Goldman's view, the move has further to run." in final
    assert any(h.get("record") == "omnipulse_body_voice" for h in d.state["history"])
