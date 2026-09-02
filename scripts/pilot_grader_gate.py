"""Grader separation gate (plan 4.2), the gate that matters most.

Every grader dimension must FAIL its seeded known-bad artifact and
PASS the clean counterpart. A grader that passes both is TOO WEAK; one
that fails both is too harsh. Day 1 does not start until every
dimension separates its pair, and the gate re-runs whenever a grader
prompt changes.

Two modes:
  --build <out_dir>   write the fixture inputs the grader prompts are
                      followed by (one file per dimension and case)
  --judge <grades_dir> read grades/<dim>-<bad|clean>.json produced by
                      the agents and decide; writes verdict.json;
                      exit 1 unless every dimension separates
"""
from __future__ import annotations

import argparse
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(REPO, "docs", "superpowers", "routines", "pilot",
                        "grader-fixtures", "fixtures.json")
DIMS = ("grouping", "fidelity", "brief_fidelity", "mechanism")


def load_fixtures() -> dict:
    with open(FIXTURES, encoding="utf-8") as fh:
        return json.load(fh)


def build_inputs(out_dir: str) -> None:
    fx = load_fixtures()
    os.makedirs(out_dir, exist_ok=True)
    src1 = os.path.join(out_dir, "source-capex.txt")
    src2 = os.path.join(out_dir, "source-rates.txt")
    with open(src1, "w", encoding="utf-8") as fh:
        fh.write(fx["source_text"])
    with open(src2, "w", encoding="utf-8") as fh:
        fh.write(fx["sentences_source_text"])
    for case in ("clean", "bad"):
        g = fx[f"grouping_{case}"]
        lines = ["## Ledger (fixture)", "", "### Reader topic labels"]
        for label, ids in g["labels"].items():
            lines.append(f"- {label}: " + "; ".join(g["cards"][i] for i in ids))
        with open(os.path.join(out_dir, f"grouping-{case}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        sents = fx[f"sentences_{case}"]
        lines = ["## Artifact: fixture", "", "### Sentences"]
        lines += [f"- {s['id']}: {s['text']}" for s in sents]
        lines += ["", "### Source text files", f"- {src2}"]
        with open(os.path.join(out_dir, f"fidelity-{case}.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        brief = fx[f"brief_{case}"]
        text = (f"## Briefs to audit\n\n### [d1] {fx['source_meta']['source']} — "
                f"{fx['source_meta']['title']} (tier top)\nsource text: {src1}\n\n{brief}\n")
        with open(os.path.join(out_dir, f"brief_fidelity-{case}.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        mech = fx[f"mechanism_{case}"]
        text = (f"## Artifact: fixture\n\n### Lead theme\n\n{mech}\n\n"
                f"### Source text files\n- {src2}\n")
        with open(os.path.join(out_dir, f"mechanism-{case}.md"), "w", encoding="utf-8") as fh:
            fh.write(text)


def grade_says_fail(dim: str, doc: dict) -> bool | None:
    """True = the grader failed the artifact, False = passed, None =
    unusable grade."""
    if not isinstance(doc, dict) or doc.get("failed"):
        return None
    if dim == "grouping":
        share = doc.get("fragmented_mass_share")
        if not isinstance(share, (int, float)):
            return None
        bad_merge = any(m.get("would_change_theme_selection") for m in doc.get("mis_merges") or [])
        return share > 0.10 or bad_merge
    if dim == "fidelity":
        return (doc.get("unsupported", 0) or 0) > 0 or (doc.get("distorted", 0) or 0) > 0
    if dim == "brief_fidelity":
        return (doc.get("material_total", 0) or 0) > 0
    if dim == "mechanism":
        p = doc.get("preserved")
        return None if p is None else (not p)
    return None


def judge(grades_dir: str) -> dict:
    verdict = {"dimensions": {}, "all_separate": True}
    for dim in DIMS:
        row = {}
        for case in ("bad", "clean"):
            path = os.path.join(grades_dir, f"{dim}-{case}.json")
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except Exception:
                doc = None
            row[case] = grade_says_fail(dim, doc)
        bad, clean = row["bad"], row["clean"]
        if bad is True and clean is False:
            status = "separates"
        elif bad is None or clean is None:
            status = "unusable grade"
        elif bad is False and clean is False:
            status = "TOO WEAK (passed the seeded bad artifact)"
        elif bad is True and clean is True:
            status = "TOO HARSH (failed the clean artifact)"
        else:
            status = "inverted"
        verdict["dimensions"][dim] = {"bad_failed": bad, "clean_passed": (clean is False), "status": status}
        if status != "separates":
            verdict["all_separate"] = False
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build")
    ap.add_argument("--judge")
    a = ap.parse_args()
    if a.build:
        build_inputs(a.build)
        print(f"fixture inputs written to {a.build}")
        return 0
    if a.judge:
        v = judge(a.judge)
        with open(os.path.join(a.judge, "verdict.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, indent=1)
        for dim, r in v["dimensions"].items():
            print(f"  {dim:16s} {r['status']}")
        print("GRADER GATE:", "every dimension separates" if v["all_separate"] else "NOT CLEARED")
        return 0 if v["all_separate"] else 1
    ap.error("--build or --judge")
    return 2


if __name__ == "__main__":
    sys.exit(main())
