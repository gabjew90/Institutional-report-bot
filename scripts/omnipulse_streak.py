#!/usr/bin/env python3
"""The Omnipulse retirement streak (spec 2026-09-26: the classic pulse
retires after 10 consecutive market days with body_source: omnipulse).

A light day whose reason is no new bank research is NEUTRAL (owner,
2026-10-08): it neither counts toward the streak nor breaks it, since no
Omnipulse could have been written. Every other non-Omnipulse day breaks it.

Reads the archived pulses on the pulse-data branch:

    git fetch origin pulse-data
    py -3.12 scripts/omnipulse_streak.py            # since 2026-09-28
    py -3.12 scripts/omnipulse_streak.py --since 2026-10-01
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TARGET = 10
NEUTRAL_REASON = "no new bank research"


def frontmatter(md: str) -> dict:
    m = re.match(r"---\n(.*?)\n---", md or "", re.S)
    out = {}
    for ln in (m.group(1).splitlines() if m else []):
        if ":" in ln:
            k, v = ln.split(":", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def classify(meta: dict) -> str:
    """'omnipulse', 'neutral' or 'miss'."""
    src = (meta.get("body_source") or "classic").lower()
    if src == "omnipulse":
        return "omnipulse"
    if src == "light" and (meta.get("light_reason") or "").lower().startswith(NEUTRAL_REASON):
        return "neutral"
    return "miss"


def streak(days: list[tuple[str, str]]) -> int:
    """Consecutive Omnipulse days ending at the latest, neutral days skipped.
    `days` is [(date, kind)] in date order."""
    n = 0
    for _date, kind in reversed(days):
        if kind == "neutral":
            continue
        if kind != "omnipulse":
            break
        n += 1
    return n


def archive_days(ref: str = "origin/pulse-data", since: str = "2026-09-28") -> list[tuple[str, str, dict]]:
    """[(date, kind, meta)], one per date (the last archived pulse of the day)."""
    names = subprocess.run(["git", "ls-tree", "--name-only", ref, "pulse-output/archive/"],
                           capture_output=True, text=True, check=True).stdout.split()
    by_date: dict[str, str] = {}
    for n in sorted(names):
        d = n.rsplit("/", 1)[-1][:10]
        if d >= since:
            by_date[d] = n
    out = []
    for d in market_days(since, max(by_date) if by_date else since):
        if d not in by_date:
            out.append((d, "miss", {"body_source": "none"}))  # no pulse published
            continue
        md = subprocess.run(["git", "show", f"{ref}:{by_date[d]}"], capture_output=True,
                            text=True, encoding="utf-8", check=True).stdout
        meta = frontmatter(md)
        out.append((d, classify(meta), meta))
    return out


def market_days(start: str, end: str) -> list[str]:
    """NYSE session dates from start to end inclusive."""
    from world_context import next_trading_day
    from datetime import date
    d = start if date.fromisoformat(start).weekday() < 5 and not is_holiday(start)         else next_trading_day(start)
    out = []
    while d <= end:
        out.append(d)
        d = next_trading_day(d)
    return out


def is_holiday(d: str) -> bool:
    from world_context import is_us_market_holiday
    return bool(is_us_market_holiday(d))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-28")
    ap.add_argument("--ref", default="origin/pulse-data")
    a = ap.parse_args(argv)
    days = archive_days(a.ref, a.since)
    for d, kind, meta in days:
        why = meta.get("light_reason") or ""
        print(f"{d}  {meta.get('body_source', 'classic'):9}  {kind:9}  {why}")
    n = streak([(d, k) for d, k, _ in days])
    print(f"streak: {n} of {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
