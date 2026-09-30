# Light Pulse on an Omnipulse Miss Day Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the Omnipulse is missing or unusable and `MISS_DAY == "light"`, the routine publishes RECAP, TRADE BOARD and WHAT TO WATCH around a one-paragraph note instead of writing the classic pulse.

**Architecture:** The light pulse rides the existing Omnipulse path. `scripts/omnipulse_body.py fetch` writes the note as the locked body (exit 4) and the driver's gate turns that into `DECISION: LIGHT`, writing `pulse_mode.txt = light`, which every `omnipulse_mode()` branch already honors. Downstream consumers already tolerate a body with no `###` themes; this plan proves that with tests and fixes the one place that does not (preflight's heading check). The bridge pages ops when it posts a `body_source: light` pulse.

**Tech Stack:** Python 3.12, pytest, the repo's `scripts/pulse_driver.py` gate CLI, `tests/fixtures/omnipulse/draft-2026-09-25.md` as the DRAFT fixture.

Spec: `docs/superpowers/specs/2026-09-29-light-pulse-miss-day-design.md`.

Run every test command from the repo root with `py -3.12`. Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. Write scripts and commit messages with the Write tool, never a bash heredoc (heredocs in this environment turn `\b` into a backspace byte and `\n` into a newline).

---

## File map

- Modify `scripts/omnipulse_body.py`: `MISS_DAY`, `LIGHT_BODY_NOTE`, `light_body()`, `splice()` keeps the pulse's H1 when the headline is empty, `main()` exit 4 on a light day.
- Modify `scripts/pulse_driver.py`: `omnipulse_mode()` accepts `light`, `gate_omnipulse` maps exit 4 to `LIGHT` and writes `light_reason.txt`, preflight checks the note when the body has no headings.
- Modify `github_bridge/jobs.py`: `_light_day_ping(meta, name)` called after the post, next to the residuals page.
- Modify `docs/superpowers/routines/synthesis-routine.md`: STEP 2.4 `DECISION: LIGHT`, DRAFT block light variant, STEP 6 `light_reason` frontmatter.
- Modify `CLAUDE.md` (Shadow pilot section) and `NOTES.md`.
- Tests: `tests/test_omnipulse_body.py`, `tests/test_omnipulse_driver.py`, new `tests/test_light_pulse.py`.

---

### Task 1: The light body in `omnipulse_body.py`

**Files:**
- Modify: `scripts/omnipulse_body.py`
- Test: `tests/test_omnipulse_body.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_omnipulse_body.py`:

```python
# --- 2026-09-29: light pulse on a miss day (owner option a) -------------
def test_the_light_body_is_one_note_under_the_insights_header():
    body = O.light_body()
    assert body.startswith(O.INSIGHTS_HEADER + "\n\n")
    assert O.LIGHT_BODY_NOTE in body
    assert "### " not in body and "—" not in body and ";" not in body


def test_splice_keeps_the_pulses_own_headline_when_none_is_supplied():
    draft = _read("draft-2026-09-25.md")
    out = O.splice(draft, "", O.light_body())
    assert out.splitlines()[0] == draft.splitlines()[0]
    assert O.LIGHT_BODY_NOTE in out and "### " not in out.split("## 3. WHAT TO WATCH")[0].split("## 2.")[1]


def test_a_miss_day_writes_the_light_body_when_the_switch_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(O, "_raw_get", lambda url, token: None)
    body, head = tmp_path / "b.md", tmp_path / "h.txt"
    args = ["fetch", "--date", "2026-09-30", "--wait", "0",
            "--body", str(body), "--headline", str(head)]
    monkeypatch.setattr(O, "MISS_DAY", "classic")
    assert O.main(args) == 3 and not body.exists()
    monkeypatch.setattr(O, "MISS_DAY", "light")
    assert O.main(args) == 4
    assert body.read_text(encoding="utf-8") == O.light_body()
    assert head.read_text(encoding="utf-8") == ""
    assert "light pulse" in capsys.readouterr().out and "not published" in O.LAST_FETCH_REASON


def test_an_unusable_omnipulse_also_goes_light(tmp_path, monkeypatch):
    md, meta = _omni("2026-09-24")
    monkeypatch.setattr(O, "fetch", lambda *a, **k: (md, dict(meta, structural_problems=["x"])))
    monkeypatch.setattr(O, "MISS_DAY", "light")
    body, head = tmp_path / "b.md", tmp_path / "h.txt"
    assert O.main(["fetch", "--date", "2026-09-24", "--wait", "0",
                   "--body", str(body), "--headline", str(head)]) == 4
    assert body.read_text(encoding="utf-8") == O.light_body()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `py -3.12 -m pytest -q tests/test_omnipulse_body.py -k "light or own_headline"`
Expected: 4 failed (`AttributeError: ... has no attribute 'light_body'` / `MISS_DAY`).

- [ ] **Step 3: Implement**

In `scripts/omnipulse_body.py`, after `MAX_BRIEFS = 5`, add:

```python
# What a miss day publishes when the Omnipulse is missing or unusable
# after the wait (owner option a, 2026-09-29, spec
# 2026-09-29-light-pulse-miss-day-design.md). "classic" = today's
# classic pulse. "light" = RECAP, TRADE BOARD and WHAT TO WATCH around
# LIGHT_BODY_NOTE, no research body; flipped in the retirement commit.
MISS_DAY = "classic"
LIGHT_BODY_NOTE = (
    "No research body today. The morning's bank research was not ready "
    "in time. The market read above and the calendar below are "
    "current, and the full edition returns tomorrow."
)
EXIT_LIGHT = 4
```

Add after `to_insights`:

```python
def light_body() -> str:
    """The INSIGHTS section for a light pulse: the header and the note,
    no themes."""
    return INSIGHTS_HEADER + "\n\n" + LIGHT_BODY_NOTE + "\n"
```

In `splice`, replace the headline substitution:

```python
    out = pulse_md[:m.start()] + insights.rstrip() + "\n\n" + pulse_md[end:]
    if not headline.strip():
        return out          # light pulse: DRAFT's own H1 stands
    out, n = re.subn(r"(?m)^# (?!#).*$", lambda _m: headline, out, count=1)
    if not n:
        out = headline + "\n\n" + out
    return out
```

In `main()`, replace the two `return 3` branches of the fetch command with a shared exit:

```python
    if a.cmd == "fetch":
        if a.src_dir:
            got = read_local(a.src_dir, a.date, a.sha)
        else:
            got = fetch(a.date, _token(), wait_s=a.wait)
        why = ""
        if not got:
            why = LAST_FETCH_REASON or "not found"
        else:
            md, meta = got
            bad = problems(md, meta)
            if bad:
                why = "not usable: " + "; ".join(bad)
        if why:
            return _miss_day(a.date, why, body_path, headline_path)
        headline, insights = to_insights(md)
        ...  # unchanged from here
```

and add the helper above `main`:

```python
def _miss_day(date: str, why: str, body_path: str, headline_path: str) -> int:
    """Exit for a day without a usable Omnipulse: 3 = classic pulse,
    EXIT_LIGHT = the light body is written and the routine runs the
    omnipulse-day path around it."""
    if MISS_DAY != "light":
        print(f"omnipulse: none for {date} ({why}) -> classic pulse")
        return 3
    with open(body_path, "w", encoding="utf-8") as fh:
        fh.write(light_body())
    with open(headline_path, "w", encoding="utf-8") as fh:
        fh.write("")
    print(f"omnipulse: none for {date} ({why}) -> light pulse")
    return EXIT_LIGHT
```

Keep the existing `not usable` print text inside `why` so the driver's detail still says `not usable` (the routine's STEP 2.4 branches on that word).

- [ ] **Step 4: Run the tests**

Run: `py -3.12 -m pytest -q tests/test_omnipulse_body.py`
Expected: all pass, including the older `test_the_gate_says_why_it_went_classic` (exit 3 under `MISS_DAY="classic"`).

- [ ] **Step 5: Commit**

Message: `Omnipulse: light body on a miss day behind MISS_DAY (default classic)`

---

### Task 2: Driver gate and preflight

**Files:**
- Modify: `scripts/pulse_driver.py` (`omnipulse_mode`, `gate_omnipulse`, `_body_headings` use in `preflight`)
- Test: `tests/test_omnipulse_driver.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_omnipulse_driver.py`:

```python
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


def test_the_env_override_still_forces_classic_on_a_light_day(tmp_path, monkeypatch):
    d = _driver(tmp_path)
    monkeypatch.setenv("OMNIPULSE_BODY", "off")
    with patch.object(O, "ENABLED", True), patch.object(O, "MISS_DAY", "light"), _fake_light(tmp_path):
        assert d.gate_omnipulse("2026-09-30") == "CLASSIC"
    assert not d.omnipulse_mode()
```

- [ ] **Step 2: Run them to verify they fail**

Run: `py -3.12 -m pytest -q tests/test_omnipulse_driver.py -k "light"`
Expected: first two fail (`gate_omnipulse` returns `CLASSIC` for exit 4; `omnipulse_mode()` false for `light`). The third passes already.

- [ ] **Step 3: Implement**

`omnipulse_mode`:

```python
    def omnipulse_mode(self) -> bool:
        """True on an omnipulse day AND a light day: both run with a
        locked body the routine did not write."""
        try:
            return (self.tmp / "pulse_mode.txt").read_text(
                encoding="utf-8").strip() in ("omnipulse", "light")
        except OSError:
            return False
```

In `gate_omnipulse`, between the `code == 0` branch and the classic fallthrough:

```python
        if code == _omni().EXIT_LIGHT:
            # Miss day under MISS_DAY="light": the note is the locked
            # body; every omnipulse-day branch applies. The reason is
            # kept for STEP 6's frontmatter and the bridge's ops page.
            reason = out.strip().splitlines()[-1] if out.strip() else "miss day"
            (self.tmp / "light_reason.txt").write_text(reason[:300], encoding="utf-8")
            mode_file.write_text("light", encoding="utf-8")
            return self._decide("omnipulse", "LIGHT", reason[-300:])
```

Replace `_body_headings` with markers that cover a heading-less body:

```python
    def _body_markers(self) -> list[str]:
        """Strings that must survive in final.md: the body's `###`
        headings, or, for a light body with none, its note text."""
        try:
            body = self._body_path().read_text(encoding="utf-8")
        except OSError:
            return []
        heads = re.findall(r"(?m)^### .+$", body)
        if heads:
            return heads
        paras = [p.strip() for p in re.split(r"\n\s*\n", body)
                 if p.strip() and not p.strip().startswith("#")]
        return paras[:1]
```

and in `preflight` change `heads = self._body_headings()` to `heads = self._body_markers()`. Grep for any other `_body_headings` caller (`grep -n _body_headings scripts/pulse_driver.py`) and rename it too.

- [ ] **Step 4: Run the tests**

Run: `py -3.12 -m pytest -q tests/test_omnipulse_driver.py tests/test_omnipulse_body.py`
Expected: all pass.

- [ ] **Step 5: Commit**

Message: `Pulse driver: DECISION LIGHT, light_reason.txt, preflight checks the note`

---

### Task 3: Downstream tolerance, proven by tests

**Files:**
- Create: `tests/test_light_pulse.py`
- Possibly modify: `scripts/pulse_draft_validate.py` (only if Step 2 shows a hard kind on the light document)

- [ ] **Step 1: Write the tests**

```python
"""The light pulse (spec 2026-09-29) through every consumer that reads
the INSIGHTS section. Built from the real 9/25 DRAFT with the note as
its body."""
import json
import os
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
    p = subprocess.run([sys.executable, *args], capture_output=True, text=True,
                       encoding="utf-8", cwd=str(REPO))
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
    assert O.LIGHT_BODY_NOTE[:40] in html and "insights" in html


def test_the_light_document_passes_the_gates(tmp_path):
    md = _light_md()
    doc = tmp_path / "final.md"
    doc.write_text(md, encoding="utf-8")
    ctx = tmp_path / "ctx.json"
    ctx.write_text(json.dumps({"today": "2026-09-25", "theme_map": {}}), encoding="utf-8")
    out_json = tmp_path / "v.json"
    code, out = _run("scripts/pulse_draft_validate.py", str(doc), str(ctx), str(out_json), "--omnipulse")
    v = json.loads(out_json.read_text(encoding="utf-8"))
    assert v["hard_count"] == 0, [x["kind"] for x in v["violations"]]
    lint_json = tmp_path / "lint.json"
    code, out = _run("scripts/pulse_lint.py", str(doc), str(lint_json), str(ctx))
    hard = [i for i in json.loads(lint_json.read_text(encoding="utf-8")) if i.get("severity") == "hard"]
    assert not hard, hard


def test_the_note_obeys_the_voice_rules():
    from ai_analysis.voice_rules import compose_lint_patterns
    import re
    for pat in compose_lint_patterns():
        rx = pat if isinstance(pat, str) else pat.get("pattern", "")
        assert not re.search(rx, O.LIGHT_BODY_NOTE, re.I), rx
```

Before running, check the lint report's shape: `grep -n '"severity"' scripts/pulse_lint.py | head -3`. If issues carry `severity` differently (for example `hard: true`), adjust the `hard = [...]` filter to that field. Check `compose_lint_patterns()`'s return shape the same way (`grep -n "def compose_lint_patterns" -A12 ai_analysis/voice_rules.py`) and adjust the `rx` extraction.

- [ ] **Step 2: Run them**

Run: `py -3.12 -m pytest -q tests/test_light_pulse.py -x`
Expected: pass. If `test_the_light_document_passes_the_gates` fails with a hard kind, read the kind from the assertion message. A kind that only makes sense with themes (for example one complaining that INSIGHTS has no `###` slots) is added to `OMNIPULSE_EXEMPT_KINDS` in `scripts/pulse_draft_validate.py` with a comment `# light pulse (2026-09-29): a note, not themes`. Any other kind means the note text itself trips a rule; fix the note in `LIGHT_BODY_NOTE`, not the validator.

- [ ] **Step 3: Commit**

Message: `Light pulse: downstream consumers accept a theme-less body (tests)`

---

### Task 4: Ops page from the bridge

**Files:**
- Modify: `github_bridge/jobs.py` (new `_light_day_ping`, called after the residuals page)
- Test: `tests/test_light_pulse.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_light_pulse.py`:

```python
def test_a_light_pulse_pages_ops_once_with_the_reason(monkeypatch):
    import asyncio
    from github_bridge import jobs as J
    sent = []

    async def fake_alert(text, dedupe_key=""):
        sent.append((text, dedupe_key))
    monkeypatch.setattr("discord_bot.ops_alert.ops_alert", fake_alert)
    asyncio.run(J._light_day_ping(
        {"body_source": "light", "light_reason": "not published after 600s"}, "2026-09-30T14-11-00Z.md"))
    asyncio.run(J._light_day_ping({"body_source": "omnipulse"}, "x.md"))
    assert len(sent) == 1
    text, key = sent[0]
    assert "light" in text and "not published after 600s" in text and key == "pulse-light-2026-09-30T14-11-00Z.md"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -3.12 -m pytest -q tests/test_light_pulse.py -k pages_ops`
Expected: FAIL, `module has no attribute '_light_day_ping'`.

- [ ] **Step 3: Implement**

In `github_bridge/jobs.py`, add a module-level coroutine near `_parse_frontmatter`:

```python
async def _light_day_ping(meta: dict, name: str) -> None:
    """One ops page when a light pulse posts (spec 2026-09-29): the
    Omnipulse did not land and the reader got RECAP and WATCH around a
    note. Keyed per pulse file so a bridge retry does not page twice."""
    if (meta.get("body_source") or "").strip() != "light":
        return
    reason = (meta.get("light_reason") or "no reason recorded").strip()
    try:
        from discord_bot.ops_alert import ops_alert as _ops_alert
        await _ops_alert(
            f"🟠 pulse {name} went out LIGHT (no research body): {reason}",
            dedupe_key=f"pulse-light-{name}",
        )
    except Exception as e:
        log.warning(f"Bridge: light-day page failed: {e}")
```

Call it right after the residuals block (after the `if _n_res:` block, before `await asyncio.to_thread(gh.put_file, ...)` at the archive write):

```python
        await _light_day_ping(meta, name)
```

- [ ] **Step 4: Run the tests**

Run: `py -3.12 -m pytest -q tests/test_light_pulse.py`
Expected: pass.

- [ ] **Step 5: Commit**

Message: `Bridge: ops page when a light pulse posts`

---

### Task 5: Routine markdown

**Files:**
- Modify: `docs/superpowers/routines/synthesis-routine.md` (STEP 2.4 bullets, DRAFT omnipulse-day block, STEP 6 frontmatter)
- Test: `tests/test_light_pulse.py`

- [ ] **Step 1: Write the failing test**

```python
def test_the_routine_documents_the_light_decision():
    md = (REPO / "docs/superpowers/routines/synthesis-routine.md").read_text(encoding="utf-8")
    assert "DECISION: LIGHT" in md
    assert "light_reason" in md
    i = md.index("### STEP 2.4")
    assert md.index("DECISION: LIGHT") > i
```

- [ ] **Step 2: Run it to verify it fails**

Run: `py -3.12 -m pytest -q tests/test_light_pulse.py -k routine_documents`
Expected: FAIL.

- [ ] **Step 3: Edit the routine**

In STEP 2.4, after the `DECISION: OMNIPULSE` bullet, add:

```markdown
- **`DECISION: LIGHT`** — a miss day with `MISS_DAY = "light"`: no usable
  Omnipulse after the wait, so `/tmp/omnipulse_body.md` holds a one-
  paragraph note and `/tmp/omnipulse_headline.txt` is empty. Follow every
  "omnipulse day" instruction below exactly as for OMNIPULSE, with two
  differences: write the H1 yourself from the live tape (there is no
  supplied headline), and write `## _LEANS` from desk calls in the
  research as on a classic day. `/tmp/light_reason.txt` says why.
  (Superseded: the shipped bullet tries the backup route once on a not published or fetch error detail.)
```

In the DRAFT omnipulse-day block (the fenced text starting `OMNIPULSE DAY.`), add after the `_LEANS` bullet:

```
- LIGHT DAY (the supplied body is a single note, no themes): write the H1
  from RECAP's tape. Write `## _LEANS` from desk calls in the research.
  Do not write themes.
```

In STEP 6's frontmatter code, after `frontmatter_lines.append(f'body_source: {_mode}')`:

```python
if _mode == 'light':
    try:
        _why = open('/tmp/light_reason.txt').read().strip().replace('\n', ' ')[:200]
    except FileNotFoundError:
        _why = ''
    if _why:
        frontmatter_lines.append(f'light_reason: {_why}')
```

Confirm the STEP 6 heredoc still parses: `py -3.12 -c "import re,ast; s=open('docs/superpowers/routines/synthesis-routine.md',encoding='utf-8').read(); [ast.parse(b) for b in re.findall(r\"<< ?'PYEOF'[^\n]*\n(.*?)\nPYEOF\", s, re.S) if 'body_source' in b]; print('ok')"`.

- [ ] **Step 4: Run the tests**

Run: `py -3.12 -m pytest -q tests/test_light_pulse.py`
Expected: pass.

- [ ] **Step 5: Commit**

Message: `Routine: DECISION LIGHT and light_reason frontmatter`

---

### Task 6: Docs, full suite, review, push

**Files:**
- Modify: `CLAUDE.md` (Shadow pilot section), `NOTES.md`

- [ ] **Step 1: CLAUDE.md**

In the "Shadow pilot (claim-card redesign)" section, after the sentence ending `without touching the committed switch.`, add:

```markdown
`MISS_DAY` in the same file decides a day with no usable Omnipulse: `"classic"` (current) runs the classic pulse, `"light"` (owner option a, 2026-09-29, spec `docs/superpowers/specs/2026-09-29-light-pulse-miss-day-design.md`) publishes RECAP, TRADE BOARD and WHAT TO WATCH around a one-paragraph note (`DECISION: LIGHT`, frontmatter `body_source: light`, one ops page from the bridge). It flips in the retirement commit, gated on 10 consecutive market days with `body_source: omnipulse`.
```

- [ ] **Step 2: NOTES.md**

Append:

```markdown
## 2026-09-29: light pulse on a miss day (built, not live)

Owner option (a): when the Omnipulse is missing or unusable after the 10-minute wait and `MISS_DAY = "light"`, the pulse ships RECAP, TRADE BOARD and WHAT TO WATCH around `LIGHT_BODY_NOTE`, no research body. Rides the omnipulse-day path: `omnipulse_body.py fetch` exits 4 with the note as the locked body, the driver prints `DECISION: LIGHT` and writes `pulse_mode.txt = light` (`omnipulse_mode()` is true for both), preflight checks the note instead of theme headings, STEP 6 stamps `body_source: light` and `light_reason`, the bridge pages ops once. The splitter, formatter and web fragment already tolerated a theme-less INSIGHTS section; tests pin that. `MISS_DAY` stays `"classic"` until the classic pulse is retired.
```

- [ ] **Step 3: Full suite and smokes**

Run: `py -3.12 -m pytest -q tests` then `py -3.12 scripts/run_smokes.py --full`
Expected: all pass (the smokes set `OMNIPULSE_BODY=off`, so they stay classic).

- [ ] **Step 4: Code review**

Invoke the `code-review` skill at low effort over the uncommitted tree plus the task commits (`git diff HEAD~5`), fix findings, re-run Step 3.

- [ ] **Step 5: Commit and push**

Message: `Light pulse docs; NOTES`. Then `git push` (the pre-push hook runs preflight and the fast smoke tier).
