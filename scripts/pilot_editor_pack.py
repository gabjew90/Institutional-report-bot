"""Build the shadow editor's input pack from the pilot card tree.

The pack is what the editor reads: every brief (d1..dN) and every card
(c1..cN) for the window, the ledger's groupings beside them, and the
concentration stats. Grouping filters nothing (spec section 4): the
editor sees every brief and every card whatever the ledger thinks.

Card numbering IS the ledger order the editor sees, and metric 4
(ledger attention) measures which positions get cited, so the order is
deterministic: instrument groups by bank_count descending then name,
cards inside a group for/against/neutral, then macro cards, then
anything left. Brief numbering follows the cards' first appearance.

Outputs: <out>.md (the pack the prompt embeds) and <out>.json (the id
maps the citation verifier resolves against).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from scripts.pilot_ledger import build, load_cards  # noqa: E402


def window_dirs(cards_root: str, day_iso: str, days: int) -> list[str]:
    d0 = date.fromisoformat(day_iso)
    wanted = {(d0 - timedelta(days=i)).isoformat() for i in range(days + 1)}
    out = []
    for p in sorted(glob.glob(os.path.join(cards_root, "*"))):
        if os.path.isdir(p) and os.path.basename(p) in wanted:
            out.append(p)
    return out


def load_window(cards_root: str, day_iso: str, days: int) -> list[dict]:
    cards: list[dict] = []
    for d in window_dirs(cards_root, day_iso, days):
        cards.extend(load_cards(d))
    return cards


def _doc_key(c: dict) -> str:
    return c.get("_file") or f"{c.get('bank')}::{c.get('document')}"


def order_cards(cards: list[dict], ledger: dict) -> list[dict]:
    """Deterministic ledger order (see module docstring)."""
    seen: set[int] = set()
    ordered: list[dict] = []
    idx = {id(c): c for c in cards}
    by_claim: dict[tuple, list[dict]] = {}
    for c in cards:
        by_claim.setdefault((c.get("bank"), c.get("claim")), []).append(c)

    def take(bank, claim):
        for c in by_claim.get((bank, claim), []):
            if id(c) not in seen:
                seen.add(id(c))
                ordered.append(c)
                return

    # Walk the ledger's groups but take EVERY card in the group, not
    # the ledger's bank-deduplicated entries: the ledger keeps one
    # voice per bank per side, and driving the order from that put a
    # bank's second card on the same name at the very end of the pack
    # (found in the 2026-09-01 dry run). Same-bank cards stay together.
    side_rank = {"bullish": 0, "bearish": 1, "neutral": 2}
    groups = sorted(ledger["by_instrument"].items(),
                    key=lambda kv: (-kv[1]["bank_count"], kv[0]))
    ordered_groups = [g for g in groups if g[0] != "_MACRO"] + \
        [g for g in groups if g[0] == "_MACRO"]
    for inst, _g in ordered_groups:
        members = [c for c in cards if id(c) not in seen and (
            (inst == "_MACRO" and not (c.get("instruments") or []))
            or inst in (c.get("instruments") or []))]
        members.sort(key=lambda c: (side_rank.get(c.get("direction"), 3),
                                    str(c.get("bank") or "")))
        for c in members:
            seen.add(id(c))
            ordered.append(c)
    for c in cards:
        if id(c) not in seen:
            seen.add(id(c))
            ordered.append(c)
    _ = take
    assert len(ordered) == len(cards), (len(ordered), len(cards))
    return [idx[id(c)] for c in ordered]


def build_pack(cards: list[dict], briefs: dict[str, dict]) -> tuple[str, dict]:
    ledger = build(cards)
    ordered = order_cards(cards, ledger)
    card_ids: dict[int, str] = {}
    doc_ids: dict[str, str] = {}
    for i, c in enumerate(ordered, 1):
        card_ids[id(c)] = f"c{i}"
        k = _doc_key(c)
        if k not in doc_ids:
            doc_ids[k] = f"d{len(doc_ids) + 1}"
    for k in briefs:
        if k not in doc_ids:
            doc_ids[k] = f"d{len(doc_ids) + 1}"

    lines: list[str] = []
    lines.append(f"# EDITOR PACK — {ledger['document_count']} documents, "
                 f"{ledger['card_count']} cards")
    lines.append("")
    lines.append("## Concentration")
    for bank, share in sorted(ledger["bank_concentration"].items(),
                              key=lambda kv: -kv[1]):
        lines.append(f"- {bank}: {share:.0%} of cards")
    lines.append(f"- reader tiers: {json.dumps(ledger['tier_counts'])}")
    lines.append("")
    lines.append("## Briefs (cite a brief as [dN] for the mechanism it argues)")
    lines.append("")
    for k, did in sorted(doc_ids.items(), key=lambda kv: int(kv[1][1:])):
        b = briefs.get(k) or {}
        lines.append(f"### [{did}] {b.get('bank', '?')} — {b.get('title', k)} "
                     f"(reader tier {b.get('tier', '?')})")
        lines.append((b.get("brief") or "(no brief)").strip())
        lines.append("")
    lines.append("## Ledger (cite a card as [cN] for every figure and attributed call)")
    lines.append("")
    lines.append("Groups are a backstop warning, not a filter: read every card.")
    lines.append("")
    pos = 0
    groups = sorted(ledger["by_instrument"].items(),
                    key=lambda kv: (-kv[1]["bank_count"], kv[0]))
    printed: set[int] = set()

    def card_line(c):
        return (f"- [{card_ids[id(c)]}] {c.get('bank')}: {c.get('claim')} "
                f"({c.get('status')}, {c.get('direction')}, "
                f"{c.get('conviction')} conviction"
                + (f", {c.get('timeframe')}" if c.get("timeframe") else "")
                + f") ← {doc_ids[_doc_key(c)]}")

    for inst, g in groups:
        label = "MACRO (no instrument)" if inst == "_MACRO" else inst
        lines.append(f"### {label} — {g['bank_count']} bank(s): "
                     f"{len(g['for'])} for / {len(g['against'])} against / "
                     f"{len(g['neutral'])} neutral")
        for c in ordered:
            if id(c) in printed:
                continue
            insts = c.get("instruments") or []
            is_here = (inst == "_MACRO" and not insts) or (inst in insts)
            if is_here:
                printed.add(id(c))
                lines.append(card_line(c))
                pos += 1
        lines.append("")
    rest = [c for c in ordered if id(c) not in printed]
    if rest:
        lines.append("### Other")
        for c in rest:
            lines.append(card_line(c))
        lines.append("")
    if ledger["by_figure"]:
        lines.append("## Same figure, several banks (hard key)")
        for fk, entries in ledger["by_figure"].items():
            if len(entries) > 1:
                lines.append(f"- {' / '.join(str(x) for x in fk)}: "
                             + "; ".join(f"{e['bank']}" for e in entries))
        lines.append("")
    if ledger["by_topic_label"]:
        lines.append("## Reader topic labels (soft key; may fragment)")
        for label, entries in sorted(ledger["by_topic_label"].items(),
                                     key=lambda kv: -len(kv[1]))[:40]:
            lines.append(f"- {label}: {len(entries)} card(s)")
        lines.append("")

    meta = {
        "cards": {card_ids[id(c)]: {
            "bank": c.get("bank"), "claim": c.get("claim"),
            "anchor": c.get("anchor"), "status": c.get("status"),
            "instruments": c.get("instruments") or [],
            "direction": c.get("direction"), "conviction": c.get("conviction"),
            "doc": doc_ids[_doc_key(c)], "file": c.get("_file"),
            "tier": c.get("_reader_tier"),
        } for c in ordered},
        "docs": {did: {"key": k, **(briefs.get(k) or {})}
                 for k, did in doc_ids.items()},
        "card_count": len(ordered),
        "document_count": len(doc_ids),
    }
    return "\n".join(lines) + "\n", meta


def load_briefs(cards_root: str, day_iso: str, days: int) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in window_dirs(cards_root, day_iso, days):
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except Exception:
                continue
            meta = {}
            src = doc.get("source_text_path") or ""
            meta_path = src.replace(".txt", "").split("__")[0] + ".meta.json" if src else ""
            if meta_path and os.path.exists(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as fh:
                        meta = json.load(fh)
                except Exception:
                    meta = {}
            first = (doc.get("cards") or [{}])[0] if doc.get("cards") else {}
            out[os.path.basename(path)] = {
                "bank": meta.get("source") or first.get("bank") or "?",
                "title": meta.get("title") or first.get("document") or os.path.basename(path),
                "tier": doc.get("reader_tier"),
                "brief": doc.get("brief") or "",
                "source_text_path": src,
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cards_root")
    ap.add_argument("--date", required=True, help="YYYY-MM-DD (UTC)")
    ap.add_argument("--days", type=int, default=1,
                    help="how many prior day-directories to include (default 1)")
    ap.add_argument("--out", required=True, help="path stem: writes <out>.md and <out>.json")
    a = ap.parse_args()
    cards = load_window(a.cards_root, a.date, a.days)
    briefs = load_briefs(a.cards_root, a.date, a.days)
    md, meta = build_pack(cards, briefs)
    with open(a.out + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(a.out + ".json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    print(f"pack: {meta['document_count']} docs, {meta['card_count']} cards "
          f"-> {a.out}.md ({len(md)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
