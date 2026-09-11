"""Economic prints at release (2026-09-11). Fixtures are the real BLS
payload and the real FOMC statement fetched that day."""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from report import print_watch as PW

BLS = json.loads(Path("tests/fixtures/bls_cpi_jobs_2026-09-11.json").read_text(encoding="utf-8"))
FOMC_HTML = Path("tests/fixtures/fomc_statement_2026-07-29.html").read_text(encoding="utf-8")
FF = [
    {"event": "CPI m/m", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 0.4, "prev": 0.1, "actual": None, "unit": "%"},
    {"event": "Core CPI m/m", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 0.2, "prev": 0.2, "actual": None, "unit": "%"},
    {"event": "CPI y/y", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 3.4, "prev": 3.4, "actual": None, "unit": "%"},
    {"event": "Core CPI y/y", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 2.4, "prev": 2.5, "actual": None, "unit": "%"},
]


def test_bls_payload_parses_by_calendar_month():
    obs = PW.parse_bls(BLS)
    assert set(obs) == {"CUSR0000SA0", "CUSR0000SA0L1E", "CUUR0000SA0", "CUUR0000SA0L1E",
                        "CES0000000001", "LNS14000000", "CES0500000003"}
    assert obs["CUUR0000SA0"][0] == ("2026-08", 334.98)
    assert all(p[0][:4] in ("2025", "2026") for p in obs["CUUR0000SA0"])
    assert PW.parse_bls({"status": "REQUEST_NOT_PROCESSED"}) == {}


def test_cpi_lines_match_the_bls_print():
    obs = PW.parse_bls(BLS)
    assert PW.release_ready(PW.CPI, obs, "2026-08")
    assert not PW.release_ready(PW.CPI, obs, "2026-09"), "September is not out"
    lines = PW.build_lines(PW.CPI, obs, "2026-08", FF)
    assert lines[0].startswith("**CPI m/m** +0.4%") and "consensus +0.4%" in lines[0] and "prior +0.1%" in lines[0]
    assert lines[1].startswith("**CPI y/y** 3.4%")
    assert lines[2].startswith("**Core CPI m/m** +0.3%") and "consensus +0.2%" in lines[2]
    assert lines[3].startswith("**Core CPI y/y** 2.4%"), lines[3]


def test_jobs_lines_from_the_same_payload():
    obs = PW.parse_bls(BLS)
    lines = PW.build_lines(PW.JOBS, obs, "2026-08", [])
    assert lines[0].startswith("**Non Farm Payrolls** +162K"), lines[0]
    assert lines[1].startswith("**Unemployment Rate** 4.1%")
    assert lines[2].startswith("**Avg Hourly Earnings m/m** +0.3%")
    assert "prior" in lines[0], "prior falls back to the series when the feed has none"


def test_a_missing_reference_month_is_not_ready_and_computes_nothing():
    obs = PW.parse_bls(BLS)
    july_only = {k: [p for p in v if p[0] != "2026-08"] for k, v in obs.items()}
    assert not PW.release_ready(PW.CPI, july_only, "2026-08")
    assert PW.compute(july_only["CUUR0000SA0"], "yoy", "2026-08") is None
    # a gap in the comparison month yields None rather than a wrong base
    no_aug_2025 = [p for p in obs["CUUR0000SA0"] if p[0] != "2025-08"]
    assert PW.compute(no_aug_2025, "yoy", "2026-08") is None


def test_fomc_statement_parses_range_action_and_vote():
    p = PW.parse_fomc_statement(FOMC_HTML)
    assert p == {"action": "maintain", "low": 3.5, "high": 3.75, "vote": "9-3"}, p
    hike = "The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent."
    assert PW.parse_fomc_statement(hike)["action"] == "raise"
    assert PW.parse_fomc_statement(hike)["high"] == 4.0
    assert PW.parse_fomc_statement("nothing here") is None
    ff = [{"event": "FOMC Interest Rate Decision", "estimate": 3.75, "prev": 3.75}]
    assert PW.fomc_lines(p, ff)[0] == "**Fed holds** · target range 3.50%–3.75% · consensus upper bound 3.75% · vote 9-3"


def test_due_releases_come_from_the_calendar_and_the_slot():
    rows = FF + [{"event": "FOMC Interest Rate Decision", "country": "US", "time": "2026-09-16T18:00:00"}]
    assert [s.key for s in PW.due_releases("2026-09-11", rows)] == ["cpi", "fomc"]
    assert [s.key for s in PW.due_releases("2026-09-11", rows, "08:30")] == ["cpi"]
    assert [s.key for s in PW.due_releases("2026-09-11", rows, "14:00")] == ["fomc"]
    assert PW.due_releases("2026-09-11", [{"event": "Wholesale Inventories m/m"}]) == []


def test_agency_enrichment_fills_the_print_and_only_for_the_reference_month():
    rows = [dict(r) for r in FF] + [
        {"event": "CPI y/y", "country": "US", "time": "2026-10-13T12:30:00", "actual": None},  # future
        {"event": "Wholesale Inventories m/m", "country": "US", "time": "2026-09-11T14:00:00", "actual": None},
    ]
    with patch("report.print_watch.fetch_bls", return_value=PW.parse_bls(BLS)):
        out = PW.enrich_rows_with_agency_actuals(rows)
    assert out[0]["actual"] == 0.4 and out[0]["actual_period"] == "2026-08" and out[0]["actual_source"] == "bls:CUSR0000SA0"
    assert out[2]["actual"] == 3.4 and out[3]["actual"] == 2.4
    assert out[4]["actual"] is None, "a future row is untouched"
    assert out[5]["actual"] is None, "an unmapped event is left for FRED"
    # FRED lagging: the same rows dated a month later would want September, which is absent
    late = [{"event": "CPI y/y", "country": "US", "time": "2026-10-13T12:30:00", "actual": None}]
    with patch("report.print_watch.fetch_bls", return_value=PW.parse_bls(BLS)), \
         patch("report.print_watch.datetime") as dt:
        from datetime import datetime as real
        dt.utcnow.return_value = real(2026, 10, 13, 12, 31)
        dt.fromisoformat = real.fromisoformat
        out = PW.enrich_rows_with_agency_actuals(late)
    assert out[0]["actual"] is None, "August is not the October release's print"


def test_agency_layer_runs_before_fred_in_the_calendar_merge():
    import inspect
    from report import news_data
    src = inspect.getsource(news_data)
    i = src.index("enrich_rows_with_agency_actuals")
    j = src.index("enrich_rows_with_fred_actuals(kept)")
    assert i < j


def test_posted_ledger_dedupes_within_a_day():
    with tempfile.TemporaryDirectory() as td:
        with patch("config.settings.db_path", str(Path(td) / "reports.db")):
            assert not PW.already_posted("2026-09-11", "cpi")
            PW.mark_posted("2026-09-11", "cpi", ["x"])
            assert PW.already_posted("2026-09-11", "cpi")
            assert not PW.already_posted("2026-09-11", "jobs")
            assert not PW.already_posted("2026-09-12", "cpi")


def test_job_posts_once_and_records_it():
    """The watch with everything stubbed: the calendar says CPI today,
    BLS has the month, one embed goes out, the ledger records it, a
    second run is a no-op."""
    sent = []

    class _Chan:
        async def send(self, embed=None, **kw):
            sent.append(embed)
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan()

    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.print_alert_channel_id", "123"), \
         patch("report.print_watch._ff_rows_for_day", return_value=FF), \
         patch("report.print_watch.fetch_bls", return_value=PW.parse_bls(BLS)), \
         patch("report.print_watch.datetime") as dt:
        from datetime import datetime as real
        fixed = real(2026, 9, 11, 8, 31, tzinfo=PW._ET)
        dt.now.return_value = fixed
        dt.utcnow.return_value = real(2026, 9, 11, 12, 31)
        dt.strptime = real.strptime
        dt.fromisoformat = real.fromisoformat
        asyncio.run(PW.print_watch_job(_Bot(), "08:30"))
        assert len(sent) == 1
        assert sent[0].title == "CPI · August 2026"
        assert "**CPI y/y** 3.4%" in sent[0].description
        asyncio.run(PW.print_watch_job(_Bot(), "08:30"))
        assert len(sent) == 1, "second run must not repost"


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
