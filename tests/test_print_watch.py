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


def test_the_print_goes_to_every_alert_channel_and_one_failure_does_not_block_the_rest():
    """PRINT_ALERT_CHANNEL_ID is a list (2026-09-17: the room and the test
    channel side by side). One embed per channel, one ledger entry; a
    channel that fails is logged and the others still post."""
    sent = []

    class _Chan:
        def __init__(self, cid):
            self.cid = cid

        async def send(self, embed=None, **kw):
            if self.cid == 999:
                raise RuntimeError("no access")
            sent.append((self.cid, embed.title))
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan(cid)

    with tempfile.TemporaryDirectory() as td,          patch("config.settings.db_path", str(Path(td) / "reports.db")),          patch("config.settings.print_alert_channel_id", "123, 456,999,123"),          patch("report.print_watch._ff_rows_for_day", return_value=FF),          patch("report.print_watch.fetch_bls", return_value=PW.parse_bls(BLS)),          patch("discord_bot.sender._RETRY_SLEEP_S", 0, create=True),          patch("report.print_watch.datetime") as dt:
        from datetime import datetime as real
        fixed = real(2026, 9, 11, 8, 31, tzinfo=PW._ET)
        dt.now.return_value = fixed
        dt.utcnow.return_value = real(2026, 9, 11, 12, 31)
        dt.strptime = real.strptime
        dt.fromisoformat = real.fromisoformat
        assert PW.alert_channel_ids() == [123, 456, 999]
        asyncio.run(PW.print_watch_job(_Bot(), "08:30"))
        assert sorted(c for c, _ in sent) == [123, 456], sent
        assert PW.already_posted("2026-09-11", "cpi")


# BEA NIPA GetData shape (table 2.8.4, monthly price indexes). Synthetic
# values: the API has no unregistered tier, so no live fixture yet.
def _bea_row(code, desc, period, value):
    return {"TableName": "T20804", "SeriesCode": code, "LineNumber": "1", "LineDescription": desc,
            "TimePeriod": period, "METRIC_NAME": "Fisher Price Index", "CL_UNIT": "Level",
            "UNIT_MULT": "0", "DataValue": value, "NoteRef": "T20804"}


BEA = {"BEAAPI": {"Request": {}, "Results": {"Statistic": "NIPA Table", "Data": [
    _bea_row("DPCERG", "Personal consumption expenditures (PCE)", "2025M08", "121.000"),
    _bea_row("DPCERG", "Personal consumption expenditures (PCE)", "2026M07", "124.500"),
    _bea_row("DPCERG", "Personal consumption expenditures (PCE)", "2026M08", "124.873"),
    _bea_row("DPCCRG", "PCE excluding food and energy", "2025M08", "120.000"),
    _bea_row("DPCCRG", "PCE excluding food and energy", "2026M07", "123.700"),
    _bea_row("DPCCRG", "PCE excluding food and energy", "2026M08", "123.947"),
    _bea_row("DPCERG", "Personal consumption expenditures (PCE)", "2026", "1,000"),
]}}}
FF_PCE = [{"event": "Core PCE Price Index m/m", "country": "US", "time": "2026-09-25T12:30:00",
           "estimate": 0.2, "prev": 0.3, "actual": None, "unit": "%"}]


def test_bea_payload_parses_by_calendar_month_and_skips_annual_rows():
    obs = PW.parse_bea(BEA, ["DPCERG", "DPCCRG"])
    assert obs["DPCERG"][0] == ("2026-08", 124.873)
    assert [p for p, _ in obs["DPCCRG"]] == ["2026-08", "2026-07", "2025-08"]
    assert PW.parse_bea({"BEAAPI": {"Results": {"Error": {"APIErrorCode": "3", "APIErrorDescription": "bad key"}}}}) == {}


def test_pce_lines_match_the_bea_print():
    obs = PW.parse_bea(BEA)
    assert PW.release_ready(PW.PCE, obs, "2026-08")
    lines = PW.build_lines(PW.PCE, obs, "2026-08", FF_PCE)
    assert lines[0] == "**PCE m/m** +0.3%"  # no calendar row and no June index, so no consensus or prior
    assert lines[1].startswith("**PCE y/y** 3.2%")
    assert lines[2] == "**Core PCE m/m** +0.2% · consensus +0.2% · prior +0.3%"
    assert lines[3].startswith("**Core PCE y/y** 3.3%")


def test_pce_is_armed_only_with_a_bea_key():
    with patch("config.settings.bea_api_key", ""):
        assert [s.key for s in PW.due_releases("2026-09-25", FF_PCE, "08:30")] == []
    with patch("config.settings.bea_api_key", "k"):
        assert [s.key for s in PW.due_releases("2026-09-25", FF_PCE, "08:30")] == ["pce"]
    # CPI and jobs never depend on the BEA key
    with patch("config.settings.bea_api_key", ""):
        assert [s.key for s in PW.due_releases("2026-09-11", FF, "08:30")] == ["cpi"]


def test_the_feed_fills_a_core_pce_row_from_bea():
    rows = [dict(FF_PCE[0])]
    with patch("config.settings.bea_api_key", "k"),          patch("report.print_watch._bea_get", return_value=BEA),          patch("report.print_watch.datetime") as dt:
        from datetime import datetime as real
        dt.utcnow.return_value = real(2026, 9, 25, 13, 0)
        dt.strptime = real.strptime
        dt.fromisoformat = real.fromisoformat
        PW._BEA_CACHE.update({"at": None, "key": None, "obs": None})
        out = PW.enrich_rows_with_agency_actuals(rows)
    assert out[0]["actual"] == 0.2 and out[0]["actual_source"] == "bea:DPCCRG" and out[0]["actual_period"] == "2026-08"


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")


# --- a second arming source (2026-09-25) --------------------------------
def test_the_agency_schedule_arms_a_release_the_feed_does_not_list():
    """Arming used to key on the ForexFactory feed alone, by exact event
    name; a gap or a renamed row meant a silent miss."""
    with patch("config.settings.bea_api_key", "k"):
        assert [s.key for s in PW.due_releases("2026-09-30", [], "08:30")] == ["pce"]
    assert [s.key for s in PW.due_releases("2026-10-02", [], "08:30")] == ["jobs"]
    assert [s.key for s in PW.due_releases("2026-10-28", [], "14:00")] == ["fomc"]
    assert PW.due_releases("2026-10-28", [], "08:30") == []
    # a quiet day in both sources stays quiet
    assert PW.due_releases("2026-10-05", [], "08:30") == []


def test_the_schedule_holds_the_published_dates():
    """From bls.gov/schedule, bea.gov/news/schedule and the Fed's FOMC
    calendar, read 2026-09-25. PCE is 9/30 and 10/29, not the 9/25 and
    10/30 I gave from memory."""
    O = PW.OFFICIAL_RELEASES
    assert O["2026-09-30"] == ("pce",) and O["2026-10-29"] == ("pce",)
    assert "2026-09-25" not in O and "2026-10-30" not in O
    assert O["2026-10-14"] == ("cpi",) and O["2026-12-09"] == ("fomc",)
    assert all(k in {"cpi", "jobs", "pce", "fomc"} for ks in O.values() for k in ks)


def test_an_exhausted_schedule_is_flagged():
    assert not PW.official_calendar_exhausted("2026-12-23")
    assert PW.official_calendar_exhausted("2027-01-04")


def test_a_scheduled_release_with_no_key_pings_ops_before_it_is_missed():
    sent = []

    async def fake_ops(text, dedupe_key=""):
        sent.append(text)

    with patch("config.settings.print_alert_channel_id", "1"), \
         patch("config.settings.bea_api_key", ""), \
         patch("report.print_watch._ff_rows_for_day", return_value=[]), \
         patch("report.print_watch.datetime") as dt, \
         patch("discord_bot.ops_alert.ops_alert", side_effect=fake_ops):
        from datetime import datetime as _real
        dt.now.return_value = _real(2026, 9, 30, 8, 29, tzinfo=PW._ET)
        asyncio.run(PW.print_watch_job(bot=object(), release_et="08:30"))
    assert any("PCE" in t and "key is not set" in t for t in sent), sent
