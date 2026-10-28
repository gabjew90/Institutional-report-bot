"""The alert as Discord shows it (2026-10-28 redesign, spec
docs/superpowers/specs/2026-10-28-print-alerts-deterministic-design.md).

The message text is the lock-screen line ("Sep CPI: Hotter than
expected"). The embed has no title: the primary numbers, a blank line,
the secondary ones, a blank line, the takeaway. One metric per line and
no column alignment, since Discord's font is proportional and most of
the room reads on a phone.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from report.print_data import Release
from report.print_templates import month_name
from report.print_verdict import Verdict

# A data line longer than this wraps on a phone (tests check it).
MAX_DATA_LINE = 60

_PRIMARY = {
    "cpi": ["Core CPI m/m", "Core CPI y/y", "CPI m/m", "CPI y/y"],
    "ppi": ["Core PPI m/m", "Core PPI y/y", "PPI m/m", "PPI y/y"],
    "pce": ["Core PCE m/m", "Core PCE y/y", "PCE m/m", "PCE y/y"],
}
_SECONDARY = {
    "cpi": ["Shelter m/m", "Food m/m", "Energy m/m"],
    "ppi": ["PPI ex trade m/m"],
    "pce": ["Real Consumer Spending m/m", "Personal Spending m/m", "Personal Income m/m", "Saving Rate"],
    "jobs": ["Participation Rate", "Avg Weekly Hours"],
}
_THREE_M = {"cpi": "Core CPI 3M", "pce": "Core PCE 3M"}
_SHORT_MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass
class Alert:
    content: str                     # the message text, without the mention
    lines: list[str] = field(default_factory=list)
    footer: str = ""


def _unrounded(label: str, value: float) -> str:
    return f"{value:.2f}%" if label == "Unemployment Rate" else f"{value:+.2f}%"


def metric_line(row: dict, unrounded: float | None = None) -> str:
    """'Core CPI MoM +0.3% vs +0.2% expected (+0.31% unrounded)'."""
    line = f"{row['display']} {row['actual']}"
    if row.get("consensus") is not None:
        line += f" vs {row['consensus']} expected"
    if unrounded is not None:
        line += f" ({_unrounded(row['label'], unrounded)} unrounded)"
    return line


def _rows(rel: Release, labels: list[str]) -> list[str]:
    out = []
    for label in labels:
        row = rel.rows.get(label)
        if not row or row.get("actual") is None or row.get("hidden"):
            continue
        # nominal spending only when the feed forecast it: real spending
        # is the measure, and the forecast is for the nominal one
        if label == "Personal Spending m/m" and row.get("consensus") is None:
            continue
        out.append(metric_line(row, rel.unrounded.get(label)))
    return out


def _revised(month: str, now: str, was: str, what: str = "") -> str:
    return f"{month}{' ' + what if what else ''} revised to {now} from {was}"


def _clock(release_et: str) -> str:
    t = datetime.strptime(release_et, "%H:%M")
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _dissent_line(g: dict) -> str:
    size = {0.25: "quarter-point ", 0.5: "half-point "}.get(round(g["size"], 2) if g.get("size") else None, "")
    what = "no change" if g["move"] == "hold" else f"a {size}{g['move']}"
    return f"Dissent: {_names(g['names'])} wanted {what}"


def _fomc_blocks(rel: Release, v: Verdict) -> tuple[list[str], list[str]]:
    f = rel.fomc or {}
    word = f["action"].capitalize()
    decision = v.signals.get("decision")
    if decision == 0:
        first = f"Decision: {word}, as expected"
    elif decision is not None and f.get("expected_action"):
        first = f"Decision: {word}, expected {f['expected_action']}"
    elif decision is not None:
        first = f"Decision: {word}, not expected"
    else:
        first = f"Decision: {word}"
    primary = [first, f"Target range: {f['low']:.2f}% to {f['high']:.2f}%"]
    if f.get("vote"):
        primary.append(f"Vote: {f['vote']}")
    secondary = [_dissent_line(g) for g in f.get("dissents") or []]
    changes = f.get("changes") or []
    if changes:
        secondary.append("Statement changes:")
        for was, now in changes:
            if was:
                secondary.append(f"Was: {was}")
            secondary.append(f"Now: {now}" if now else "Now: removed")
    return primary, secondary


def _data_blocks(rel: Release) -> tuple[list[str], list[str]]:
    if rel.key == "jobs":
        rev = rel.payroll_revisions or {}
        months = rev.get("months") or []
        primary = _rows(rel, ["Non Farm Payrolls"])
        if months:
            label = "2-month revision" if len(months) == 2 else "Prior-month revision"
            primary.append(f"{label} {rev['net']:+,.0f}K")
        primary += _rows(rel, ["Unemployment Rate", "Avg Hourly Earnings m/m", "Avg Hourly Earnings y/y"])
        secondary = [_revised(month_name(m["period"]), f"{m['now']:+,.0f}K", f"{m['was']:+,.0f}K")
                     for m in months]
        secondary += _rows(rel, _SECONDARY["jobs"])
        return primary, secondary
    primary = _rows(rel, _PRIMARY[rel.key])
    if rel.three_m is not None and rel.key in _THREE_M:
        primary.append(f"{_THREE_M[rel.key]} {rel.three_m:.1f}%")
    secondary = _rows(rel, _SECONDARY[rel.key])
    secondary += [_revised(month_name(r["period"]), f"{r['now']:+.1f}%", f"{r['was']:+.1f}%", "core")
                  for r in rel.prior_revisions]
    return primary, secondary


def footer(spec, rel: Release, release_et: str) -> str:
    when = f"released {_clock(release_et)} ET"
    if rel.key == "fomc":
        return f"Federal Reserve statement · {when}"
    forecast = " · forecast: ForexFactory" if rel.has_consensus else ""
    return f"{spec.agency}{forecast} · {when}"


def render(spec, rel: Release, v: Verdict, takeaway: str, release_et: str) -> Alert:
    """`spec` is the release's print_watch.ReleaseSpec."""
    primary, secondary = _fomc_blocks(rel, v) if rel.key == "fomc" else _data_blocks(rel)
    lines = list(primary)
    if secondary:
        lines += [""] + secondary
    if takeaway:
        lines += ["", f"Takeaway: {takeaway}"]
    content = f"{_SHORT_MONTH[int(rel.period[5:7]) - 1]} {spec.name}: {v.label}"
    return Alert(content=content, lines=lines, footer=footer(spec, rel, release_et))
