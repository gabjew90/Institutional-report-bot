"""Omnipulse body in the production pulse (spec 2026-09-26).

Fixtures: two real Omnipulse outputs (9/17, 9/24) and the real production
DRAFT of 9/25, whose INSIGHTS section the Omnipulse replaces.
"""
import json
import os
import re

from scripts import omnipulse_body as O

FX = os.path.join(os.path.dirname(__file__), "fixtures", "omnipulse")


def _read(name):
    return open(os.path.join(FX, name), encoding="utf-8").read()


def _omni(day):
    return _read(f"{day}.clean.md"), json.loads(_read(f"{day}.meta.json"))


def test_real_omnipulse_days_pass_the_checks():
    for day in ("2026-09-17", "2026-09-24"):
        md, meta = _omni(day)
        assert O.problems(md, meta) == [], day


def test_the_body_converts_to_productions_format_with_the_main_event_first():
    md, _ = _omni("2026-09-24")
    headline, ins = O.to_insights(md)
    assert headline == "# The Five Percent Problem"
    assert ins.startswith("## 2. INSIGHTS & ALPHA\n\n### The bond market repriced growth")
    # 1 main event + the first 5 of its 8 briefs (owner cap, 2026-09-26)
    assert len(re.findall(r"(?m)^### ", ins)) == 1 + O.MAX_BRIEFS
    assert "### The buyback bid" in ins and "### Small-cap volatility" not in ins
    assert "## 3. BRIEFS" not in ins and "THE MAIN EVENT" not in ins


def test_splice_replaces_only_the_headline_and_insights():
    md, _ = _omni("2026-09-24")
    headline, ins = O.to_insights(md)
    draft = _read("draft-2026-09-25.md")
    out = O.splice(draft, headline, ins)
    assert out.splitlines()[0] == "# The Five Percent Problem"
    # RECAP, WHAT TO WATCH and the internal blocks are the draft's own
    for sec in ("## 1. RECAP", "## 3. WHAT TO WATCH", "## _LEANS"):
        before = draft[draft.index(sec):].split("\n## ", 1)[0]
        assert before in out, sec
    assert "The bond market's break higher isn't finished" not in out, "old theme gone"
    assert "The bond market repriced growth, not inflation" in out
    assert out.index("## 1. RECAP") < out.index("## 2. INSIGHTS") < out.index("## 3. WHAT TO WATCH")


def test_splice_is_idempotent_so_it_can_run_after_draft_and_after_edit():
    md, _ = _omni("2026-09-24")
    headline, ins = O.to_insights(md)
    once = O.splice(_read("draft-2026-09-25.md"), headline, ins)
    assert O.splice(once, headline, ins) == once


def test_a_placeholder_body_is_replaced():
    md, _ = _omni("2026-09-17")
    headline, ins = O.to_insights(md)
    draft = "# x\n\n## 1. RECAP\n\nr\n\n## 2. INSIGHTS & ALPHA\n\n<<OMNIPULSE_BODY>>\n\n## 3. WHAT TO WATCH\n\nw\n"
    out = O.splice(draft, headline, ins)
    assert "<<OMNIPULSE_BODY>>" not in out and "## 3. WHAT TO WATCH\n\nw" in out


def test_unusable_omnipulse_falls_back():
    md, meta = _omni("2026-09-24")
    assert O.problems(md, dict(meta, unread_source_files_at_edit=3))
    assert O.problems(md, dict(meta, structural_problems=["x"]))
    assert O.problems(md + "\nsee [c12]", meta)
    one_brief = md[:md.index("### Muse is repricing")]
    assert any("BRIEFS" in p for p in O.problems(one_brief, meta))


def test_fetch_polls_until_the_file_lands_then_gives_up():
    calls = []
    files = {}

    def get(path, token):
        calls.append(path)
        return files.get(path)
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:        # the editor finishes during the wait
            files["pilot/shadow/2026-09-28.clean.md"] = "# h"
            files["pilot/shadow/2026-09-28.meta.json"] = "{}"
    got = O.fetch("2026-09-28", "t", wait_s=600, every_s=60, _sleep=sleep, _get=get)
    assert got == ("# h", {}) and len(sleeps) == 2
    assert O.fetch("2026-09-29", "t", wait_s=120, every_s=60,
                   _sleep=lambda s: None, _get=lambda p, t: None) is None


def test_the_cli_writes_body_and_headline(tmp_path, monkeypatch):
    md, meta = _omni("2026-09-24")
    monkeypatch.setattr(O, "BODY_PATH", str(tmp_path / "body.md"))
    monkeypatch.setattr(O, "HEADLINE_PATH", str(tmp_path / "hl.txt"))
    monkeypatch.setattr(O, "fetch", lambda *a, **k: (md, meta))
    assert O.main(["fetch", "--date", "2026-09-24", "--wait", "0"]) == 0
    draft = tmp_path / "draft.md"
    draft.write_text(_read("draft-2026-09-25.md"), encoding="utf-8")
    assert O.main(["splice", "--in", str(draft), "--out", str(draft)]) == 0
    assert draft.read_text(encoding="utf-8").startswith("# The Five Percent Problem")
    monkeypatch.setattr(O, "fetch", lambda *a, **k: None)
    assert O.main(["fetch", "--date", "2026-09-25", "--wait", "0"]) == 3


def test_hard_fetch_errors_fall_back_without_the_full_wait():
    """The routine's proxy refused api.github.com with 403 on every call;
    an error is not 'not yet', so three in a row end the wait."""
    sleeps = []

    def boom(path, token):
        raise OSError("403 Forbidden")
    assert O.fetch("2026-09-28", "t", wait_s=1200, every_s=60,
                   _sleep=sleeps.append, _get=boom) is None
    assert len(sleeps) == O.MAX_FETCH_ERRORS - 1


def test_the_fetch_reads_the_raw_host_not_the_api():
    import inspect
    src = inspect.getsource(O.fetch_text)
    assert "raw.githubusercontent.com" in src and "api.github.com/repos" not in src


# --- 2026-09-28: the routine's token made the raw host answer 404 -------
def test_the_fetch_tries_without_the_token_first(monkeypatch):
    seen = []

    def raw(url, token):
        seen.append(token)
        return None if token else "# ok"      # a rejected token reads as 404
    monkeypatch.setattr(O, "_raw_get", raw)
    assert O.fetch_text("pilot/shadow/x.clean.md", "inert-token") == "# ok"
    assert seen == [""]


def test_the_token_is_a_second_try_for_a_private_fork(monkeypatch):
    monkeypatch.setattr(O, "_raw_get", lambda url, token: "# ok" if token else None)
    assert O.fetch_text("p", "good-token") == "# ok"


def test_the_gate_says_why_it_went_classic(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(O, "_raw_get", lambda url, token: None)
    assert O.main(["fetch", "--date", "2026-09-28", "--wait", "0",
                   "--body", str(tmp_path / "b"), "--headline", str(tmp_path / "h")]) == 3
    assert "not published" in capsys.readouterr().out


def test_the_backup_route_reads_files_the_routine_saved(tmp_path):
    md, meta = _omni("2026-09-24")
    (tmp_path / "2026-09-28.clean.md").write_text(md, encoding="utf-8")
    (tmp_path / "2026-09-28.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    body, head = tmp_path / "b.md", tmp_path / "h.txt"
    assert O.main(["fetch", "--date", "2026-09-28", "--from", str(tmp_path),
                   "--body", str(body), "--headline", str(head)]) == 0
    assert head.read_text(encoding="utf-8") == "# The Five Percent Problem"
    assert O.main(["fetch", "--date", "2026-09-29", "--from", str(tmp_path),
                   "--body", str(body), "--headline", str(head)]) == 3


def test_the_default_wait_is_ten_minutes():
    assert O.DEFAULT_WAIT_S == 600


def test_the_backup_copy_must_match_githubs_hash(tmp_path):
    md, meta = _omni("2026-09-24")
    (tmp_path / "2026-09-28.clean.md").write_text(md, encoding="utf-8", newline="")
    (tmp_path / "2026-09-28.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    good = O.git_blob_sha(md)
    assert O.read_local(str(tmp_path), "2026-09-28", good) is not None
    assert O.read_local(str(tmp_path), "2026-09-28", "0" * 40) is None
    assert "sha" in O.LAST_FETCH_REASON


def test_the_hash_is_gits_blob_hash():
    assert O.git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


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
    insights = out.split("## 3. WHAT TO WATCH")[0].split("## 2.")[1]
    assert O.LIGHT_BODY_NOTE in out and "### " not in insights


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
