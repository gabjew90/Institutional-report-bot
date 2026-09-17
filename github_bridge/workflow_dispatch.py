"""Dispatch GitHub Actions workflows from the always-on worker.

WHY (2026-09-02, shakedown day 1): GitHub's cron dropped most of our
schedules. The 30-minute heartbeat fired twice in a day, the readers'
hourly 09-14 UTC window fired once, and the shadow editor's 13:55 UTC
run never fired at all. GitHub documents that scheduled runs may be
delayed or skipped under load; for a pilot whose editor must run in a
five-minute window before production, that is disqualifying.

The worker runs APScheduler on a real clock. These jobs POST
`workflow_dispatch` at the times the workflow files declare. The
workflows keep their `schedule:` blocks as a fallback; `concurrency`
groups in each workflow make a double fire harmless.

Requires the worker's GITHUB_TOKEN to carry Actions: read and write
(a fine-grained PAT without it answers 403, which pages ops once an
hour rather than failing silently). Gated on PILOT_DISPATCH_ENABLED.
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from config import settings

log = logging.getLogger(__name__)

DEFAULT_REF = "claude/financial-pdf-discord-bot-mDpbk"

PILOT_WORKFLOWS = {
    # file -> list of (hour_utc, minute, day_of_week)
    "pilot-readers.yml": [(h, 0, "mon-fri") for h in range(9, 15)]
                         + [(13, 15, "mon-fri")]
                         + [(h, 0, "*") for h in (1, 5, 17, 21)],
    "pilot-editor.yml": [(13, 55, "mon-fri")],
    "pilot-graders.yml": [(17, 0, "mon-fri")],
}


# GitHub's run states for "created, not yet running": `pending` is the
# concurrency hold, `queued` is waiting for a runner.
WAITING_STATUSES = frozenset({"pending", "queued", "waiting", "requested"})


def _headers(tok: str) -> dict:
    return {"Authorization": "Bearer " + tok,
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": "omnibeta-worker-dispatch"}


def has_queued_run(workflow_file: str, tok: str, repo: str) -> bool:
    """True when the workflow already has a run waiting to start.

    A concurrency group holds one running and one queued run; a second
    queued run cancels the first (GitHub's rule, not ours). The hourly
    reader dispatch stacked on a long run does exactly that, and the
    ops record showed 'cancelled' runs that were never failures
    (2026-09-16, 21:00 and 21:56 UTC). Any error answers False so a
    flaky GET never suppresses a dispatch."""
    # No status filter: a run held by the concurrency group is
    # `pending`, not `queued` (review 2026-09-17), and the filter takes
    # one value. The newest five runs cover every waiting state.
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}"
        f"/runs?per_page=5",
        headers=_headers(tok))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
        return any((run.get("status") or "") in WAITING_STATUSES
                   for run in (data or {}).get("workflow_runs") or [])
    except Exception as e:
        log.warning(f"workflow dispatch {workflow_file}: queued-run check failed ({e})")
        return False


def dispatch(workflow_file: str, ref: str = DEFAULT_REF,
             inputs: dict | None = None) -> int:
    """POST a workflow_dispatch. Returns the HTTP status (204 = queued,
    0 = not sent). Never raises: a failed dispatch is logged and, on
    401/403, paged. A workflow with a run already queued is left alone."""
    tok = (settings.github_token or "").strip()
    if not tok:
        log.error("workflow dispatch: GITHUB_TOKEN not set")
        return 0
    repo = settings.github_repo.strip().strip("/")
    if has_queued_run(workflow_file, tok, repo):
        log.info(f"workflow dispatch {workflow_file}: a run is already queued, skipped")
        return 0
    body = {"ref": ref}
    if inputs:
        body["inputs"] = inputs
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        data=json.dumps(body).encode(),
        headers=_headers(tok),
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            status = r.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        log.error(f"workflow dispatch {workflow_file}: {type(e).__name__}: {e}")
        return 0
    if status == 204:
        log.info(f"workflow dispatch {workflow_file}: queued")
    else:
        log.error(f"workflow dispatch {workflow_file}: HTTP {status}")
        if status in (401, 403):
            try:
                from discord_bot.ops_alert import ops_alert_sync
                ops_alert_sync(
                    f"Workflow dispatch for {workflow_file} answered HTTP {status}: "
                    "the worker's GITHUB_TOKEN needs Actions: read and write. "
                    "Pilot jobs are falling back to GitHub's cron.",
                    dedupe_key="workflow-dispatch-auth")
            except Exception as ae:
                log.warning(f"dispatch alert failed: {ae}")
    return status


def register_jobs(scheduler) -> int:
    """Add one APScheduler cron job per (workflow, time), all on UTC
    because the workflow files declare UTC. Returns the count."""
    from apscheduler.triggers.cron import CronTrigger
    import pytz
    n = 0
    for wf, slots in PILOT_WORKFLOWS.items():
        for hour, minute, dow in slots:
            scheduler.add_job(
                dispatch,
                trigger=CronTrigger(day_of_week=dow, hour=hour, minute=minute,
                                    timezone=pytz.utc),
                id=f"dispatch:{wf}:{hour:02d}{minute:02d}:{dow}",
                name=f"Dispatch {wf} at {hour:02d}:{minute:02d} UTC",
                kwargs={"workflow_file": wf},
                max_instances=1,
                misfire_grace_time=600,
            )
            n += 1
    return n
