"""Worker-side workflow dispatch (2026-09-02): the pilot's schedule on a
real clock, because GitHub's cron dropped most runs on shakedown day 1."""
import json
import sys
import urllib.request

from github_bridge import workflow_dispatch as W


class _Resp:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _with_token(fn):
    import config
    orig = config.settings.github_token
    config.settings.github_token = "github_pat_test"
    try:
        return fn()
    finally:
        config.settings.github_token = orig


def test_dispatch_posts_the_right_url_ref_and_inputs():
    seen = {}

    def fake_open(req, timeout=0):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data.decode())
        seen["auth"] = req.get_header("Authorization")
        return _Resp(204)
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        status = _with_token(lambda: W.dispatch("pilot-editor.yml", inputs={"date": "2026-09-02"}))
    finally:
        urllib.request.urlopen = orig
    assert status == 204
    assert seen["url"].endswith("/actions/workflows/pilot-editor.yml/dispatches")
    assert seen["body"] == {"ref": W.DEFAULT_REF, "inputs": {"date": "2026-09-02"}}
    assert seen["auth"] == "Bearer github_pat_test"


def test_403_pages_ops_and_never_raises():
    alerts = []

    def fake_open(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)
    from discord_bot import ops_alert as OA
    orig_open, orig_alert = urllib.request.urlopen, OA.ops_alert_sync
    urllib.request.urlopen = fake_open
    OA.ops_alert_sync = lambda text, dedupe_key="": alerts.append(dedupe_key)
    try:
        status = _with_token(lambda: W.dispatch("pilot-readers.yml"))
    finally:
        urllib.request.urlopen, OA.ops_alert_sync = orig_open, orig_alert
    assert status == 403 and alerts == ["workflow-dispatch-auth"]


def test_missing_token_is_a_zero_not_an_exception():
    import config
    orig = config.settings.github_token
    config.settings.github_token = ""
    try:
        assert W.dispatch("pilot-editor.yml") == 0
    finally:
        config.settings.github_token = orig


def test_register_jobs_covers_every_declared_slot():
    class _Sched:
        def __init__(self): self.jobs = []
        def add_job(self, fn, **kw): self.jobs.append(kw)
    s = _Sched()
    n = W.register_jobs(s)
    expected = sum(len(v) for v in W.PILOT_WORKFLOWS.values())
    assert n == expected == len(s.jobs)
    ids = {j["id"] for j in s.jobs}
    assert "dispatch:pilot-editor.yml:1355:mon-fri" in ids
    assert all(j["max_instances"] == 1 for j in s.jobs)


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


def test_a_queued_run_suppresses_the_dispatch_and_a_failed_check_does_not():
    # A concurrency group holds one queued run; a second queued run
    # cancels the first (2026-09-16: two "cancelled" reader runs that
    # were the hourly dispatch stacking, not failures).
    import io
    calls = []

    class _Json(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_open(req, timeout=0):
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "GET":
            # a run held by the concurrency group is `pending`, never `queued`
            return _Json(json.dumps({"workflow_runs": [{"id": 2, "status": "completed"},
                                                       {"id": 1, "status": "pending"}]}).encode())
        return _Resp(204)
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        status = _with_token(lambda: W.dispatch("pilot-readers.yml"))
    finally:
        urllib.request.urlopen = orig
    assert status == 0
    assert [m for m, _ in calls] == ["GET"]
    assert "per_page=5" in calls[0][1] and "status=" not in calls[0][1]

    calls.clear()

    def flaky_open(req, timeout=0):
        calls.append(req.get_method())
        if req.get_method() == "GET":
            raise OSError("api down")
        return _Resp(204)
    urllib.request.urlopen = flaky_open
    try:
        status = _with_token(lambda: W.dispatch("pilot-readers.yml"))
    finally:
        urllib.request.urlopen = orig
    assert status == 204 and calls == ["GET", "POST"]

def test_no_pilot_workflow_keeps_a_cron_and_the_worker_covers_every_slot():
    """The worker's clock is the only scheduler for the pilot (2026-09-18).

    GitHub's cron and the worker dispatch fired the same minute, and the
    concurrency group cancels the run that arrives second, so the ops
    record filled with cancellations that were never failures. The cron
    was never a usable fallback either: a worker that is down publishes
    no source text to read."""
    import pathlib
    import yaml
    root = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows"
    for wf in ("pilot-readers.yml", "pilot-editor.yml", "pilot-graders.yml"):
        doc = yaml.safe_load((root / wf).read_text(encoding="utf-8"))
        trig = doc.get("on", doc.get(True)) or {}   # PyYAML reads `on:` as True
        assert "schedule" not in trig, f"{wf} still declares a cron"
        assert "workflow_dispatch" in trig, wf
        assert wf in W.PILOT_WORKFLOWS, f"{wf} has no worker slot"
    # the slots the readers' cron used to declare, still covered
    readers = set(W.PILOT_WORKFLOWS["pilot-readers.yml"])
    assert {(h, 0, "mon-fri") for h in range(9, 15)} <= readers
    assert (13, 15, "mon-fri") in readers
    assert {(h, 0, "*") for h in (1, 5, 17, 21)} <= readers
