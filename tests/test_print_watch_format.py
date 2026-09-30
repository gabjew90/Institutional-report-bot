"""Print embed body in the owner's layout (2026-09-30)."""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from report import print_watch as PW

BLS = json.loads(Path("tests/fixtures/bls_cpi_jobs_2026-09-11.json").read_text(encoding="utf-8"))
FF = [
    {"event": "CPI m/m", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 0.4, "prev": 0.1, "actual": None, "unit": "%"},
    {"event": "Core CPI m/m", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 0.2, "prev": 0.2, "actual": None, "unit": "%"},
    {"event": "CPI y/y", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 3.4, "prev": 3.4, "actual": None, "unit": "%"},
    {"event": "Core CPI y/y", "country": "US", "time": "2026-09-11T12:30:00", "estimate": 2.4, "prev": 2.5, "actual": None, "unit": "%"},
]


def _row(label, actual, consensus=None, prior=None, verdict="", display=None, pair="", row="",
         transform="mom", actual_value=None, prior_value=None):
    return {"label": label, "display": display or label, "pair": pair, "row": row, "optional": False,
            "unit": "%", "transform": transform, "actual": actual, "actual_value": actual_value,
            "consensus": consensus, "consensus_value": None, "prior": prior, "prior_value": prior_value,
            "verdict": verdict}


def test_verdict_compares_at_the_displayed_precision():
    assert PW.verdict(0.31, 0.3, "%", "mom") == "in line"
    assert PW.verdict(0.2, 0.3, "%", "mom") == "below"
    assert PW.verdict(0.4, 0.3, "%", "mom") == "above"
    assert PW.verdict(162, 160, "K", "m_change_k") == "above"
    assert PW.verdict(None, 0.3, "%", "mom") == "" and PW.verdict(0.3, None, "%", "mom") == ""


def test_cpi_body_from_the_real_print():
    obs = PW.parse_bls(BLS)
    rows = PW.build_rows(PW.CPI, obs, "2026-08", FF)
    body = PW.render_release(rows, computed=["Core CPI 3-month annualized: 2.0%"],
                             source=PW.source_line(PW.CPI))
    assert body[0] == "• Core CPI (MoM): +0.3% (vs. +0.2% exp, above)"
    assert body[1] == "• Core CPI (YoY): 2.4% (vs. 2.4% exp, in line)"
    assert body[2] == "• Headline CPI: +0.4% MoM / 3.4% YoY (vs. +0.4% / 3.4% exp, in line / in line)"
    assert body[3] == "• Core CPI 3-month annualized: 2.0%"
    assert body[-1].startswith("Source: bls.gov") and "ForexFactory" in body[-1]
    assert body[-2] == ""


def test_pairs_fall_back_to_priors_and_levels_say_up_or_down():
    rows = [
        _row("PCE m/m", "+0.3%", prior="+0.1%", display="Headline PCE", pair="headline"),
        _row("PCE y/y", "3.4%", prior="3.4%", display="Headline PCE", pair="headline", transform="yoy"),
        _row("Personal Income m/m", "+0.2%", consensus="+0.5%", verdict="below", display="Personal Income", row="income"),
        _row("Saving Rate", "4.1%", prior="4.4%", display="Saving Rate", row="income", transform="level",
             actual_value=4.1, prior_value=4.4),
        _row("Participation Rate", "62.6%", prior="62.6%", transform="level", actual_value=62.6, prior_value=62.6),
        _row("Shelter m/m", "+0.3%", display="Shelter (MoM)"),
    ]
    body = PW.render_release(rows)
    assert body[0] == "• Headline PCE: +0.3% MoM / 3.4% YoY (prior +0.1% / prior 3.4%)"
    assert body[1] == "• Personal Income: +0.2% (vs. +0.5% exp, below) | Saving Rate: 4.1% (down from 4.4%)"
    assert body[2] == "• Participation Rate: 62.6% (unchanged from 62.6%)"
    assert body[3] == "• Shelter (MoM): +0.3%"


def test_pair_with_consensus_on_one_side_compares_each_side_on_its_own_terms():
    ahe = [
        _row("Avg Hourly Earnings m/m", "+0.3%", consensus="+0.3%", prior="+0.2%", verdict="in line",
             display="Avg Hourly Earnings", pair="ahe"),
        _row("Avg Hourly Earnings y/y", "3.1%", prior="3.2%", display="Avg Hourly Earnings", pair="ahe",
             transform="yoy"),
    ]
    assert PW.render_release(ahe) == [
        "• Avg Hourly Earnings: +0.3% MoM / 3.1% YoY (vs. +0.3% exp, in line / prior 3.2%)"]
    # labels come from each row's transform, not from argument order
    assert PW.render_release(list(reversed(ahe)))[0].startswith(
        "• Avg Hourly Earnings: 3.1% YoY / +0.3% MoM")
    # no comparison on either side: no parenthetical
    bare = [_row("A m/m", "+0.1%", display="A", pair="p"), _row("A y/y", "2.0%", display="A", pair="p",
                                                               transform="yoy")]
    assert PW.render_release(bare) == ["• A: +0.1% MoM / 2.0% YoY"]
    # a level row with no numeric values falls back to its prior
    lvl = _row("Rate", "4.1%", prior="4.4%", transform="level")
    assert PW.render_release([lvl]) == ["• Rate: 4.1% (prior 4.4%)"]


def test_takeaway_sits_between_the_numbers_and_the_source():
    rows = [_row("Core PCE m/m", "+0.2%", consensus="+0.3%", verdict="below", display="Core PCE (MoM)")]
    body = PW.render_release(rows, takeaway=["• **Cooler core:** below the +0.3% consensus."],
                             source="Source: bea.gov")
    assert body == [
        "• Core PCE (MoM): +0.2% (vs. +0.3% exp, below)",
        "", "**Quick Takeaway**", "• **Cooler core:** below the +0.3% consensus.",
        "", "Source: bea.gov",
    ]
    assert PW.render_release([]) == []


def test_titles_and_annualized_rate():
    assert PW.release_title(PW.CPI, "2026-08") == "August CPI Inflation Print"
    assert PW.release_title(PW.JOBS, "2026-09") == "September Jobs Report"
    obs = PW.parse_bls(BLS)
    a = PW.annualized_3m(obs["CUSR0000SA0L1E"], "2026-08")
    assert a is not None and 1.5 < a < 2.5, a
    assert PW.annualized_3m([("2026-08", 100.0)], "2026-08") is None
    assert PW.source_line(PW.PCE).startswith("Source: bea.gov (NIPA tables 2.8.4, 2.6, 2.8.6)")
    assert PW.source_line(PW.FOMC) == "Source: federalreserve.gov, FOMC statement"


def test_the_post_is_green_and_pings_everyone():
    sent = []

    class _Chan:
        async def send(self, embed=None, **kw):
            sent.append((embed, kw))
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan()

    with patch("config.settings.print_alert_channel_id", "123"):
        ok = asyncio.run(PW._post(_Bot(), "CPI · August 2026", ["• x"], "f"))
    assert ok and len(sent) == 1
    embed, kw = sent[0]
    assert embed.color.value == 0x228B22
    assert kw["content"] == "@everyone"
    assert kw["allowed_mentions"].everyone is True


def test_the_job_posts_the_bullet_body_and_records_the_plain_lines():
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
        dt.now.return_value = real(2026, 9, 11, 8, 31, tzinfo=PW._ET)
        dt.utcnow.return_value = real(2026, 9, 11, 12, 31)
        dt.strptime = real.strptime
        dt.fromisoformat = real.fromisoformat
        asyncio.run(PW.print_watch_job(_Bot(), "08:30"))
        assert len(sent) == 1
        desc = sent[0].description
        assert desc.startswith("• Core CPI (MoM): +0.3% (vs. +0.2% exp, above)")
        ledger = json.loads((Path(td) / "print-alerts" / "2026-09-11.json").read_text(encoding="utf-8"))
        assert ledger["cpi"]["lines"][0].startswith("**Core CPI m/m** +0.3%")
