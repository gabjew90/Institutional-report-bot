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


def test_cpi_rows_match_the_bls_print():
    obs = PW.parse_bls(BLS)
    assert PW.release_ready(PW.CPI, obs, "2026-08")
    assert not PW.release_ready(PW.CPI, obs, "2026-09"), "September is not out"
    rows = {r["label"]: r for r in PW.build_rows(PW.CPI, obs, "2026-08", FF)}
    assert (rows["Core CPI m/m"]["actual"], rows["Core CPI m/m"]["consensus"]) == ("+0.3%", "+0.2%")
    assert rows["Core CPI y/y"]["actual"] == "2.4%"
    assert (rows["CPI m/m"]["actual"], rows["CPI m/m"]["consensus"], rows["CPI m/m"]["prior"]) == ("+0.4%", "+0.4%", "+0.1%")
    assert rows["CPI y/y"]["actual"] == "3.4%"


def test_jobs_rows_from_the_same_payload():
    obs = PW.parse_bls(BLS)
    rows = {r["label"]: r for r in PW.build_rows(PW.JOBS, obs, "2026-08", [])}
    assert rows["Non Farm Payrolls"]["actual"] == "+162K"
    assert rows["Unemployment Rate"]["actual"] == "4.1%"
    assert rows["Avg Hourly Earnings m/m"]["actual"] == "+0.3%"
    assert rows["Non Farm Payrolls"]["prior"], "prior falls back to the series when the feed has none"


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
    text = p.pop("text")
    assert p == {"action": "maintain", "low": 3.5, "high": 3.75, "size": None, "vote": "9-3"}, p
    # the statement body alone, not the page (the ledger diffs it next meeting)
    assert text.startswith("The Federal Open Market Committee approved the following statement"), text[:120]
    assert "target range for the federal funds rate" in text and "<" not in text
    assert "Skip to main content" not in text and "﻿" not in text
    assert "For media inquiries" not in text and "Implementation Note" not in text
    assert text.endswith("at this meeting."), text[-80:]
    assert len(text) < 4000, len(text)
    hike = "The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent."
    assert PW.parse_fomc_statement(hike)["action"] == "raise"
    assert PW.parse_fomc_statement(hike)["high"] == 4.0
    assert PW.parse_fomc_statement("nothing here") is None


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
    BLS has the month, one alert goes out, the ledger records it, a
    second run is a no-op."""
    sent = []

    class _Chan:
        async def send(self, content=None, embed=None, **kw):
            sent.append((content, embed))
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
        content, embed = sent[0]
        assert content == "@everyone Aug CPI: Hotter than expected"
        lines = embed.description.split("\n")
        assert lines[0].startswith("Core CPI MoM +0.3% vs +0.2% expected (+0.")
        assert "Headline CPI YoY 3.4% vs 3.4% expected" in lines
        assert lines[-1].startswith("Takeaway: Core inflation rose more than expected.")
        assert embed.footer.text == "BLS · forecast: ForexFactory · released 8:30 AM ET"
        ledger = json.loads((Path(td) / "print-alerts" / "2026-09-11.json").read_text(encoding="utf-8"))
        assert ledger["cpi"]["rows"][0] == {"label": "Core CPI m/m", "actual_value": 0.3, "period": "2026-08"}
        assert ledger["cpi"]["lines"] == lines
        assert ledger["cpi"]["content"] == "Aug CPI: Hotter than expected"
        assert ledger["cpi"]["verdict"]["direction"] == "hawkish"
        assert "statement" not in ledger["cpi"]
        asyncio.run(PW.print_watch_job(_Bot(), "08:30"))
        assert len(sent) == 1, "second run must not repost"


def test_fomc_job_posts_the_decision_with_the_statement_changes():
    """The 14:00 slot on a scheduled FOMC day (OFFICIAL_RELEASES lists
    2026-10-28): the statement is the real 7/29 fixture, the ledger holds
    a previous decision whose statement differs by one sentence."""
    sent = []

    class _Chan:
        async def send(self, content=None, embed=None, **kw):
            sent.append((content, embed))
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan()

    parsed = PW.parse_fomc_statement(FOMC_HTML)
    kept = "The Committee is continuing its policy of maintaining ample reserves in the banking system."
    assert kept in parsed["text"]
    old = "The Committee is continuing to reduce its holdings of Treasury securities."
    prev_text = parsed["text"].replace(kept, old)
    ff = [{"event": "FOMC Interest Rate Decision", "country": "US", "time": "2026-10-28T18:00:00",
           "estimate": 3.75, "prev": 3.75, "actual": None, "unit": "%"}]
    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.print_alert_channel_id", "123"), \
         patch("report.print_watch._ff_rows_for_day", return_value=ff), \
         patch("report.print_watch.fetch_fomc_today", return_value=dict(parsed)), \
         patch("report.print_watch.datetime") as dt:
        from datetime import datetime as real
        dt.now.return_value = real(2026, 10, 28, 14, 1, tzinfo=PW._ET)
        dt.utcnow.return_value = real(2026, 10, 28, 18, 1)
        dt.strptime = real.strptime
        dt.fromisoformat = real.fromisoformat
        PW.mark_posted("2026-09-17", "fomc", ["Fed holds · vote 9-3"], statement=prev_text)
        asyncio.run(PW.print_watch_job(_Bot(), "14:00"))
        assert len(sent) == 1
        content, embed = sent[0]
        # the same three dissents as last time: no change, so as expected
        assert content == "@everyone Oct FOMC: Hold as expected"
        lines = embed.description.split("\n")
        assert lines[:3] == ["Decision: Hold, as expected", "Target range: 3.50% to 3.75%", "Vote: 9-3"]
        assert "Dissent: Hammack, Kashkari and Logan wanted a quarter-point hike" in lines
        assert lines[lines.index("Statement changes:") + 1:][:2] == [f"Was: {old}", f"Now: {kept}"]
        assert not any("Skip to main content" in ln for ln in lines)
        assert embed.footer.text == "Federal Reserve statement · released 2:00 PM ET"
        ledger = json.loads((Path(td) / "print-alerts" / "2026-10-28.json").read_text(encoding="utf-8"))
        assert ledger["fomc"]["statement"] == parsed["text"]
        assert ledger["fomc"]["rows"] == [{"label": "Target range", "actual_value": 3.75, "period": "2026-10"}]
        assert ledger["fomc"]["lines"] == lines
        assert ledger["fomc"]["action"] == "hold" and ledger["fomc"]["dissent_net"] == 3


def test_no_model_is_called_on_the_release_path():
    """2026-10-28: the takeaway is templated. The module that called
    Gemini is gone and nothing in the watch builds a client."""
    import inspect
    from report import print_data, print_render, print_templates, print_verdict
    for mod in (PW, print_data, print_render, print_templates, print_verdict):
        src = inspect.getsource(mod)
        assert "make_client" not in src and "genai" not in src, mod.__name__
    assert not Path("report/print_takeaway.py").exists()


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
            sent.append((self.cid, kw.get("content")))
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan(cid)

    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.print_alert_channel_id", "123, 456,999,123"), \
         patch("report.print_watch._ff_rows_for_day", return_value=FF), \
         patch("report.print_watch.fetch_bls", return_value=PW.parse_bls(BLS)), \
         patch("discord_bot.sender._RETRY_SLEEP_S", 0, create=True), \
         patch("report.print_watch.datetime") as dt:
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


def test_pce_rows_match_the_bea_print():
    obs = PW.parse_bea(BEA)
    assert PW.release_ready(PW.PCE, obs, "2026-08")
    rows = {r["label"]: r for r in PW.build_rows(PW.PCE, obs, "2026-08", FF_PCE)}
    core = rows["Core PCE m/m"]
    assert (core["actual"], core["consensus"], core["prior"]) == ("+0.2%", "+0.2%", "+0.3%")
    assert rows["Core PCE y/y"]["actual"] == "3.3%"
    # no calendar row and no June index, so no consensus or prior
    assert (rows["PCE m/m"]["actual"], rows["PCE m/m"]["consensus"], rows["PCE m/m"]["prior"]) == ("+0.3%", None, None)
    assert rows["PCE y/y"]["actual"] == "3.2%"


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
        PW._BEA_CACHE.clear()
        out = PW.enrich_rows_with_agency_actuals(rows)
    assert out[0]["actual"] == 0.2 and out[0]["actual_source"] == "bea:DPCCRG" and out[0]["actual_period"] == "2026-08"


def test_specs_carry_the_extra_series_as_optional_lines():
    core = [ln for ln in PW.PCE.lines if not ln.optional]
    extra = [ln for ln in PW.PCE.lines if ln.optional]
    assert [ln.series for ln in core] == ["DPCCRG", "DPCCRG", "DPCERG", "DPCERG"]
    assert {ln.series for ln in extra} == {"DPCERX", "DPCERC", "A065RC", "A072RC"}
    assert {ln.table for ln in extra} == {"T20806", "T20600"}
    assert {ln.series for ln in PW.CPI.lines if ln.optional} == {"CUSR0000SAH1", "CUSR0000SA0E", "CUSR0000SAF1"}
    assert {ln.series for ln in PW.JOBS.lines if ln.optional} == {"LNS11300000", "CES0500000002",
                                                                   "LNS13000000", "LNS11000000"}
    assert {ln.series for ln in PW.JOBS.lines if ln.hidden} == {"LNS13000000", "LNS11000000"}
    assert {ln.series for ln in PW.PPI.lines if ln.optional} == {"WPSFD49116"}
    assert [s.name for s in PW.SPECS] == ["CPI", "Jobs", "FOMC", "PCE", "PPI"]


def test_release_ready_ignores_optional_lines_and_build_rows_drops_them_when_absent():
    obs = PW.parse_bls(BLS)      # the fixture has no shelter/energy/food series
    assert PW.release_ready(PW.CPI, obs, "2026-08")
    rows = PW.build_rows(PW.CPI, obs, "2026-08", FF)
    assert [r["label"] for r in rows] == ["Core CPI m/m", "Core CPI y/y", "CPI m/m", "CPI y/y"]
    assert rows[0]["display"] == "Core CPI MoM" and rows[2]["display"] == "Headline CPI MoM"
    assert rows[0]["actual_value"] == 0.3 and rows[0]["consensus_value"] == 0.2


def test_bea_fetch_groups_series_by_table(monkeypatch):
    calls = []

    def fake_get(table, years):
        calls.append(table)
        return {"BEAAPI": {"Results": {"Data": []}}}
    monkeypatch.setattr(PW, "_bea_get", fake_get)
    monkeypatch.setattr("config.settings.bea_api_key", "k")
    PW._BEA_CACHE.clear()
    PW.fetch_observations(PW.PCE.lines, force=True)
    assert sorted(calls) == ["T20600", "T20804", "T20806"]


def test_bea_cache_holds_one_entry_per_table(monkeypatch):
    calls = []

    def fake_get(table, years):
        calls.append(table)
        rows = [{"SeriesCode": ln.series, "TimePeriod": "2026M08", "DataValue": "1.0"}
                for ln in PW.PCE.lines if (ln.table or PW.BEA_PCE_TABLE) == table]
        return {"BEAAPI": {"Results": {"Data": rows}}}
    monkeypatch.setattr(PW, "_bea_get", fake_get)
    monkeypatch.setattr("config.settings.bea_api_key", "k")
    PW._BEA_CACHE.clear()
    PW.fetch_observations(PW.PCE.lines)
    PW.fetch_observations(PW.PCE.lines)
    assert sorted(calls) == ["T20600", "T20804", "T20806"], "one request per table, not one per call"


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
    # bls.gov/schedule/news_release/ppi.htm, read 2026-10-28
    assert O["2026-11-13"] == ("ppi",) and O["2026-12-15"] == ("ppi",)
    assert all(k in {"cpi", "jobs", "pce", "ppi", "fomc", "ism_manufacturing", "ism_services"}
               for ks in O.values() for k in ks)


def test_ism_dates_on_the_schedule_never_arm_the_watch():
    """The ISM keys are for the omni-calendar; the watch has no spec for
    them and must not arm or ping on an ISM-only day."""
    assert PW.due_releases("2026-10-05", [], "08:30") == []
    assert PW.due_releases("2026-10-05", [], "13:59") == []


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


# --- the ledger remembers the print, the next print compares (2026-09-30) ---
def test_ledger_keeps_rows_and_statement_and_finds_the_previous_post():
    with tempfile.TemporaryDirectory() as td, patch("config.settings.db_path", str(Path(td) / "reports.db")):
        PW.mark_posted("2026-09-11", "cpi", ["x"], rows=[{"label": "CPI m/m", "actual_value": 0.4}])
        PW.mark_posted("2026-09-17", "fomc", ["y"], statement="The Committee decided to maintain.")
        assert PW.previous_post("cpi", before="2026-10-15")["rows"][0]["actual_value"] == 0.4
        assert PW.previous_post("cpi", before="2026-09-11") is None, "same day is not previous"
        assert PW.previous_post("fomc", before="2026-10-29")["statement"].startswith("The Committee")
        assert PW.previous_post("pce", before="2026-10-29") is None


def test_without_freds_archive_the_ledger_gives_last_months_revision():
    from report import print_data as PD
    obs = PW.parse_bls(BLS)          # August +162K; July is whatever the payload now says
    july_now = PW.compute(obs["CES0000000001"], "m_change_k", "2026-07")
    prev = {"rows": [{"label": "Non Farm Payrolls", "actual_value": july_now + 20, "period": "2026-07"}]}
    rev = PD.payroll_revisions(PW.JOBS, obs, "2026-08", None, prev)
    assert rev == {"months": [{"period": "2026-07", "now": july_now, "was": july_now + 20}], "net": -20}
    same = {"rows": [{"label": "Non Farm Payrolls", "actual_value": july_now, "period": "2026-07"}]}
    assert PD.payroll_revisions(PW.JOBS, obs, "2026-08", None, same) is None
    assert PD.payroll_revisions(PW.JOBS, obs, "2026-08", None, None) is None


def test_fomc_release_reads_the_expected_move_from_the_feed():
    from report import print_data as PD
    parsed = PW.parse_fomc_statement(FOMC_HTML)     # maintain, 3.50 to 3.75, 9-3
    rel = PD.fomc_release(parsed, [{"event": "FOMC Interest Rate Decision", "estimate": 3.75}], None, "2026-07-29")
    f = rel.fomc
    assert (f["action"], f["prior_high"], f["expected"], f["expected_action"]) == ("hold", 3.75, 3.75, "hold")
    assert rel.rows["Target range"]["actual_value"] == 3.75 and rel.period == "2026-07"
    cut = PD.fomc_release(parsed, [{"event": "FOMC Interest Rate Decision", "estimate": 3.5}], None, "2026-07-29")
    assert cut.fomc["expected_action"] == "cut"
    bare = PD.fomc_release(parsed, [], None, "2026-07-29")
    assert bare.fomc["expected"] is None and bare.fomc["expected_action"] is None
    assert bare.fomc["prev_dissent_net"] is None and bare.fomc["dissent_net"] == 3


def test_sentences_split_around_abbreviations_and_initials():
    abbr = "Growth in the U.S. economy slowed. For release at 2:00 p.m. EDT. Voting for were Stephen I. Miran and Lisa D. Cook."
    assert PW._sentences(abbr) == ["Growth in the U.S. economy slowed.", "For release at 2:00 p.m. EDT.",
                                   "Voting for were Stephen I. Miran and Lisa D. Cook."]


def test_ppi_fills_the_calendar_row_and_arms_the_watch():
    """2026-10-18 audit A3/P1: PPI printed 10/15 at 8:30 and the bot said it
    was not out at 8:32, because no agency series filled the row. The
    September 2026 index values are from the BLS API; the 2025 bases are
    set to give the published year-over-year rates."""
    obs = {
        "WPSFD4": [("2026-09", 159.780), ("2026-08", 157.662)],
        "WPUFD4": [("2026-09", 159.613), ("2025-09", 150.162)],
        "WPSFD49104": [("2026-09", 156.172), ("2026-08", 155.110)],
        "WPUFD49104": [("2026-09", 155.924), ("2025-09", 148.540)],
    }
    rows = [{"event": e, "country": "US", "time": "2026-10-15T12:30:00", "actual": None}
            for e in ("PPI m/m", "Core PPI m/m", "PPI y/y", "Core PPI y/y")]
    with patch("report.print_watch.fetch_observations", return_value=obs):
        out = PW.enrich_rows_with_agency_actuals(rows)
    assert [r["actual"] for r in out] == [1.3, 0.7, 6.3, 5.0]
    assert out[0]["actual_source"] == "bls:WPSFD4"
    ff = [{"event": "PPI m/m", "country": "US", "time": "2026-10-15T12:30:00"}]
    assert [s.key for s in PW.due_releases("2026-10-15", ff, "08:30")] == ["ppi"]
    assert PW.PPI.agency == "BLS"


def _fomc_day_run(proj_side_effect, clock_step_s=0):
    """The 14:00 slot on Dec 9 (a projection meeting) with the statement
    out. The clock advances `clock_step_s` per read so a wait can end."""
    from datetime import datetime as real, timedelta
    sent, alerts = [], []

    class _Chan:
        async def send(self, content=None, embed=None, **kw):
            sent.append((content, embed))
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan()

    async def _ops(msg, **kw):
        alerts.append(msg)

    async def _nosleep(*a, **k):
        return None

    t = [real(2026, 12, 9, 14, 0, 5, tzinfo=PW._ET)]

    def _now(tz=None):
        t[0] += timedelta(seconds=clock_step_s)
        return t[0]

    parsed = PW.parse_fomc_statement(FOMC_HTML)
    ff = [{"event": "FOMC Interest Rate Decision", "country": "US", "time": "2026-12-09T19:00:00",
           "estimate": 3.75, "prev": 3.75, "actual": None, "unit": "%"}]
    with tempfile.TemporaryDirectory() as td, \
         patch("config.settings.db_path", str(Path(td) / "reports.db")), \
         patch("config.settings.print_alert_channel_id", "123"), \
         patch("report.print_watch._ff_rows_for_day", return_value=ff), \
         patch("report.print_watch.fetch_fomc_today", return_value=dict(parsed)), \
         patch("report.fed_projections.fetch_projections", side_effect=proj_side_effect) as fp, \
         patch("discord_bot.ops_alert.ops_alert", _ops), \
         patch("report.print_watch.asyncio.sleep", _nosleep), \
         patch("report.print_watch.datetime") as dt:
        dt.now.side_effect = _now
        dt.utcnow.return_value = real(2026, 12, 9, 19, 0, 5)
        dt.strptime = real.strptime
        dt.fromisoformat = real.fromisoformat
        asyncio.run(PW.print_watch_job(_Bot(), "14:00"))
        ledger = json.loads((Path(td) / "print-alerts" / "2026-12-09.json").read_text(encoding="utf-8")) \
            if (Path(td) / "print-alerts" / "2026-12-09.json").exists() else {}
    return sent, alerts, fp.call_count, ledger


def test_projection_meeting_waits_for_the_table_then_posts_with_it():
    proj = {"label": "December 2026", "columns": ["2026", "2027", "Longer run"],
            "median": {"2026": 3.6, "2027": 3.9, "Longer run": 3.2}, "prev_label": "September",
            "prev_median": {"2026": 4.1, "2027": 4.1, "Longer run": 3.2}, "url": "u"}
    sent, alerts, calls, ledger = _fomc_day_run([None, None, proj], clock_step_s=1)
    assert calls == 3, "the post waited for the table"
    assert len(sent) == 1 and not alerts
    content, embed = sent[0]
    assert content == "@everyone Dec FOMC: Dovish hold"
    lines = embed.description.split("\n")
    assert "Median rate projections vs September:" in lines
    assert "End of 2027: 3.9%, was 4.1%" in lines
    assert ledger["fomc"]["projections"]["median"]["2027"] == 3.9


def test_projection_meeting_posts_without_the_table_after_the_grace():
    sent, alerts, calls, ledger = _fomc_day_run(lambda today: None, clock_step_s=5)
    assert len(sent) == 1
    assert sent[0][0] == "@everyone Dec FOMC: Hold as expected"
    assert not any("projection" in ln.lower() for ln in sent[0][1].description.split("\n"))
    assert len(alerts) == 1 and "projection table" in alerts[0]
    assert calls > 1
