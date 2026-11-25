"""HIGH cutover switch to the Claude analysis lane (github_bridge/claude_lane.py, 2026-10-06)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import db
from ai_analysis.models import PdfAnalysis, TriageResult
from config import settings
from github_bridge import claude_lane as L
from github_bridge import client as gh

TRIAGE = TriageResult(priority="high", report_type="macro", key_tickers=[], summary="s",
                      source="Goldman Sachs", input_tokens=900, output_tokens=40)


@pytest.fixture
def lane_on(monkeypatch):
    # the suite shares one DB: start every lane test from an empty table
    db.get_connection().execute("DELETE FROM bridge_ingestion_state")
    db.get_connection().commit()
    monkeypatch.setattr(settings, "high_ingestion_backend", "claude_lane")
    monkeypatch.setattr(settings, "github_token", "t")
    monkeypatch.setattr(settings, "github_repo", "o/r")
    monkeypatch.setattr(settings, "pilot_publish_enabled", True)
    monkeypatch.setattr(settings, "pilot_dispatch_enabled", False, raising=False)
    monkeypatch.setattr(L, "in_pulse_window", lambda now=None: False)
    monkeypatch.setattr(L, "_last_waiting", None)
    monkeypatch.setattr(L, "_last_run_request", 0.0)
    # The sweep and the window ask whether a pulse is still due today. Pinned,
    # or these tests would change their answer on a weekend or a holiday.
    monkeypatch.setattr(L, "_pulse_still_to_come", lambda now: True)


class FakeGitHub:
    def __init__(self):
        self.files: dict[str, str] = {}

    def list_dir(self, path, ref=None):
        pre = path.rstrip("/") + "/"
        return [{"type": "file", "name": p[len(pre):]} for p in self.files
                if p.startswith(pre) and "/" not in p[len(pre):]]

    def get_file_text(self, path, ref=None):
        return self.files.get(path)

    def get_file(self, path, ref=None):
        return {"path": path} if path in self.files else None

    def put_file(self, path, content, message, ref=None):
        self.files[path] = content
        self.writes = getattr(self, "writes", 0) + 1
        return {}


@pytest.fixture
def fake_gh(monkeypatch):
    f = FakeGitHub()
    for name in ("list_dir", "get_file_text", "get_file", "put_file"):
        monkeypatch.setattr(gh, name, getattr(f, name))
    return f


def _pdf(tmp_path, name):
    local = tmp_path / name
    local.write_bytes(b"%PDF")
    pid = db.insert_pdf_file(f"/Current/GS/{name}", name, str(local))
    db.update_pdf_status(pid, "PROCESSING")
    return pid, local


def _queue(pid, date="2026-10-06"):
    db.queue_for_claude_lane(pid, L.analysis_path(pid, date), json.dumps(TRIAGE.__dict__))


def _record(pid, **over):
    rec = {"pdf_file_id": pid, "model": "claude-sonnet-5-5", "claude_source": "Goldman Sachs",
           "analysis": {"pdf_file_id": pid, "source": "Goldman Sachs", "key_insights": ["x"],
                        "total_pages": 12, "entities_mentioned": []}}
    rec.update(over)
    return json.dumps(rec)


def _latest(pid):
    return dict(db.get_connection().execute(
        "SELECT * FROM pdf_analyses WHERE pdf_file_id = ? ORDER BY id DESC LIMIT 1", (pid,)
    ).fetchone())


def _status(pid):
    return db.get_connection().execute(
        "SELECT status FROM pdf_files WHERE id = ?", (pid,)).fetchone()[0]


def test_off_by_default_hands_nothing_off():
    assert settings.high_ingestion_backend == "gemini"
    assert L.hand_off(pdf_file_id=1, file_name="x.pdf", triage=TRIAGE,
                      full_text="t", total_pages=1) is False


def test_hand_off_publishes_and_queues(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, _ = _pdf(tmp_path, "handoff.pdf")
    seen = {}
    import github_bridge.pilot_publish as pp
    monkeypatch.setattr(pp, "publish_high_document", lambda **kw: seen.update(kw) or True)
    assert L.hand_off(pdf_file_id=pid, file_name="handoff.pdf", triage=TRIAGE,
                      full_text="text", total_pages=9)
    row = db.get_bridge_state(pid)
    assert row["status"] == "committed"
    assert row["bridge_filename"] == L.analysis_path(pid, seen["date"])
    assert json.loads(row["triage_json"])["source"] == "Goldman Sachs"
    assert seen["priority"] == "high" and seen["total_pages"] == 9


def test_hand_off_falls_back_when_the_text_is_not_published(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, _ = _pdf(tmp_path, "nopub.pdf")
    import github_bridge.pilot_publish as pp
    monkeypatch.setattr(pp, "publish_high_document", lambda **kw: False)
    assert not L.hand_off(pdf_file_id=pid, file_name="nopub.pdf", triage=TRIAGE,
                          full_text="t", total_pages=1)
    assert db.get_bridge_state(pid) is None


def test_an_already_published_document_is_still_handed_off(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, _ = _pdf(tmp_path, "again.pdf")
    import github_bridge.pilot_publish as pp
    monkeypatch.setattr(pp, "publish_high_document", lambda **kw: False)
    fake_gh.files[pp.meta_path_for(pid, L._today())] = "{}"
    assert L.hand_off(pdf_file_id=pid, file_name="again.pdf", triage=TRIAGE,
                      full_text="t", total_pages=1)


def test_pull_ingests_an_arrived_record(tmp_path, lane_on, fake_gh):
    pid, local = _pdf(tmp_path, "arrived.pdf")
    _queue(pid)
    fake_gh.files[L.analysis_path(pid, "2026-10-06")] = _record(pid)
    out = L.pull()
    assert out["ingested"] == 1
    row = _latest(pid)
    assert row["model_used"] == "claude-sonnet-5-5"
    # Gemini's spend only: the triage
    assert (row["input_tokens_used"], row["output_tokens_used"]) == (900, 40)
    assert json.loads(row["triage_json"])["report_type"] == "macro"
    assert _status(pid) == "PROCESSED"
    assert db.get_bridge_state(pid)["status"] == "completed"
    assert not local.exists()


def test_pull_routes_an_invalid_record_to_gemini(tmp_path, lane_on, fake_gh):
    pid, local = _pdf(tmp_path, "invalid.pdf")
    _queue(pid)
    fake_gh.files[L.analysis_path(pid, "2026-10-06")] = _record(pid + 1)
    L.pull()
    assert db.get_bridge_state(pid)["status"] == "fallback_to_gemini"
    assert local.exists()


def test_pull_routes_a_given_up_document_to_gemini(tmp_path, lane_on, fake_gh):
    pid, _ = _pdf(tmp_path, "gaveup.pdf")
    _queue(pid)
    fake_gh.files[f"pilot/analysis-failures/2026-10-06/{pid}.json"] = json.dumps({"attempts": 3})
    L.pull()
    assert "gave up" in db.get_bridge_state(pid)["fallback_reason"]


def test_pull_waits_then_times_out(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, _ = _pdf(tmp_path, "slow.pdf")
    _queue(pid)
    fake_gh.files[f"pilot/analysis-failures/2026-10-06/{pid}.json"] = json.dumps({"attempts": 1})
    assert L.pull()["waiting"] >= 1
    assert db.get_bridge_state(pid)["status"] == "committed"
    pings = []
    import discord_bot.ops_alert as oa
    monkeypatch.setattr(oa, "ops_alert_sync", lambda text, dedupe_key="": pings.append(text))
    db.get_connection().execute(
        "UPDATE bridge_ingestion_state SET committed_at = datetime('now', '-3 hours') "
        "WHERE pdf_file_id = ?", (pid,))
    db.get_connection().commit()
    L.pull()
    assert db.get_bridge_state(pid)["status"] == "fallback_to_gemini"
    assert pings and str(pid) in pings[0]


def test_a_row_moves_once(lane_on):
    pid = db.insert_pdf_file("/Current/GS/cas.pdf", "cas.pdf", "")
    _queue(pid)
    assert db.move_bridge_row(pid, "committed", "completed")
    assert not db.move_bridge_row(pid, "committed", "fallback_to_gemini")


def _stub_gemini(monkeypatch, calls):
    import ai_analysis.analyzer as an
    import github_bridge.ingestion as ing
    import pdf_processing.extractor as ex

    async def deep(**kw):
        calls.append(kw)
        return PdfAnalysis(pdf_file_id=kw["pdf_file_id"], file_name=kw["file_name"],
                           source="Goldman Sachs", title="t", report_type="macro",
                           priority="high", key_insights=["g"], input_tokens=5000,
                           output_tokens=800)

    async def no_triage(*a, **k):
        raise AssertionError("the stored triage must be reused")

    monkeypatch.setattr(an, "analyze_pdf_deep", deep)
    monkeypatch.setattr(an, "triage_pdf", no_triage)
    monkeypatch.setattr(ex, "extract_pdf", lambda *a, **k: SimpleNamespace(full_text="t"))
    monkeypatch.setattr(ing, "_gather_local_path", lambda row: row.get("local_path"))


def test_fallback_runs_gemini_with_the_stored_triage(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, local = _pdf(tmp_path, "fallback.pdf")
    _queue(pid)
    db.move_bridge_row(pid, "committed", "fallback_to_gemini", "test")
    calls = []
    _stub_gemini(monkeypatch, calls)
    assert asyncio.run(L.run_fallbacks()) >= 1
    assert calls[-1]["source"] == "Goldman Sachs" and calls[-1]["report_type"] == "macro"
    row = _latest(pid)
    assert row["model_used"] == settings.gemini_model
    assert row["input_tokens_used"] == 5900
    assert db.get_bridge_state(pid)["status"] == "gemini_done"
    assert _status(pid) == "PROCESSED" and not local.exists()


def test_pre_pulse_sweep_runs_everything_still_waiting(tmp_path, lane_on, fake_gh, monkeypatch):
    done_pid, _ = _pdf(tmp_path, "sweep_done.pdf")
    wait_pid, _ = _pdf(tmp_path, "sweep_wait.pdf")
    _queue(done_pid)
    _queue(wait_pid)
    fake_gh.files[L.analysis_path(done_pid, "2026-10-06")] = _record(done_pid)
    _stub_gemini(monkeypatch, [])
    out = asyncio.run(L.sweep_before_pulse())
    assert out["ingested"] >= 1 and out["swept"] >= 1
    assert db.get_bridge_state(done_pid)["status"] == "completed"
    assert db.get_bridge_state(wait_pid)["status"] == "gemini_done"


def test_orchestrator_hands_high_documents_to_the_lane(tmp_path, lane_on, monkeypatch):
    import pipeline.orchestrator as orch
    pid, local = _pdf(tmp_path, "orch.pdf")

    async def triage(*a, **k):
        return TRIAGE

    async def deep(**kw):
        raise AssertionError("Gemini deep analysis must not run")

    monkeypatch.setattr(orch, "extract_text_per_page",
                        lambda p: [SimpleNamespace(text="page one")])
    monkeypatch.setattr(orch, "triage_pdf", triage)
    monkeypatch.setattr(orch, "analyze_pdf_deep", deep)
    handed = []
    monkeypatch.setattr(L, "hand_off", lambda **kw: handed.append(kw) or True)
    out = asyncio.run(orch.process_single_pdf(
        {"id": pid, "file_name": "orch.pdf", "local_path": str(local),
         "dropbox_path": "/Current/GS/orch.pdf"}))
    assert out is None and handed[0]["total_pages"] == 1
    assert _status(pid) == "PROCESSING" and local.exists()


def test_status_counts_rows_queued_today(lane_on):
    pid = db.insert_pdf_file("/Current/GS/count.pdf", "count.pdf", "")
    _queue(pid)
    from datetime import datetime, timedelta
    cutoff = (datetime.utcnow() - timedelta(hours=1)).isoformat()
    assert db.count_bridge_outcomes_since(cutoff)["committed"] >= 1


def test_a_retried_document_keeps_its_first_folder(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, _ = _pdf(tmp_path, "retry.pdf")
    _queue(pid, date="2026-10-01")
    db.move_bridge_row(pid, "committed", "failed", "boom")
    seen = {}
    import github_bridge.pilot_publish as pp
    monkeypatch.setattr(pp, "publish_high_document", lambda **kw: seen.update(kw) or True)
    assert L.hand_off(pdf_file_id=pid, file_name="retry.pdf", triage=TRIAGE,
                      full_text="t", total_pages=1)
    assert seen["date"] == "2026-10-01"
    assert db.get_bridge_state(pid)["bridge_filename"] == L.analysis_path(pid, "2026-10-01")


def test_switching_off_still_finishes_handed_off_documents(tmp_path, lane_on, fake_gh, monkeypatch):
    pid, _ = _pdf(tmp_path, "switchoff.pdf")
    _queue(pid)
    fake_gh.files[L.analysis_path(pid, "2026-10-06")] = _record(pid)
    monkeypatch.setattr(settings, "high_ingestion_backend", "gemini")
    assert not L.enabled()
    assert L.pull()["ingested"] == 1


def test_notes_arriving_in_the_pulse_window_go_to_gemini(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(settings, "daily_pulse_hour", 10)
    monkeypatch.setattr(settings, "daily_pulse_minute", 0)
    monkeypatch.setattr(settings, "timezone", "America/New_York")
    W = L.in_pulse_window
    delivered = set()
    monkeypatch.setattr(db, "daily_pulse_delivered_on", lambda day: day in delivered)
    summer = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=timezone.utc)  # EDT, Tuesday
    assert not W(summer(13, 20))      # 9:20 ET
    assert W(summer(13, 35))          # 9:35 ET
    assert W(summer(14, 25))          # 10:25 ET
    assert not W(summer(14, 40))      # 10:40 ET
    winter = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=timezone.utc)  # EST
    assert W(winter(13, 35))          # 8:35 ET, before the routine's 14:00 UTC
    assert W(winter(15, 20))          # 10:20 ET, after the 10 ET pulse
    assert not W(datetime(2026, 10, 10, 13, 45, tzinfo=timezone.utc))  # Saturday
    # 2026-11-25: the routine posted at 14:27 UTC and 108 notes uploaded at
    # 15:00 UTC still went to Gemini. Once the pulse is out the window is shut.
    assert W(winter(15, 5))
    delivered.add("2026-11-10")
    assert not W(winter(15, 5)) and not W(winter(13, 45))
    assert W(datetime(2026, 11, 11, 13, 45, tzinfo=timezone.utc))      # the next morning it opens again
    # Thanksgiving: no pulse, so no window.
    assert not W(datetime(2026, 11, 26, 13, 45, tzinfo=timezone.utc))
    # If the database cannot say, the window stays as it was.
    def boom(day):
        raise RuntimeError("locked")
    monkeypatch.setattr(db, "daily_pulse_delivered_on", boom)
    assert W(winter(15, 5))


def test_the_second_sweep_leaves_notes_waiting_once_the_pulse_is_out(tmp_path, lane_on, fake_gh, monkeypatch):
    async def none(limit=10):
        return 0
    monkeypatch.setattr(L, "run_fallbacks", none)
    pid, _ = _pdf(tmp_path, "late.pdf")
    _queue(pid)
    monkeypatch.setattr(L, "_pulse_still_to_come", lambda now: False)
    out = asyncio.run(L.sweep_before_pulse())
    assert out["swept"] == 0 and db.get_bridge_state(pid)["status"] == "committed"
    monkeypatch.setattr(L, "_pulse_still_to_come", lambda now: True)
    out = asyncio.run(L.sweep_before_pulse())
    assert out["swept"] == 1 and db.get_bridge_state(pid)["status"] == "fallback_to_gemini"


@pytest.mark.parametrize("name,title", [
    ("Inside The Jobs Rollercoaster_ How August's Seasonal _Gift_ Came Due.pdf",
     "Inside The Jobs Rollercoaster: How August's Seasonal 'Gift' Came Due"),
    ("Lock And Re-Load_ Rubner Flips Bullish As _The Buyers Are Coming Back_.pdf",
     "Lock And Re-Load: Rubner Flips Bullish As 'The Buyers Are Coming Back'"),
    ("Are These The WTFest ETF Charts You've Ever Seen_.pdf",
     "Are These The WTFest ETF Charts You've Ever Seen"),
    ("GS_US_Economics.pdf", "GS US Economics"),
    ("Meta - Sep 28.pdf", "Meta - Sep 28"),
    ("BTIG - SOX Continue to Track 2000 Analog.PDF", "BTIG - SOX Continue to Track 2000 Analog"),
])
def test_titles_come_from_cleaned_file_names(name, title):
    assert L.title_from_file_name(name) == title


WAITING_PATH = "pilot/lane/waiting.json"


def _listed(fake_gh):
    return json.loads(fake_gh.files[WAITING_PATH])["waiting"]


def test_no_note_is_sent_to_gemini_for_being_past_the_readers_cap(tmp_path, lane_on, fake_gh, monkeypatch):
    """2026-11-23: the readers' 25-a-day cap is another workflow's. Under it
    289 of 420 HIGH notes went to Gemini at once in nine days, and Claude's
    analysis of 221 of them arrived within two hours anyway."""
    import scripts.pilot_list_unread as plu
    monkeypatch.setattr(plu, "MAX_PER_DAY", 2)
    pids = []
    for i, day in enumerate(["Oct 5", "Oct 6", "Oct 6", "Oct 5"]):
        local = tmp_path / f"n{i}.pdf"
        local.write_bytes(b"%PDF")
        pid = db.insert_pdf_file(f"/Current/2026/October/{day}/GS/n{i}.pdf", f"n{i}.pdf", str(local))
        db.update_pdf_status(pid, "PROCESSING")
        _queue(pid)
        pids.append(pid)
    out = L.pull()
    assert out["to_gemini"] == 0 and out["waiting"] == 4
    assert all(db.get_bridge_state(p)["status"] == "committed" for p in pids)
    assert not hasattr(L, "over_reader_cap")


def test_pull_publishes_what_it_waits_for_with_deadlines(tmp_path, lane_on, fake_gh, monkeypatch):
    monkeypatch.setattr(settings, "medium_ingestion_backend", "claude_lane", raising=False)
    h1, _ = _pdf(tmp_path, "h1.pdf")
    m1, _ = _pdf(tmp_path, "m1.pdf")
    h2, _ = _pdf(tmp_path, "h2.pdf")
    done, _ = _pdf(tmp_path, "done.pdf")
    _queue(h1)
    db.queue_for_claude_lane(m1, L.analysis_path(m1, "2026-10-06"), json.dumps(MEDIUM.__dict__))
    _queue(h2)
    _queue(done)
    # h2 was handed over an hour before the others: its deadline is nearest.
    db.get_connection().execute(
        "UPDATE bridge_ingestion_state SET committed_at = datetime('now','-60 minutes') "
        "WHERE pdf_file_id = ?", (h2,))
    db.get_connection().commit()
    fake_gh.files[L.analysis_path(done, "2026-10-06")] = _record(done)
    out = L.pull()
    assert out == {"ingested": 1, "to_gemini": 0, "waiting": 3}
    rows = _listed(fake_gh)
    # HIGH before MEDIUM, nearest deadline first, and the arrived note is off the list.
    assert [r["id"] for r in rows] == [h2, h1, m1]
    assert [r["priority"] for r in rows] == ["high", "high", "medium"]
    assert all(r["date"] == "2026-10-06" for r in rows)
    from datetime import datetime, timedelta, timezone
    d = datetime.strptime(rows[1]["deadline"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    left = d - datetime.now(timezone.utc)
    assert timedelta(minutes=settings.claude_lane_timeout_minutes - 2) < left <= timedelta(
        minutes=settings.claude_lane_timeout_minutes)


def test_an_unchanged_list_is_not_written_again(tmp_path, lane_on, fake_gh):
    pid, _ = _pdf(tmp_path, "a.pdf")
    _queue(pid)
    L.pull()
    assert fake_gh.writes == 1
    L.pull()
    L.pull()
    assert fake_gh.writes == 1
    # A restart forgets what it wrote, reads the branch and still writes nothing.
    L._last_waiting = None
    L.pull()
    assert fake_gh.writes == 1
    fake_gh.files[L.analysis_path(pid, "2026-10-06")] = _record(pid)
    L.pull()
    assert fake_gh.writes == 2 and _listed(fake_gh) == []


def test_a_timed_out_note_leaves_the_list(tmp_path, lane_on, fake_gh, monkeypatch):
    monkeypatch.setattr("discord_bot.ops_alert.ops_alert_sync", lambda *a, **k: None)
    pid, _ = _pdf(tmp_path, "late.pdf")
    _queue(pid)
    L.pull()
    assert [r["id"] for r in _listed(fake_gh)] == [pid]
    db.get_connection().execute(
        "UPDATE bridge_ingestion_state SET committed_at = datetime('now','-3 hours')")
    db.get_connection().commit()
    assert L.pull()["to_gemini"] == 1
    assert _listed(fake_gh) == []


def test_a_failed_write_of_the_list_does_not_stop_the_pull(tmp_path, lane_on, fake_gh, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("GitHub 502")
    monkeypatch.setattr(gh, "put_file", boom)
    pid, _ = _pdf(tmp_path, "a.pdf")
    _queue(pid)
    assert L.pull()["waiting"] == 1
    assert WAITING_PATH not in fake_gh.files
    monkeypatch.setattr(gh, "put_file", fake_gh.put_file)
    L.pull()                                  # and the next tick writes it
    assert [r["id"] for r in _listed(fake_gh)] == [pid]


def test_pull_starts_a_run_only_when_notes_are_waiting(tmp_path, lane_on, fake_gh, monkeypatch):
    from github_bridge import workflow_dispatch as W
    started = []
    monkeypatch.setattr(W, "dispatch", lambda wf, **k: started.append(wf) or 204)
    L.pull()
    assert started == []                      # nothing handed over
    pid, _ = _pdf(tmp_path, "a.pdf")
    _queue(pid)
    L.pull()
    assert started == []                      # dispatch is off (PILOT_DISPATCH_ENABLED)
    monkeypatch.setattr(settings, "pilot_dispatch_enabled", True, raising=False)
    L.pull()
    assert started == ["pilot-analysis.yml"]
    L.pull()
    assert started == ["pilot-analysis.yml"]  # no record since: wait before asking again
    other, _ = _pdf(tmp_path, "b.pdf")
    _queue(other)
    fake_gh.files[L.analysis_path(pid, "2026-10-06")] = _record(pid)
    L.pull()
    assert len(started) == 2                  # records are arriving: queue the next run
    monkeypatch.setattr(L, "_last_run_request", L._last_run_request - L.RUN_RETRY_S - 1)
    L.pull()
    assert len(started) == 3                  # quiet for 15 minutes: ask again
    fake_gh.files[L.analysis_path(other, "2026-10-06")] = _record(other)
    L.pull()
    assert len(started) == 3                  # ingested, nothing waiting, no new run


def test_the_sweep_empties_the_list(tmp_path, lane_on, fake_gh, monkeypatch):
    async def none(limit=10):
        return 0
    monkeypatch.setattr(L, "run_fallbacks", none)
    pid, _ = _pdf(tmp_path, "a.pdf")
    _queue(pid)
    L.pull()
    assert len(_listed(fake_gh)) == 1
    out = asyncio.run(L.sweep_before_pulse())
    assert out["swept"] == 1 and _listed(fake_gh) == []


# --- MEDIUM through the lane (2026-10-27, owner) -------------------------

MEDIUM = TriageResult(priority="medium", report_type="equity_research", key_tickers=[],
                      summary="s", source="UBS", input_tokens=800, output_tokens=30)


def test_medium_stays_on_gemini_until_its_own_switch(tmp_path, lane_on, fake_gh, monkeypatch):
    assert settings.medium_ingestion_backend == "gemini"
    pid, _ = _pdf(tmp_path, "med-off.pdf")
    assert not L.hand_off(pdf_file_id=pid, file_name="med-off.pdf", triage=MEDIUM,
                          full_text="t", total_pages=1)
    assert db.get_bridge_state(pid) is None


def test_medium_goes_to_the_graders_folder_marked_for_the_lane(tmp_path, lane_on, fake_gh, monkeypatch):
    """Never source-text/: that is the frozen readers' corpus."""
    monkeypatch.setattr(settings, "medium_ingestion_backend", "claude_lane")
    monkeypatch.setattr(settings, "high_ingestion_backend", "gemini")
    pid, _ = _pdf(tmp_path, "med.pdf")
    seen = {}
    import github_bridge.pilot_publish as pp
    monkeypatch.setattr(pp, "publish_high_document", lambda **kw: seen.update(kw) or True)
    assert L.hand_off(pdf_file_id=pid, file_name="med.pdf", triage=MEDIUM,
                      full_text="text", total_pages=4)
    assert seen["priority"] == "medium" and seen["lane"] is True
    assert db.get_bridge_state(pid)["status"] == "committed"
    # HIGH is off here, and its own switch decides it
    assert not L.hand_off(pdf_file_id=pid + 1000, file_name="h.pdf", triage=TRIAGE,
                          full_text="t", total_pages=1)


def test_an_already_published_medium_is_checked_in_its_own_folder(tmp_path, lane_on, fake_gh, monkeypatch):
    monkeypatch.setattr(settings, "medium_ingestion_backend", "claude_lane")
    pid, _ = _pdf(tmp_path, "med-again.pdf")
    import github_bridge.pilot_publish as pp
    from scripts.pilot_config import SOURCE_TEXT_ALL_DIR
    monkeypatch.setattr(pp, "publish_high_document", lambda **kw: False)
    fake_gh.files[pp.meta_path_for(pid, L._today(), SOURCE_TEXT_ALL_DIR)] = "{}"
    assert L.hand_off(pdf_file_id=pid, file_name="med-again.pdf", triage=MEDIUM,
                      full_text="t", total_pages=1)


def test_a_medium_record_is_stored_as_medium(tmp_path, lane_on, fake_gh):
    pid, _ = _pdf(tmp_path, "med-arrived.pdf")
    db.queue_for_claude_lane(pid, L.analysis_path(pid, "2026-10-06"), json.dumps(MEDIUM.__dict__))
    fake_gh.files[L.analysis_path(pid, "2026-10-06")] = _record(pid)
    assert L.pull()["ingested"] == 1
    assert _latest(pid)["priority"] == "medium"


def test_orchestrator_offers_medium_documents_to_the_lane(tmp_path, lane_on, monkeypatch):
    import pipeline.orchestrator as orch
    pid, local = _pdf(tmp_path, "orch-med.pdf")

    async def triage(*a, **k):
        return MEDIUM

    async def deep(**kw):
        raise AssertionError("Gemini deep analysis must not run")

    monkeypatch.setattr(orch, "extract_text_per_page",
                        lambda p: [SimpleNamespace(text="page one")])
    monkeypatch.setattr(orch, "triage_pdf", triage)
    monkeypatch.setattr(orch, "analyze_pdf_deep", deep)
    handed = []
    monkeypatch.setattr(L, "hand_off", lambda **kw: handed.append(kw) or True)
    out = asyncio.run(orch.process_single_pdf(
        {"id": pid, "file_name": "orch-med.pdf", "local_path": str(local),
         "dropbox_path": "/Current/UBS/orch-med.pdf"}))
    assert out is None and handed[0]["triage"].priority == "medium"
