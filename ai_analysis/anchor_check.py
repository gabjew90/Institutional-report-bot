"""Normalized-match verification of extraction anchors against source
text — claim-card redesign sequencing step 2 (spec
docs/superpowers/specs/2026-08-20-claim-card-synthesis-redesign.md §3).

WHAT THIS BUYS
==============
Deep analysis asks Gemini for a verbatim `anchor` quote on every
key_data_point. This module checks that the anchor actually appears in
the extracted PDF text. With it, extraction fidelity is a MEASURED
number per document instead of an assumed property — the verification
plumbing the shadow pilot's claim cards will reuse.

Step 2 is deliberately WARN-ONLY: a failed anchor is logged and counted
in the stats attached to the analysis, and nothing is dropped or
retried. The current pipeline's consumers were built on unanchored
extraction; changing their behavior is pilot territory (spec §11 —
drops and re-asks belong to the reader design, not this step).

WHY NORMALIZED MATCHING, NOT LITERAL SUBSTRING
==============================================
PyMuPDF text from two-column, exhibit-heavy bank PDFs is full of
artifacts that break literal matching while leaving the text humanly
identical: soft line-wrap hyphens ("infla-\\ntion"), ligature glyphs
(fi/fl/ffi), non-breaking and thin spaces, smart quotes, en/em dashes,
and arbitrary line breaks mid-sentence. A literal check would fail
honest anchors constantly and the metric would measure the PDF
renderer, not the extraction. Both sides are normalized identically, so
a match means "these words appear in this document" — which is the
claim being verified.

The normalization is deliberately conservative: it never touches
digits, so a paraphrase that reformats a number ("$751 billion" for
"$751B") still fails — that distinction is the point of the check.
"""
from __future__ import annotations

import logging
import re
import unicodedata

log = logging.getLogger(__name__)

# Anchors shorter than this normalize into strings that match almost any
# document ("4.4%"), which reads as fidelity while verifying nothing.
# Counted as too_short, not as matches.
MIN_ANCHOR_CHARS = 12

_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}

# Quote/dash/space variants that PDF extractors emit interchangeably.
_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "−": "-", "‐": "-",
    "‑": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "",  # zero-width space
}


def normalize(text: str) -> str:
    """Collapse PDF-extraction artifacts so identical words compare
    equal. Digits are never altered."""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text)
    for src, dst in _LIGATURES.items():
        s = s.replace(src, dst)
    for src, dst in _CHAR_MAP.items():
        s = s.replace(src, dst)
    # Soft line-wrap dehyphenation: a hyphen at end-of-line joining two
    # letter runs is a rendering artifact ("infla-\ntion"). A hyphen
    # between letters WITHOUT a line break is real ("bear-steepener")
    # and survives.
    s = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", s)
    s = s.casefold()
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def check_anchors(key_data_points: list, source_text: str) -> dict:
    """Verify each point's anchor is a normalized substring of the
    source. Returns stats; never raises.

    {
      "total":     points inspected,
      "matched":   anchors found in the source,
      "missed":    anchors NOT found (the fidelity signal),
      "empty":     points with no anchor (model omitted the field),
      "too_short": anchors under MIN_ANCHOR_CHARS (unverifiable),
      "match_rate": matched / verifiable, None when nothing verifiable,
      "misses":    up to 10 {figure, anchor} samples for the log/QC,
    }

    `key_data_points` accepts dataclasses or dicts — the caller has
    dataclasses in the live path, the QC scripts read JSON back from
    the DB.
    """
    stats = {"total": 0, "matched": 0, "missed": 0, "empty": 0,
             "too_short": 0, "match_rate": None, "misses": []}
    try:
        haystack = normalize(source_text or "")
        for p in key_data_points or []:
            get = p.get if isinstance(p, dict) else (
                lambda k, _p=p: getattr(_p, k, ""))
            stats["total"] += 1
            anchor = (get("anchor") or "").strip()
            if not anchor:
                stats["empty"] += 1
                continue
            if len(anchor) < MIN_ANCHOR_CHARS:
                stats["too_short"] += 1
                continue
            if normalize(anchor) in haystack:
                stats["matched"] += 1
            else:
                stats["missed"] += 1
                if len(stats["misses"]) < 10:
                    stats["misses"].append({
                        "figure": str(get("figure") or "")[:60],
                        "anchor": anchor[:160],
                    })
        verifiable = stats["matched"] + stats["missed"]
        if verifiable:
            stats["match_rate"] = round(stats["matched"] / verifiable, 3)
    except Exception as e:
        # The checker must never take down an analysis. A stats dict
        # with an error marker is still a data point for the pilot.
        log.warning(f"anchor check failed internally: {e}")
        stats["error"] = f"{type(e).__name__}: {e}"
    return stats
