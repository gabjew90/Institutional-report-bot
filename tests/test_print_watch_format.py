"""Print embed layout, color and room ping (owner, 2026-09-30)."""
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
]


def test_verdict_compares_at_the_displayed_precision():
    assert PW.verdict(0.31, 0.3, "%", "mom") == "in line"
    assert PW.verdict(0.2, 0.3, "%", "mom") == "below"
    assert PW.verdict(0.4, 0.3, "%", "mom") == "above"
    assert PW.verdict(162, 160, "K", "m_change_k") == "above"
    assert PW.verdict(None, 0.3, "%", "mom") == "" and PW.verdict(0.3, None, "%", "mom") == ""


def test_the_body_leads_with_the_consensus_line_and_aligns_a_table():
    obs = PW.parse_bls(BLS)
    rows = PW.build_rows(PW.CPI, obs, "2026-08", FF)
    body = PW.render_release(rows)
    assert body[0] == "**Core CPI m/m +0.3%** vs +0.2% consensus, above"
    assert body[1] == "```" and body[-1] == "```"
    table = body[2:-1]
    assert table[0].split() == ["actual", "consensus", "prior"]
    # every data row has the same width up to the verdict column
    core = next(l for l in table if l.startswith("Core CPI m/m"))
    assert core.endswith("above"), core
    yoy = next(l for l in table if l.startswith("CPI y/y"))
    assert yoy.split()[3] == "-", "no consensus in the feed renders as a dash"
    assert len(core.rsplit("  ", 1)[0]) == len(yoy)


def test_a_release_with_no_consensus_at_all_still_renders():
    rows = [{"label": "PCE m/m", "actual": "+0.3%", "consensus": None, "prior": "+0.1%", "verdict": ""}]
    body = PW.render_release(rows)
    assert body[0] == "**PCE m/m +0.3%** (prior +0.1%)"
    assert PW.render_release([]) == []


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
        ok = asyncio.run(PW._post(_Bot(), "CPI · August 2026", ["**x**", "```", "t", "```"], "f"))
    assert ok and len(sent) == 1
    embed, kw = sent[0]
    assert embed.color.value == 0x228B22
    assert kw["content"] == "@everyone"
    assert kw["allowed_mentions"].everyone is True


def test_the_job_posts_the_table_body_and_records_the_plain_lines():
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
        assert desc.startswith("**Core CPI m/m +0.3%** vs +0.2% consensus, above")
        assert "```" in desc and "CPI m/m" in desc
        ledger = json.loads((Path(td) / "print-alerts" / "2026-09-11.json").read_text(encoding="utf-8"))
        assert ledger["cpi"]["lines"][0].startswith("**Core CPI m/m** +0.3%")
