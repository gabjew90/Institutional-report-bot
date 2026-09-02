"""Render pilot/scoreboard.md from grades/**, shadow/*.meta.json and ops.

Per day, per metric, against the FROZEN thresholds (spec section 8):
  1  fragmented mass <= 10% and no theme-changing mis-merge
  2  shadow faithful-rate >= production and zero unsupported
  2a zero MATERIAL distortions (non-material soft ceiling 20%: flag)
  3  shadow preserved-days >= production over the window
  4  flag when > 70% of citations sit in the first and last quintiles
  5  reader failure rate < 10%, no read colliding with the pulse window

Two agents per dimension: a day's grade is the agreement; a
disagreement is shown and left for the owner's tiebreak (spec). Days
before the DAY1 marker are shakedown: shown, never counted. Metrics 2
and 2a are split by reader tier (plan 3.2).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import defaultdict

SCOPE_LIMIT = ("Scope limit (plan 6): ~19 HIGH PDFs/day is the lightest month on "
               "record. A passing fragmentation number certifies the architecture at "
               "light corpus load only.")


def _load(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def day1(pilot_root: str) -> str | None:
    p = os.path.join(pilot_root, "DAY1")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return fh.read().strip()[:10] or None
    return None


def collect(pilot_root: str) -> dict:
    days: dict[str, dict] = defaultdict(lambda: {"grades": defaultdict(dict)})
    for path in sorted(glob.glob(os.path.join(pilot_root, "grades", "*", "*.json"))):
        date = os.path.basename(os.path.dirname(path))
        name = os.path.basename(path)[:-5]  # e.g. fidelity-shadow-a
        doc = _load(path)
        if doc is None:
            continue
        parts = name.split("-")
        agent = parts[-1]
        dim = "-".join(parts[:-1])
        days[date]["grades"][dim][agent] = doc
    for path in sorted(glob.glob(os.path.join(pilot_root, "shadow", "*.meta.json"))):
        date = os.path.basename(path)[:10]
        days[date]["shadow_meta"] = _load(path) or {}
    for path in sorted(glob.glob(os.path.join(pilot_root, "ops", "*.json"))):
        date = os.path.basename(path)[:10]
        days[date]["ops"] = _load(path) or {}
    return dict(days)


def agree(a, b, key, tol=0.0):
    if a is None or b is None:
        return None, "one agent missing"
    va, vb = a.get(key), b.get(key)
    if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
        return ((va + vb) / 2, "") if abs(va - vb) <= tol else (None, f"disagree {va} vs {vb}")
    return (va, "") if va == vb else (None, f"disagree {va} vs {vb}")


def _apply_tiebreaks(grades: dict) -> dict:
    """An owner tiebreak (agent `tiebreak` or `owner`, see RUNBOOK) is
    the day's call for that dimension: it replaces both agent grades so
    the disagreement clears. Review 2026-09-01: it was recorded and
    ignored."""
    out = {}
    for dim, agents in grades.items():
        tb = agents.get("tiebreak") or agents.get("owner")
        out[dim] = {"a": tb, "b": tb} if tb else agents
    return out


def day_row(date: str, d: dict) -> dict:
    g = _apply_tiebreaks(d["grades"])
    row = {"date": date}
    # metric 1
    ga, gb = g.get("grouping", {}).get("a"), g.get("grouping", {}).get("b")
    share, note = agree(ga, gb, "fragmented_mass_share", tol=0.05)
    merges = None
    if ga and gb:
        merges = any(m.get("would_change_theme_selection") for x in (ga, gb) for m in (x.get("mis_merges") or []))
    row["m1"] = {"share": share, "theme_changing_merge": merges, "note": note,
                 "pass": (share is not None and share <= 0.10 and merges is False)}
    # metric 2 per artifact
    for art in ("shadow", "production"):
        a_, b_ = g.get(f"fidelity-{art}", {}).get("a"), g.get(f"fidelity-{art}", {}).get("b")
        rate, note = agree(a_, b_, "faithful_rate", tol=0.14)
        unsup = None
        if a_ and b_:
            unsup = max(a_.get("unsupported", 0) or 0, b_.get("unsupported", 0) or 0)
        row[f"m2_{art}"] = {"rate": rate, "unsupported": unsup, "note": note}
    s, p = row["m2_shadow"], row["m2_production"]
    row["m2_pass"] = (s["rate"] is not None and p["rate"] is not None
                      and s["rate"] >= p["rate"] and s["unsupported"] == 0)
    # metric 2a with tier split
    a_, b_ = g.get("brief_fidelity", {}).get("a"), g.get("brief_fidelity", {}).get("b")
    mat, note = agree(a_, b_, "material_total")
    # Both agents audit the SAME briefs: count each brief once, taking
    # the stricter agent's call (review 2026-09-01: audited was doubled).
    per_brief: dict[str, dict] = {}
    for x in (a_, b_):
        for br in (x or {}).get("briefs") or []:
            bid = br.get("id") or f"{br.get('bank')}:{br.get('tier')}"
            cur = per_brief.setdefault(bid, {"tier": br.get("tier") or "?", "material": 0, "non_material": 0})
            cur["material"] = max(cur["material"], br.get("material_count", 0) or 0)
            cur["non_material"] = max(cur["non_material"], br.get("non_material_count", 0) or 0)
    tiers = defaultdict(lambda: {"audited": 0, "material": 0, "non_material": 0})
    for v in per_brief.values():
        t = tiers[v["tier"]]
        t["audited"] += 1
        t["material"] += v["material"]
        t["non_material"] += v["non_material"]
    audited = len(per_brief)
    # The soft ceiling is "20% of audited BRIEFS carry a non-material
    # distortion", not a count of distortions (day 1 rendered 333%).
    briefs_with_nonmat = sum(1 for v in per_brief.values() if v["non_material"] > 0)
    row["m2a"] = {"material": mat, "note": note, "tiers": dict(tiers),
                  "non_material_share": round(briefs_with_nonmat / audited, 2) if audited else None,
                  "pass": mat == 0}
    # metric 3 per artifact
    for art in ("shadow", "production"):
        a_, b_ = g.get(f"mechanism-{art}", {}).get("a"), g.get(f"mechanism-{art}", {}).get("b")
        pres, note = agree(a_, b_, "preserved")
        row[f"m3_{art}"] = {"preserved": pres, "note": note}
    # metric 4 from the editor's citation meta
    cit = (d.get("shadow_meta") or {}).get("citations") or {}
    row["m4"] = {"edge_share": cit.get("edge_quintile_share"), "flag": cit.get("metric4_flag"),
                 "failures": len(cit.get("failures") or [])}
    row["unread_at_edit"] = (d.get("shadow_meta") or {}).get("unread_source_files_at_edit")
    ops = d.get("ops") or {}
    row["m5"] = {"reader_failure_rate": ops.get("reader_failure_rate"),
                 "collided_with_pulse_window": ops.get("collided_with_pulse_window"),
                 "pass": (ops.get("reader_failure_rate") is not None
                          and ops["reader_failure_rate"] < 0.10
                          and not ops.get("collided_with_pulse_window"))}
    row["disagreements"] = [k for k in ("m1", "m2_shadow", "m2_production", "m2a", "m3_shadow", "m3_production")
                            if row[k].get("note", "").startswith("disagree")]
    return row


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
    d1 = day1(pilot_root)
    counted = [d for d in sorted(days) if d1 and d >= d1]
    out = ["# Shadow pilot scoreboard", "",
           f"Day 1: {d1 or 'NOT SET (every day below is shakedown, uncounted)'}. "
           f"Counted days: {len(counted)} of 10.", "", SCOPE_LIMIT, "",
           "| day | counted | m1 frag | m1 merge | m2 shadow | m2 prod | m2 pass | 2a material | 2a non-mat | m3 shadow | m3 prod | m4 edge | m5 | unread@edit | tiebreak |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for date in sorted(days):
        r = day_row(date, days[date])
        r["counted"] = bool(d1) and date >= d1
        rows.append(r)
        out.append("| " + " | ".join([
            date, "yes" if r["counted"] else "shakedown",
            fmt(r["m1"]["share"], pct=True), fmt(r["m1"]["theme_changing_merge"]),
            fmt(r["m2_shadow"]["rate"], pct=True), fmt(r["m2_production"]["rate"], pct=True),
            fmt(r["m2_pass"]), fmt(r["m2a"]["material"]), fmt(r["m2a"]["non_material_share"], pct=True),
            fmt(r["m3_shadow"]["preserved"]), fmt(r["m3_production"]["preserved"]),
            fmt(r["m4"]["edge_share"], pct=True) + (" FLAG" if r["m4"]["flag"] else ""),
            fmt(r["m5"]["pass"]), fmt(r["unread_at_edit"]),
            ", ".join(r["disagreements"]) or "",
        ]) + " |")
    out.append("")
    c = [r for r in rows if r["counted"]]
    if c:
        m1 = all(r["m1"]["pass"] for r in c)
        m2 = all(r["m2_pass"] for r in c)
        m2a = all(r["m2a"]["pass"] for r in c)
        sh = sum(1 for r in c if r["m3_shadow"]["preserved"] is True)
        pr = sum(1 for r in c if r["m3_production"]["preserved"] is True)
        m3 = sh >= pr
        soft = [r for r in c if (r["m2a"]["non_material_share"] or 0) > 0.20]
        pending = [r["date"] for r in c if r["disagreements"]]
        out += ["## Running verdict (counted days only)", "",
                f"- metric 1 grouping: {'PASS' if m1 else 'FAIL'} so far",
                f"- metric 2 fact fidelity: {'PASS' if m2 else 'FAIL'} so far",
                f"- metric 2a brief fidelity: {'PASS' if m2a else 'FAIL'} so far"
                + (f"; non-material soft ceiling breached on {len(soft)} day(s) (flag)" if soft else ""),
                f"- metric 3 mechanism: shadow {sh} vs production {pr} preserved days ({'PASS' if m3 else 'FAIL'} so far)",
                f"- metric 4 attention: {sum(1 for r in c if r['m4']['flag'])} flagged day(s)",
                f"- metric 5 ops: {sum(1 for r in c if not r['m5']['pass'])} failing day(s)",
                f"- awaiting owner tiebreak: {', '.join(pending) or 'none'}", "",
                "Decision rule (frozen): expand to MEDIUM only if 1, 2, 2a and 3 all pass; "
                "kill if 2, 2a or 3 regress; anything else buys exactly one reader-prompt iteration.", ""]
    # tier split for 2a
    tiers = defaultdict(lambda: {"audited": 0, "material": 0, "non_material": 0})
    for r in c:
        for t, v in r["m2a"]["tiers"].items():
            for k in v:
                tiers[t][k] += v[k]
    if tiers:
        out += ["## Metric 2a by reader tier (counted days)", "", "| tier | audited | material | non-material |", "|---|---|---|---|"]
        for t, v in sorted(tiers.items()):
            out.append(f"| {t} | {v['audited']} | {v['material']} | {v['non_material']} |")
        out.append("")
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
