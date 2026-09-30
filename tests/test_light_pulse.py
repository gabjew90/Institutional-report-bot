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
                       encoding="utf-8", errors="replace", cwd=str(REPO), env=env)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def test_the_splitter_leaves_a_theme_less_body_alone():
    from report.pulse_sections import split_main_event_briefs
    md = _light_md()
    assert O.LIGHT_BODY_NOTE in md
    # With no ### themes the splitter must return the document untouched.
    assert split_main_event_briefs(md) == md


def test_the_formatter_renders_the_note_as_a_section_embed():
    from report.formatter import format_report_embeds
    from report.models import DailyReport
    report = DailyReport(report_date="2026-09-30", report_type="daily", pdf_count=0,
                         markdown_content=_light_md())
    embeds = format_report_embeds(report)
    titles = [e.title for e in embeds if getattr(e, "title", None)]
    assert any("RECAP" in t.upper() for t in titles)
    assert any("WATCH" in t.upper() for t in titles)
    # The body stays one INSIGHTS embed carrying exactly the note.
    insights = [e for e in embeds if "INSIGHTS" in (e.title or "").upper()]
    assert len(insights) == 1, titles
    assert (insights[0].description or "").strip() == O.LIGHT_BODY_NOTE
    assert not any("MAIN EVENT" in t.upper() or "BRIEFS" in t.upper() for t in titles), titles


def test_the_web_fragment_carries_the_note_under_the_insights_hook():
    from scripts.pulse_dashboard import render_pulse_fragment
    import html as _html
    out = render_pulse_fragment(_light_md())
    # The renderer emits a typographic apostrophe as an entity. Normalize it.
    text = _html.unescape(out).replace("’", "'")
    assert O.LIGHT_BODY_NOTE in text
    heading = text.index('<h2 class="insights"')
    body = text.index('class="insights-body"')
    note = text.index(O.LIGHT_BODY_NOTE)
    watch = text.index('<h2 class="watch"')
    assert heading < body < note < watch


def test_the_light_document_passes_the_gates(tmp_path):
    from scripts.pulse_lint import classify_issues
    md = _light_md()
    doc = tmp_path / "final.md"
    doc.write_text(md, encoding="utf-8")
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"today": "2026-09-25", "theme_map": {}}), encoding="utf-8")
    out_json = tmp_path / "v.json"
    # Exit 0 is clean and 4 is soft-only. A crash must fail with its traceback.
    code, out = _run("scripts/pulse_draft_validate.py", str(doc), str(ctx), str(out_json), "--omnipulse")
    assert code in (0, 4), out
    v = json.loads(out_json.read_text(encoding="utf-8"))
    assert v["hard_count"] == 0, [x["kind"] for x in v["violations"]]
    lint_json = tmp_path / "lint.json"
    code, out = _run("scripts/pulse_lint.py", str(doc), str(lint_json), str(ctx))
    assert code in (0, 4), out
    issues = json.loads(lint_json.read_text(encoding="utf-8"))
    hard, _soft = classify_issues(issues)
    assert hard == 0, issues


def test_the_note_obeys_the_voice_rules():
    from ai_analysis.voice_rules import compose_lint_patterns
    # Each entry is (regex, kind_label).
    for rx, kind in compose_lint_patterns():
        assert not re.search(rx, O.LIGHT_BODY_NOTE, re.I), (kind, rx)
