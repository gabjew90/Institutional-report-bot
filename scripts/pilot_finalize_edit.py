"""Turn the editor agent's raw output into shadow/<date>.md plus meta.

Salvage belongs in code, not in a prompt plea (same rule as the
reader): strip a leading preamble and markdown fences, then require
the structural minimum: an H1 headline, `## 2. THE MAIN EVENT`, and
the `## _LEANS` block. A write that lacks any of them is a failed
edit and the day records that, rather than a shadow pulse nobody
would grade.

Meta carries provenance (plan 3.7) and unread_source_files_at_edit
(plan 3.6).
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

REQUIRED = ("## 2. THE MAIN EVENT", "## _LEANS")


def extract_markdown(raw: str) -> str | None:
    text = (raw or "").strip()
    if not text:
        return None
    m = re.search(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, re.S)
    if m and m.group(1).lstrip().startswith("#"):
        text = m.group(1).strip()
    i = text.find("\n# ")
    if not text.startswith("# ") and i >= 0:
        text = text[i + 1:]
    if not text.startswith("# "):
        return None
    return text.strip() + "\n"


def structural_problems(md: str) -> list[str]:
    out = []
    for req in REQUIRED:
        if req not in md:
            out.append(f"missing {req}")
    if "## 3. BRIEFS" not in md:
        out.append("missing ## 3. BRIEFS")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--unread", type=int, default=0)
    ap.add_argument("--given-up", type=int, default=0,
                    help="documents the readers gave up on (read-failures/), a coverage gap that is not 'unread'")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--pack", required=True)
    ap.add_argument("--reasked", action="store_true")
    a = ap.parse_args()
    with open(a.raw, encoding="utf-8") as fh:
        raw = fh.read()
    md = extract_markdown(raw)
    ver = None
    m = re.search(r"model[_ ]version[\"':\s]+([A-Za-z0-9._-]+)", raw)
    if m:
        ver = m.group(1)
    with open(a.pack, encoding="utf-8") as fh:
        pack = json.load(fh)
    with open(a.prompt, encoding="utf-8") as fh:
        prompt_text = fh.read()
    meta = {}
    if os.path.exists(a.meta):
        try:
            with open(a.meta, encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            meta = {}
    meta.update({
        "unread_source_files_at_edit": a.unread,
        "given_up_at_edit": a.given_up,
        "pack": {"documents": pack.get("document_count"), "cards": pack.get("card_count")},
        "provenance": provenance(a.model, ver, prompt_text),
        "reasked": bool(a.reasked) or bool(meta.get("reasked")),
    })
    problems = structural_problems(md) if md else ["no markdown document in output"]
    meta["structural_problems"] = problems
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.meta, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    if problems:
        print("FAILED edit: " + "; ".join(problems))
        return 1
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write(md)
    print(f"OK: shadow pulse {len(md.split())} words, unread at edit {a.unread}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
