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
    monkeypatch.setattr(L, "in_pulse_window", lambda now=None: False)


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


@pytest.fixture
def fake_gh(monkeypatch):
    f = FakeGitHub()
    for name in ("list_dir", "get_file_text", "get_file"):
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
    summer = lambda h, m: datetime(2026, 10, 6, h, m, tzinfo=timezone.utc)  # EDT, Tuesday
    assert not W(summer(13, 20))      # 9:20 ET
    assert W(summer(13, 35))          # 9:35 ET
    assert W(summer(14, 25))          # 10:25 ET
    assert not W(summer(14, 40))      # 10:40 ET
    winter = lambda h, m: datetime(2026, 11, 10, h, m, tzinfo=timezone.utc)  # EST
    assert W(winter(13, 35))          # 8:35 ET, before the routine's 14:00 UTC
    assert W(winter(15, 20))          # 10:20 ET, after the 10 ET pulse
    assert not W(datetime(2026, 10, 10, 13, 45, tzinfo=timezone.utc))  # Saturday


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
