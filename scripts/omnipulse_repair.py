"""Narrow, deterministic repairs to the locked Omnipulse body (owner call
option C, 2026-10-06).

On an Omnipulse day the morning routine does not write the body and,
until now, did not fix it either: the fact checker's findings that quote
the body were recorded and shipped, and lint violations in it stayed.
2026-09-29 shipped a diesel brief saying the White House "is preparing a
90-day ban on diesel exports" (the research said "potential" bans; the
checker flagged it fabricated-event), and 2026-10-05 shipped "Goldman
says ..." (a banned source-prefix opener).

Two repairs, both mechanical, applied to the routine's saved copy of the
body (the pilot branch keeps the editor's original for grading):

  cut_findings  removes the sentence a fabricated/invented finding quotes,
                at most MAX_CUTS a day. Other kinds (overstated-claim,
                unsupported-figure, ...) are judgment calls and stay
                recorded only: across the 25 most recent checker reports
                they were 33 of 39 findings.
  fix_voice     em-dashes and semicolons to commas, and a "Bank says X"
                opener to "In Bank's view, X", which keeps the
                attribution and passes lint.
"""
from __future__ import annotations

import re

CUT_KIND_RE = re.compile(r"fabricat|invent", re.I)
MAX_CUTS = 2

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z$\"'(*\[])")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def cut_findings(body: str, findings: list[dict],
                 max_cuts: int = MAX_CUTS) -> tuple[str, list[str]]:
    """(new body, sentences cut). A finding is cut only when its kind is
    fabricated/invented and its quote is in the body; headings never are.
    `max_cuts` is what is left of the day's MAX_CUTS (the driver counts
    across the checker's first run and its recheck)."""
    cut: list[str] = []
    lines = body.split("\n")
    for f in findings:
        if len(cut) >= max_cuts:
            break
        if not isinstance(f, dict) or not CUT_KIND_RE.search(str(f.get("kind") or "")):
            continue
        q = _norm(f.get("quote") or "")
        if len(q) < 12:
            continue

        def hit(s: str) -> bool:
            ns = _norm(s)
            # the quoted text sits in this sentence, or a sentence the
            # quote spans sits wholly inside the quote
            return bool(ns) and (q[:60] in ns or ns in q)

        for i, line in enumerate(lines):
            if line.lstrip().startswith("#") or not hit(line) and q[:60] not in _norm(line):
                continue
            sents = _SENT_SPLIT.split(line)
            keep = [s for s in sents if not hit(s)]
            if len(keep) == len(sents):
                continue
            cut.extend(s.strip() for s in sents if hit(s))
            lines[i] = " ".join(keep)
            break
    new = "\n".join(lines)
    # a paragraph emptied by a cut leaves no blank run behind
    new = re.sub(r"\n{3,}", "\n\n", new)
    return new, cut


def _banks_and_verbs() -> tuple[list[str], list[str], set[str]]:
    try:
        from ai_analysis import voice_rules as V
    except ImportError:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from ai_analysis import voice_rules as V
    return (list(V.SOURCE_PREFIX_BANKS), list(V.SOURCE_PREFIX_VERBS),
            set(V.BANNED_PUBLICATION_NAMES))


def _possessive(bank: str) -> str:
    return bank + ("'" if bank.endswith("s") else "'s")


def fix_voice(body: str) -> tuple[str, list[str]]:
    """(new body, one note per change). Mirrors the lint's source-prefix
    pattern (voice_rules), so whatever it rewrites the lint stops flagging."""
    notes: list[str] = []
    out = body
    if "—" in out or "–" in out:
        n = out.count("—") + out.count("–")
        # a dash between numbers is a range ("0.2–0.3%" -> "0.2 to 0.3%")
        # and one leading a number is a minus sign; only a dash between
        # words or clauses becomes a comma
        out = re.sub(r"(?<=\d)\s*[—–]\s*(?=[$\d])", " to ", out)
        out = re.sub(r"(?<![\w%])–(?=\d)", "-", out)
        out = re.sub(r"\s*[—–]\s*", ", ", out)
        notes.append(f"{n} dash(es) rewritten")
    # semicolons in prose only; a heading or table line is left alone
    fixed_lines = []
    semis = 0
    for line in out.split("\n"):
        if ";" in line and not line.lstrip().startswith(("#", "|")):
            semis += line.count(";")
            line = re.sub(r"\s*;\s*", ", ", line)
        fixed_lines.append(line)
    out = "\n".join(fixed_lines)
    if semis:
        notes.append(f"{semis} semicolon(s) to commas")

    banks, verbs, banned = _banks_and_verbs()
    # a banned publication (TME, The Market Ear) must not be cited at all;
    # rewriting its opener would keep the citation, so lint keeps it
    banks = [b for b in banks if b not in banned]
    banks_alt = "|".join(re.escape(b) for b in sorted(banks, key=len, reverse=True))
    verbs_alt = "|".join(re.escape(v) for v in sorted(verbs, key=len, reverse=True))
    rx = re.compile(rf"\b({banks_alt})(?:'s)?\s+(?:{verbs_alt})\b\s+(?:that\s+)?")

    def repl(m: re.Match) -> str:
        start = m.start()
        before = out_ref[0][:start].rstrip(" *")
        at_start = (not before or before.endswith(("\n", ".", "!", "?", ":"))
                    or re.search(r"(?m)^\s*[-*]\s*$", before.split("\n")[-1] or "") is not None)
        lead = "In" if at_start else "in"
        notes.append(f"'{m.group(0).strip()}' -> '{lead} {_possessive(m.group(1))} view,'")
        return f"{lead} {_possessive(m.group(1))} view, "

    out_ref = [out]
    out = rx.sub(repl, out)
    return out, notes
