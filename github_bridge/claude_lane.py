"""HIGH documents analysed by the Claude lane instead of Gemini (2026-10-06).

The cutover switch for phase 1 of docs/superpowers/specs/2026-10-06-
retire-gemini-pdf-pipeline-design.md. Off unless
HIGH_INGESTION_BACKEND=claude_lane.

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


def reachable() -> bool:
    """GitHub is configured. The pull and fallback jobs need only this, so
    documents already handed off are finished even after the switch is
    turned off."""
    return bool(settings.github_token) and bool(settings.github_repo)


def enabled() -> bool:
    """New HIGH documents go to the lane."""
    return (
        settings.high_ingestion_backend == "claude_lane"
        and reachable()
        and bool(getattr(settings, "pilot_publish_enabled", False))
    )


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


def in_pulse_window(now: datetime | None = None) -> bool:
    """Weekdays, from the first pre-pulse sweep until 30 minutes after the
    later pulse time. A note arriving then goes straight to Gemini: the lane
    could not return it before the pulse reads the research."""
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    now = now or datetime.now(timezone.utc)
    if now.astimezone(ZoneInfo(settings.timezone)).weekday() >= 5:
        return False
    times = _pulse_times_utc(now)
    return (min(times) - timedelta(minutes=SWEEP_LEAD_MIN)
            <= now <= max(times) + timedelta(minutes=PULSE_TAIL_MIN))


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
             total_pages: int) -> bool:
    """Give one HIGH document to the lane. False means "run Gemini now":
    the switch is off, or the source text could not be put where the
    lane reads it. Never raises."""
    if not enabled():
        return False
    if in_pulse_window():
        log.info(f"claude lane: {pdf_file_id} arrived in the pulse window; Gemini runs")
        return False
    try:
        from github_bridge import client as gh
        from github_bridge.pilot_publish import meta_path_for, publish_high_document
        branch = _pilot()[0]
        # A document handed off before (a retry) keeps its first folder, so
        # it is never published twice under two dates.
        prior = db.get_bridge_state(pdf_file_id) or {}
        prior_path = prior.get("bridge_filename") or ""
        date = _date_of(prior_path) if prior_path.startswith(_prefix()) else _today()
        published = publish_high_document(
            pdf_file_id=pdf_file_id, file_name=file_name, source=triage.source,
            title=title_from_file_name(file_name), priority="high", published_at=None,
            full_text=full_text, total_pages=total_pages, date=date)
        # False also means "already there" (a retried document); the lane
        # reads it either way.
        if not published and not gh.get_file(meta_path_for(pdf_file_id, date), ref=branch):
            log.warning(f"claude lane: source text for {pdf_file_id} not on {branch}; Gemini runs")
            return False
        db.queue_for_claude_lane(pdf_file_id, analysis_path(pdf_file_id, date),
                                 json.dumps(asdict(triage)))
        db.log_event(pdf_file_id, "claude_lane", "queued",
                     f"HIGH -> Claude lane ({triage.source} / {triage.report_type})")
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
            priority="high",
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
    swept = 0
    for r in db.get_lane_rows("committed", _prefix(), limit=500):
        swept += _to_gemini(r, "pre-pulse sweep")
    out["swept"] = swept
    out["gemini_done"] = await run_fallbacks(limit=500)
    log.info(f"claude lane pre-pulse sweep: {out}")
    return out
