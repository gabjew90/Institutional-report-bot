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


def dispatch(workflow_file: str, ref: str = DEFAULT_REF,
             inputs: dict | None = None) -> int:
    """POST a workflow_dispatch. Returns the HTTP status (204 = queued).
    Never raises: a failed dispatch is logged and, on 401/403, paged."""
    tok = (settings.github_token or "").strip()
    if not tok:
        log.error("workflow dispatch: GITHUB_TOKEN not set")
        return 0
    body = {"ref": ref}
    if inputs:
        body["inputs"] = inputs
    repo = settings.github_repo.strip().strip("/")
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + tok,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "omnibeta-worker-dispatch"},
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
