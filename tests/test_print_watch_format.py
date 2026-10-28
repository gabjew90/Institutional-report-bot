"""Print values, rates and payroll revisions as the alert carries them
(layout 2026-10-28; the full alert text is in tests/test_print_alerts.py)."""
import json
from pathlib import Path

from report import print_data as PD
from report import print_watch as PW

BLS = json.loads(Path("tests/fixtures/bls_cpi_jobs_2026-09-11.json").read_text(encoding="utf-8"))


def test_verdict_compares_at_the_displayed_precision():
    assert PW.verdict(0.31, 0.3, "%", "mom") == "in line"
    assert PW.verdict(0.2, 0.3, "%", "mom") == "below"
    assert PW.verdict(0.4, 0.3, "%", "mom") == "above"
    assert PW.verdict(162, 160, "K", "m_change_k") == "above"
    assert PW.verdict(None, 0.3, "%", "mom") == "" and PW.verdict(0.3, None, "%", "mom") == ""


def test_changes_carry_a_sign_and_levels_do_not():
    assert PW._fmt(0.3, "%", "mom") == "+0.3%" and PW._fmt(-0.1, "%", "mom") == "-0.1%"
    assert PW._fmt(2.6, "%", "yoy") == "2.6%", "a y/y inflation rate is headlined unsigned"
    assert PW._fmt(3.9, "%", "yoy", signed=True) == "+3.9%", "wage growth carries its sign"
    assert PW._fmt(4.2, "%", "level") == "4.2%" and PW._fmt(34.3, "", "level") == "34.3"
    assert PW._fmt(142, "K", "m_change_k") == "+142K" and PW._fmt(-10, "K", "m_change_k") == "-10K"
    assert PW._fmt(0.0, "%", "mom") == "0.0%" and PW._fmt(-0.04, "%", "mom") == "0.0%"


def test_unrounded_change_and_annualized_rate_come_from_index_levels():
    obs = PW.parse_bls(BLS)
    core = obs["CUSR0000SA0L1E"]
    u = PW.unrounded_change(core, "2026-08")
    assert u is not None and round(u, 1) == PW.compute(core, "mom", "2026-08")
    a = PW.annualized_3m(core, "2026-08")
    assert a is not None and 1.5 < a < 2.5, a
    assert PW.annualized_3m([("2026-08", 100.0)], "2026-08") is None
    assert PW.unrounded_change([("2026-08", 100.0)], "2026-08") is None


def test_the_release_carries_the_unrounded_core_and_the_3m_rate():
    rel = PD.data_release(PW.CPI, PW.parse_bls(BLS), "2026-08", [{"event": "Core CPI m/m", "estimate": 0.2}])
    assert set(rel.unrounded) == {"Core CPI m/m"} and rel.three_m is not None
    assert rel.has_consensus and [r["label"] for r in rel.ledger_rows()][0] == "Core CPI m/m"


# --- payroll revisions from FRED's archive (2026-10-05 audit) -----------
_NOW = {"CES0000000001": [("2026-06", 158892.0), ("2026-07", 158882.0),
                          ("2026-08", 159015.0), ("2026-09", 159044.0)]}
_BEFORE = {"observations": [{"date": "2026-06-01", "value": "158892"},
                            {"date": "2026-07-01", "value": "158913"},
                            {"date": "2026-08-01", "value": "159075"}]}


def test_both_revised_months_come_from_freds_archive():
    """The 2026-10-02 jobs post: BLS revised July +21K to -10K and August
    +162K to +133K, net -60K."""
    from unittest.mock import patch
    with patch("report.fred_data._fred_get", return_value=_BEFORE) as get:
        before = PW.payroll_vintage(PW.JOBS, "2026-09", "2026-10-02")
    rev = PD.payroll_revisions(PW.JOBS, _NOW, "2026-09", before, None)
    assert rev == {"months": [{"period": "2026-07", "now": -10, "was": 21},
                              {"period": "2026-08", "now": 133, "was": 162}], "net": -60}
    params = get.call_args[0][1]
    assert params["realtime_start"] == params["realtime_end"] == "2026-10-01"


def test_no_revision_when_nothing_changed_or_fred_is_down():
    from unittest.mock import patch
    same = {"observations": [{"date": f"{p}-01", "value": str(v)} for p, v in _NOW["CES0000000001"][:3]]}
    with patch("report.fred_data._fred_get", return_value=same):
        before = PW.payroll_vintage(PW.JOBS, "2026-09", "2026-10-02")
    assert PD.payroll_revisions(PW.JOBS, _NOW, "2026-09", before, None) is None
    with patch("report.fred_data._fred_get", return_value=None):
        assert PW.payroll_vintage(PW.JOBS, "2026-09", "2026-10-02") is None
    assert PD.payroll_revisions(PW.JOBS, _NOW, "2026-09", None, None) is None
    assert PW.payroll_vintage(PW.CPI, "2026-09", "2026-10-14") is None


def test_a_core_revision_of_a_tenth_or_more_is_kept():
    obs = {"WPSFD49104": [("2026-10", 157.0), ("2026-09", 156.5), ("2026-08", 155.9)]}
    sep_now = PW.compute(obs["WPSFD49104"], "mom", "2026-09")
    prev = {"rows": [{"label": "Core PPI m/m", "actual_value": round(sep_now - 0.2, 1), "period": "2026-09"}]}
    rev = PD.prior_revisions(PW.PPI, obs, "2026-10", prev)
    assert rev and rev[0]["period"] == "2026-09" and rev[0]["now"] == sep_now
    small = {"rows": [{"label": "Core PPI m/m", "actual_value": sep_now, "period": "2026-09"}]}
    assert PD.prior_revisions(PW.PPI, obs, "2026-10", small) == []
