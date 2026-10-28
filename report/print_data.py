"""The economic print as the alert reads it (2026-10-28 redesign, spec
docs/superpowers/specs/2026-10-28-print-alerts-deterministic-design.md).

One `Release` per print: the rows build_rows produced (reported values
with the forecast feed's consensus beside them), the values computed
from the agency's own index levels (the change before rounding, the
3-month annualized rate, the jobless rate from its counts), and the
revisions to months already posted. Nothing here is formatted for the
reader and nothing is inferred: a value the agency or the feed lacks is
absent. print_verdict, print_templates and print_render read this.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from report import print_watch as PW
from report.fred_data import month_shift

# The core month-on-month line per inflation release, which carries the
# figure before rounding and the prior-month revision.
CORE_MOM = {"cpi": "Core CPI m/m", "pce": "Core PCE m/m", "ppi": "Core PPI m/m"}
# A prior month counts as revised at this size, in percentage points.
REVISION_MIN_PT = 0.1


@dataclass
class Release:
    key: str
    period: str                      # the reference month, YYYY-MM (the meeting month for the FOMC)
    rows: dict = field(default_factory=dict)        # label -> build_rows row
    unrounded: dict = field(default_factory=dict)   # label -> value before rounding
    three_m: float | None = None     # core 3-month annualized, CPI and PCE
    payroll_revisions: dict | None = None  # {"months": [{"period", "now", "was"}], "net"}
    prior_revisions: list = field(default_factory=list)  # [{"label", "period", "now", "was"}]
    fomc: dict | None = None
    incomplete: bool = False

    def value(self, label: str) -> float | None:
        r = self.rows.get(label)
        return None if r is None else r.get("actual_value")

    @property
    def has_consensus(self) -> bool:
        return any(r.get("consensus_value") is not None for r in self.rows.values())

    def ledger_rows(self) -> list[dict]:
        return [{k: r.get(k) for k in ("label", "actual_value", "period")}
                for r in self.rows.values() if not r.get("hidden")]


def payroll_revisions(spec: PW.ReleaseSpec, obs: dict, period: str,
                      before: list[tuple[str, float]] | None, prev: dict | None) -> dict | None:
    """Both months BLS revises, against FRED's archive as it stood the day
    before the release (`before`, from payroll_vintage). Without it, the
    one month our own ledger posted last time. None when nothing changed."""
    ln = next((x for x in spec.lines if x.transform == "m_change_k"), None)
    if ln is None:
        return None
    now_obs = obs.get(ln.series) or []
    months: list[dict] = []
    if before:
        for back in (-2, -1):
            month = month_shift(period, back)
            was = PW.compute(before, "m_change_k", month)
            now = PW.compute(now_obs, "m_change_k", month)
            if was is None or now is None:
                months = []
                break
            months.append({"period": month, "now": now, "was": was})
    if not months and prev:
        last = month_shift(period, -1)
        posted = next((r for r in prev.get("rows") or []
                       if r.get("label") == ln.label and r.get("period") == last), None)
        now = PW.compute(now_obs, "m_change_k", last)
        if posted and posted.get("actual_value") is not None and now is not None:
            months = [{"period": last, "now": now, "was": posted["actual_value"]}]
    if not months or all(round(m["now"]) == round(m["was"]) for m in months):
        return None
    return {"months": months, "net": sum(m["now"] - m["was"] for m in months)}


def prior_revisions(spec: PW.ReleaseSpec, obs: dict, period: str, prev: dict | None) -> list[dict]:
    """The core month-on-month rate we posted last month, as the agency now
    has it, when it moved by REVISION_MIN_PT or more."""
    label = CORE_MOM.get(spec.key)
    ln = next((x for x in spec.lines if x.label == label), None)
    if ln is None or not prev:
        return []
    last = month_shift(period, -1)
    posted = next((r for r in prev.get("rows") or []
                   if r.get("label") == label and r.get("period") == last), None)
    now = PW.compute(obs.get(ln.series) or [], "mom", last)
    if not posted or posted.get("actual_value") is None or now is None:
        return []
    if abs(now - posted["actual_value"]) < REVISION_MIN_PT - 1e-9:
        return []
    return [{"label": label, "period": last, "now": now, "was": posted["actual_value"]}]


def data_release(spec: PW.ReleaseSpec, obs: dict, period: str, ff_rows: list[dict],
                 prev: dict | None = None, vintage: list[tuple[str, float]] | None = None,
                 incomplete: bool = False) -> Release:
    rows = PW.build_rows(spec, obs, period, ff_rows)
    rel = Release(key=spec.key, period=period, rows={r["label"]: r for r in rows}, incomplete=incomplete)
    core = CORE_MOM.get(spec.key)
    core_line = next((x for x in spec.lines if x.label == core), None)
    if core_line is not None:
        u = PW.unrounded_change(obs.get(core_line.series) or [], period)
        if u is not None:
            rel.unrounded[core] = u
        if spec.key in ("cpi", "pce"):
            rel.three_m = PW.annualized_3m(obs.get(core_line.series) or [], period)
        rel.prior_revisions = prior_revisions(spec, obs, period, prev)
    if spec.key == "jobs":
        unemployed, force = rel.value("Unemployed"), rel.value("Labor Force")
        if unemployed is not None and force:
            rel.unrounded["Unemployment Rate"] = unemployed / force * 100
        rel.payroll_revisions = payroll_revisions(spec, obs, period, vintage, prev)
    return rel


# ---------------------------------------------------------------- FOMC

_LINE_ACTION_RE = re.compile(r"\bFed (hikes|holds|cuts)\b")
_LINE_ACTION = {"hikes": "hike", "holds": "hold", "cuts": "cut"}


def ledger_action(entry: dict | None) -> str | None:
    """hike / hold / cut for a ledger FOMC entry. Entries since this
    redesign store it, the two before it (2026-09-16, 2026-10-28) carry it
    in their posted lines ("Fed hikes", "Fed holds")."""
    if not entry:
        return None
    if entry.get("action") in ("hike", "hold", "cut"):
        return entry["action"]
    for line in entry.get("lines") or []:
        m = _LINE_ACTION_RE.search(str(line))
        if m:
            return _LINE_ACTION[m.group(1)]
    return None


def previous_dissent_net(entry: dict | None) -> int | None:
    """Net dissents for a higher rate at the previous meeting: stored on
    entries since this redesign, else parsed from the stored statement,
    else 0 from a unanimous vote in the posted lines, else unknown."""
    if not entry:
        return None
    if isinstance(entry.get("dissent_net"), int):
        return entry["dissent_net"]
    action = ledger_action(entry)
    if entry.get("statement") and action:
        return PW.dissent_net(PW.parse_dissents(entry["statement"]), action)
    if any(re.search(r"\bvote \d+-0\b", str(line)) for line in entry.get("lines") or []):
        return 0
    return None


def _previous_upper(entry: dict | None) -> float | None:
    for r in (entry or {}).get("rows") or []:
        if r.get("label") == "Target range" and isinstance(r.get("actual_value"), (int, float)):
            return float(r["actual_value"])
    return None


def fomc_release(parsed: dict, ff_rows: list[dict], prev: dict | None, today_iso: str) -> Release:
    """The decision against the feed's expected upper bound, the dissents
    against the previous meeting's, and the statement sentences that
    changed. `parsed` is parse_fomc_statement's result plus its 'url'."""
    action = PW.FED_ACTION.get(parsed.get("action"), "hold")
    high, low = parsed["high"], parsed["low"]
    size = parsed.get("size")
    if action == "hold":
        prior_high, size = high, 0.0
    elif size is not None:
        prior_high = high - size if action == "hike" else high + size
    else:
        prior_high = _previous_upper(prev)
        size = abs(high - prior_high) if prior_high is not None else None
    expected = PW._ff_value(ff_rows, "FOMC Interest Rate Decision", "estimate")
    expected_action = None
    if expected is not None and prior_high is not None:
        expected_action = ("hike" if round(expected - prior_high, 4) > 0
                           else "cut" if round(expected - prior_high, 4) < 0 else "hold")
    dissents = PW.parse_dissents(parsed.get("text", ""))
    period = today_iso[:7]
    row = {"label": "Target range", "display": "Target range", "hidden": False, "optional": False,
           "unit": "%", "transform": "range", "period": period,
           "actual": f"{low:.2f}% to {high:.2f}%", "actual_value": high,
           "consensus": f"{expected:.2f}%" if expected is not None else None, "consensus_value": expected,
           "prior": None, "prior_value": None, "verdict": ""}
    return Release(key="fomc", period=period, rows={"Target range": row}, fomc={
        "action": action, "low": low, "high": high, "size": size, "prior_high": prior_high,
        "expected": expected, "expected_action": expected_action, "vote": parsed.get("vote", ""),
        "dissents": dissents, "dissent_net": PW.dissent_net(dissents, action, size),
        "prev_dissent_net": previous_dissent_net(prev),
        "changes": PW.statement_diff((prev or {}).get("statement", ""), parsed.get("text", "")),
        # the officials' rate projections, published at four meetings a
        # year; None until a projection fetcher exists
        "projection_signal": None,
        "url": parsed.get("url", ""),
    })
