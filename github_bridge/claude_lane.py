"""HIGH and MEDIUM documents analysed by the Claude lane instead of Gemini.

The cutover switches for phases 1 and 2 of docs/superpowers/specs/2026-10-06-
retire-gemini-pdf-pipeline-design.md: HIGH_INGESTION_BACKEND=claude_lane
(2026-10-06) and MEDIUM_INGESTION_BACKEND=claude_lane (2026-10-27, owner).
Each is off unless set.

A MEDIUM note's text goes to source-text-all/ (the graders' folder), never
to source-text/, which is the frozen Omnipulse readers' corpus; its meta
carries "lane": true so the lane reads only the MEDIUM notes handed to it,
not the ones Gemini already analysed.

The worker says what it is waiting for (2026-11-23). Until then the two sides
shared no list: the workflow analysed every note of the last seven days,
oldest id first, 25 a run, while the worker waited for the newest 25 HIGH
notes of the day and sent the rest to Gemini at once on the belief that the
lane would never reach them. Between 11-15 and 11-23 Claude analysed all 960
notes handed to it and the worker loaded 83: 221 notes were sent to Gemini
"past the readers' cap" although their record arrived within two hours, the
53 HIGH notes it did wait for were analysed last and arrived a median eight
hours late, and 432 MEDIUM notes queued behind every HIGH note of the week.
Now pull() publishes pilot/lane/waiting.json, the notes still waiting with
each one's deadline, HIGH first and the nearest deadline first, and starts a
run when the list is not empty. The workflow analyses only what is on that
list (scripts/pilot_analyze.py). The readers' daily cap has nothing to do
with this lane and no longer routes anything.

With it on:

  orchestrator, after triage says HIGH -> hand_off():
    publishes the source text to pilot-data (the lane's input), records a
    bridge_ingestion_state row ('committed', bridge_filename = the record
    path under pilot/analyses/, the triage kept for a fallback), keeps the
    local PDF. pdf_files stays PROCESSING (the stale reaper skips bridge
    rows).
  pull() every 5 min:
    a lane record has arrived -> pdf_analyses row (model = the lane's
    model string), PROCESSED, local PDF deleted;
    the lane gave up (analysis-failures attempts at the cap), or nothing
    arrived within claude_lane_timeout_minutes -> 'fallback_to_gemini'.
  run_fallbacks() every 5 min:
    Gemini deep analysis with the stored triage, exactly as before the
    switch, model tagged as usual; the row ends 'gemini_done'.
  sweep_before_pulse() 30 minutes before the scheduled pulse, weekdays:
    takes what has arrived, sends everything still waiting to Gemini, and
    runs those now, so the pulse never misses a morning note.

The Opus-bridge rows share the table; lane rows are told apart by their
bridge_filename prefix, and the Opus jobs only run when the backend is
"opus_bridge".
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from config import settings
import db

log = logging.getLogger(__name__)

_FALLBACK_LOCK = asyncio.Lock()
FALLBACK_CONCURRENCY = 3


def _pilot():
    from scripts.pilot_config import PILOT_BRANCH, PILOT_ROOT
    from scripts.pilot_analyze import ANALYSES, FAILURES, MAX_ATTEMPTS
    return PILOT_BRANCH, PILOT_ROOT, ANALYSES, FAILURES, MAX_ATTEMPTS


ANALYSIS_WORKFLOW = "pilot-analysis.yml"
# What the last published list held, so an unchanged list is not written
# again. None until this process has read or written it once.
_last_waiting: str | None = None


def waiting_entries(rows: list[dict], timeout_minutes: int) -> list[dict]:
    """The published form of the rows still waiting: HIGH before MEDIUM, then
    the nearest deadline. The deadline is when pull() stops waiting."""
    from datetime import timedelta
    out = []
    for r in rows:
        ts = db._normalize_ts(r.get("committed_at") or r.get("queued_at") or "")
        try:
            t = datetime.fromisoformat((ts or "")[:19]).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out.append({
            "id": int(r["pdf_file_id"]),
            "date": _date_of(r["bridge_filename"]),
            "priority": _priority(r),
            "deadline": (t + timedelta(minutes=timeout_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    out.sort(key=lambda e: (e["priority"] != "high", e["deadline"], e["id"]))
    return out


def publish_waiting(entries: list[dict], now: datetime | None = None) -> bool:
    """Write the list the workflow reads, if it changed. Never raises: with
    no list, or a stale one, the workflow does less and the timeout sends
    the notes to Gemini, as before."""
    global _last_waiting
    from github_bridge import client as gh
    from scripts.pilot_analyze import WAITING
    branch, root = _pilot()[:2]
    path = f"{root}/{WAITING}"
    body = json.dumps(entries, separators=(",", ":"))
    try:
        if _last_waiting is None:
            prior = json.loads(gh.get_file_text(path, ref=branch) or "{}")
            _last_waiting = json.dumps(prior.get("waiting") or [], separators=(",", ":"))
        if body == _last_waiting:
            return False
        now = now or datetime.now(timezone.utc)
        gh.put_file(path, json.dumps({
            "written_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "timeout_minutes": int(settings.claude_lane_timeout_minutes),
            "waiting": entries,
        }, indent=0), f"lane: {len(entries)} note(s) waiting", ref=branch)
        _last_waiting = body
        return True
    except Exception as e:
        log.warning(f"claude lane: waiting list not published: {e}")
        return False


RUN_RETRY_S = 900
_last_run_request = 0.0


def _start_a_run(progress: bool) -> None:
    """Ask for an analysis run. A run already waiting to start is left alone
    (workflow_dispatch.dispatch). While records keep arriving every tick asks,
    so the next run is queued behind the one working and starts when it ends.
    With no record arriving, one request per RUN_RETRY_S: a lane that is down
    (a usage limit, an expired token) fails a run every 15 minutes, not every
    five, until the timeout hands its notes to Gemini."""
    global _last_run_request
    if not getattr(settings, "pilot_dispatch_enabled", False):
        return
    import time
    now = time.monotonic()
    if not progress and _last_run_request and now - _last_run_request < RUN_RETRY_S:
        return
    try:
        from github_bridge.workflow_dispatch import dispatch
        if dispatch(ANALYSIS_WORKFLOW) == 204:
            _last_run_request = now
    except Exception as e:
        log.warning(f"claude lane: could not start a run: {e}")


def reachable() -> bool:
    """GitHub is configured. The pull and fallback jobs need only this, so
    documents already handed off are finished even after the switch is
    turned off."""
    return bool(settings.github_token) and bool(settings.github_repo)


def enabled(priority: str = "high") -> bool:
    """New documents of this priority go to the lane."""
    backend = {"high": settings.high_ingestion_backend,
               "medium": getattr(settings, "medium_ingestion_backend", "gemini")}.get(
        (priority or "").lower())
    return (
        backend == "claude_lane"
        and reachable()
        and bool(getattr(settings, "pilot_publish_enabled", False))
    )


def _priority(row: dict) -> str:
    """The triage priority stored with a lane row; rows from before MEDIUM
    joined the lane are HIGH."""
    try:
        return str(json.loads(row.get("triage_json") or "{}").get("priority") or "high").lower()
    except (ValueError, AttributeError):
        return "high"


# The routine fires on its own UTC cron ("0 14 * * 1-5", CLAUDE.md) while
# the pulse hour in settings is Eastern; in winter the two are an hour
# apart until the routine is moved, so the lane works around both.
ROUTINE_UTC = (14, 0)
SWEEP_LEAD_MIN = 30
PULSE_TAIL_MIN = 30


def _pulse_times_utc(now: datetime) -> list[datetime]:
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo(settings.timezone)).replace(
        hour=settings.daily_pulse_hour, minute=settings.daily_pulse_minute,
        second=0, microsecond=0)
    routine = now.astimezone(timezone.utc).replace(
        hour=ROUTINE_UTC[0], minute=ROUTINE_UTC[1], second=0, microsecond=0)
    return [local.astimezone(timezone.utc), routine]


def _local_date(now: datetime) -> str:
    from zoneinfo import ZoneInfo
    return now.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


def _pulse_still_to_come(now: datetime) -> bool:
    """Whether a scheduled pulse can still read research today: a trading
    day whose scheduled pulse has not been posted. Unknown reads as yes, which
    is the behaviour before this check existed."""
    day = _local_date(now)
    try:
        from world_context import is_trading_day
        if not is_trading_day(day):
            return False
        return not db.daily_pulse_delivered_on(day)
    except Exception as e:
        log.warning(f"claude lane: could not tell whether today's pulse is out: {e}")
        return True


def in_pulse_window(now: datetime | None = None) -> bool:
    """The stretch around the scheduled pulse when a note goes straight to
    Gemini, because the lane could not return it before the pulse reads the
    research: from the first pre-pulse sweep until 30 minutes after the later
    pulse time, on a trading day, and only until the pulse has been posted.

    The last condition is from 2026-11-25. In winter the window ran to 15:30
    UTC (the settings' 10:00 ET) while the routine posts at about 14:15 UTC:
    that day 108 of 271 HIGH and MEDIUM notes, all uploaded at 15:00 UTC,
    went to Gemini for a pulse that was already out, while the lane was
    returning its notes in ten minutes. A market holiday has no pulse at all."""
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    times = _pulse_times_utc(now)
    if not (min(times) - timedelta(minutes=SWEEP_LEAD_MIN)
            <= now <= max(times) + timedelta(minutes=PULSE_TAIL_MIN)):
        return False
    return _pulse_still_to_come(now)


def _prefix() -> str:
    _, root, analyses, _, _ = _pilot()
    return f"{root}/{analyses}/"


def analysis_path(pdf_file_id: int, date: str) -> str:
    return f"{_prefix()}{date}/{int(pdf_file_id)}.json"


def _date_of(path: str) -> str:
    return path.rstrip("/").split("/")[-2]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def title_from_file_name(file_name: str) -> str:
    """A readable title from a PDF's file name, for the pilot meta (the
    Omnipulse editor labels notes by it). Gemini's extracted title is not
    available yet at hand-off. Dropbox names replace ':' '?' and quotes
    with '_': "Rollercoaster_ How August's Seasonal _Gift_ Came" becomes
    "Rollercoaster: How August's Seasonal 'Gift' Came"."""
    t = re.sub(r"\.pdf$", "", (file_name or "").strip(), flags=re.I)
    t = re.sub(r"(^|\s)_([^_]+?)_(?=\s|$)", r"\1'\2'", t)  # _quoted_
    t = re.sub(r"(?<=\w)_(?=\w)", " ", t)  # GS_US_Economics
    t = re.sub(r"_ ", ": ", t)  # colon
    t = t.replace("_", "")  # a trailing ? or '
    return re.sub(r"\s+", " ", t).strip() or (file_name or "")


def hand_off(*, pdf_file_id: int, file_name: str, triage, full_text: str,
             total_pages: int, dropbox_path: str = "") -> bool:
    """Give one HIGH or MEDIUM document to the lane. False means "run
    Gemini now": the switch for its priority is off, or the source text
    could not be put where the lane reads it. Never raises."""
    priority = (getattr(triage, "priority", "") or "high").lower()
    if not enabled(priority):
        return False
    if in_pulse_window():
        log.info(f"claude lane: {pdf_file_id} arrived in the pulse window; Gemini runs")
        return False
    try:
        from github_bridge import client as gh
        from github_bridge.pilot_publish import meta_path_for, publish_high_document
        from scripts.pilot_config import SOURCE_TEXT_ALL_DIR, SOURCE_TEXT_DIR
        branch = _pilot()[0]
        base = SOURCE_TEXT_DIR if priority == "high" else SOURCE_TEXT_ALL_DIR
        # A document handed off before (a retry) keeps its first folder, so
        # it is never published twice under two dates.
        prior = db.get_bridge_state(pdf_file_id) or {}
        prior_path = prior.get("bridge_filename") or ""
        date = _date_of(prior_path) if prior_path.startswith(_prefix()) else _today()
        published = publish_high_document(
            pdf_file_id=pdf_file_id, file_name=file_name, source=triage.source,
            title=title_from_file_name(file_name), priority=priority, published_at=None,
            full_text=full_text, total_pages=total_pages, date=date,
            dropbox_path=dropbox_path,
            # HIGH meta stays as the frozen readers know it
            lane=priority == "medium")
        # False also means "already there" (a retried document); the lane
        # reads it either way.
        if not published and not gh.get_file(meta_path_for(pdf_file_id, date, base), ref=branch):
            log.warning(f"claude lane: source text for {pdf_file_id} not on {branch}; Gemini runs")
            return False
        db.queue_for_claude_lane(pdf_file_id, analysis_path(pdf_file_id, date),
                                 json.dumps(asdict(triage)))
        db.log_event(pdf_file_id, "claude_lane", "queued",
                     f"{priority.upper()} -> Claude lane ({triage.source} / {triage.report_type})")
        log.info(f"claude lane: handed off {pdf_file_id} ({file_name})")
        return True
    except Exception as e:
        log.warning(f"claude lane: hand-off failed for {pdf_file_id}, Gemini runs: {e}")
        return False


def _age_minutes(row: dict, now: datetime) -> float:
    ts = db._normalize_ts(row.get("committed_at") or row.get("queued_at") or "")
    try:
        t = datetime.fromisoformat((ts or "")[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    return (now - t).total_seconds() / 60


def _to_gemini(row: dict, reason: str) -> bool:
    moved = db.move_bridge_row(row["pdf_file_id"], "committed", "fallback_to_gemini", reason)
    if moved:
        log.warning(f"claude lane: {row['pdf_file_id']} -> Gemini ({reason})")
    return moved


def _record_problem(rec, pdf_file_id: int) -> str | None:
    if not isinstance(rec, dict):
        return "record is not an object"
    if int(rec.get("pdf_file_id") or 0) != int(pdf_file_id):
        return f"pdf_file_id {rec.get('pdf_file_id')} != {pdf_file_id}"
    a = rec.get("analysis")
    if not isinstance(a, dict) or not isinstance(a.get("key_insights"), list) or not a["key_insights"]:
        return "no key_insights"
    return None


def _ingest(row: dict) -> bool:
    from github_bridge import client as gh
    pdf_file_id = int(row["pdf_file_id"])
    raw = gh.get_file_text(row["bridge_filename"], ref=_pilot()[0])
    if raw is None:
        return False  # listed but not readable yet; next tick
    try:
        rec = json.loads(raw)
    except ValueError as e:
        _to_gemini(row, f"lane record unreadable: {e}")
        return False
    problem = _record_problem(rec, pdf_file_id)
    if problem:
        _to_gemini(row, f"lane record invalid: {problem}")
        return False
    if not db.move_bridge_row(pdf_file_id, "committed", "completed"):
        return False  # the pre-pulse sweep took it
    triage = json.loads(row.get("triage_json") or "{}")
    a = rec["analysis"]
    try:
        db.insert_analysis(
            pdf_file_id=pdf_file_id,
            triage_json=row.get("triage_json"),
            analysis_json=json.dumps(a),
            priority=str(triage.get("priority") or "high").lower(),
            pages_analyzed=int(a.get("pages_analyzed") or 0),
            total_pages=int(a.get("total_pages") or 0),
            # Gemini's spend only (the triage). The lane runs on the Claude
            # subscription; its usage stays inside analysis_json.
            input_tokens=int(triage.get("input_tokens") or 0),
            output_tokens=int(triage.get("output_tokens") or 0),
            model=str(rec.get("model") or "claude-lane"),
            duration=0.0,
        )
    except Exception as e:
        db.move_bridge_row(pdf_file_id, "completed", "committed")
        log.error(f"claude lane: insert failed for {pdf_file_id}: {e}", exc_info=True)
        return False
    db.update_pdf_status(pdf_file_id, "PROCESSED")
    db.log_event(pdf_file_id, "process", "completed", f"Claude lane ({rec.get('model')})")
    _remove_local(row)
    return True


def _remove_local(row: dict) -> None:
    for p in {row.get("local_path") or "", row.get("_materialized") or ""}:
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass


def _given_up(path: str, max_attempts: int) -> bool:
    from github_bridge import client as gh
    raw = gh.get_file_text(path, ref=_pilot()[0])
    try:
        return int(json.loads(raw or "{}").get("attempts") or 0) >= max_attempts
    except (ValueError, AttributeError):
        return False


def pull(now: datetime | None = None) -> dict:
    """One tick: ingest arrived records, route given-up and overdue
    documents to Gemini. Blocking (GitHub API); run it in a thread."""
    out = {"ingested": 0, "to_gemini": 0, "waiting": 0}
    if not reachable():
        return out
    from github_bridge import client as gh
    branch, root, analyses, failures, max_attempts = _pilot()
    now = now or datetime.now(timezone.utc)
    rows = db.get_lane_rows("committed", _prefix(), limit=500)
    by_date: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_date[_date_of(r["bridge_filename"])].append(r)
    timeout = settings.claude_lane_timeout_minutes
    waiting: list[dict] = []
    for date, group in by_date.items():
        done = {it.get("name") for it in gh.list_dir(f"{root}/{analyses}/{date}", ref=branch)
                if it.get("type") == "file"}
        failed = {it.get("name") for it in gh.list_dir(f"{root}/{failures}/{date}", ref=branch)
                  if it.get("type") == "file"}
        for r in group:
            name = f"{int(r['pdf_file_id'])}.json"
            if name in done:
                if _ingest(r):
                    out["ingested"] += 1
                continue
            if name in failed and _given_up(f"{root}/{failures}/{date}/{name}", max_attempts):
                out["to_gemini"] += _to_gemini(r, f"lane gave up after {max_attempts} attempts")
                continue
            if _age_minutes(r, now) > timeout:
                if _to_gemini(r, f"no lane record after {timeout} min"):
                    out["to_gemini"] += 1
                    try:
                        from discord_bot.ops_alert import ops_alert_sync
                        ops_alert_sync(f"Claude lane: no analysis for pdf {r['pdf_file_id']} "
                                       f"after {timeout} min; Gemini ran instead.",
                                       dedupe_key="claude_lane_timeout")
                    except Exception:
                        pass
                continue
            out["waiting"] += 1
            waiting.append(r)
    publish_waiting(waiting_entries(waiting, timeout), now)
    if waiting:
        _start_a_run(progress=out["ingested"] > 0)
    if out["ingested"] or out["to_gemini"]:
        log.info(f"claude lane pull: {out}")
    return out


async def _gemini_one(row: dict) -> bool:
    from ai_analysis.analyzer import analyze_pdf_deep, triage_pdf
    from ai_analysis.models import TriageResult
    from github_bridge.ingestion import _gather_local_path
    from pdf_processing.extractor import extract_pdf
    pdf_file_id = int(row["pdf_file_id"])
    file_name = row.get("file_name") or ""
    try:
        local_path = await asyncio.to_thread(_gather_local_path, row)
        if not local_path:
            raise RuntimeError("PDF not on disk and Dropbox re-download failed")
        if local_path != (row.get("local_path") or ""):
            row["_materialized"] = local_path
        extraction = await asyncio.to_thread(extract_pdf, local_path, None)
        if row.get("triage_json"):
            triage = TriageResult(**json.loads(row["triage_json"]))
        else:
            folder = (row.get("dropbox_path") or "").rsplit("/", 1)[0]
            triage = await triage_pdf(file_name, extraction.full_text, folder_path=folder)
        analysis = await analyze_pdf_deep(
            pdf_file_id=pdf_file_id, file_name=file_name, extraction=extraction,
            priority=triage.priority, source=triage.source, report_type=triage.report_type)
        # 'gemini_done', not 'completed', so /status can tell the lanes apart
        if not db.move_bridge_row(pdf_file_id, "fallback_to_gemini", "gemini_done"):
            return False
        db.insert_analysis(
            pdf_file_id=pdf_file_id,
            triage_json=json.dumps(asdict(triage)),
            analysis_json=json.dumps(asdict(analysis)),
            priority=triage.priority,
            pages_analyzed=analysis.pages_analyzed,
            total_pages=analysis.total_pages,
            input_tokens=analysis.input_tokens + triage.input_tokens,
            output_tokens=analysis.output_tokens + triage.output_tokens,
            model=settings.gemini_model,
            duration=0.0,
        )
        db.update_pdf_status(pdf_file_id, "PROCESSED")
        db.log_event(pdf_file_id, "process", "completed",
                     f"Gemini after Claude lane ({row.get('fallback_reason') or 'n/a'})")
        _remove_local(row)
        return True
    except Exception as e:
        from ai_analysis.token_budget import BudgetExceeded
        if isinstance(e, BudgetExceeded):
            # stays routed to Gemini; run_fallbacks waits for the reset
            log.warning(f"claude lane: Gemini fallback for {pdf_file_id} waits for the budget reset")
            return False
        log.error(f"claude lane: Gemini fallback failed for {pdf_file_id}: {e}", exc_info=True)
        # FAILED re-enters the normal retry queue, which hands the document
        # to the lane again; a record that arrived meanwhile is then used.
        if db.move_bridge_row(pdf_file_id, "fallback_to_gemini", "failed", str(e)):
            db.update_pdf_status(pdf_file_id, "FAILED", f"Claude lane fallback: {str(e)[:200]}")
        return False


async def run_fallbacks(limit: int = 10) -> int:
    """Run Gemini on lane rows routed to it. One runner at a time, so the
    5-minute job and the pre-pulse sweep never analyse a document twice."""
    if not reachable():
        return 0
    from ai_analysis.token_budget import get_budget
    if get_budget().exhausted():
        return 0        # the rows stay routed to Gemini until the UTC reset
    async with _FALLBACK_LOCK:
        rows = db.get_lane_rows("fallback_to_gemini", _prefix(), limit=limit)
        if not rows:
            return 0
        sem = asyncio.Semaphore(FALLBACK_CONCURRENCY)

        async def one(r):
            async with sem:
                return await _gemini_one(r)

        done = sum(await asyncio.gather(*(one(r) for r in rows)))
        log.info(f"claude lane: Gemini fallback {done}/{len(rows)} done")
        return done


async def sweep_before_pulse() -> dict:
    """Weekdays, 30 minutes before the scheduled pulse: take what the lane
    has finished, then run Gemini now on everything still waiting."""
    if not reachable():
        return {}
    out = await asyncio.to_thread(pull)
    # Two sweeps are scheduled, one for each pulse time (they differ in
    # winter). Once the pulse is out, or on a day with none, the second has
    # nothing to hurry for: it takes what arrived and leaves the rest waiting.
    if not _pulse_still_to_come(datetime.now(timezone.utc)):
        out["swept"] = 0
        log.info(f"claude lane pre-pulse sweep: the pulse is out or none is due, nothing swept: {out}")
        return out
    swept = 0
    for r in db.get_lane_rows("committed", _prefix(), limit=500):
        swept += _to_gemini(r, "pre-pulse sweep")
    out["swept"] = swept
    # Nothing is waiting now: say so, or the workflow keeps analysing notes
    # Gemini is about to finish.
    await asyncio.to_thread(publish_waiting, [])
    out["gemini_done"] = await run_fallbacks(limit=500)
    log.info(f"claude lane pre-pulse sweep: {out}")
    return out
