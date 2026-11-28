"""Triage by Claude (scripts/pilot_triage.py, 2026-11-28)."""
import gzip
import json
import subprocess

from scripts import pilot_triage as T


def test_the_prompt_is_gemini_s_with_the_same_first_32000_characters():
    from ai_analysis.prompts import TRIAGE_SYSTEM_PROMPT
    text = "A" * 32000 + "TAIL-NOT-SENT"
    p = T.prompt_for("GS_Flow.pdf", "/Current/2026/November/Nov 27/Goldman", text)
    assert p.startswith(TRIAGE_SYSTEM_PROMPT)
    assert "Filename: GS_Flow.pdf" in p and "Folder path: /Current/2026/November/Nov 27/Goldman" in p
    assert "A" * 32000 in p and "TAIL-NOT-SENT" not in p
    assert p.rstrip().endswith("no tool use.")


def test_an_answer_is_read_into_the_triage_fields():
    got = T.read_answer('```json\n{"priority": "HIGH", "report_type": "macro", "source": "GS", '
                        '"key_tickers": ["SPX"], "summary": "s"}\n```')
    assert got == {"priority": "high", "report_type": "macro", "source": "GS",
                   "key_tickers": ["SPX"], "summary": "s"}
    # prose around the object is tolerated, a list is unwrapped
    assert T.read_answer('Here it is: {"priority": "low"} done')["priority"] == "low"
    assert T.read_answer('[{"priority": "medium"}]')["priority"] == "medium"


def test_an_answer_without_one_of_the_three_priorities_has_none():
    for answer in ("I cannot classify this.", "", '{"priority": "urgent"}', '{"report_type": "macro"}',
                   '{"priority": null}', "[]"):
        got = T.read_answer(answer)
        assert got["priority"] == "" and got["report_type"] in ("other", "macro")


class _Proc:
    def __init__(self, out):
        self.stdout = out.encode("utf-8")


def _cli(result, usage=None):
    return json.dumps({"result": result, "num_turns": 1, "duration_api_ms": 3100,
                       "usage": usage or {"input_tokens": 13000, "output_tokens": 220,
                                          "cache_read_input_tokens": 300}})


def test_a_call_has_no_tools_and_runs_outside_the_checkout(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return _Proc(_cli('{"priority": "medium", "report_type": "macro", "source": "UBS", "summary": "s"}'))
    monkeypatch.setattr(subprocess, "run", fake_run)
    res = T.call("the prompt", "claude-sonnet-5-5", str(tmp_path))
    assert res["priority"] == "medium" and res["source"] == "UBS" and "err" not in res
    assert res["in"] == 13000 and res["cache_read"] == 300 and res["out"] == 220
    cmd = seen["cmd"]
    assert cmd[:4] == ["claude", "-p", "--model", "claude-sonnet-5-5"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--strict-mcp-config" in cmd
    assert "--disable-slash-commands" in cmd and "--no-session-persistence" in cmd
    assert seen["cwd"] == str(tmp_path) and seen["input"] == b"the prompt"


def test_a_usage_limit_is_told_apart_from_a_bad_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Proc(_cli("You've hit your usage limit.")))
    assert T.call("p", "m", str(tmp_path)).get("limit") is True
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Proc("Rate limit exceeded"))
    assert T.call("p", "m", str(tmp_path)).get("limit") is True
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Proc(_cli("Sorry, I can't help.")))
    res = T.call("p", "m", str(tmp_path))
    assert not res.get("limit") and res["priority"] == "" and "no priority" in res["err"]
    # a document that talks about rate limits is not a limit
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _Proc(_cli(
        '{"priority": "low", "summary": "A note on the rate limit of Fed cuts"}')))
    assert T.call("p", "m", str(tmp_path))["priority"] == "low"


def test_a_timeout_or_a_missing_cli_is_an_error_not_a_crash(tmp_path, monkeypatch):
    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(subprocess, "run", slow)
    assert T.call("p", "m", str(tmp_path))["err"] == "timeout"

    def missing(cmd, **kw):
        raise FileNotFoundError("claude")
    monkeypatch.setattr(subprocess, "run", missing)
    assert "did not start" in T.call("p", "m", str(tmp_path))["err"]


def test_runs_are_label_model_and_an_optional_count():
    assert T.parse_runs("sonnet=claude-sonnet-5-5, again=claude-sonnet-5-5:120,") == [
        ("sonnet", "claude-sonnet-5-5", 0), ("again", "claude-sonnet-5-5", 120)]
    assert T.parse_runs("") == []


def _bundle(tmp_path, n):
    path = tmp_path / "bundle.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"id": 100 + i, "file_name": f"n{i}.pdf", "folder": "/Current/GS",
                                 "preview": f"text {i}", "gemini": {"priority": "low"}}) + "\n")
    return str(path)


def test_a_bundle_run_writes_one_line_per_call(tmp_path):
    out = tmp_path / "results.jsonl"
    st = T.run_bundle(_bundle(tmp_path, 4), str(out), T.parse_runs("a=m1,b=m2:2"), workers=2,
                      ask=lambda prompt, model, cwd: {"priority": "low", "secs": 1.0}, say=lambda *_: None)
    assert st["done"] == 6 and st["errors"] == 0 and st["left"] == 0 and not st["limit"]
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert sorted((r["run"], r["id"]) for r in rows) == [
        ("a", 100), ("a", 101), ("a", 102), ("a", 103), ("b", 100), ("b", 101)]
    assert {r["model"] for r in rows if r["run"] == "b"} == {"m2"}


def test_a_bundle_run_stops_on_a_usage_limit(tmp_path):
    out = tmp_path / "results.jsonl"
    st = T.run_bundle(_bundle(tmp_path, 5), str(out), T.parse_runs("a=m1"), workers=1,
                      ask=lambda prompt, model, cwd: {"limit": True}, say=lambda *_: None)
    assert st["limit"] and st["done"] == 0 and st["left"] == 4
