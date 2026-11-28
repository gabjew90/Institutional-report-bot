"""Claude analysis lane helper (scripts/pilot_analyze.py, 2026-10-06)."""
import datetime as dt
import json
from pathlib import Path

from scripts import pilot_analyze as PA

TEXT = ("Goldman Sachs US Economics. We expect core PCE to rise 0.3% in September. "
        "Our 10-year Treasury forecast is 4.6% by year end.")


def _doc(root, doc_id="18282", date=None):
    date = date or dt.date.today().isoformat()
    st = root / "pilot" / "source-text" / date
    st.mkdir(parents=True, exist_ok=True)
    text_rel = f"pilot/source-text/{date}/{doc_id}__gs-econ.txt"
    (root / text_rel).write_text(TEXT, encoding="utf-8")
    (st / f"{doc_id}.meta.json").write_text(json.dumps({
        "pdf_file_id": int(doc_id), "file_name": "GS_US_Economics.pdf",
        "source": "Goldman Sachs", "title": "US Economics", "priority": "high",
        "text_path": text_rel}), encoding="utf-8")
    _wait(root, doc_id, date)
    return date


def _wait(root, doc_id, date, priority="high", minutes_left=120, replace=False):
    """Put a note on the worker's list (pilot/lane/waiting.json)."""
    f = root / "pilot" / PA.WAITING
    f.parent.mkdir(parents=True, exist_ok=True)
    rows = [] if replace or not f.exists() else json.loads(f.read_text())["waiting"]
    deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes_left)
    rows.append({"id": int(doc_id), "date": date, "priority": priority,
                 "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")})
    f.write_text(json.dumps({"waiting": rows}), encoding="utf-8")


def test_pending_lists_documents_without_an_analysis(tmp_path):
    date = _doc(tmp_path)
    _doc(tmp_path, "18283", date)
    (tmp_path / "pilot" / "analyses" / date).mkdir(parents=True)
    (tmp_path / "pilot" / "analyses" / date / "18283.json").write_text("{}")
    rows = PA.pending(tmp_path / "pilot", limit=25)
    assert [r[0] for r in rows] == ["18282"]


def test_a_document_stops_after_three_failed_attempts(tmp_path):
    date = _doc(tmp_path)
    for _ in range(PA.MAX_ATTEMPTS):
        PA.fail(tmp_path / "pilot", date, "18282", "no JSON")
    assert PA.pending(tmp_path / "pilot", limit=25) == []


def test_the_prompt_is_the_production_analysis_prompt_with_the_text(tmp_path):
    from ai_analysis.prompts import ANALYSIS_SYSTEM_PROMPT
    date = _doc(tmp_path)
    text, meta = PA.pending(tmp_path / "pilot", 25)[0][1:3]
    out = tmp_path / "p.txt"
    PA.write_prompt(text, meta, str(out))
    p = out.read_text(encoding="utf-8")
    assert p.startswith(ANALYSIS_SYSTEM_PROMPT[:200])
    assert TEXT in p and "GS_US_Economics.pdf" in p and "JSON object only" in p
    assert date


def test_finalize_builds_a_full_record_with_the_anchor_check(tmp_path):
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 25)[0]
    answer = tmp_path / "out.txt"
    answer.write_text("```json\n" + json.dumps({
        "source": "Goldman Sachs", "title": "US Economics", "report_type": "macro",
        "key_insights": ["Goldman expects core PCE +0.3% in September."],
        "macro_indicators": [{"indicator": "Core PCE m/m", "reading": "0.3%",
                              "interpretation": "firm", "status": "forecast"}],
        "key_data_points": [{"figure": "0.3%", "metric": "Core PCE m/m", "source_bank": "Goldman Sachs",
                             "anchor": "We expect core PCE to rise 0.3% in September"}],
        "trade_ideas": [], "entities_mentioned": [],
    }) + "\n```", encoding="utf-8")
    out = tmp_path / "a.json"
    rec = PA.finalize(str(answer), text, meta, str(out), "claude-sonnet-5-5")
    a = json.loads(out.read_text(encoding="utf-8"))["analysis"]
    assert rec["pdf_file_id"] == 18282 and a["priority"] == "high"
    assert a["macro_indicators"][0]["indicator"] == "Core PCE m/m"
    assert a["anchor_check"]["matched"] == 1


def test_finalize_refuses_an_answer_without_analysis(tmp_path):
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 25)[0]
    bad = tmp_path / "bad.txt"
    bad.write_text("I could not read the document.", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError):
        PA.finalize(str(bad), text, meta, str(tmp_path / "x.json"), "m")


def test_cli_json_output_carries_the_answer_and_the_token_usage(tmp_path):
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 25)[0]
    m = json.loads(open(meta, encoding="utf-8").read())
    m["total_pages"] = 14
    open(meta, "w", encoding="utf-8").write(json.dumps(m))
    answer = json.dumps({"key_insights": ["x"], "source": "Goldman Sachs"})
    raw = tmp_path / "cli.json"
    raw.write_text(json.dumps({"type": "result", "result": answer,
                               "usage": {"input_tokens": 900, "cache_read_input_tokens": 100,
                                         "output_tokens": 1200}}), encoding="utf-8")
    PA.finalize(str(raw), text, meta, str(tmp_path / "a.json"), "m")
    a = json.loads((tmp_path / "a.json").read_text(encoding="utf-8"))["analysis"]
    assert (a["total_pages"], a["input_tokens"], a["output_tokens"]) == (14, 1000, 1200)


def test_a_usage_limit_is_not_a_document_failure():
    assert PA.is_limit(json.dumps({"result": "You've hit your weekly limit. Resets Monday."}))
    assert PA.is_limit("Claude AI usage limit reached|1759800000")
    assert not PA.is_limit(json.dumps({"result": '{"key_insights": ["rate limit on banks"]}'}))


def test_sources_are_one_name_per_house():
    from ai_analysis.analysis_build import canonical_source as C
    assert C("Citi (via ZeroHedge / Market Ear)") == "Citi"
    assert C("BofA Global Research") == "Bank of America"
    assert C("BofA (via ZeroHedge, Tyler Durden)") == "Bank of America"
    assert C("Goldman Sachs (ETF desk, Chris Hussey)") == "Goldman Sachs"
    assert C("Zero Hedge") == "ZeroHedge"
    assert C("Société Générale") == "Societe Generale"
    assert C("Raymond James") == "Raymond James"
    assert C("") == "Unknown"


def test_a_market_ear_document_keeps_its_banned_source(tmp_path):
    """The TRADE BOARD drops calls sourced to The Market Ear; a Claude
    re-attribution to the underlying desk must not bypass that."""
    _doc(tmp_path)
    _, text, meta, _ = PA.pending(tmp_path / "pilot", 25)[0]
    m = json.loads(open(meta, encoding="utf-8").read())
    m["source"] = "The Market Ear"
    open(meta, "w", encoding="utf-8").write(json.dumps(m))
    raw = tmp_path / "r.json"
    raw.write_text(json.dumps({"key_insights": ["x"], "source": "Goldman Sachs (ETF desk)"}),
                   encoding="utf-8")
    rec = PA.finalize(str(raw), text, meta, str(tmp_path / "a.json"), "m")
    assert rec["analysis"]["source"] == "The Market Ear"
    assert rec["claude_source"] == "Goldman Sachs"


def _medium(root, doc_id, date, lane):
    st = root / "pilot" / "source-text-all" / date
    st.mkdir(parents=True, exist_ok=True)
    text_rel = f"pilot/source-text-all/{date}/{doc_id}__ubs.txt"
    (root / text_rel).write_text(TEXT, encoding="utf-8")
    meta = {"pdf_file_id": int(doc_id), "file_name": "UBS.pdf", "source": "UBS",
            "title": "UBS", "priority": "medium", "text_path": text_rel}
    if lane:
        meta["lane"] = True
    (st / f"{doc_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


def test_only_notes_on_the_workers_list_are_pending_in_its_order(tmp_path):
    """2026-11-23: the workflow analysed every note of the week, oldest id
    first, while the worker waited for other notes. Now the list decides."""
    today = dt.date.today().isoformat()
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    _doc(tmp_path, "18282", yesterday)          # HIGH, listed by _doc
    _doc(tmp_path, "18290", today)
    _medium(tmp_path, "30001", today, lane=True)
    _medium(tmp_path, "30002", today, lane=True)   # published, but the worker gave up on it
    root = tmp_path / "pilot"
    # The worker's order: HIGH before MEDIUM, nearest deadline first.
    _wait(tmp_path, "18290", today, minutes_left=30, replace=True)
    _wait(tmp_path, "18282", yesterday, minutes_left=90)
    _wait(tmp_path, "30001", today, priority="medium", minutes_left=60)
    assert [r[0] for r in PA.pending(root, limit=25)] == ["18290", "18282", "30001"]
    assert PA.pending(root, limit=1)[0][0] == "18290"
    assert PA.pending(root, limit=25)[2][2].replace("\\", "/").endswith(
        f"source-text-all/{today}/30001.meta.json")


def test_no_list_means_nothing_to_do(tmp_path):
    _doc(tmp_path)
    (tmp_path / "pilot" / PA.WAITING).unlink()
    assert PA.pending(tmp_path / "pilot", limit=25) == []
    (tmp_path / "pilot" / PA.WAITING).write_text("not json")
    assert PA.pending(tmp_path / "pilot", limit=25) == []


def test_a_note_too_close_to_its_deadline_is_left_to_gemini(tmp_path):
    date = _doc(tmp_path)
    _wait(tmp_path, "18282", date, minutes_left=(PA.MIN_LEFT_S - 30) / 60, replace=True)
    assert PA.pending(tmp_path / "pilot", limit=25) == []
    _wait(tmp_path, "18282", date, minutes_left=(PA.MIN_LEFT_S + 60) / 60, replace=True)
    assert [r[0] for r in PA.pending(tmp_path / "pilot", limit=25)] == ["18282"]
    _wait(tmp_path, "18282", date, minutes_left=-5, replace=True)   # already past
    assert PA.pending(tmp_path / "pilot", limit=25) == []


def test_a_list_entry_cannot_point_outside_its_folders(tmp_path):
    date = _doc(tmp_path)
    f = tmp_path / "pilot" / PA.WAITING
    f.write_text(json.dumps({"waiting": [
        {"id": "../../x", "date": date, "deadline": "2099-01-01T00:00:00Z"},
        {"id": 18282, "date": "../" + date, "deadline": "2099-01-01T00:00:00Z"},
        "junk", {"id": 18282}]}))
    assert PA.pending(tmp_path / "pilot", limit=25) == []


# ── the parallel run ────────────────────────────────────────────────────────

GOOD = json.dumps({"result": json.dumps({
    "source": "Goldman Sachs", "title": "US Economics", "report_type": "macro",
    "key_insights": ["Goldman expects core PCE +0.3% in September."]}), "usage": {}})


def _five(tmp_path):
    date = dt.date.today().isoformat()
    for i in range(5):
        _doc(tmp_path, str(18300 + i), date)
    return date


def test_run_analyses_the_list_several_at_a_time(tmp_path):
    import threading
    import time
    date = _five(tmp_path)
    root = tmp_path / "pilot"
    live, peak, lock = [0], [0], threading.Lock()

    def ask(prompt, model):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.15)
        with lock:
            live[0] -= 1
        return GOOD
    st = PA.run(root, workers=3, ask=ask, say=lambda *_: None)
    assert st["analyzed"] == 5 and st["failed"] == 0 and not st["limit"]
    assert peak[0] >= 2                       # in parallel, not one at a time
    for i in range(5):
        rec = json.loads((root / "analyses" / date / f"{18300 + i}.json").read_text(encoding="utf-8"))
        assert rec["pdf_file_id"] == 18300 + i and rec["analysis"]["key_insights"]
    assert PA.pending(root, limit=25) == []    # and nothing is analysed twice


def test_run_counts_a_bad_answer_as_one_attempt_and_goes_on(tmp_path):
    date = _five(tmp_path)
    root = tmp_path / "pilot"

    calls = []

    def ask2(prompt, model):
        calls.append(1)
        return "no JSON here" if len(calls) == 2 else GOOD
    st = PA.run(root, workers=1, ask=ask2, say=lambda *_: None)
    assert st["analyzed"] == 4 and st["failed"] == 1
    failed = list((root / "analysis-failures" / date).glob("*.json"))
    assert len(failed) == 1 and json.loads(failed[0].read_text())["attempts"] == 1
    # The failed one is still on the list for the next run.
    assert len(PA.pending(root, limit=25)) == 1


def test_a_usage_limit_stops_the_run_and_fails_nothing(tmp_path):
    _five(tmp_path)
    root = tmp_path / "pilot"
    calls = []

    def ask(prompt, model):
        calls.append(1)
        return GOOD if len(calls) == 1 else json.dumps(
            {"result": "You have hit your usage limit. Resets 9pm.", "usage": {}})
    st = PA.run(root, workers=1, ask=ask, say=lambda *_: None)
    assert st["limit"] and st["analyzed"] == 1 and st["failed"] == 0
    assert len(calls) == 2
    assert not (root / "analysis-failures").exists()
    assert len(PA.pending(root, limit=25)) == 4


def test_run_stops_taking_documents_when_its_time_is_up(tmp_path):
    _five(tmp_path)
    st = PA.run(tmp_path / "pilot", workers=2, budget_s=-1, ask=lambda p, m: GOOD, say=lambda *_: None)
    assert st["analyzed"] == 0


def test_a_dry_run_reads_recent_notes_and_writes_nothing_to_the_checkout(tmp_path):
    date = _five(tmp_path)
    root = tmp_path / "pilot"
    (root / PA.WAITING).unlink()               # a dry run needs no list
    docs = PA.recent_high(root, 3)
    assert [d[0] for d in docs] == ["18304", "18303", "18302"]
    out = tmp_path / "elsewhere"
    st = PA.run(root, workers=2, ask=lambda p, m: GOOD, docs=docs, out_root=out, say=lambda *_: None)
    assert st["analyzed"] == 3
    assert not (root / "analyses").exists()
    assert len(list((out / "analyses" / date).glob("*.json"))) == 3


def test_the_workflow_runs_the_parallel_driver_and_has_a_dry_run():
    from pathlib import Path
    import yaml
    doc = yaml.safe_load((Path(__file__).resolve().parents[1] / ".github" / "workflows"
                          / "pilot-analysis.yml").read_text(encoding="utf-8"))
    trig = doc.get("on", doc.get(True))
    assert set(trig["workflow_dispatch"]["inputs"]) == {"dry_run", "workers", "dry_run_triage", "triage_bundle", "triage_runs"}
    assert "schedule" not in trig
    steps = {s["name"]: s for s in doc["jobs"]["analyze"]["steps"]}
    run = steps["Analyze"]["run"]
    assert "pilot_analyze.py run" in run and "--repo pilot-data" in run
    # Inputs reach the shell through the environment, never spliced into the script.
    assert "${{" not in run and "${{" not in steps["Find documents the worker is waiting for"]["run"]
    # The side-by-side measurement is its own job and group: it never queues behind the lane.
    assert doc["jobs"]["analyze"]["if"] == "${{ inputs.triage_bundle == '' }}"
    assert doc["jobs"]["triage_test"]["if"] == "${{ inputs.triage_bundle != '' }}"
    assert "pilot-triage-test" in doc["concurrency"]["group"]
    assert "${{" not in {s["name"]: s for s in doc["jobs"]["triage_test"]["steps"]}["Triage the bundle"]["run"]


def _git(cwd, *args):
    import subprocess
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stderr)
    return p.stdout


def test_run_commits_and_pushes_each_record_and_picks_up_a_list_change(tmp_path):
    """The real git path: a bare remote, the runner's checkout, and a second
    clone standing in for the worker, which changes the list mid-run."""
    import subprocess
    import threading
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "pilot-data", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    date = dt.date.today().isoformat()
    for i in range(3):
        _doc(seed, str(18400 + i), date)
    _doc(seed, "18409", date)
    # The list starts with three notes. 18409 is published but not waited for.
    f = seed / "pilot" / PA.WAITING
    rows = json.loads(f.read_text())["waiting"]
    f.write_text(json.dumps({"waiting": [r for r in rows if r["id"] != 18409]}))
    _git(seed, "init", "-q", "-b", "pilot-data")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git(seed, "config", k, v)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "pilot-data")

    checkout, worker = tmp_path / "pilot-data", tmp_path / "worker"
    for c in (checkout, worker):
        subprocess.run(["git", "clone", "-q", str(remote), str(c)], check=True)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
            _git(c, "config", k, v)

    first = threading.Event()

    def ask(prompt, model):
        if not first.is_set():
            first.set()
            # The worker hands 18409 over and gives up on 18402 while the run works.
            wf = worker / "pilot" / PA.WAITING
            cur = json.loads(wf.read_text())["waiting"]
            keep = [r for r in cur if r["id"] != 18402]
            keep.append({"id": 18409, "date": date, "priority": "high",
                         "deadline": "2099-01-01T00:00:00Z"})
            wf.write_text(json.dumps({"waiting": keep}))
            _git(worker, "commit", "-q", "-am", "lane: list changed")
            _git(worker, "push", "-q", "origin", "pilot-data")
        return GOOD
    import scripts.pilot_analyze as mod
    old = mod.REFRESH_S
    mod.REFRESH_S = 0
    try:
        st = PA.run(checkout / "pilot", workers=1, ask=ask, repo=checkout, say=lambda *_: None)
    finally:
        mod.REFRESH_S = old
    assert st["failed"] == 0 and not st["limit"]
    _git(worker, "pull", "-q", "--rebase", "origin", "pilot-data")
    done = sorted(p.stem for p in (worker / "pilot" / "analyses" / date).glob("*.json"))
    # 18400 was in flight when the list changed. 18402 was dropped, 18409 picked up.
    assert done == ["18400", "18401", "18409"]
    log = _git(worker, "log", "--format=%s")
    assert log.count("pilot: analysis (") == 3
    assert _git(checkout, "status", "--porcelain").strip() == ""


def test_the_analysis_call_has_no_tools_and_reads_nothing_of_the_repository(tmp_path, monkeypatch):
    """2026-11-23: it ran with every tool allowed on bank PDF text, beside a
    token that can push to the deploying branch, and loaded CLAUDE.md into
    every call (83,505 input tokens a note where the note needs about 17,000)."""
    import subprocess
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["cwd"] = list(argv), kw.get("cwd")
        return subprocess.CompletedProcess(argv, 0, stdout=GOOD.encode(), stderr=b"")
    monkeypatch.setattr(PA.subprocess, "run", fake_run)
    work = tmp_path / "lane0"
    work.mkdir()
    (work / "prompt.txt").write_text("x", encoding="utf-8")
    assert PA.claude_cli(work / "prompt.txt", "m") == GOOD
    argv = seen["argv"]
    assert argv[:4] == ["claude", "-p", "--model", "m"]
    assert argv[argv.index("--tools") + 1] == ""            # no built-in tool at all
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv
    assert "--disable-slash-commands" in argv
    assert "bypassPermissions" not in argv and "--permission-mode" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert argv[argv.index("--system-prompt") + 1] == PA.LANE_SYSTEM_PROMPT
    # It runs in its own temp folder, so no CLAUDE.md, hook or setting of this repository loads.
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    assert Path(seen["cwd"]) == work and repo not in Path(seen["cwd"]).parents
    src = (repo / ".github" / "workflows" / "pilot-analysis.yml").read_text(encoding="utf-8")
    assert "bypassPermissions" not in src.split("- name: Analyze")[1]


# ── triage by the lane (2026-11-28) ─────────────────────────────────────────

def _note(root, doc_id, date, *, folder="/Current/2026/November/Nov 27/Goldman",
          file_name="GS_US_Economics.pdf", listed=True):
    """A note the worker handed over before triage: one file, and an entry
    on the list marked for triage."""
    d = root / "pilot" / PA.NOTES / date
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{doc_id}.json").write_text(json.dumps({
        "pdf_file_id": int(doc_id), "file_name": file_name, "folder_path": folder,
        "total_pages": 9, "note_date": "2026-11-27", "truncated": False, "text": TEXT}),
        encoding="utf-8")
    if listed:
        f = root / "pilot" / PA.WAITING
        f.parent.mkdir(parents=True, exist_ok=True)
        rows = json.loads(f.read_text())["waiting"] if f.exists() else []
        deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=120)
        rows.append({"id": int(doc_id), "date": date, "priority": "untriaged", "triage": True,
                     "deadline": deadline.strftime("%Y-%m-%dT%H:%M:%SZ")})
        f.write_text(json.dumps({"waiting": rows}), encoding="utf-8")


def _answers(by_id):
    """A triage stub: the priority per note id, read from the prompt's file name."""
    seen = []

    def triage(prompt, model, cwd):
        doc_id = next(k for k in by_id if f"note{k}.pdf" in prompt)
        seen.append((doc_id, model))
        got = by_id[doc_id]
        if isinstance(got, dict):
            return got
        return {"priority": got, "report_type": "macro", "source": "Goldman Sachs",
                "key_tickers": ["SPX"], "summary": "what it says", "in": 9000, "out": 200}
    triage.seen = seen
    return triage


def test_a_note_waiting_for_triage_is_listed_apart_and_not_analysed(tmp_path):
    date = dt.date.today().isoformat()
    _note(tmp_path, "19001", date)
    root = tmp_path / "pilot"
    assert [r[0] for r in PA.untriaged(root)] == ["19001"]
    assert PA.pending(root) == []
    # with its triage written it is no longer waiting for one
    (root / PA.TRIAGE / date).mkdir(parents=True)
    (root / PA.TRIAGE / date / "19001.json").write_text(json.dumps({
        "pdf_file_id": 19001, "source": "Goldman Sachs", "triage": {"priority": "low"}}))
    assert PA.untriaged(root) == [] and PA.pending(root) == []      # LOW: nothing to analyse


def test_run_triages_then_analyses_what_is_high_or_medium(tmp_path):
    date = dt.date.today().isoformat()
    for doc_id in ("19011", "19012", "19013"):
        _note(tmp_path, doc_id, date, file_name=f"note{doc_id}.pdf")
    root = tmp_path / "pilot"
    order = []
    triage = _answers({"19011": "low", "19012": "medium", "19013": "high"})

    def ask(prompt, model):
        order.append(model)
        return GOOD
    st = PA.run(root, workers=1, ask=ask, triage=triage, say=lambda *_: None)
    assert st["triaged"] == 3 and st["analyzed"] == 2 and st["failed"] == 0
    assert {m for _, m in triage.seen} == {PA.TRIAGE_MODEL}
    rec = json.loads((root / PA.TRIAGE / date / "19012.json").read_text(encoding="utf-8"))
    assert rec["pdf_file_id"] == 19012 and rec["triage"]["priority"] == "medium"
    assert rec["source"] == "Goldman Sachs" and rec["model"] == PA.TRIAGE_MODEL
    assert rec["usage"] == {"input_tokens": 9000, "output_tokens": 200}
    # LOW: a triage record and nothing else
    assert not (root / "analyses" / date / "19011.json").exists()
    # MEDIUM and HIGH: analysed with the priority the triage gave
    for doc_id, pri in (("19012", "medium"), ("19013", "high")):
        a = json.loads((root / "analyses" / date / f"{doc_id}.json").read_text(encoding="utf-8"))
        assert a["analysis"]["priority"] == pri and a["analysis"]["total_pages"] == 9
    assert PA.untriaged(root) == [] and PA.pending(root) == []


def test_a_note_triaged_high_is_analysed_before_the_other_triage_calls(tmp_path):
    date = dt.date.today().isoformat()
    for doc_id in ("19021", "19022", "19023"):
        _note(tmp_path, doc_id, date, file_name=f"note{doc_id}.pdf")
    root = tmp_path / "pilot"
    steps = []
    answers = _answers({"19021": "high", "19022": "medium", "19023": "low"})

    def triage(prompt, model, cwd):
        res = answers(prompt, model, cwd)
        steps.append(("triage", answers.seen[-1][0]))
        return res

    def ask(prompt, model):
        steps.append(("analysis", "19021" if "note19021.pdf" in Path(prompt).read_text(encoding="utf-8")
                      else "19022"))
        return GOOD
    PA.run(root, workers=1, ask=ask, triage=triage, say=lambda *_: None)
    assert steps == [("triage", "19021"), ("analysis", "19021"), ("triage", "19022"),
                     ("triage", "19023"), ("analysis", "19022")]


def test_a_high_note_is_copied_for_the_readers_in_their_layout(tmp_path, monkeypatch):
    date = dt.date.today().isoformat()
    _note(tmp_path, "19031", date, file_name="note19031.pdf")
    _note(tmp_path, "19032", date, file_name="note19032.pdf")
    root = tmp_path / "pilot"
    PA.run(root, workers=1, ask=lambda p, m: GOOD, say=lambda *_: None,
           triage=_answers({"19031": "high", "19032": "medium"}))
    meta = json.loads((root / "source-text" / date / "19031.meta.json").read_text(encoding="utf-8"))
    assert meta["priority"] == "high" and meta["source"] == "Goldman Sachs"
    assert meta["pdf_file_id"] == 19031 and meta["file_name"] == "note19031.pdf"
    assert meta["note_date"] == "2026-11-27" and meta["total_pages"] == 9 and "lane" not in meta
    assert set(meta) == {"pdf_file_id", "file_name", "source", "title", "priority", "published_at",
                         "text_path", "truncated", "total_pages", "note_date"}
    assert (tmp_path / meta["text_path"]).read_text(encoding="utf-8") == TEXT
    # MEDIUM never reaches the readers' folder
    assert not (root / "source-text" / date / "19032.meta.json").exists()
    # and the readers' own listing finds the HIGH note
    import sys
    from scripts import pilot_list_unread as plu
    out = tmp_path / "unread.json"
    monkeypatch.setattr(sys, "argv", ["x", "--root", str(root), "--out", str(out)])
    assert plu.main() == 0
    (row,) = json.loads(out.read_text(encoding="utf-8"))
    assert row["id"] == "19031" and row["source"] == "Goldman Sachs" and row["tier"] == "top"
    assert row["note_date"] == "2026-11-27"


def test_a_failed_triage_counts_an_attempt_and_the_note_stops_after_three(tmp_path):
    date = dt.date.today().isoformat()
    _note(tmp_path, "19041", date, file_name="note19041.pdf")
    root = tmp_path / "pilot"
    bad = _answers({"19041": {"priority": "", "err": "no priority in the answer: sorry"}})
    for n in (1, 2, 3):
        st = PA.run(root, workers=1, triage=bad, say=lambda *_: None)
        assert st["triaged"] == 0 and st["failed"] == 1
        f = json.loads((root / PA.FAILURES / date / "19041.json").read_text())
        assert f["attempts"] == n and f["history"][-1]["reason"].startswith("triage:")
    assert PA.untriaged(root) == []         # given up: the worker's Gemini takes it
    assert PA.run(root, workers=1, triage=bad, say=lambda *_: None)["failed"] == 0


def test_a_usage_limit_during_triage_stops_the_run_and_fails_nothing(tmp_path):
    date = dt.date.today().isoformat()
    for doc_id in ("19051", "19052"):
        _note(tmp_path, doc_id, date, file_name=f"note{doc_id}.pdf")
    root = tmp_path / "pilot"
    st = PA.run(root, workers=1, say=lambda *_: None,
                triage=lambda p, m, c: {"limit": True, "err": "You've hit your limit"})
    assert st["limit"] and st["triaged"] == 0 and st["failed"] == 0
    assert not (root / PA.FAILURES).exists()
    assert len(PA.untriaged(root)) == 2


def test_a_note_filed_under_a_banned_publication_keeps_that_source(tmp_path):
    date = dt.date.today().isoformat()
    _note(tmp_path, "19061", date, file_name="note19061.pdf",
          folder="/Current/2026/November/Nov 27/The Market Ear")
    root = tmp_path / "pilot"
    # the triage reads the author the text credits
    triage = _answers({"19061": {"priority": "high", "report_type": "vol_commentary",
                                 "source": "JPMorgan", "key_tickers": [], "summary": "s"}})
    PA.run(root, workers=1, ask=lambda p, m: GOOD, triage=triage, say=lambda *_: None)
    rec = json.loads((root / PA.TRIAGE / date / "19061.json").read_text(encoding="utf-8"))
    assert rec["source"] == "The Market Ear" and rec["triage"]["source"] == "JPMorgan"
    meta = json.loads((root / "source-text" / date / "19061.meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "The Market Ear"
    a = json.loads((root / "analyses" / date / "19061.json").read_text(encoding="utf-8"))
    assert a["analysis"]["source"] == "The Market Ear"      # GOOD says Goldman Sachs


def test_the_pending_command_counts_notes_waiting_for_triage(tmp_path, capsys):
    date = _doc(tmp_path, "19071")
    _note(tmp_path, "19072", date)
    assert PA.main(["pending", "--root", str(tmp_path / "pilot"), "--limit", "1000"]) == 0
    lines = [l.split("\t") for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert [l[0] for l in lines] == ["19071", "19072"]
    assert lines[1][-1] == "triage" and len(lines[0]) == 4


def test_triage_records_and_the_readers_copy_are_committed_and_pushed(tmp_path):
    import subprocess
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "pilot-data", str(remote)], check=True)
    seed = tmp_path / "seed"
    seed.mkdir()
    date = dt.date.today().isoformat()
    _note(seed, "19081", date, file_name="note19081.pdf")
    _note(seed, "19082", date, file_name="note19082.pdf")
    _git(seed, "init", "-q", "-b", "pilot-data")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git(seed, "config", k, v)
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "pilot-data")
    checkout = tmp_path / "pilot-data"
    subprocess.run(["git", "clone", "-q", str(remote), str(checkout)], check=True)
    for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
        _git(checkout, "config", k, v)
    st = PA.run(checkout / "pilot", workers=2, repo=checkout, ask=lambda p, m: GOOD,
                triage=_answers({"19081": "high", "19082": "low"}), say=lambda *_: None)
    assert st["triaged"] == 2 and st["analyzed"] == 1
    files = _git(remote, "ls-tree", "-r", "--name-only", "pilot-data").split()
    assert f"pilot/{PA.TRIAGE}/{date}/19081.json" in files
    assert f"pilot/{PA.TRIAGE}/{date}/19082.json" in files
    assert f"pilot/source-text/{date}/19081.meta.json" in files
    assert f"pilot/analyses/{date}/19081.json" in files
    assert f"pilot/analyses/{date}/19082.json" not in files
    assert _git(checkout, "status", "--porcelain").strip() == ""


def test_the_rehearsal_stages_recent_notes_outside_the_checkout(tmp_path, capsys):
    date = dt.date.today().isoformat()
    for doc_id in ("19091", "19092"):
        _doc(tmp_path, doc_id, date)
    root = tmp_path / "pilot"
    staged = tmp_path / "elsewhere" / "pilot"
    assert PA.stage_untriaged(root, staged, 2) == 2
    assert [r[0] for r in PA.untriaged(staged)] == ["19092", "19091"]
    note = json.loads((staged / PA.NOTES / date / "19091.json").read_text(encoding="utf-8"))
    assert note["text"] == TEXT and note["file_name"] == "GS_US_Economics.pdf"
    st = PA.run(staged, workers=1, ask=lambda p, m: GOOD, say=lambda *_: None,
                triage=lambda p, m, c: {"priority": "high", "report_type": "macro",
                                        "source": "Goldman Sachs", "summary": "s"})
    assert st["triaged"] == 2 and st["analyzed"] == 2
    # nothing was written into the checkout the notes came from
    assert not (root / PA.TRIAGE).exists() and not (root / "analyses").exists()
    assert len(list((root / "source-text" / date).glob("*.meta.json"))) == 2
