"""Desk idiom in /ask answers (2026-10-05 audit, finding #12).

The audience trades options and crypto but does not read bank research.
A Treasury-buyback answer shipped "dealer balance sheets" and "long-end
duration supply" unexplained. The pulse has a translation map
(ai_analysis.voice_rules.JARGON_WITH_TRANSLATIONS) that /ask never used.

`find` returns the desk terms an answer uses with their plain meaning.
bot.py feeds them to the existing register rewrite, which rewrites the
sentences and is accepted only if every figure survives and the terms
are gone. The list is desk idiom only: options words this room uses
every day (gamma, skew, IV, theta) are not jargon here, and words with
an everyday meaning ("carry", "issuance", "basis") are left out because
a regex cannot tell the two uses apart. Named metrics (NII, SLR, EBITDA)
are not listed either: the house rule keeps their name with a first-use
gloss, and this rewrite removes the term.
"""
from __future__ import annotations

import re

# term -> plain meaning. Taken from voice_rules where it has the term,
# trimmed to the meaning a rewrite needs.
ASK_JARGON: dict[str, str] = {
    "dealer balance sheet": "how much room the big banks that trade Treasuries have to hold more bonds",
    "dealer balance sheets": "how much room the big banks that trade Treasuries have to hold more bonds",
    "duration supply": "new long-dated bonds coming to market",
    "duration risk": "how much a bond's price falls when yields rise",
    "term premium": "the extra yield investors demand to hold long-dated bonds",
    "term structure": "what the market expects rates to do over time",
    "long-end": "long-dated Treasuries (20 to 30 years)",
    "long end of the curve": "long-dated Treasuries (20 to 30 years)",
    "breakevens": "the inflation rate priced into the bond market",
    "convexity": "how a bond's price sensitivity changes as yields move",
    "bear-steepen": "long-term yields rising faster than short-term",
    "bear-steepening": "long-term yields rising faster than short-term",
    "bear-flatten": "short-term yields rising faster than long-term",
    "bear-flattening": "short-term yields rising faster than long-term",
    "bull-steepening": "short-term yields falling faster than long-term",
    "bull-flattening": "long-term yields falling faster than short-term",
    "coupon supply": "new Treasury bonds being auctioned",
    "prime brokerage": "what hedge funds are doing, as seen by the banks that lend to them",
    "dispersion trade": "a bet that stocks move in different directions rather than the index moving",
}

_RES = [(t, re.compile(rf"(?<![\w-]){re.escape(t)}(?![\w-])", re.I))
        for t in sorted(ASK_JARGON, key=len, reverse=True)]


def find(answer: str) -> dict[str, str]:
    """The desk terms `answer` uses, longest match first, with their plain
    meaning. Terms inside code blocks or links are ignored."""
    text = re.sub(r"```.*?```|`[^`]*`|\[[^\]]*\]\([^)]*\)|<https?://[^>]+>", " ",
                  answer or "", flags=re.S)
    out: dict[str, str] = {}
    for term, rx in _RES:
        if rx.search(text):
            # "long-end" and "long end" are one term to the reader
            if any(term in t or t in term for t in out):
                continue
            out[term] = ASK_JARGON[term]
    return out


_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def keeps_numbers(original: str, rewritten: str) -> bool:
    """Every number written in the original is still in the rewrite."""
    have = {n.replace(",", "") for n in _NUMBER.findall(rewritten or "")}
    return all(n.replace(",", "") in have for n in _NUMBER.findall(original or ""))


def directive(terms: dict[str, str]) -> str:
    """The rewrite instruction for the register pass."""
    lines = "; ".join(f'"{t}" means {m}' for t, m in terms.items())
    return ("Rewrite every sentence that uses these desk terms so a retail trader "
            "follows it without the term: rewrite the sentence, do not keep the term "
            f"and add a definition. {lines}. Keep every number exactly. ")
