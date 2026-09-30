"""The light pulse (spec 2026-09-29) through every consumer that reads
the INSIGHTS section. Built from the real 9/25 DRAFT with the note as
its body."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from scripts import omnipulse_body as O

FX = Path(os.path.dirname(__file__)) / "fixtures" / "omnipulse"
REPO = Path(__file__).resolve().parents[1]


def _light_md() -> str:
    draft = (FX / "draft-2026-09-25.md").read_text(encoding="utf-8")
    return O.splice(draft, "", O.light_body())


def _run(*args):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    p = subprocess.run([sys.executable, *args], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(REPO), env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_the_splitter_leaves_a_theme_less_body_alone():
    from report.pulse_sections import split_main_event_briefs
    out = split_main_event_briefs(_light_md())
    assert "THE MAIN EVENT" not in out and "## 3. BRIEFS" not in out
    assert "## 2. INSIGHTS & ALPHA" in out and O.LIGHT_BODY_NOTE in out


def test_the_formatter_renders_the_note_as_a_section_embed():
    from report.formatter import format_report_embeds
    from report.models import DailyReport
    report = DailyReport(report_date="2026-09-30", report_type="daily", pdf_count=0,
                         markdown_content=_light_md())
    embeds = format_report_embeds(report)
    titles = [e.title for e in embeds if getattr(e, "title", None)]
    assert any("RECAP" in t.upper() for t in titles)
    assert any("WATCH" in t.upper() for t in titles)
    assert any(O.LIGHT_BODY_NOTE[:40] in (e.description or "") for e in embeds)


def test_the_web_fragment_carries_the_note_under_the_insights_hook():
    from scripts.pulse_dashboard import render_pulse_fragment
    html = render_pulse_fragment(_light_md())
    # The renderer turns the straight apostrophe into a typographic one.
    note = O.LIGHT_BODY_NOTE.replace("'", "&rsquo;")
    assert note in html and 'class="insights"' in html and 'class="insights-body"' in html


def test_the_light_document_passes_the_gates(tmp_path):
    from scripts.pulse_lint import SOFT_ISSUE_KINDS
    md = _light_md()
    doc = tmp_path / "final.md"
    doc.write_text(md, encoding="utf-8")
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"today": "2026-09-25", "theme_map": {}}), encoding="utf-8")
    out_json = tmp_path / "v.json"
    _run("scripts/pulse_draft_validate.py", str(doc), str(ctx), str(out_json), "--omnipulse")
    v = json.loads(out_json.read_text(encoding="utf-8"))
    assert v["hard_count"] == 0, [x["kind"] for x in v["violations"]]
    lint_json = tmp_path / "lint.json"
    _run("scripts/pulse_lint.py", str(doc), str(lint_json), str(ctx))
    issues = json.loads(lint_json.read_text(encoding="utf-8"))
    # Lint marks hardness by kind: anything outside SOFT_ISSUE_KINDS is hard.
    hard = [i for i in issues if i.get("kind") not in SOFT_ISSUE_KINDS]
    assert not hard, hard


def test_the_note_obeys_the_voice_rules():
    from ai_analysis.voice_rules import compose_lint_patterns
    # Each entry is (regex, kind_label).
    for rx, kind in compose_lint_patterns():
        assert not re.search(rx, O.LIGHT_BODY_NOTE, re.I), (kind, rx)
