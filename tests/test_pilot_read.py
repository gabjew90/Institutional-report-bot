"""The Omnipulse readers run several documents at a time (2026-11-25).

scripts/pilot_read.py decides how many readers run and when to stop handing
out documents. scripts/pilot_read_one.sh holds the reading steps, moved out of
the workflow file unchanged."""
import threading
import time
from pathlib import Path

import yaml

from scripts import pilot_read as R

REPO = Path(__file__).resolve().parents[1]


def _docs(n):
    return [{"id": str(100 + i), "text_path": f"t{i}.txt", "model": "m", "tier": "t", "date": "2026-11-25"}
            for i in range(n)]


def test_documents_are_read_several_at_a_time_in_list_order():
    live, peak, order, lock = [0], [0], [], threading.Lock()

    def one(doc, dry):
        with lock:
            order.append(doc["id"])
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.1)
        with lock:
            live[0] -= 1
        return 0
    st = R.run(_docs(7), workers=3, one=one, say=lambda *_: None)
    assert st["total"] == 7 and st["failed"] == 0 and st["left"] == 0
    assert peak[0] == 3
    assert order[:3] == ["100", "101", "102"]      # newest first, as the list gives them


def test_no_document_is_started_after_the_time_budget():
    """A run used to be cancelled at 90:00 with documents in flight. Now it
    stops handing them out and ends by itself."""
    started = []

    def one(doc, dry):
        started.append(doc["id"])
        time.sleep(0.15)
        return 0
    st = R.run(_docs(6), workers=2, budget_s=0.1, one=one, say=lambda *_: None)
    assert len(started) == 2                 # the two in flight finish, nothing new starts
    assert st["total"] == 2 and st["left"] == 4


def test_failures_and_retirements_are_counted_apart():
    codes = iter([0, 3, R.RETIRED, 0])
    st = R.run(_docs(4), workers=1, one=lambda d, dry: next(codes), say=lambda *_: None)
    assert (st["total"], st["failed"], st["gave_up"]) == (4, 2, 1)
    assert R.verdict(st) == (0, "")


def test_a_reader_that_crashes_costs_its_document_not_the_run():
    def one(doc, dry):
        if doc["id"] == "101":
            raise OSError("bash not found")
        return 0
    st = R.run(_docs(3), workers=1, one=one, say=lambda *_: None)
    assert st["total"] == 3 and st["failed"] == 1


def test_the_verdict_is_loud_only_for_a_real_wipeout():
    assert R.verdict({"total": 3, "failed": 3, "gave_up": 0})[0] == 1
    code, msg = R.verdict({"total": 2, "failed": 2, "gave_up": 2})   # all retired: the rule working
    assert code == 0 and "retired" in msg
    assert R.verdict({"total": 0, "failed": 0, "gave_up": 0}) == (0, "")
    assert R.verdict({"total": 5, "failed": 4, "gave_up": 0}) == (0, "")


def test_a_dry_run_picks_documents_that_already_have_cards(tmp_path):
    root = tmp_path / "pilot"
    for date, ids in (("2026-11-24", ["500"]), ("2026-11-25", ["601", "602", "603"])):
        (root / "source-text" / date).mkdir(parents=True)
        (root / "cards" / date).mkdir(parents=True)
        for i in ids:
            (root / "source-text" / date / f"{i}__gs-note.txt").write_text("x")
            (root / "source-text" / date / f"{i}.meta.json").write_text('{"source": "Goldman Sachs"}')
    for i in ("601", "603"):
        (root / "cards" / "2026-11-25" / f"{i}.json").write_text("{}")
    (root / "cards" / "2026-11-24" / "500.json").write_text("{}")
    docs = R.recent_read(str(root), 2)
    assert [d["id"] for d in docs] == ["603", "601"]          # newest first, 602 has no cards
    assert all(d["model"] and d["tier"] and d["date"] == "2026-11-25" for d in docs)


def test_the_reader_call_is_read_only_and_loads_nothing_of_this_repository():
    """It ran with every tool allowed on bank PDF text, beside a token that
    can push to the deploying branch, and loaded CLAUDE.md on every turn."""
    src = (REPO / "scripts" / "pilot_read_one.sh").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "bypassPermissions" not in code and "dangerously" not in code
    assert '--tools "Read,Grep,Glob"' in code and "--strict-mcp-config" in code
    assert "cd pilot-data && claude -p" in code
    assert code.count("claude -p") == 1                      # one place, used for the read and the re-ask
    # The prompt file is the frozen one, read from the repository and not edited here.
    assert 'READER_PROMPT="docs/superpowers/routines/pilot/reader.md"' in code
    # Every git command is behind the lock.
    assert "locked persist" in code and "locked note" in code and "locked ops" in code


def test_the_reading_steps_kept_their_order():
    src = (REPO / "scripts" / "pilot_read_one.sh").read_text(encoding="utf-8")
    steps = ["pilot_known_labels.py", "SOURCE_TEXT_PATH=", "KNOWN_LABELS (already in this ledger window",
             'reader "$WORK/reader_prompt.txt"', "pilot_finalize_read.py",
             "--reask-out", "[RE-ASK] failed_cards had anchors NOT found in the source",
             "Re-emit the COMPLETE artifact.", 'reader "$WORK/reask_prompt.txt"', "--final"]
    at = [src.index(s) for s in steps]
    assert at == sorted(at)


def test_the_workflow_runs_the_driver_and_ends_before_its_own_limit():
    doc = yaml.safe_load((REPO / ".github" / "workflows" / "pilot-readers.yml").read_text(encoding="utf-8"))
    trig = doc.get("on", doc.get(True))
    assert set(trig["workflow_dispatch"]["inputs"]) == {"dry_run", "workers"}
    job = doc["jobs"]["read"]
    steps = {s["name"]: s for s in job["steps"]}
    run = steps["Read the documents"]["run"]
    assert "scripts/pilot_read.py" in run and "--budget-minutes 75" in run
    assert "${{" not in run                                   # inputs arrive through the environment
    assert job["timeout-minutes"] > 75 + 18                   # room for the slowest document in flight
    assert "bypassPermissions" not in (REPO / ".github" / "workflows" / "pilot-readers.yml").read_text(encoding="utf-8")
