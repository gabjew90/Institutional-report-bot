"""Resolve the shadow pulse's citations against the editor pack.

Spec section 5: card citations `[cN]` get HARD verification (the
sentence's numbers and bank names must appear in the cited card);
brief citations `[dN]` get existence verification. Metric 4 (ledger
attention) is the distribution of cited card positions, computed here.

Review 2026-09-01: checks run once per SENTENCE (a sentence with two
card cites used to report the same missing figure twice), a sentence
that carries a figure but cites no card is itself a failure (editor
rule 1 is otherwise unenforced), and bank matching is word-bounded on
both sides ("ing" is a bank; "holding" and "JPMorgan" are not
"Morgan Stanley").

Usage:
  pilot_verify_citations.py <shadow.md> <pack.json> --meta-out <meta.json>
      [--stripped-out <clean.md>] [--reask-out <reask.json>] [--final]

Exit 0 when clean or when --final (record and continue); exit 2 when
failures exist and --reask-out was requested (the workflow re-asks
once).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

CITE_RE = re.compile(r"\[(c|d)(\d+)\]")
# A magnitude suffix must end at a word boundary: "16 meeting" used to
# read as "16m" (plan-4.3 stress run, 2026-09-17), and the k/m/bn
# forms are folded into the value below so "40k" in a card matches
# "40,000" in the pulse. The editor writes figures out in plain English
# by contract; the cards keep the source's shorthand.
NUM_RE = re.compile(
    r"(?<![A-Za-z])[$€£¥]?\d[\d,]*(?:\.\d+)?%?"
    r"(?:\s?(?:basis points?|bp|bps|k|K|mn|m|M|bn|bln|B|tn|T|x|times|thousand|million|billion|trillion)"
    r"(?![A-Za-z]))?")
_MAGNITUDE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
              "bn": 1e9, "b": 1e9, "bln": 1e9, "billion": 1e9,
              "t": 1e12, "tn": 1e12, "trillion": 1e12}
# "3.5-3.75%" carries the % on the second number only; the pulse writes
# "3.5% to 3.75%". The first bound gets its own % figure.
_RANGE_PCT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)%")


def _fmt(val: float) -> str:
    return str(int(val)) if val == int(val) else f"{val:.6f}".rstrip("0").rstrip(".")


def _aliases(n: str) -> set[str]:
    """A card's "536bp" also satisfies a pulse "5.36%": the editor
    writes percentages by contract. One direction only, and only on
    the card side (review 2026-09-17): a symmetric alias let a card's
    "GDP growth of 3.5%" pass a pulse "spreads widened 350bp"."""
    out = {n}
    if n.endswith("bp"):
        try:
            out.add(_fmt(float(n[:-2]) / 100) + "%")
        except ValueError:
            pass
    return out
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z$\[(])")

KNOWN_BANKS = ["goldman", "morgan stanley", "jpm", "jpmorgan", "citi", "bofa", "bank of america",
               "ubs", "rbc", "barclays", "deutsche", "mizuho", "mufg", "rabobank", "ts lombard",
               "ing", "anz", "market ear", "tme", "hsbc", "wells fargo", "nomura", "jefferies"]
# "wells fargo", not "wells": the bank check now reads every sentence, and
# "drilling fewer wells" is energy prose (2026-09-25 review).


def _norm_num(tok: str) -> str:
    t = tok.strip().lower().replace(",", "").replace(" ", "")
    t = t.replace("$", "").replace("€", "").replace("£", "").replace("¥", "")
    t = re.sub(r"(basispoints?|bps|bp)$", "bp", t)
    # A multiple is the bare number: "14.5x" and "14.5 times" are one
    # figure, and "raised guidance 3 times" is a count, not a figure.
    t = re.sub(r"(times|x)$", "", t)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(k|thousand|mn|m|million|bn|bln|b|billion|tn|t|trillion)", t)
    if m:
        return _fmt(float(m.group(1)) * _MAGNITUDE[m.group(2)])
    return t


_INDEX_NAME_RE = re.compile(
    r"\b(?:nasdaq|russell|s&p|sp|stoxx|nikkei|ftse|dax|cac|hang seng|topix|msci)\s*-?\s*\d{2,4}\b",
    re.IGNORECASE)


def _numbers(text: str, *, card_side: bool = False) -> set[str]:
    """Figures in `text`, normalised. The pulse side keeps each figure
    in the form the sentence used, so a failure names text the editor
    can find; the card side adds the bp->% alias."""
    # "Nasdaq 100", "Russell 2000", "S&P 500" are names, not figures
    # (shakedown 2026-09-02: three false failures on one shadow pulse)
    text = _INDEX_NAME_RE.sub(" ", text or "")
    out = set()
    for m in NUM_RE.finditer(text or ""):
        n = _norm_num(m.group(0))
        # bare 1-2 digit counts and calendar years are not figures the
        # card must carry ("2026 capex" cites the estimate, not the year)
        if n and not re.fullmatch(r"\d{1,2}", n) and not re.fullmatch(r"(19|20)\d\d", n):
            out |= _aliases(n) if card_side else {n}
    for m in _RANGE_PCT_RE.finditer(text or ""):
        # "by 2026 to 3.5%" is a year and a figure, not a range
        if re.fullmatch(r"(19|20)\d\d", m.group(1)):
            continue
        out.add(m.group(1) + "%")
    return out


def _word_in(needle: str, hay: str) -> bool:
    return re.search(r"(?<![a-z])" + re.escape(needle) + r"(?![a-z])", hay) is not None


def _bank_matches(named: str, cited_bank: str) -> bool:
    """Does the bank a sentence names correspond to a cited card's bank?"""
    cb = (cited_bank or "").lower()
    if _word_in(named, cb):
        return True
    aliases = {
        "goldman": ["goldman", "gs"], "jpm": ["jpm", "jpmorgan", "j.p. morgan"],
        "jpmorgan": ["jpm", "jpmorgan", "j.p. morgan"], "citi": ["citi", "citigroup"],
        "bofa": ["bofa", "bank of america"], "bank of america": ["bofa", "bank of america"],
        "deutsche": ["deutsche", "db"], "morgan stanley": ["morgan stanley", "ms"],
        "market ear": ["market ear", "tme"], "tme": ["market ear", "tme"],
    }
    return any(_word_in(a, cb) for a in aliases.get(named, []))


def sentences(md: str) -> list[tuple[str, list[tuple[str, int]]]]:
    """Every prose sentence of the body with its citations (possibly none)."""
    # Artifacts written before 2026-09-18 carry a `## _LEANS` block that
    # was never prose; the split is a no-op for everything since.
    body = md.split("## _LEANS")[0]
    out = []
    for para in body.split("\n"):
        if not para.strip() or para.startswith("#"):
            continue
        for s in SENT_SPLIT.split(para.strip()):
            cites = [(k, int(n)) for k, n in CITE_RE.findall(s)]
            out.append((s, cites))
    return out


def verify(md: str, pack: dict) -> dict:
    cards = pack.get("cards") or {}
    docs = pack.get("docs") or {}
    card_count = int(pack.get("card_count") or len(cards))
    failures: list[dict] = []
    cited_positions: list[int] = []
    n_card_cites = n_doc_cites = 0
    for sent, cites in sentences(md):
        clean = CITE_RE.sub("", sent)
        sent_nums = _numbers(clean)
        card_keys = [f"c{n}" for k, n in cites if k == "c"]
        for kind, n in cites:
            key = f"{kind}{n}"
            if kind == "d":
                n_doc_cites += 1
                if key not in docs:
                    failures.append({"sentence": sent[:200], "cite": key, "reason": "brief does not exist"})
            else:
                n_card_cites += 1
                if key in cards:
                    cited_positions.append(n)
                else:
                    failures.append({"sentence": sent[:200], "cite": key, "reason": "card does not exist"})
        valid_cards = [cards[k] for k in card_keys if k in cards]
        # Rule 1: a figure needs a card. No card cited at all is a failure
        # in its own right, not a sentence the verifier never looks at.
        if sent_nums and not card_keys:
            failures.append({"sentence": sent[:200], "cite": "",
                             "reason": f"figures with no card citation: {sorted(sent_nums)}"})
            continue
        # A bank named in a sentence must be the bank of a card or brief
        # the sentence cites. Checked before the no-card early exit: on
        # 2026-09-24 "Morgan Stanley takes the other side of the level."
        # carried no figure and no citation, so nothing looked at it, and
        # it was the one sentence of the day's shadow pulse that no source
        # supported (the rest the graders flagged were in files their
        # search skipped). The editor stages a bank-vs-bank argument the
        # cards do not contain; this makes that a re-ask.
        low = clean.lower()
        named = [b for b in KNOWN_BANKS if _word_in(b, low)]
        if named:
            cited_banks = [c.get("bank") or "" for c in valid_cards]
            cited_banks += [(docs.get(f"d{n}") or {}).get("bank") or ""
                            for k, n in cites if k == "d"]
            if not any(_bank_matches(b, cb) for b in named for cb in cited_banks):
                failures.append({"sentence": sent[:200],
                                 "cite": ",".join(f"{k}{n}" for k, n in cites),
                                 "reason": (f"bank named ({named}) is not the bank of any "
                                            f"cited card or brief")})
        if not valid_cards:
            continue
        # HARD: every figure in the sentence must be in SOME cited card
        all_nums = _numbers(" ".join(f"{c.get('claim', '')} {c.get('anchor', '')}" for c in valid_cards),
                            card_side=True)
        missing = sorted(x for x in sent_nums if x not in all_nums)
        if missing:
            failures.append({"sentence": sent[:200], "cite": ",".join(card_keys),
                             "reason": f"figures not in cited card(s): {missing}"})
    # metric 4: quintile distribution of cited positions
    quintiles = [0, 0, 0, 0, 0]
    for p in cited_positions:
        if card_count > 0:
            q = min(4, int((p - 1) * 5 / card_count))
            quintiles[q] += 1
    total = len(cited_positions)
    edge_share = ((quintiles[0] + quintiles[4]) / total) if total else 0.0
    return {
        "card_citations": n_card_cites,
        "brief_citations": n_doc_cites,
        "distinct_cards_cited": len(set(cited_positions)),
        "card_count": card_count,
        "quintiles": quintiles,
        "edge_quintile_share": round(edge_share, 3),
        "metric4_flag": bool(total) and edge_share > 0.70,
        "failures": failures,
        "word_count": len(CITE_RE.sub("", md.split("## _LEANS")[0]).split()),
    }


def strip_markers(md: str) -> str:
    return re.sub(r"\s?\[(?:c|d)\d+\]", "", md)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("shadow_md")
    ap.add_argument("pack_json")
    ap.add_argument("--meta-out", required=True)
    ap.add_argument("--stripped-out")
    ap.add_argument("--reask-out")
    ap.add_argument("--final", action="store_true")
    a = ap.parse_args()
    with open(a.shadow_md, encoding="utf-8") as fh:
        md = fh.read()
    with open(a.pack_json, encoding="utf-8") as fh:
        pack = json.load(fh)
    res = verify(md, pack)
    existing = {}
    if os.path.exists(a.meta_out):
        try:
            with open(a.meta_out, encoding="utf-8") as fh:
                existing = json.load(fh)
        except Exception:
            existing = {}
    existing["citations"] = res
    with open(a.meta_out, "w", encoding="utf-8") as fh:
        json.dump(existing, fh, indent=1)
    if a.stripped_out:
        with open(a.stripped_out, "w", encoding="utf-8") as fh:
            fh.write(strip_markers(md))
    print(f"citations: {res['card_citations']} card / {res['brief_citations']} brief, "
          f"{len(res['failures'])} failure(s), "
          f"edge-quintile share {res['edge_quintile_share']:.0%}")
    if res["failures"] and a.reask_out and not a.final:
        with open(a.reask_out, "w", encoding="utf-8") as fh:
            json.dump(res["failures"], fh, indent=1)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
