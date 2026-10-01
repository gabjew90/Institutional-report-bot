"""Does a stock answer say which part of the business drives its figures?

Owner, 2026-09-30: a ticker answer, whatever the question, should say what
part of the business drives the metric (GPUs, cloud, retail). The business
primer (discord_bot/primer_tool.py) puts that in front of the model, and
the first live answers still left it out: the ACN answer named Goldman's
book-to-bill and the implied move and never said Accenture is half
consulting, half outsourcing. This module decides, in code, whether an
answer names a business line from the primer; bot phase 9 rewrites once
when it does not.
"""
from __future__ import annotations

import re

# Acronyms that name no business line.
_GENERIC = {
    "US", "USA", "AI", "EPS", "CEO", "CFO", "FY", "Q1", "Q2", "Q3", "Q4", "YOY", "QOQ",
    "GAAP", "TTM", "IPO", "ETF", "SEC", "EU", "UK", "NYSE", "PE", "ROI", "EBITDA", "FCF",
    "R&D", "OEM", "B2B", "B2C", "IT",
}
_STOP = {
    "sells", "sell", "their", "these", "those", "which", "other", "about", "across", "products",
    "product", "services", "service", "customers", "customer", "companies", "company", "global",
    "makers", "maker", "including", "related", "world", "worldwide", "enterprises", "businesses",
    "approximately", "revenue", "segment", "segments", "reportable", "single", "latest", "fiscal",
    "year", "share", "percent", "operates", "delivering", "providing", "provides", "through",
    "under", "within", "where", "while", "because", "large", "small", "major", "governments",
}
_SEG_PREFIX_RE = re.compile(
    r"^(?:its|the)?\s*(?:reported|reportable|main|two|three|four|five)?\s*(?:business\s+)?"
    r"segments?\s+(?:are|is|include)\s*:?\s*", re.I)
# A segment's name is the run of capitalised words that opens its chunk:
# "Cloud Memory", "Mobile & Client", "Data Center includes ..." -> "Data Center".
_LEAD_NAME_RE = re.compile(r"^((?:[A-Z][\w/-]*|&)(?:\s+(?:[A-Z][\w/-]*|&))*)")
_PAREN_ACRONYM_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9&]{1,7})\)")
_ACRONYM_RE = re.compile(r"\b([A-Z][A-Z0-9&]{1,5})\b")
# Words of a segment name too general to count alone.
_WEAK = {"center", "client", "other", "others", "global", "group", "total", "products", "services"}


def _line(primer: str, label: str) -> str:
    for ln in (primer or "").splitlines():
        if ln.upper().startswith(label + ":"):
            return ln.split(":", 1)[1].strip()
    return ""


def segment_names(segments_line: str) -> list[str]:
    """'Its reported segments are Cloud Memory (~30%, ...), Core Data Center
    (...), and Automotive & Embedded (...)' -> the four names."""
    text = re.sub(r"\([^)]*\)", "", segments_line or "")
    names = []
    for chunk in re.split(r"[;,]|\s+and\s+(?=[A-Z])", text):
        chunk = _SEG_PREFIX_RE.sub("", chunk.strip())
        chunk = re.split(r":|\bapprox|~|\d", chunk, maxsplit=1)[0].strip()
        m = _LEAD_NAME_RE.match(chunk)
        if not m:
            continue
        name = m.group(1).strip(" &")
        if name and name.lower() not in _STOP and len(name.split()) <= 5:
            names.append(name)
    return names


def business_terms(primer: str) -> set[str]:
    """Lower-cased names a business-line mention would use: the segment
    names and their distinctive words ('core data center', 'memory'), the
    product acronyms the primer introduces, and, for a one-segment
    company, the content words of what it sells. The company's own name is
    not a business line and is never a term."""
    segs, sells, drivers = (_line(primer, k) for k in ("SEGMENTS", "SELLS", "DRIVERS"))
    company = (sells.split()[0] if sells else "").lower()
    terms: set[str] = set()
    for name in segment_names(segs):
        terms.add(name.lower())
        for w in re.findall(r"[A-Za-z][A-Za-z-]{5,}", name):
            if w.lower() not in _WEAK and w.lower() not in _STOP:
                terms.add(w.lower())
    for text in (segs, drivers, sells):
        for a in _PAREN_ACRONYM_RE.findall(text):
            terms.add(a.lower())
    for text in (segs, drivers):
        for a in _ACRONYM_RE.findall(text):
            if a not in _GENERIC:
                terms.add(a.lower())
    if not segment_names(segs):
        # one segment: what it sells
        for w in re.findall(r"[A-Za-z][A-Za-z\-]{4,}", sells):
            if w.lower() not in _STOP:
                terms.add(w.lower())
    terms.discard(company)
    return terms


def names_business_line(answer: str, primer: str) -> bool:
    """True when the answer names at least one business line from the
    primer, or when the primer gives nothing to check against."""
    terms = business_terms(primer)
    if not terms:
        return True
    low = (answer or "").lower()
    return any(re.search(rf"(?<![a-z]){re.escape(t)}(?![a-z])", low) for t in terms)


REWRITE_PROMPT = (
    "Rewrite the answer below so it says which part of {sym}'s business drives the figures "
    "it discusses, using the BUSINESS PRIMER: name the segment or product line and what it "
    "sells, in plain words, beside the figure it explains. Keep every figure, date, bank name "
    "and attribution exactly as written. Add no figure that is not in the answer or the "
    "primer. Keep the arrow format; you may add one arrow. Output only the rewritten answer.\n\n"
    "BUSINESS PRIMER:\n{primer}\n\nANSWER:\n{answer}"
)
