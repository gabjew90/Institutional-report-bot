"""Smoke: 2026-07-02 stale-context incident — three-layer fix.

The 9 AM pulse told readers to WATCH the 8:30 payrolls print at 9:06.
Root causes, each with its own layer:

#1 The bridge dump job HUNG at 09:25 UTC (one stuck call; APScheduler
   max_instances=1 silently skipped every subsequent tick: "maximum
   number of running instances reached") so the pulse consumed 4-hour-
   stale context. Fix: dump_context_job is now a 10-min watchdog wrapper
   — on timeout the stuck worker is abandoned and the slot FREES.

#2 A calendar event past its scheduled time with no ingested actual sat
   under ALREADY RELEASED with only an est= — indistinguishable from a
   data gap. Fix: explicit 'PRINTED — actual not ingested yet' marker.

#3 DRAFT's {now} comes from the SNAPSHOT, so a print that lands between
   dump and fire is 'upcoming' from its point of view. Fix: routine
   STEP 2.5 computes a binding [PRESS-TIME NOTE] at fire time; DRAFT_USER
   anchors it as overriding the calendar split.
"""

import inspect
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_dump_job_watchdogged():
    import github_bridge.jobs as jobs
    src = inspect.getsource(jobs.dump_context_job)
    assert "ThreadPoolExecutor" in src, "watchdog executor missing"
    assert "timeout=600" in src, "10-min wall clock missing"
    assert "shutdown(wait=False)" in src, "must never wait on a stuck worker"
    assert "log.critical" in src, "watchdog trip must be loud"
    # the real body moved intact
    inner = inspect.getsource(jobs._dump_context_job_inner)
    assert "bridge_enabled()" in inner and "put_file(CONTEXT_PATH" in inner, \
        "job body must live in _dump_context_job_inner"
    _ok("dump job: 10-min watchdog wrapper, slot frees on hang")


def test_calendar_marks_printed_pending():
    import report.news_data as nd
    now = datetime.utcnow()
    past = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    future = (now + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    events = [
        {"event": "Non Farm Payrolls", "country": "US", "impact": "high",
         "time": past, "estimate": 114, "prev": 172, "actual": None,
         "unit": "K"},
        {"event": "ISM Manufacturing PMI", "country": "US", "impact": "high",
         "time": future, "estimate": 53.8, "prev": 54.0, "actual": None,
         "unit": ""},
    ]
    with patch.object(nd, "_fetch_economic_events_raw",
                      return_value=(events, "finnhub")):
        out = nd.fetch_economic_calendar()
    assert "PRINTED — actual not ingested yet" in out, out
    assert "NEVER as upcoming" in out, "marker must carry the framing directive"
    # the printed-pending row sits under RELEASED, the future one under UPCOMING
    released_part = out.split("STILL UPCOMING", 1)[0]
    assert "Non Farm Payrolls" in released_part, out
    upcoming_part = out.split("STILL UPCOMING", 1)[1]
    assert "ISM Manufacturing" in upcoming_part, out
    assert "PRINTED — actual not ingested" not in upcoming_part, \
        "future events must not get the printed marker"
    _ok("calendar: past-no-actual → 'PRINTED — actual not ingested yet' marker")


def test_routine_has_press_time_check():
    md = open("docs/superpowers/routines/synthesis-routine.md",
              encoding="utf-8").read()
    assert "STEP 2.5 — Press-time freshness check" in md, "STEP 2.5 missing"
    assert "press_time_note.txt" in md, "note file missing from routine"
    assert "dumped < sched_utc <= now" in md, \
        "straddled-event detection (dump < scheduled <= fire) missing"
    assert md.count("press_time_note.txt") >= 2, \
        "the note must also be wired into the DRAFT dispatch step"
    _ok("routine: STEP 2.5 press-time check + STEP 4 wiring present")


def test_draft_prompt_honors_note():
    from ai_analysis import prompts
    assert "[PRESS-TIME NOTE]" in prompts.DRAFT_USER, \
        "DRAFT must anchor the press-time note"
    assert "BINDING and overrides {now}" in prompts.DRAFT_USER, \
        "the note must override the snapshot's {now}"
    assert "number still propagating at press time" in prompts.DRAFT_USER, \
        "the printed-not-ingested framing must be spelled out"
    _ok("DRAFT prompt: press-time note is binding, overrides the snapshot clock")


if __name__ == "__main__":
    print("=== press-time / stale-context smoke ===")
    test_dump_job_watchdogged()
    test_calendar_marks_printed_pending()
    test_routine_has_press_time_check()
    test_draft_prompt_honors_note()
    print("\nALL PRESS-TIME SMOKE TESTS PASS")
