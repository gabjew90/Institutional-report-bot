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
    cut = final.replace("### Bitcoin cleared the miners' cost line", "### Bitcoin")
    (tmp_path / "final.md").write_text(cut, encoding="utf-8")
    d.preflight()
    assert "### Bitcoin cleared the miners' cost line" in         (tmp_path / "final.md").read_text(encoding="utf-8")
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
