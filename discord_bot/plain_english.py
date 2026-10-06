"""Desk idiom in /ask answers (2026-10-05 audit, finding #12).

The audience trades options and crypto but does not read bank research.
A Treasury-buyback answer shipped "dealer balance sheets" and "long-end
duration supply" unexplained. The pulse has a translation map
(ai_analysis.voice_rules.JARGON_WITH_TRANSLATIONS) that /ask never used.

`find` returns the desk terms an answer uses with their plain meaning.
bot.py feeds them to the existing register rewrite, which rewrites the
sentences and is accepted only if every figure survives and the terms
are gone. Entries are patterns, not fixed phrases: the first live sample
after a fixed list wrote "help dealers recycle balance sheets" and
"backstop the long end", which a phrase list missed.

Desk idiom only. Options words this room uses every day (gamma, skew,
IV, theta) are not jargon here. Words with an everyday meaning ("carry",
"issuance", "basis", a company's "balance sheet") are left out because a
regex cannot tell the two uses apart. Named metrics (NII, SLR, EBITDA)
are not listed: the house rule keeps their name with a first-use gloss,
and this rewrite removes the term.
"""
from __future__ import annotations

import re

# (label, pattern, plain meaning). The labels down to "coupon supply" are
# rates idioms; they count only in an answer that is about bonds
# (_BOND_CONTEXT): "long-duration" is also energy storage, and car
# dealers have balance sheets too.
_TERMS: list[tuple[str, str, str]] = [
    ("dealer balance sheets", r"dealers?(?:'|’)?\s+(?:[a-z-]+\s+){0,3}balance[- ]sheets?",
     "how much room the big banks that trade Treasuries have to hold more bonds"),
    ("duration", r"(?:long|short)[- ]duration|duration\s+(?:supply|risk|exposure|trade)",
     "how far a bond's price moves when yields change: long-dated bonds move the most"),
    ("long end", r"(?:the\s+)long[- ]end|long-end",
     "long-dated Treasuries (20 to 30 years)"),
    ("off-the-run", r"(?:off|on)-the-run",
     "older Treasury issues that trade less often than the newest ones"),
    ("term premium", r"term\s+premium", "the extra yield investors demand to hold long-dated bonds"),
    ("term structure", r"term\s+structure", "what the market expects rates to do over time"),
    ("breakevens", r"breakevens?(?:\s+inflation)?", "the inflation rate priced into the bond market"),
    ("convexity", r"convexity", "how a bond's price sensitivity changes as yields move"),
    ("steepening", r"(?:bear|bull)[- ]steepen(?:ing|er|ed)?",
     "long-term yields moving up relative to short-term ones"),
    ("flattening", r"(?:bear|bull)[- ]flatten(?:ing|er|ed)?",
     "short-term yields moving up relative to long-term ones"),
    ("coupon supply", r"coupon\s+supply", "new Treasury bonds being auctioned"),
    ("prime brokerage", r"prime\s+brokerage", "what hedge funds are doing, as seen by the banks that lend to them"),
    ("dispersion trade", r"dispersion\s+trades?", "a bet that stocks move in different directions rather than the index moving"),
]
_RATES = {"dealer balance sheets", "duration", "long end", "off-the-run", "term premium",
          "term structure", "breakevens", "convexity", "steepening", "flattening",
          "coupon supply"}
_BOND_CONTEXT = re.compile(
    r"\b(?:treasur\w*|bonds?|yields?|the curve|coupons?|auctions?|t-bills?|gilts?|bunds?)\b", re.I)
_RES = [(label, re.compile(rf"(?<![\w-]){pat}(?![\w-])", re.I), meaning)
        for label, pat, meaning in _TERMS]


def find(answer: str) -> dict[str, str]:
    """{label: plain meaning} for each desk term `answer` uses. Terms in
    code blocks and in markdown links (source titles) are ignored."""
    text = re.sub(r"```.*?```|`[^`]*`|\[[^\]]*\]\([^)]*\)|<https?://[^>]+>", " ",
                  answer or "", flags=re.S)
    bonds = bool(_BOND_CONTEXT.search(text))
    return {label: meaning for label, rx, meaning in _RES
            if rx.search(text) and (bonds or label not in _RATES)}


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
