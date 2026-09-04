"""Turn a grader agent's raw output into grades/<date>/<dim>-<a|b>.json.

Salvage in code (same rule as reader and editor): find the JSON
object, validate the fields the scoreboard needs, attach provenance.
A grade that cannot be parsed is recorded as {"failed": true} so the
scoreboard shows a missing grade rather than silently skipping a day.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_config import provenance  # noqa: E402
from scripts.pilot_finalize_read import extract_json  # noqa: E402

REQUIRED = {
    "grouping": ("fragmented_mass_share", "mis_merges"),
    "fidelity": ("sentences", "faithful", "distorted", "unsupported"),
    "brief_fidelity": ("briefs", "material_total", "non_material_total"),
    "mechanism": ("preserved", "source_chain", "pulse_chain"),
}


def validate(dim: str, doc: dict) -> list[str]:
    missing = [k for k in REQUIRED[dim] if k not in doc]
    problems = [f"missing {k}" for k in missing]
    if dim == "fidelity" and "sentences" in doc:
        n = len(doc["sentences"])
        if doc.get("faithful", 0) + doc.get("distorted", 0) + doc.get("unsupported", 0) != n:
            problems.append("counts do not match the sentence list")
        if n == 0:
            # An empty sentence list is a grader that produced nothing,
            # not a pulse that scored zero. The old `else 0.0` wrote a
            # real-looking 0% with failed=False, and metric 2 regression
            # is a KILL criterion, so a grader timing out at its turn cap
            # read as the shadow writer failing fidelity (2026-09-03).
            problems.append("empty sentence list: the grader graded nothing")
        else:
            doc["faithful_rate"] = round(doc.get("faithful", 0) / n, 3)
    if dim == "grouping":
        share = doc.get("fragmented_mass_share")
        if isinstance(share, (int, float)):
            bad_merge = any(m.get("would_change_theme_selection") for m in doc.get("mis_merges") or [])
            doc["pass"] = (share <= 0.10) and not bad_merge
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dim", required=True, choices=sorted(REQUIRED))
    ap.add_argument("--agent", required=True, choices=["a", "b"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--artifact", default="")
    a = ap.parse_args()
    with open(a.raw, encoding="utf-8") as fh:
        raw = fh.read()
    doc = extract_json(raw)
    ver = None
    m = re.search(r"model[_ ]version[\"':\s]+([A-Za-z0-9._-]+)", raw)
    if m:
        ver = m.group(1)
    with open(a.prompt, encoding="utf-8") as fh:
        prompt_text = fh.read()
    prov = provenance(a.model, ver, prompt_text)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    if not isinstance(doc, dict):
        out = {"failed": True, "dim": a.dim, "agent": a.agent, "artifact": a.artifact,
               "provenance": prov, "raw_tail": raw[-800:]}
        with open(a.out, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=1)
        print(f"FAILED grade: no JSON in {a.dim}-{a.agent}")
        return 1
    problems = validate(a.dim, doc)
    doc.update({"dim": a.dim, "agent": a.agent, "artifact": a.artifact or doc.get("artifact", ""),
                "provenance": prov, "failed": bool(problems), "problems": problems})
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
    print(("OK" if not problems else "PROBLEMS " + "; ".join(problems)) + f": {a.dim}-{a.agent}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
