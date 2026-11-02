"""A revenue growth figure is checked against the year-ago revenue in the snapshot.

2026-10-29 audit A13: SanDisk's quarter was given as "$11.03B (up +23% y/y)".
23% was the change from the prior quarter, lifted from a news line, and the
snapshot in the same turn showed the year-ago quarter: about +378%. The figure
is corrected in place from data, with no model involved.
"""
from __future__ import annotations

import re

_UNITS = {"M": 1e6, "B": 1e9, "T": 1e12}
_CLAIM_RE = re.compile(
    r"\b(?:revenue|sales)\b[^.\n→]{0,40}?\$(\d+(?:\.\d+)?)\s?([MBT])\b"
    r"(?P<between>[^.\n→]{0,40}?)"
    r"(?P<sign>[+-]?)(?P<pct>\d+(?:\.\d+)?)\s?%(?P<tail>\*{0,2}\s*(?:y/y|yoy|year[- ]over[- ]year)\b)",
    re.I)
# Another metric between the revenue and the percentage owns the percentage.
_OTHER_METRIC_RE = re.compile(
    r"\b(?:eps|earnings|margins?|profits?|income|ebitda|guid\w*|shares?|stock|users?|subscribers?)\b|\$",
    re.I)
_DOWN_RE = re.compile(r"\b(?:down|fell|fall|drop|declin|shrank)", re.I)
MIN_GAP_POINTS = 5.0
SAME_QUARTER = 0.01         # a reported figure, to within rounding
NEAR_CONSENSUS = 0.15       # a print against the consensus row for its quarter


def _year_over_year(revenue: float, growth: dict, fresh_print: bool) -> float | None:
    for r in growth.get("revenue_reports") or []:
        rev = r.get("revenue")
        if rev and r.get("yoy_pct") is not None and abs(rev - revenue) / rev <= SAME_QUARTER:
            return float(r["yoy_pct"])
    # Hours after a print Yahoo has no reported row yet, and the quarter is
    # still its consensus row, which carries the year-ago revenue. Only then:
    # on any other day that row is a different quarter from the one named.
    row = growth.get("next_quarter") or {}
    est, ago = row.get("revenue"), row.get("revenue_year_ago")
    if fresh_print and est and ago and abs(est - revenue) / revenue <= NEAR_CONSENSUS:
        return (revenue / ago - 1) * 100
    return None


def correct(answer: str, growth: dict | None, fresh_print: bool = False) -> tuple[str, list[str]]:
    """(the answer with wrong revenue y/y figures replaced, what changed)."""
    if not answer or not growth:
        return answer, []
    changed: list[str] = []

    def _sub(m: re.Match) -> str:
        if _OTHER_METRIC_RE.search(m.group("between")):
            return m.group(0)
        revenue = float(m.group(1)) * _UNITS[m.group(2).upper()]
        expected = _year_over_year(revenue, growth, fresh_print)
        if expected is None:
            return m.group(0)
        claimed = float(m.group("pct"))
        if m.group("sign") == "-" or _DOWN_RE.search(m.group("between")):
            claimed = -claimed
        if abs(claimed - expected) < MIN_GAP_POINTS:
            return m.group(0)
        changed.append(f"{claimed:+.0f}% -> {expected:+.0f}%")
        between = m.group("between")
        if expected >= 0:
            between = re.sub(r"\bdown\b", "up", between, flags=re.I)
        else:
            between = re.sub(r"\bup\b", "down", between, flags=re.I)
        head = m.group(0)[:m.start("between") - m.start()]
        return f"{head}{between}{expected:+.0f}%{m.group('tail')}"

    return _CLAIM_RE.sub(_sub, answer), changed
