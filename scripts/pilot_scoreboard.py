"""Render pilot/scoreboard.md from shadow/*.meta.json and ops/*.json.

One row per edition, from numbers the pipeline computes in code:

  citations     card and brief citations in the edition
  failed        sentences the citation check could not vouch for after
                the editor's one repair pass (pilot_verify_citations.py).
                From the 2026-11-24 edition on they are dropped from the
                body before it is published
  unread@edit   source files still unread when the editor ran. Any such
                day is VOID: the edition was written from an incomplete
                card set, and production does not take its body
                (omnipulse_body.problems)
  given up      documents the readers gave up on, a coverage gap
  edge          share of citations in the first and last fifth of the
                pack, flagged above 70% (the editor reading only the
                ends of a long pack)
  readers       reader failure rate under 10% and no read colliding
                with the pulse window (ops/<date>.json)

Model grading was retired on 2026-11-23 (owner). Two agents per
dimension graded each edition against the source text three hours after
it was published, took 42 to 73 minutes a day, and fed a ten-day verdict
whose day 1 was never set: the decision it was built for was taken on
2026-09-26 without it. The grades through 2026-11-20 stay under
pilot/grades/ on pilot-data, and the scoreboard that showed them is in
that file's history. This file is rendered by the editor workflow after
each edition.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

RETIRED_NOTE = ("Model grading was retired on 2026-11-23. Grades through 2026-11-20 are under "
                "`pilot/grades/`, and the scoreboard that showed them is in this file's history.")


def _load(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def collect(pilot_root: str) -> dict:
    days: dict[str, dict] = defaultdict(dict)
    for path in sorted(glob.glob(os.path.join(pilot_root, "shadow", "*.meta.json"))):
        days[os.path.basename(path)[:10]]["shadow_meta"] = _load(path) or {}
    for path in sorted(glob.glob(os.path.join(pilot_root, "ops", "*.json"))):
        days[os.path.basename(path)[:10]]["ops"] = _load(path) or {}
    return dict(days)


def is_void(d: dict) -> bool:
    """A day whose editor ran with source files still unread was written
    from an incomplete (on 2026-09-03, an entirely stale) card set."""
    return bool((d.get("shadow_meta") or {}).get("unread_source_files_at_edit"))


def day_row(date: str, d: dict) -> dict | None:
    """The day's numbers, or None when no edition was written that day
    (a reader-only ops record is not an edition)."""
    meta = d.get("shadow_meta")
    if meta is None:
        return None
    cit = meta.get("citations") or {}
    ops = d.get("ops") or {}
    rate = ops.get("reader_failure_rate")
    return {
        "date": date,
        "void": is_void(d),
        "citations": (cit.get("card_citations") or 0) + (cit.get("brief_citations") or 0),
        "failed": len(cit.get("failures") or []),
        "unread_at_edit": meta.get("unread_source_files_at_edit"),
        "given_up_at_edit": meta.get("given_up_at_edit"),
        "edge_share": cit.get("edge_quintile_share"),
        "edge_flag": bool(cit.get("metric4_flag")),
        "readers_ok": (None if rate is None
                       else bool(rate < 0.10 and not ops.get("collided_with_pulse_window"))),
        "structural_problems": meta.get("structural_problems") or [],
    }


def fmt(v, pct=False):
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:.0%}" if pct else f"{v:.2f}"
    return str(v)


def render(pilot_root: str) -> str:
    days = collect(pilot_root)
    rows = [r for r in (day_row(d, days[d]) for d in sorted(days)) if r]
    out = ["# Omnipulse editions", "", RETIRED_NOTE, "",
           "| day | citations | failed | unread@edit | given up | edge | readers ok | note |",
           "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        note = []
        if r["void"]:
            note.append("VOID (stale cards)")
        if r["structural_problems"]:
            note.append("failed edit: " + "; ".join(r["structural_problems"]))
        out.append("| " + " | ".join([
            r["date"], fmt(r["citations"]), fmt(r["failed"]), fmt(r["unread_at_edit"]),
            fmt(r["given_up_at_edit"]),
            fmt(r["edge_share"], pct=True) + (" FLAG" if r["edge_flag"] else ""),
            fmt(r["readers_ok"]), ", ".join(note),
        ]) + " |")
    out.append("")
    last = rows[-30:]
    if last:
        cites = sum(r["citations"] for r in last)
        failed = sum(r["failed"] for r in last)
        out += [f"Last {len(last)} editions: {failed} failed sentences against {cites} citations, "
                f"{sum(1 for r in last if r['void'])} void, "
                f"{sum(1 for r in last if r['edge_flag'])} flagged for edge citing, "
                f"{sum(1 for r in last if r['readers_ok'] is False)} with reader failures.", ""]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot-root", required=True)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    text = render(a.pilot_root)
    out = a.out or os.path.join(a.pilot_root, "scoreboard.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"scoreboard -> {out} ({len(text)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
