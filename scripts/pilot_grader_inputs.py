"""Build the graders' inputs for one day, deterministically.

- metric 2 (fidelity): 15 sentences sampled from the shadow pulse and
  15 from the production pulse, seeded by the date so a re-run grades
  the same sentences. Headlines and the _LEANS block are excluded.
- metric 2a (brief fidelity): 3 to 5 briefs, weighted toward the ones
  the shadow MAIN EVENT cites, tier-stratified (at least one of each
  tier when both exist). When the window has no briefs at all, a
  `brief_fidelity.SKIP` marker is written instead and the workflow
  skips that dimension for the day.
- metric 3 (mechanism): the shadow lead theme with its cited briefs'
  source paths; the production lead theme with the day's source list.
- metric 1 (grouping): the ledger's labels and instrument groups.

Sources and cards are restricted to the same window the editor pack
used (`--days`), so graders trace against what the pulse could have
read, not the whole history (review 2026-09-01).

Writes <out_dir>/{grouping,fidelity-shadow,fidelity-production,
brief_fidelity,mechanism-shadow,mechanism-production}.md, each a
self-contained block the grader prompt is followed by.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import sys
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_ledger import build, load_cards  # noqa: E402
from scripts.pilot_verify_citations import CITE_RE, strip_markers  # noqa: E402

SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z$(])")


def window_dates(day_iso: str, days: int) -> set[str]:
    d0 = date.fromisoformat(day_iso)
    return {(d0 - timedelta(days=i)).isoformat() for i in range(days + 1)}


SHARED_SECTIONS_RE = re.compile(
    r"(## 2\. THE MAIN EVENT.*?)(?=\n## (?!3\. BRIEFS)|\Z)", re.S)


def shared_sections(md: str) -> str:
    """THE MAIN EVENT and BRIEFS only (plan 3.6): the shadow pulse has
    no RECAP or WHAT TO WATCH, so those production sections must not be
    sampled. Shakedown day 1 graded production's live-market RECAP
    lines as 'unsupported', which measured the section list, not
    fidelity."""
    m = SHARED_SECTIONS_RE.search(md.split("## _LEANS")[0])
    return m.group(1) if m else md


def sentences_of(md: str, keep_markers: bool = False) -> list[str]:
    body = shared_sections(md)
    body = re.sub(r"^#.*$", "", body, flags=re.M)
    out = []
    for para in body.split("\n"):
        p = para.strip()
        if not p or p.startswith(("- ", "|", "```")):
            continue
        for s in SENT_SPLIT.split(p):
            s = s.strip()
            if len(s.split()) >= 6:
                out.append(s if keep_markers else strip_markers(s))
    return out


def sample_sentences(md: str, date_iso: str, n: int = 15, keep_markers: bool = False) -> list[dict]:
    sents = sentences_of(md, keep_markers)
    rng = random.Random(f"{date_iso}:{len(sents)}")
    idx = sorted(rng.sample(range(len(sents)), min(n, len(sents))))
    return [{"id": f"s{i + 1}", "text": sents[j]} for i, j in enumerate(idx)]


def main_event(md: str) -> str:
    m = re.search(r"## 2\. THE MAIN EVENT\s*\n(.*?)(?=\n## )", md, re.S)
    return (m.group(1) if m else "").strip()


def cited_docs(section: str, pack: dict) -> list[str]:
    ids = []
    for k, n in CITE_RE.findall(section):
        if k == "d":
            ids.append(f"d{n}")
        else:
            card = (pack.get("cards") or {}).get(f"c{n}")
            if card and card.get("doc"):
                ids.append(card["doc"])
    seen, out = set(), []
    for d in ids:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def choose_briefs(pack: dict, main_ids: list[str], date_iso: str, k: int = 4) -> list[dict]:
    docs = {d: v for d, v in (pack.get("docs") or {}).items() if (v.get("brief") or "").strip()}
    if not docs:
        return []
    rng = random.Random(f"briefs:{date_iso}")
    ordered = [d for d in main_ids if d in docs]
    rest = [d for d in docs if d not in ordered]
    rng.shuffle(rest)
    chosen = ordered[:3] + rest
    tiers = {docs[d].get("tier") for d in docs}
    picked: list[str] = []
    for d in chosen:
        if len(picked) >= k:
            break
        picked.append(d)
    for tier in tiers:
        if tier and not any(docs[d].get("tier") == tier for d in picked):
            extra = next((d for d in chosen if docs[d].get("tier") == tier and d not in picked), None)
            if extra:
                picked.append(extra)
    return [{"id": d, **docs[d]} for d in picked[:5]]


def source_files(source_root: str, date_iso: str, days: int) -> list[str]:
    """HIGH sources (source-text/) plus the MEDIUM grading corpus
    (source-text-all/, a sibling directory) for the window. Production
    draws on MEDIUM documents; grading it against HIGH only marked
    real sentences 'unsupported' on shakedown day 1."""
    wanted = window_dates(date_iso, days)
    roots = [source_root, os.path.join(os.path.dirname(source_root.rstrip("/\\")), "source-text-all")]
    out = []
    for root in roots:
        for p in sorted(glob.glob(os.path.join(root, "*", "*.txt"))):
            if os.path.basename(os.path.dirname(p)) in wanted:
                out.append(p)
    return out


def window_cards(cards_root: str, date_iso: str, days: int) -> list[dict]:
    wanted = window_dates(date_iso, days)
    cards = []
    for d in sorted(glob.glob(os.path.join(cards_root, "*"))):
        if os.path.isdir(d) and os.path.basename(d) in wanted:
            cards.extend(load_cards(d))
    return cards


def write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot-root", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--production-md", required=True)
    ap.add_argument("--pack-json", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    os.makedirs(a.out_dir, exist_ok=True)
    shadow_path = os.path.join(a.pilot_root, "shadow", f"{a.date}.md")
    with open(shadow_path, encoding="utf-8") as fh:
        shadow = fh.read()
    with open(a.production_md, encoding="utf-8") as fh:
        prod = fh.read()
    with open(a.pack_json, encoding="utf-8") as fh:
        pack = json.load(fh)
    srcs = source_files(os.path.join(a.pilot_root, "source-text"), a.date, a.days)
    src_block = "\n".join(f"- {p}" for p in srcs) or "(no source text in the window)"

    # metric 1
    ledger = build(window_cards(os.path.join(a.pilot_root, "cards"), a.date, a.days))
    lines = [f"## Ledger for {a.date}: {ledger['card_count']} cards", ""]
    lines.append("### Reader topic labels")
    for label, entries in ledger["by_topic_label"].items():
        lines.append(f"- {label}: " + "; ".join(f"{e['bank']}: {e['claim']}" for e in entries[:12]))
    lines.append("")
    lines.append("### Instrument groups")
    for inst, g in ledger["by_instrument"].items():
        lines.append(f"- {inst}: {g['bank_count']} bank(s), {len(g['for'])} for / {len(g['against'])} against")
    write(os.path.join(a.out_dir, "grouping.md"), "\n".join(lines) + "\n")

    # metric 2
    for name, md in (("shadow", shadow), ("production", prod)):
        sents = sample_sentences(md, a.date)
        block = [f"## Artifact: {name}", "", "### Sentences"]
        block += [f"- {s['id']}: {s['text']}" for s in sents]
        block += ["", "### Source text files", src_block]
        write(os.path.join(a.out_dir, f"fidelity-{name}.md"), "\n".join(block) + "\n")
        write(os.path.join(a.out_dir, f"fidelity-{name}.sentences.json"), json.dumps(sents, indent=1))

    # metric 2a
    me = main_event(shadow)
    main_ids = cited_docs(me, pack)
    briefs = choose_briefs(pack, main_ids, a.date)
    skip = os.path.join(a.out_dir, "brief_fidelity.SKIP")
    if os.path.exists(skip):
        os.remove(skip)
    if briefs:
        block = ["## Briefs to audit", ""]
        for b in briefs:
            block.append(f"### [{b['id']}] {b.get('bank')} — {b.get('title')} (tier {b.get('tier')})")
            block.append(f"source text: {b.get('source_text_path')}")
            block.append("")
            block.append((b.get("brief") or "").strip())
            block.append("")
        write(os.path.join(a.out_dir, "brief_fidelity.md"), "\n".join(block) + "\n")
    else:
        write(skip, "no briefs in the window\n")

    # metric 3
    docs = pack.get("docs") or {}
    cited = "\n".join(f"- [{d}] {docs[d].get('bank')} — {docs[d].get('title')}: {docs[d].get('source_text_path')}"
                      for d in main_ids if d in docs)
    write(os.path.join(a.out_dir, "mechanism-shadow.md"),
          f"## Artifact: shadow\n\n### Lead theme\n\n{me}\n\n### Cited briefs and their sources\n{cited or '(none cited)'}\n\n### All source text files\n{src_block}\n")
    write(os.path.join(a.out_dir, "mechanism-production.md"),
          f"## Artifact: production\n\n### Lead theme\n\n{main_event(prod)}\n\n### Source text files\n{src_block}\n")
    print(f"grader inputs: {len(srcs)} sources in window, {len(briefs)} briefs, main-event cites {main_ids}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
