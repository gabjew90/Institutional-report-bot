"""The evidence footer on a rank answer (owner, 2026-09-26: "whenever
people ask their ranks it should come with the evidence").

`lookup_user_profile` returns each rank with the record behind it
(db.lookup_user_ranks: `racism_evidence`, `trader_evidence`). The model is
free to phrase the answer, but the record is rendered here in code and
appended after it, so a rank never ships without its counts regardless
of what the model chose to mention. Built from the tool payloads the
answer was written from, never from the answer text.
"""
from __future__ import annotations

import re

_RACISM_WORDS = re.compile(r"\b(racis\w*|slur\w*|racial|race|n-word)\b", re.I)
_TRADER_WORDS = re.compile(r"\b(trader?s?|trading|trade[sd]?|win\w*|loss\w*|ledger)\b", re.I)

MAX_LINES = 12
_QUOTE_CAP = 90


def _name(u: dict) -> str:
    return u.get("display_name") or u.get("username") or "?"


def _racism_line(u: dict, rank, total) -> str:
    ev = u.get("racism_evidence") or {}
    n = ev.get("messages")
    edged = ev.get("race_edged", 0)
    days = ev.get("window_days", 30)
    if rank:
        head = f"racism #{rank}/{total}"
    else:
        head = "racism unranked"
    body = f"{edged} race-edged message{'s' if edged != 1 else ''}"
    if n:
        body += f" of {n:,} in {days}d"
        if ev.get("per_100") is not None and edged:
            body += f" ({ev['per_100']} per 100)"
    else:
        body += f" in {days}d"
    slurs = ev.get("racial_slurs") or 0
    if slurs:
        body += f", {slurs} racial slur{'s' if slurs != 1 else ''}"
    tail = ""
    if "prior_rank" in ev and (ev.get("prior_coverage") or 0) >= 0.95:
        pr = ev.get("prior_rank")
        tail = (f" · {days}d before: #{pr}/{ev.get('prior_rank_total')}"
                if pr else f" · {days}d before: unranked")
    cov = ev.get("coverage")
    if cov is not None and cov < 0.95:
        tail += f" · tagging {round(cov * 100)}% done"
    return f"{head}: {body}{tail}"


def _trader_line(u: dict, rank, total) -> str:
    ev = u.get("trader_evidence") or {}
    head = f"trader #{rank}/{total}" if rank else "trader unranked"
    body = (f"{ev.get('wins', 0)}W/{ev.get('losses', 0)}L documented in "
            f"{ev.get('window_days', 21)}d, {ev.get('receipt_points', 0)} win pts")
    if ev.get("ghosted"):
        body += f", {ev['ghosted']} opened with no close logged"
    if ev.get("avg_gain_pct_on_closes") is not None:
        body += f", avg {ev['avg_gain_pct_on_closes']:+d}% on closes"
    return f"{head}: {body}"


def _quotes(u: dict, n: int = 2) -> list[str]:
    out = []
    for ex in ((u.get("racism_evidence") or {}).get("examples") or [])[:n]:
        text = ex.get("text") or ""
        if len(text) > _QUOTE_CAP:
            text = text[:_QUOTE_CAP].rstrip() + "…"
        out.append(f'  "{text}" ({ex.get("date", "")[5:]})')
    return out


def footer(answer: str, payloads: list[dict] | None) -> str:
    """Render the evidence block for every ranked user the answer's tool
    calls returned. Empty string when there is nothing to show."""
    if not payloads:
        return ""
    wants_racism = bool(_RACISM_WORDS.search(answer or ""))
    wants_trader = bool(_TRADER_WORDS.search(answer or ""))
    if not wants_racism and not wants_trader:
        wants_racism = wants_trader = True
    lines: list[str] = []
    seen: set = set()
    for p in payloads:
        mode = p.get("mode")
        for u in p.get("users") or []:
            if mode == "single_user":
                key = ("single", u.get("user_id"))
                if key in seen:
                    continue
                seen.add(key)
                bits = []
                if wants_trader and u.get("trader_evidence") is not None:
                    bits.append(_trader_line(u, u.get("trader_rank"),
                                             u.get("trader_rank_total")))
                if wants_racism and u.get("racism_evidence") is not None:
                    bits.append(_racism_line(u, u.get("racism_rank"),
                                             u.get("racism_rank_total")))
                if bits:
                    lines.append(f"{_name(u)} · " + " · ".join(bits))
                    if wants_racism:
                        lines.extend(_quotes(u))
            else:
                metric = u.get("metric") or p.get("metric")
                key = (metric, u.get("user_id"))
                if key in seen:
                    continue
                seen.add(key)
                if metric == "racism":
                    lines.append(f"{_name(u)} · " + _racism_line(
                        u, u.get("rank"), u.get("rank_total")))
                    if mode == "rank_position":
                        lines.extend(_quotes(u))
                elif metric == "trader":
                    lines.append(f"{_name(u)} · " + _trader_line(
                        u, u.get("rank"), u.get("rank_total")))
    if not lines:
        return ""
    if len(lines) > MAX_LINES:
        lines = lines[:MAX_LINES]
    return "\n\nEvidence:\n" + "\n".join(lines)
