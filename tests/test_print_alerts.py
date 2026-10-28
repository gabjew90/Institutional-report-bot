"""Economic print alerts built with no model call (2026-10-28 redesign,
spec docs/superpowers/specs/2026-10-28-print-alerts-deterministic-design.md).

The golden replays are the five prints the bot posted between 2026-09-16
and 2026-10-28 (tests/fixtures/print_alerts/, copied from the worker's
/data/print-alerts ledger), rendered under the new rules. The scenario
cases cover, for every release: clearly above, clearly below, in line,
mixed, a large revision, missing secondary data and missing primary data."""
import asyncio
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from report import print_data as PD
from report import print_render as PR
from report import print_templates as PT
from report import print_verdict as PV
from report import print_watch as PW

LEDGER = json.loads(Path("tests/fixtures/print_alerts/ledger_2026-09-16_to_10-28.json").read_text(encoding="utf-8"))
SPEC = {s.key: s for s in PW.SPECS}
TIGHTENING = {"last_action": "hold", "direction": "tightening"}


def release(key, values, period="2026-09", **kw):
    """A Release from {label: (actual, consensus, prior)}, rows built the
    way build_rows builds them."""
    spec = SPEC[key]
    rows = {}
    for ln in spec.lines:
        if ln.label in values:
            a, c, p = (list(values[ln.label]) + [None, None])[:3]
            rows[ln.label] = PW.make_row(ln, period, a, c, p)
    return PD.Release(key=key, period=period, rows=rows, **kw)


def alert(rel, today="2026-10-14", policy=TIGHTENING, release_et="08:30"):
    v = PV.verdict_for(rel)
    take = PT.takeaway(rel, v, policy, today)
    return PR.render(SPEC[rel.key], rel, v, take, release_et), v


def takeaway_of(a):
    return next(ln for ln in a.lines if ln.startswith("Takeaway: "))[len("Takeaway: "):]


def _voice_clean(a):
    from ai_analysis.voice_rules import compose_lint_patterns
    pats = [(re.compile(rx, re.I), kind) for rx, kind in compose_lint_patterns()]
    # the Fed's own wording is quoted as it stands
    own = [a.content, a.footer] + [ln for ln in a.lines if not ln.startswith(("Was: ", "Now: "))]
    return [(kind, ln) for ln in own for p, kind in pats if p.search(ln)]


def _data_lines(a):
    out = []
    for ln in a.lines:
        if ln.startswith("Takeaway: "):
            break
        if ln and not ln.startswith(("Was: ", "Now: ", "Dissent: ", "Statement changes")):
            out.append(ln)
    return out


# ------------------------------------------------------------ verdicts

@pytest.mark.parametrize("core,head,label,direction", [
    ((0.4, 0.3), (0.4, 0.3), "Hotter than expected", PV.HAWKISH),
    ((0.4, 0.3), (0.3, 0.3), "Hotter than expected", PV.HAWKISH),
    ((0.4, 0.3), None, "Hotter than expected", PV.HAWKISH),
    ((0.2, 0.3), (0.2, 0.3), "Cooler than expected", PV.DOVISH),
    ((0.2, 0.3), (0.3, 0.3), "Cooler than expected", PV.DOVISH),
    ((0.4, 0.3), (0.2, 0.3), "Mixed", None),
    ((0.2, 0.3), (0.4, 0.3), "Mixed", None),
    ((0.3, 0.3), (0.5, 0.3), "In line with expectations", None),
    ((0.3, 0.3), (0.3, 0.3), "In line with expectations", None),
    ((0.3, None), (0.3, 0.3), "Released, no forecast to compare", None),
])
def test_inflation_truth_table(core, head, label, direction):
    vals = {"Core CPI m/m": core}
    if head:
        vals["CPI m/m"] = head
    v = PV.verdict_for(release("cpi", vals))
    assert (v.label, v.direction) == (label, direction)


def test_the_verdict_uses_the_published_precision_not_the_unrounded_figure():
    """+0.34% publishes as +0.3%: against a +0.3% forecast that is in line,
    and the unrounded figure is shown as context only."""
    rel = release("cpi", {"Core CPI m/m": (0.3, 0.3), "CPI m/m": (0.3, 0.3)})
    rel.unrounded["Core CPI m/m"] = 0.34
    a, v = alert(rel)
    assert v.label == "In line with expectations"
    assert a.lines[0] == "Core CPI MoM +0.3% vs +0.3% expected (+0.34% unrounded)"


def test_core_yoy_decides_when_core_mom_has_no_forecast():
    v = PV.verdict_for(release("pce", {"Core PCE m/m": (0.3,), "Core PCE y/y": (3.1, 3.0)}))
    assert v.label == "Hotter than expected"


@pytest.mark.parametrize("pay,unemp,wages,net,label", [
    ((225, 170), (4.0, 4.1), (0.4, 0.3), None, "Stronger than expected"),
    ((225, 170), (4.1, 4.1), (0.4, 0.3), None, "Stronger than expected"),
    ((60, 170), (4.3, 4.1), (0.2, 0.3), None, "Weaker than expected"),
    ((225, 170), (4.2, 4.1), (0.3, 0.3), None, "Mixed"),
    ((225, 170), (4.1, 4.1), (0.3, 0.3), None, "Slightly stronger than expected"),
    ((150, 170), (4.2, 4.1), (0.3, 0.3), None, "Slightly weaker than expected"),
    ((140, 150), (4.1, 4.1), (0.3, 0.3), None, "In line with expectations"),
    ((230, 170), (4.1, 4.1), (0.3, 0.3), -70, "In line with expectations"),   # revisions cancel the beat
    ((230, 170), (4.1, 4.1), (0.3, 0.3), -40, "Slightly stronger than expected"),
    ((220, None), (4.1, 4.1), (0.3, 0.3), None, "Released, no forecast to compare"),
])
def test_jobs_truth_table(pay, unemp, wages, net, label):
    rel = release("jobs", {"Non Farm Payrolls": pay, "Unemployment Rate": unemp,
                           "Avg Hourly Earnings m/m": wages})
    if net is not None:
        rel.payroll_revisions = {"months": [{"period": "2026-07", "now": 0, "was": 0},
                                            {"period": "2026-08", "now": net, "was": 0}], "net": net}
    assert PV.verdict_for(rel).label == label


def test_payrolls_within_the_band_are_in_line():
    for actual in (120, 170, 220):
        v = PV.verdict_for(release("jobs", {"Non Farm Payrolls": (actual, 170)}))
        assert v.signals["payrolls"] == 0
    assert PV.verdict_for(release("jobs", {"Non Farm Payrolls": (221, 170)})).signals["payrolls"] == 1


def _fomc(action="hold", high=4.0, expected=4.0, dissent_net=0, prev_net=0, projection=None):
    return PD.Release(key="fomc", period="2026-10", fomc={
        "action": action, "low": high - 0.25, "high": high, "size": 0.25 if action != "hold" else 0.0,
        "prior_high": high if action == "hold" else high - 0.25, "expected": expected,
        "expected_action": None, "vote": "12-0", "dissents": [], "dissent_net": dissent_net,
        "prev_dissent_net": prev_net, "changes": [], "projection_signal": projection, "url": ""})


@pytest.mark.parametrize("kw,label,direction", [
    (dict(action="hike", high=4.25, expected=4.0), "Surprise hike, hawkish", PV.HAWKISH),
    (dict(action="hold", high=4.0, expected=4.25), "Surprise hold, dovish", PV.DOVISH),
    (dict(dissent_net=2, prev_net=0), "Hawkish hold", PV.HAWKISH),
    (dict(dissent_net=-1, prev_net=0), "Dovish hold", PV.DOVISH),
    (dict(dissent_net=2, prev_net=2), "Hold as expected", None),
    (dict(dissent_net=2, prev_net=0, projection=-1), "Hold, mixed signals", None),
    (dict(projection=1), "Hawkish hold", PV.HAWKISH),
    (dict(expected=None, dissent_net=1, prev_net=0), "Hawkish hold", PV.HAWKISH),
    (dict(expected=None), "Hold", None),
    (dict(prev_net=None, dissent_net=2), "Hold as expected", None),   # no earlier vote to compare
])
def test_fomc_truth_table(kw, label, direction):
    v = PV.verdict_for(_fomc(**kw))
    assert (v.label, v.direction) == (label, direction)


@pytest.mark.parametrize("actions,direction", [
    (["hold", "hike"], "tightening"),
    (["cut", "hold"], "easing"),
    (["hold", "hold", "hold", "hold", "hike"], "neutral"),     # the hike is five decisions back
    ([], "neutral"),
    ([None, "hike"], "tightening"),
])
def test_policy_state_reads_the_last_move(actions, direction):
    assert PV.policy_state(actions)["direction"] == direction


@pytest.mark.parametrize("direction,policy,text", [
    (PV.HAWKISH, "tightening", "That leans hawkish and adds to the case for another hike, and reports like this tend to lift Treasury yields."),
    (PV.HAWKISH, "easing", "That leans hawkish and weakens the case for another cut, and reports like this tend to lift Treasury yields."),
    (PV.DOVISH, "tightening", "That leans dovish and eases pressure on the Fed to hike again, and reports like this tend to pull Treasury yields lower."),
    (PV.DOVISH, "easing", "That leans dovish and adds to the case for another cut, and reports like this tend to pull Treasury yields lower."),
    (PV.DOVISH, "neutral", "That leans dovish, and reports like this tend to pull Treasury yields lower."),
    (None, "tightening", "The report gives the Fed no clear new signal."),
])
def test_fed_sentence_follows_the_regime(direction, policy, text):
    assert PT.fed_sentence(direction, {"direction": policy}) == text


# ------------------------------------------------------- golden replays

@pytest.fixture
def ledger_dir():
    """The real ledger up to each print, so the policy state is read the
    way the worker reads it."""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "print-alerts"
        base.mkdir()
        for day, d in LEDGER.items():
            (base / f"{day}.json").write_text(json.dumps(d), encoding="utf-8")
        with patch("config.settings.db_path", str(Path(td) / "reports.db")):
            yield


def test_golden_sep16_fomc_hike(ledger_dir):
    parsed = {"action": "raise", "low": 3.75, "high": 4.0, "size": 0.25, "vote": "12-0", "text": ""}
    rel = PD.fomc_release(parsed, [{"event": "FOMC Interest Rate Decision", "estimate": 4.0}], None, "2026-09-16")
    a, v = PW.compose_alert(PW.FOMC, rel, "2026-09-16", "14:00")
    assert a.content == "Sep FOMC: Hike as expected"
    assert a.lines == [
        "Decision: Hike, as expected",
        "Target range: 3.75% to 4.00%",
        "Vote: 12-0",
        "",
        "Takeaway: The Fed raised rates by a quarter point to 3.75% to 4.00%, as expected. "
        "With the decision expected, the press conference at 2:30 PM ET is the next test.",
    ]
    assert a.footer == "Federal Reserve statement · released 2:00 PM ET"


def test_golden_sep30_pce(ledger_dir):
    rel = release("pce", {"Core PCE m/m": (0.2, 0.3), "Core PCE y/y": (3.0,), "PCE m/m": (0.3,),
                          "PCE y/y": (3.4,)}, period="2026-08")
    a, v = PW.compose_alert(PW.PCE, rel, "2026-09-30", "08:30")
    assert a.content == "Aug PCE: Cooler than expected"
    assert a.lines == [
        "Core PCE MoM +0.2% vs +0.3% expected",
        "Core PCE YoY 3.0%",
        "Headline PCE MoM +0.3%",
        "Headline PCE YoY 3.4%",
        "",
        "Takeaway: Core PCE, the Fed's preferred inflation gauge, rose less than expected. "
        "That leans dovish and eases pressure on the Fed to hike again, and reports like this "
        "tend to pull Treasury yields lower.",
    ]
    assert a.footer == "BEA · forecast: ForexFactory · released 8:30 AM ET"


def test_golden_oct2_jobs(ledger_dir):
    rel = release("jobs", {"Non Farm Payrolls": (29, 89), "Unemployment Rate": (4.2, 4.1),
                           "Avg Hourly Earnings m/m": (0.1, 0.3), "Avg Hourly Earnings y/y": (3.0, None, 3.1),
                           "Participation Rate": (61.8, None, 61.6)})
    rel.payroll_revisions = {"months": [{"period": "2026-07", "now": -10, "was": 21},
                                        {"period": "2026-08", "now": 133, "was": 162}], "net": -60}
    a, v = PW.compose_alert(PW.JOBS, rel, "2026-10-02", "08:30")
    assert a.content == "Sep Jobs: Weaker than expected"
    assert a.lines == [
        "Nonfarm payrolls +29K vs +89K expected",
        "2-month revision -60K",
        "Unemployment 4.2% vs 4.1% expected",
        "Avg hourly earnings MoM +0.1% vs +0.3% expected",
        "Avg hourly earnings YoY +3.0%",
        "",
        "July revised to -10K from +21K",
        "August revised to +133K from +162K",
        "Participation 61.8%",
        "",
        "Takeaway: Payroll growth fell short of expectations, unemployment came in higher than "
        "expected and wage growth was slower than expected. The two prior months were revised "
        "down by 60K combined. That leans dovish and eases pressure on the Fed to hike again, "
        "and reports like this tend to pull Treasury yields lower.",
    ]


def test_golden_oct14_cpi(ledger_dir):
    rel = release("cpi", {"Core CPI m/m": (0.3, 0.2), "Core CPI y/y": (2.6, 2.5), "CPI m/m": (0.6, 0.6),
                          "CPI y/y": (3.7, 3.6), "Shelter m/m": (0.4,), "Energy m/m": (5.2,),
                          "Food m/m": (0.0,)}, three_m=3.4)
    a, v = PW.compose_alert(PW.CPI, rel, "2026-10-14", "08:30")
    assert a.content == "Sep CPI: Hotter than expected"
    assert a.lines == [
        "Core CPI MoM +0.3% vs +0.2% expected",
        "Core CPI YoY 2.6% vs 2.5% expected",
        "Headline CPI MoM +0.6% vs +0.6% expected",
        "Headline CPI YoY 3.7% vs 3.6% expected",
        "Core CPI 3M 3.4%",
        "",
        "Shelter +0.4%",
        "Food 0.0%",
        "Energy +5.2%",
        "",
        "Takeaway: Core inflation rose more than expected. Shelter rose 0.4% and energy 5.2% on "
        "the month. That leans hawkish and adds to the case for another hike, and reports like "
        "this tend to lift Treasury yields.",
    ]


def test_golden_oct28_fomc_hawkish_hold(ledger_dir):
    """Today's decision: an expected hold with two dissents for a hike,
    after a unanimous September vote."""
    stored = LEDGER["2026-10-28"]["fomc"]["statement"]
    parsed = PW.parse_fomc_statement("<p>" + stored + "</p>")
    prev = PW.previous_post("fomc", before="2026-10-28")
    assert prev["lines"][0].startswith("**Fed hikes**")
    rel = PD.fomc_release(parsed, [{"event": "FOMC Interest Rate Decision", "estimate": 4.0}], prev, "2026-10-28")
    a, v = PW.compose_alert(PW.FOMC, rel, "2026-10-28", "14:00")
    assert a.content == "Oct FOMC: Hawkish hold"
    assert a.lines == [
        "Decision: Hold, as expected",
        "Target range: 3.75% to 4.00%",
        "Vote: 10-2",
        "",
        "Dissent: Cook and Hammack wanted a quarter-point hike",
        "",
        "Takeaway: The Fed held rates at 3.75% to 4.00%, as expected, and two officials voted "
        "for a hike. Decisions like this tend to lift Treasury yields.",
    ]


def test_the_next_fomc_compares_with_todays_stored_statement(ledger_dir):
    """December reads October's statement from the ledger: the changed
    sentence shows as was / now, the decision and vote sentences never."""
    stored = LEDGER["2026-10-28"]["fomc"]["statement"]
    dec = stored.replace("10 - 2 vote", "12 - 0 vote").replace(
        "Economic activity is expanding at a strong pace.", "Economic activity has slowed.").split(" Voting against")[0]
    parsed = PW.parse_fomc_statement("<p>" + dec + "</p>")
    prev = PW.previous_post("fomc", before="2026-12-09")
    rel = PD.fomc_release(parsed, [{"event": "FOMC Interest Rate Decision", "estimate": 4.0}], prev, "2026-12-09")
    a, v = PW.compose_alert(PW.FOMC, rel, "2026-12-09", "14:00")
    assert a.lines[4:7] == ["Statement changes:", "Was: Economic activity is expanding at a strong pace.",
                            "Now: Economic activity has slowed."]
    # two dissents for a hike in October, none now: the dissent signal turns dovish
    assert v.label == "Dovish hold"
    assert "The statement changed how it describes growth." in takeaway_of(a)


# ----------------------------------------------- scenarios, every release

def _scenarios():
    cpi = {
        "hotter": {"Core CPI m/m": (0.5, 0.3), "Core CPI y/y": (3.1, 3.0), "CPI m/m": (0.6, 0.4), "CPI y/y": (3.4, 3.3),
                   "Shelter m/m": (0.4,), "Food m/m": (0.1,), "Energy m/m": (1.4,)},
        "cooler": {"Core CPI m/m": (0.1, 0.3), "Core CPI y/y": (2.9, 3.0), "CPI m/m": (0.1, 0.3), "CPI y/y": (3.1, 3.2)},
        "in_line": {"Core CPI m/m": (0.3, 0.3), "Core CPI y/y": (3.0, 3.0), "CPI m/m": (0.5, 0.3), "CPI y/y": (3.2, 3.2)},
        "mixed": {"Core CPI m/m": (0.4, 0.3), "CPI m/m": (0.1, 0.3)},
        "missing_secondary": {"Core CPI m/m": (0.4, 0.3), "Core CPI y/y": (3.1, 3.0), "CPI m/m": (0.4, 0.3), "CPI y/y": (3.3, 3.3)},
        "missing_primary": {"Core CPI m/m": (0.4,), "CPI m/m": (0.4,)},
    }
    return {
        "cpi": cpi,
        "ppi": {
            "hotter": {"Core PPI m/m": (0.5, 0.2), "Core PPI y/y": (3.0, 2.8), "PPI m/m": (0.6, 0.2), "PPI y/y": (3.1, 2.9),
                       "PPI ex trade m/m": (0.3,)},
            "cooler": {"Core PPI m/m": (-0.1, 0.2), "PPI m/m": (-0.2, 0.2)},
            "in_line": {"Core PPI m/m": (0.2, 0.2), "PPI m/m": (0.2, 0.2)},
            "mixed": {"Core PPI m/m": (0.4, 0.2), "PPI m/m": (0.0, 0.2)},
            "missing_secondary": {"Core PPI m/m": (0.4, 0.2), "PPI m/m": (0.3, 0.2)},
            "missing_primary": {"Core PPI m/m": (0.4,)},
        },
        "pce": {
            "hotter": {"Core PCE m/m": (0.4, 0.2), "Core PCE y/y": (3.1,), "PCE m/m": (0.4,), "PCE y/y": (3.3,),
                       "Real Consumer Spending m/m": (0.6,), "Personal Income m/m": (0.3, 0.4), "Saving Rate": (4.1, None, 4.4)},
            "cooler": {"Core PCE m/m": (0.1, 0.2), "Core PCE y/y": (2.8,)},
            "in_line": {"Core PCE m/m": (0.2, 0.2)},
            "mixed": {"Core PCE m/m": (0.3, 0.2), "Core PCE y/y": (2.9,)},
            "missing_secondary": {"Core PCE m/m": (0.3, 0.2), "PCE m/m": (0.3,)},
            "missing_primary": {"Core PCE y/y": (2.9,)},
        },
        "jobs": {
            "hotter": {"Non Farm Payrolls": (225, 170), "Unemployment Rate": (4.0, 4.1), "Avg Hourly Earnings m/m": (0.4, 0.3),
                       "Avg Hourly Earnings y/y": (3.9,), "Participation Rate": (62.7, None, 62.6), "Avg Weekly Hours": (34.3,)},
            "cooler": {"Non Farm Payrolls": (40, 150), "Unemployment Rate": (4.3, 4.1), "Avg Hourly Earnings m/m": (0.2, 0.3)},
            "in_line": {"Non Farm Payrolls": (160, 150), "Unemployment Rate": (4.1, 4.1), "Avg Hourly Earnings m/m": (0.3, 0.3)},
            "mixed": {"Non Farm Payrolls": (225, 170), "Unemployment Rate": (4.2, 4.1), "Avg Hourly Earnings m/m": (0.3, 0.3)},
            "missing_secondary": {"Non Farm Payrolls": (225, 170), "Unemployment Rate": (4.1, 4.1)},
            "missing_primary": {"Non Farm Payrolls": (225,), "Unemployment Rate": (4.1,)},
        },
    }


EXPECT = {
    "cpi": {"hotter": "Hotter than expected", "cooler": "Cooler than expected", "in_line": "In line with expectations",
            "mixed": "Mixed", "missing_secondary": "Hotter than expected", "missing_primary": "Released, no forecast to compare"},
    "ppi": {"hotter": "Hotter than expected", "cooler": "Cooler than expected", "in_line": "In line with expectations",
            "mixed": "Mixed", "missing_secondary": "Hotter than expected", "missing_primary": "Released, no forecast to compare"},
    "pce": {"hotter": "Hotter than expected", "cooler": "Cooler than expected", "in_line": "In line with expectations",
            "mixed": "Hotter than expected", "missing_secondary": "Hotter than expected",
            "missing_primary": "Released, no forecast to compare"},
    "jobs": {"hotter": "Stronger than expected", "cooler": "Weaker than expected", "in_line": "In line with expectations",
             "mixed": "Mixed", "missing_secondary": "Slightly stronger than expected",
             "missing_primary": "Released, no forecast to compare"},
}
CASES = [(k, s) for k, cases in _scenarios().items() for s in cases]


@pytest.mark.parametrize("key,scenario", CASES)
def test_every_release_in_every_scenario(key, scenario):
    rel = release(key, _scenarios()[key][scenario])
    a, v = alert(rel, today="2026-11-13")
    assert v.label == EXPECT[key][scenario]
    assert a.content.endswith(f"{SPEC[key].name}: {v.label}")
    take = takeaway_of(a)
    assert 1 <= len(re.findall(r"[.!?](?:\s|$)", take)) <= 3, take
    assert all(len(ln) <= PR.MAX_DATA_LINE for ln in _data_lines(a)), _data_lines(a)
    assert not _voice_clean(a), _voice_clean(a)
    assert "None" not in "\n".join(a.lines)


def test_in_line_core_with_a_hot_headline_says_so():
    a, _ = alert(release("cpi", _scenarios()["cpi"]["in_line"]))
    assert takeaway_of(a).startswith("Core inflation matched expectations, while headline came in higher.")
    assert takeaway_of(a).endswith("The report gives the Fed no clear new signal.")


def test_in_line_core_never_claims_a_headline_forecast_that_does_not_exist():
    a, _ = alert(release("pce", _scenarios()["pce"]["in_line"]))
    assert takeaway_of(a).startswith("Core inflation matched expectations. ")


def test_no_forecast_states_the_figure_without_a_fed_read():
    a, _ = alert(release("cpi", _scenarios()["cpi"]["missing_primary"]))
    assert takeaway_of(a) == "Core inflation rose 0.4% on the month."
    a, _ = alert(release("jobs", _scenarios()["jobs"]["missing_primary"]))
    assert takeaway_of(a) == "Payrolls rose by 225K and unemployment was 4.1%."


def test_ppi_points_at_the_next_pce_date():
    a, _ = alert(release("ppi", _scenarios()["ppi"]["hotter"]), today="2026-11-13")
    assert "PPI matters mostly for what it implies for core PCE, out Wed Nov 25." in takeaway_of(a)
    assert "PPI ex food, energy, trade MoM +0.3%" in a.lines


def test_a_large_ppi_revision_replaces_the_pce_pointer():
    rel = release("ppi", _scenarios()["ppi"]["hotter"])
    rel.prior_revisions = [{"label": "Core PPI m/m", "period": "2026-08", "now": 0.4, "was": 0.2}]
    a, _ = alert(rel, today="2026-11-13")
    assert "August was revised to +0.4% from +0.2%." in takeaway_of(a)
    assert "core PCE, out" not in takeaway_of(a)
    assert "August core revised to +0.4% from +0.2%" in a.lines


def test_pce_secondary_lines_and_spending_sentence():
    a, _ = alert(release("pce", _scenarios()["pce"]["hotter"]))
    assert a.lines[a.lines.index("") + 1:a.lines.index("", a.lines.index("") + 1)] == [
        "Real consumer spending +0.6%", "Personal income +0.3% vs +0.4% expected", "Saving rate 4.1%"]
    assert "Real consumer spending rose 0.6% and the saving rate fell to 4.1%." in takeaway_of(a)


def test_jobs_revisions_cancelling_a_beat_are_named():
    rel = release("jobs", {"Non Farm Payrolls": (230, 170), "Unemployment Rate": (4.1, 4.1),
                           "Avg Hourly Earnings m/m": (0.4, 0.3)})
    rel.payroll_revisions = {"months": [{"period": "2026-07", "now": 100, "was": 150},
                                        {"period": "2026-08", "now": 120, "was": 160}], "net": -90}
    a, v = alert(rel)
    assert v.label == "Slightly stronger than expected"
    assert takeaway_of(a).startswith(
        "Payroll growth beat expectations, but downward revisions to the prior two months cancel it out. "
        "Wage growth was faster than expected.")


def test_incomplete_data_is_titled_and_says_so():
    rel = release("cpi", {"Core CPI m/m": (0.3, 0.3)}, incomplete=True)
    a, v = alert(rel)
    assert a.content == "Sep CPI: Incomplete data"
    assert a.lines[-1] == "Takeaway: The initial release is incomplete."


def test_unrounded_jobless_rate_from_the_counts():
    obs = {"CES0000000001": [("2026-09", 159044.0), ("2026-08", 159015.0)],
           "LNS14000000": [("2026-09", 4.2)], "CES0500000003": [("2026-09", 36.5), ("2026-08", 36.46)],
           "LNS13000000": [("2026-09", 7183.0)], "LNS11000000": [("2026-09", 170520.0)]}
    rel = PD.data_release(PW.JOBS, obs, "2026-09", [{"event": "Unemployment Rate", "estimate": 4.1}])
    a, _ = alert(rel)
    assert "Unemployment 4.2% vs 4.1% expected (4.21% unrounded)" in a.lines
    assert not any("Unemployed" in ln or "Labor Force" in ln for ln in a.lines), "inputs never show"


# ------------------------------------------------------ FOMC parsing

def test_fractions_and_move_sizes():
    assert PW._fraction("1/4") == 0.25 and PW._fraction("3-3/4") == 3.75 and PW._fraction("4") == 4.0
    hike = "The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 4 to 4-1/4 percent."
    p = PW.parse_fomc_statement(hike)
    assert (p["action"], p["size"], p["high"]) == ("raise", 0.25, 4.25)


def test_dissents_parse_names_and_the_move_they_wanted():
    july = PW.parse_fomc_statement(Path("tests/fixtures/fomc_statement_2026-07-29.html").read_text(encoding="utf-8"))
    assert PW.parse_dissents(july["text"]) == [{"names": ["Hammack", "Kashkari", "Logan"], "move": "hike", "size": 0.25}]
    two = ("Voting against the action were Stephen I. Miran, who preferred to lower the target range for the "
           "federal funds rate by 1/2 percentage point at this meeting, and Jeffrey R. Schmid, who preferred no "
           "change to the target range at this meeting.")
    groups = PW.parse_dissents(two)
    assert groups == [{"names": ["Miran"], "move": "cut", "size": 0.5}, {"names": ["Schmid"], "move": "hold", "size": None}]
    # a quarter-point cut: Miran wanted lower, Schmid higher
    assert PW.dissent_net(groups, "cut", 0.25) == 0 and PW.dissent_net(groups, "hold") == -1
    assert PW.dissent_net(groups[:1], "cut", 0.25) == -1 and PW.dissent_net(groups[1:], "cut", 0.25) == 1
    assert PW.parse_dissents("The Committee decided to maintain the target range.") == []


def test_previous_dissents_from_each_ledger_shape():
    assert PD.previous_dissent_net(LEDGER["2026-09-16"]["fomc"]) == 0, "a 12-0 vote in the posted line"
    assert PD.previous_dissent_net(LEDGER["2026-10-28"]["fomc"]) == 2, "parsed from the stored statement"
    assert PD.previous_dissent_net({"dissent_net": -1}) == -1
    assert PD.previous_dissent_net({"lines": ["x"]}) is None
    assert PD.ledger_action(LEDGER["2026-10-28"]["fomc"]) == "hold"
    assert PD.ledger_action(LEDGER["2026-09-16"]["fomc"]) == "hike"


def test_html_comments_never_reach_the_stored_statement():
    html = ("<!-- nav <div> --><p>For release at 2:00 p.m. EDT Share</p><!-- a <b> -->"
            "<p>The Committee decided to maintain the target range for the federal funds rate at 3-3/4 to 4 "
            "percent.</p><p>For media inquiries</p>")
    text = PW.parse_fomc_statement(html)["text"]
    assert text.startswith("The Committee decided") and "-->" not in text


def test_statement_diff_pairs_rewrites_and_skips_the_decision():
    prev = ("The Committee decided to maintain the target range for the federal funds rate at 4 percent. "
            "Inflation remains elevated. Job gains have been solid.")
    cur = ("The Committee decided to raise the target range for the federal funds rate to 4-1/4 percent. "
           "Inflation has eased somewhat. Job gains have been solid. The Committee will monitor carefully.")
    assert PW.statement_diff(prev, cur) == [("Inflation remains elevated.", "Inflation has eased somewhat."),
                                            (None, "The Committee will monitor carefully.")]
    assert PW.statement_diff("", cur) == [] and PW.statement_diff(cur, cur) == []


# ----------------------------------------------------------- the post

def test_the_message_text_is_the_verdict_and_pings_everyone():
    sent = []

    class _Chan:
        async def send(self, content=None, embed=None, **kw):
            sent.append((content, embed, kw))
            return object()

    class _Bot:
        def get_channel(self, cid):
            return _Chan()

    with patch("config.settings.print_alert_channel_id", "123"):
        ok = asyncio.run(PW._post(_Bot(), "Sep CPI: Hotter than expected", ["Core CPI MoM +0.4%"], "f"))
    content, embed, kw = sent[0]
    assert ok and content == "@everyone Sep CPI: Hotter than expected"
    assert embed.title is None and embed.description == "Core CPI MoM +0.4%"
    assert embed.color.value == 0x228B22 and kw["allowed_mentions"].everyone is True


def test_fast_polling_for_the_first_two_minutes():
    from datetime import datetime, timedelta
    at = datetime.now(PW._ET)
    with patch("config.settings.bls_api_key", "k"):
        assert PW._poll_s(at) == PW.POLL_S_FAST
        assert PW._poll_s(at - timedelta(seconds=PW.FAST_WINDOW_S + 1)) == PW.POLL_S_WITH_KEY
    with patch("config.settings.bls_api_key", ""):
        assert PW._poll_s(at) == PW.POLL_S_NO_KEY
