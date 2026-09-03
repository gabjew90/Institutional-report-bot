"""Figure provenance for /ask: every number in a factual answer must
appear in something the turn actually saw, or the line carrying it
goes.

WHY (2026-09-03). Two answers about LULU's implied move carried
"historical absolute moves average 10.2%" and "±9.4% across the prior
12 quarters" with no tool result and no search behind them, and the
two histories contradicted each other. The grounding backstop only
fires on shapes it recognises; this check is shape-blind. It asks one
question of each figure: is this number in the evidence?

EVIDENCE is everything the model was handed or produced with a tool:
the injected blocks, function_response payloads, code-execution
output, the question itself, the chat context and the bot's prior
answers to this asker. A figure the asker typed ("at $225") is
sourced; a figure a tool returned is sourced; a figure the sandbox
computed is sourced. A figure that is in none of those was recalled.

NEVER RAISES, NEVER EMPTIES AN ANSWER. `check()` returns the answer
unchanged on any internal error, and when every line carries an
unsourced figure it returns the original and reports that instead of
shipping nothing. Pure functions: no I/O, no model calls, so the tests
run the real code on the real answers that motivated it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# A figure worth checking: currency/percent/suffixed numbers, decimals,
# or integers of three or more digits. Bare 1-2 digit integers are
# weeks, counts and ordinals; calendar years are dates, not claims.
_NUM_RE = re.compile(
    r"(?<![A-Za-z0-9_])[$€£±]?\s?\d[\d,]*(?:\.\d+)?\s?"
    r"(?:%|bps?|k|m|bn|b|t|x|thousand|million|billion|trillion)?(?![A-Za-z0-9_])",
    re.IGNORECASE)
_TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_DATE_RE = re.compile(r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b")
_ORDINAL_RE = re.compile(r"\b\d{1,2}(?:st|nd|rd|th)\b", re.IGNORECASE)
_WEEK_RE = re.compile(r"\bweek\s+\d{1,2}\b", re.IGNORECASE)
_TICKER_NUM_RE = re.compile(r"\b[A-Z]{1,5}\d{2,5}[CP]?\b")  # NDXP 29900C, 0DTE
_INDEX_NAME_RE = re.compile(
    r"\b(?:nasdaq|russell|s&p|sp|stoxx|nikkei|ftse|dax|cac|hang seng|topix|msci)\s*-?\s*\d{2,4}\b",
    re.IGNORECASE)
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")

_SCALE = {"k": 1e3, "m": 1e6, "bn": 1e9, "b": 1e9, "t": 1e12,
          "thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
_SUFFIX = r"(%|bps?|k|m|bn|b|t|x|thousand|million|billion|trillion)?"


@dataclass
class Figure:
    token: str      # as written, e.g. "±9.4%"
    value: float    # 9.4
    unit: str       # "%", "$", "bp", "x", "k", "m", "b", "t" or ""


@dataclass
class Report:
    figures: list[Figure] = field(default_factory=list)
    unsourced: list[Figure] = field(default_factory=list)
    stripped_lines: list[str] = field(default_factory=list)
    answer: str = ""
    action: str = "none"   # none | stripped | all-unsourced | skipped | error


def _scrub(text: str) -> str:
    """Remove the number-shaped things that are not figures."""
    t = _INDEX_NAME_RE.sub(" ", text or "")
    t = _TIME_RE.sub(" ", t)
    t = _DATE_RE.sub(" ", t)
    t = _ORDINAL_RE.sub(" ", t)
    t = _WEEK_RE.sub(" ", t)
    t = _TICKER_NUM_RE.sub(" ", t)
    return t


def _parse(tok: str) -> Figure | None:
    raw = tok.strip()
    t = raw.lower().replace(",", "").replace(" ", "")
    currency = t.startswith(("$", "€", "£"))
    t = t.lstrip("$€£±")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)" + _SUFFIX, t)
    if not m:
        return None
    num, suf = m.group(1), (m.group(2) or "")
    if suf.startswith("bp"):
        suf = "bp"
    # A scale suffix decides the unit even on a currency figure: "$2.46B"
    # must compare as 2.46e9, not as 2.46 dollars.
    unit = suf if suf else ("$" if currency else "")
    # bare small integers and years are not claims
    if not suf and not currency and "." not in num:
        if len(num) <= 2 or _YEAR_RE.match(num):
            return None
    try:
        return Figure(token=raw, value=float(num), unit=unit)
    except ValueError:
        return None


def extract_figures(text: str) -> list[Figure]:
    out: list[Figure] = []
    for m in _NUM_RE.finditer(_scrub(text)):
        f = _parse(m.group(0))
        if f:
            out.append(f)
    return out


def _variants(f: Figure) -> list[float]:
    """The numeric forms a source could carry the same figure in."""
    v = f.value
    vs = [v]
    if f.unit == "%":
        vs += [v / 100.0]            # 8.1% stored as 0.081
    elif f.unit == "":
        vs += [v * 100.0]            # 0.081 written as 8.1
    elif f.unit in _SCALE:
        vs += [v * _SCALE[f.unit]]   # 2.46B stored as 2460000000 or 2460 (millions)
        vs += [v * _SCALE[f.unit] / 1e6]
    elif f.unit == "bp":
        vs += [v / 100.0, v / 10000.0]
    return vs


def evidence_values(evidence: str) -> list[float]:
    """Every number in the evidence, with no filtering: a source may
    carry 21.36 for a 21.4 the answer rounds, or 0.081 for 8.1%."""
    vals: list[float] = []
    for m in _NUM_RE.finditer(evidence or ""):
        t = m.group(0).strip().lower().replace(",", "").replace(" ", "")
        t = t.lstrip("$€£±")
        mm = re.match(r"(\d+(?:\.\d+)?)" + _SUFFIX, t)
        if not mm:
            continue
        try:
            v = float(mm.group(1))
        except ValueError:
            continue
        suf = mm.group(2) or ""
        vals.append(v)
        if suf == "%":
            vals.append(v / 100.0)
        elif suf in _SCALE:
            vals.append(v * _SCALE[suf])
    return vals


def _close(a: float, b: float) -> bool:
    """Rounding-tolerant equality: 21.4 vs 21.36, 137.1 vs 137.12,
    8.1 vs 8.10. Half a percent of the value, floor 0.06."""
    return abs(a - b) <= max(0.005 * abs(a), 0.06)


def unsourced_figures(answer: str, evidence: str) -> tuple[list[Figure], list[Figure]]:
    figs = extract_figures(answer)
    ev = evidence_values(evidence)
    missing = []
    for f in figs:
        if not any(_close(var, e) for var in _variants(f) for e in ev):
            missing.append(f)
    return figs, missing


def _lines(answer: str) -> list[str]:
    """Split on the room's arrow bullets and on blank-line paragraphs;
    a plain sentence stream splits on sentence ends."""
    text = answer or ""
    if "→" in text:
        parts = re.split(r"(?=→)", text)
        return [p for p in parts if p.strip()]
    if "\n\n" in text:
        return [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    return [p for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]


def check(answer: str, evidence: str) -> Report:
    """Strip the lines that carry an unsourced figure. Total: any
    internal failure returns the answer untouched with action=error."""
    rep = Report(answer=answer or "")
    try:
        figs, missing = unsourced_figures(answer, evidence)
        rep.figures, rep.unsourced = figs, missing
        if not missing:
            return rep
        bad_tokens = {m.token for m in missing}
        lines = _lines(answer)
        keep, drop = [], []
        for ln in lines:
            if any(tok in ln for tok in bad_tokens):
                drop.append(ln)
            else:
                keep.append(ln)
        if not keep:
            rep.action = "all-unsourced"
            return rep
        sep = "" if "→" in (answer or "") else ("\n\n" if "\n\n" in (answer or "") else " ")
        rep.answer = sep.join(k.rstrip() + ("\n\n" if sep == "" else "") for k in keep).strip()
        rep.stripped_lines = [d.strip() for d in drop]
        rep.action = "stripped"
        return rep
    except Exception:  # never let the guard take the answer down
        rep.action = "error"
        rep.answer = answer or ""
        return rep
