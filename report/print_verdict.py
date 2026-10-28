"""The alert's one judgment, decided by written rules (2026-10-28 redesign,
spec docs/superpowers/specs/2026-10-28-print-alerts-deterministic-design.md).

Each input becomes a signal (+1 above or stronger, -1 below or weaker, 0
in line, None when there is no forecast), and a truth table per release
turns the signals into the title and a direction (hawkish, dovish or
none). The title is the lock-screen line, so it is never a model's
judgment. `policy_state` reads what the Fed has done from the bot's own
FOMC ledger, so the takeaway's Fed sentence fits the current regime
without anyone keeping it up to date.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from report.print_data import CORE_MOM, Release

HAWKISH, DOVISH = "hawkish", "dovish"
# Payrolls within this many thousand of forecast count as in line (owner
# default, 2026-10-28): the market reacts to misses of about 50K, though
# BLS's own margin of error on the monthly change is about +-130K.
PAYROLL_BAND_K = 50
# The policy state looks back this many decisions for the last move.
POLICY_LOOKBACK = 4

_CORE_YOY = {"cpi": "Core CPI y/y", "pce": "Core PCE y/y", "ppi": "Core PPI y/y"}
_HEADLINE_MOM = {"cpi": "CPI m/m", "pce": "PCE m/m", "ppi": "PPI m/m"}


@dataclass
class Verdict:
    label: str                       # the title after the colon: "Hotter than expected"
    kind: str                        # what the templates branch on
    direction: str | None = None     # hawkish / dovish / None
    signals: dict = field(default_factory=dict)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def row_signal(row: dict | None) -> int | None:
    """build_rows' verdict (compared at the published precision) as a signal."""
    if not row:
        return None
    return {"above": 1, "below": -1, "in line": 0}.get(row.get("verdict") or "")


def inflation_verdict(rel: Release) -> Verdict:
    core = row_signal(rel.rows.get(CORE_MOM[rel.key]))
    if core is None:
        core = row_signal(rel.rows.get(_CORE_YOY[rel.key]))
    head = row_signal(rel.rows.get(_HEADLINE_MOM[rel.key]))
    sig = {"core": core, "headline": head}
    if core is None:
        return Verdict("Released, no forecast to compare", "no_forecast", None, sig)
    if core == 0:
        return Verdict("In line with expectations", "in_line", None, sig)
    if head == -core:
        return Verdict("Mixed", "mixed", None, sig)
    if core == 1:
        return Verdict("Hotter than expected", "hotter", HAWKISH, sig)
    return Verdict("Cooler than expected", "cooler", DOVISH, sig)


def payroll_signal(rel: Release) -> tuple[int | None, int]:
    """(signal, cancelled): cancelled is the surprise's sign when the
    two-month revision runs the other way and is larger than it, which
    turns the signal to 0; else 0."""
    row = rel.rows.get("Non Farm Payrolls")
    if not row or row.get("actual_value") is None or row.get("consensus_value") is None:
        return None, 0
    diff = row["actual_value"] - row["consensus_value"]
    if abs(diff) <= PAYROLL_BAND_K:
        return 0, 0
    surprise = _sign(diff)
    net = (rel.payroll_revisions or {}).get("net")
    if net is not None and _sign(net) == -surprise and abs(net) > abs(diff):
        return 0, surprise
    return surprise, 0


def jobs_verdict(rel: Release) -> Verdict:
    pay, cancelled = payroll_signal(rel)
    unemp = row_signal(rel.rows.get("Unemployment Rate"))
    unemp = -unemp if unemp else unemp          # a lower jobless rate is the stronger reading
    wages = row_signal(rel.rows.get("Avg Hourly Earnings m/m"))
    sig = {"payrolls": pay, "unemployment": unemp, "wages": wages, "cancelled": cancelled}
    if pay is None:
        return Verdict("Released, no forecast to compare", "no_forecast", None, sig)
    vals = [s for s in (pay, unemp, wages) if s is not None]
    up, down = vals.count(1), vals.count(-1)
    if up and down:
        return Verdict("Mixed", "mixed", None, sig)
    if up >= 2:
        return Verdict("Stronger than expected", "stronger", HAWKISH, sig)
    if down >= 2:
        return Verdict("Weaker than expected", "weaker", DOVISH, sig)
    if up == 1:
        return Verdict("Slightly stronger than expected", "slightly_stronger", HAWKISH, sig)
    if down == 1:
        return Verdict("Slightly weaker than expected", "slightly_weaker", DOVISH, sig)
    return Verdict("In line with expectations", "in_line", None, sig)


def fomc_verdict(rel: Release) -> Verdict:
    """Decision against what was expected first, then the officials'
    projections, then the change in dissents. The changed statement
    wording is shown but never decides: code cannot judge its tone."""
    f = rel.fomc or {}
    action = f.get("action", "hold")
    expected = f.get("expected")
    decision = None if expected is None else _sign(round(f["high"] - expected, 4))
    proj = f.get("projection_signal")
    prev = f.get("prev_dissent_net")
    dissent = None if prev is None else _sign(f.get("dissent_net", 0) - prev)
    sig = {"decision": decision, "projections": proj, "dissents": dissent}
    nonzero = [s for s in (proj, dissent) if s]
    secondary = "mixed" if (1 in nonzero and -1 in nonzero) else (nonzero[0] if nonzero else 0)
    word = action.capitalize()
    if decision == 1:
        return Verdict(f"Surprise {action}, hawkish", "surprise", HAWKISH, sig)
    if decision == -1:
        return Verdict(f"Surprise {action}, dovish", "surprise", DOVISH, sig)
    if secondary == 1:
        return Verdict(f"Hawkish {action}", HAWKISH, HAWKISH, sig)
    if secondary == -1:
        return Verdict(f"Dovish {action}", DOVISH, DOVISH, sig)
    if secondary == "mixed":
        return Verdict(f"{word}, mixed signals", "mixed", None, sig)
    if decision == 0:
        return Verdict(f"{word} as expected", "as_expected", None, sig)
    return Verdict(word, "no_forecast", None, sig)


def verdict_for(rel: Release) -> Verdict:
    if rel.incomplete:
        return Verdict("Incomplete data", "incomplete")
    if rel.key == "fomc":
        return fomc_verdict(rel)
    if rel.key == "jobs":
        return jobs_verdict(rel)
    return inflation_verdict(rel)


def policy_state(actions: list[str | None]) -> dict:
    """{'last_action', 'direction'} from the FOMC decisions in the ledger,
    newest first. Direction is the last move within POLICY_LOOKBACK
    decisions: a hike gives tightening, a cut easing, none neutral."""
    known = [a for a in actions if a in ("hike", "hold", "cut")]
    direction = "neutral"
    for a in known[:POLICY_LOOKBACK]:
        if a == "hike":
            direction = "tightening"
            break
        if a == "cut":
            direction = "easing"
            break
    return {"last_action": known[0] if known else None, "direction": direction}
