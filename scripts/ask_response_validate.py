#!/usr/bin/env python3
"""Pre-send validator for /ask answers.

WHY THIS EXISTS
===============
CLAUDE.md prompt-policy rule 1: if a rule can be checked by code, it is
implemented as code and the prompt text is DELETED in the same commit.
Never both.

`07b-no-meta-plumbing` is the clearest case in the suite that prompt text
is not enforcement. The prompt carried a detailed NEVER META-NARRATE
block that quoted almost the exact violating sentence, verbatim, as the
shape never to repeat. It failed 0/3 anyway, shipping `backend`, `API`,
`poll the chain daily` and `store the snapshot` at a dev-framed question.
A rule the model reads and then violates while reproducing the quoted
example is not being enforced by being written down.

SCOPE THIS SESSION: one class, backend/plumbing disclosure. The other
classes (the lines 96-102 cluster) come next and are deliberately out of
scope here.

WHAT IT IS NOT
==============
It flags; it does not rewrite. Callers decide what to do with a
violation (regenerate, strip the sentence, or refuse). Fuzzy signals must
never drive a destructive path unreviewed — the same reason
`_invented_personal_details` is wired to the rewrite stage in production
and never to the strip path.

USAGE
=====
    from scripts.ask_response_validate import validate
    violations = validate(answer, tool_calls=["lookup_options_chain"])

    python scripts/ask_response_validate.py --self-test
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field


@dataclass
class Violation:
    rule: str
    match: str
    span: tuple[int, int]
    line: str
    why: str = ""

    def __str__(self) -> str:
        return f"[{self.rule}] {self.match!r} — {self.line.strip()[:110]}"


# --------------------------------------------------------------- lexicon
# Plumbing vocabulary. On its own each of these is innocent: a trader can
# ask about Snowflake's database business or an exchange's API outage.
# What makes it a violation is the SUBJECT being the bot's own operation.
_PLUMBING = (
    r"back[- ]?end|API|endpoint|poll(?:s|ed|ing)?|snapshot|schema|feed|"
    r"stor(?:e|es|ed|ing|age)|ingest(?:s|ed|ion)?|databases?|DB|cach(?:e|es|ed|ing)|"
    r"data (?:source|pipeline|layer)|cron|scrape[sdr]?|scraping|"
    r"index(?:es|ed|ing)?|table|query the|rate[- ]limit"
)

# Density-only vocabulary. NOT used for pointwise detection, because
# "pull it from your broker or data vendor" is the sanctioned refusal the
# rule actually prescribes — flagging it would punish the correct answer.
# In aggregate though, these are the giveaway that an answer is walking
# through a fetch path.
_DENSITY_EXTRA = (
    r"REST|WebSockets?|web ?sockets?|data vendors?|data providers?|"
    r"fetch(?:es|ed|ing)?|pipelines?|latency|payloads?|"
    r"broker APIs?|market data (?:feed|provider)"
)

# The bot talking about ITSELF. Includes the dev-address shape, which is
# the 07b trigger: the asker being recognised as the maintainer.
_SELF = (
    r"\bI\b|\bI'?(?:m|ve|ll|d)\b|\bmy\b|\bwe\b|\bwe'?(?:re|ve|ll)\b|\bour\b|"
    r"\bus\b|\bthe bot\b|\bthis bot\b|\bmy (?:tools?|feed|data)\b|"
    r"\byou'?d need to\b|\byou would need to\b|\bif you'?re building\b|"
    r"\byou'?re the dev\b|\bthe tracker\b|\bthe current feed\b"
)

# Phrases that are a violation regardless of nearby pronouns, because the
# only possible subject is the bot's own plumbing. These are the exact
# shapes 07b shipped.
_ABSOLUTE = (
    r"store the snapshot",
    r"poll(?:ing)? the chain",
    r"the current feed",
    r"static state",
    r"my tool (?:inventory|list)",
    r"the tools?\s+(?:I|we)\s+have",
    r"(?:I|we)\s+(?:don'?t\s+)?(?:poll|ingest|cache|store|index)\b",
    r"(?:doesn'?t|does not|only)\s+(?:give|return)s?\s+(?:us|me)\b",
    r"(?:I|we)\s+(?:pull|fetch|query)\s+(?:it\s+)?from\s+(?:an?\s+)?"
    r"(?:API|feed|endpoint|database|db)\b",
)

# Subjects that make plumbing talk legitimate: the question is ABOUT a
# company or product whose business is infrastructure. Without this the
# validator fires on a correct answer about $SNOW or an exchange outage.
_EXTERNAL_SUBJECT = re.compile(
    r"\$[A-Z]{1,5}\b|\b(?:Snowflake|Databricks|Oracle|AWS|Azure|Cloudflare|"
    r"MongoDB|Datadog|Coinbase|Binance|Robinhood|Schwab|Finnhub|Yahoo|"
    r"Bloomberg|Reuters|Google|OpenAI|Nvidia)\b",
    re.I,
)

# Definite-article plumbing nouns. "The feed's right there in the
# backend" names the bot's own machinery with no pronoun anywhere, which
# is how a real 07b answer slipped past the windowed detector. These fire
# unless the sentence is plainly about an external company.
_BOT_NOUNS = re.compile(
    r"\b(?:the|my|our) (?:feeds?|back[- ]?ends?|plumbing|pipelines?|"
    r"trackers?|databases?|caches?|indexe?s?|data layers?|endpoints?|"
    r"APIs?|schemas?|snapshots?)\b"
    r"|\b(?:spot|static|current) snapshots?\b"
    r"|\b(?:feeds?|endpoints?) are (?:wired|live|up|down)\b",
    re.I,
)

_PLUMBING_RE = re.compile(rf"\b(?:{_PLUMBING})\b", re.I)
_DENSITY_RE = re.compile(rf"\b(?:{_PLUMBING}|{_DENSITY_EXTRA})\b", re.I)
_SELF_RE = re.compile(_SELF, re.I)
_ABSOLUTE_RES = [re.compile(p, re.I) for p in _ABSOLUTE]

# How close a self-reference has to be to count as the subject.
_WINDOW = 90


def _line_at(text: str, pos: int) -> str:
    a = text.rfind("\n", 0, pos) + 1
    b = text.find("\n", pos)
    return text[a: b if b != -1 else len(text)]


def check_meta_plumbing(answer: str, tool_calls=None) -> list[Violation]:
    """Flag plumbing vocabulary used to describe the BOT'S OWN operation.

    Two detectors:
      absolute — phrases whose only possible subject is the bot.
      windowed — plumbing vocabulary within `_WINDOW` chars of a
                 self-reference or a dev-address, with an exemption when
                 the sentence is plainly about an external company.
    """
    out: list[Violation] = []
    seen: set[tuple[int, int]] = set()

    for rx in _ABSOLUTE_RES:
        for m in rx.finditer(answer or ""):
            if m.span() in seen:
                continue
            seen.add(m.span())
            out.append(Violation(
                "meta-plumbing", m.group(0), m.span(),
                _line_at(answer, m.start()),
                "describes the bot's own data plumbing"))

    for m in _BOT_NOUNS.finditer(answer or ""):
        if m.span() in seen:
            continue
        sentence = _line_at(answer, m.start())
        if _EXTERNAL_SUBJECT.search(sentence):
            continue
        seen.add(m.span())
        out.append(Violation(
            "meta-plumbing", m.group(0), m.span(), sentence,
            "names the bot's own machinery"))

    for m in _PLUMBING_RE.finditer(answer or ""):
        if any(a <= m.start() < b for a, b in seen):
            continue
        lo = max(0, m.start() - _WINDOW)
        hi = min(len(answer), m.end() + _WINDOW)
        window = answer[lo:hi]
        if not _SELF_RE.search(window):
            continue
        # An external subject in the same sentence makes this legitimate
        # ("$SNOW's database business", "Coinbase's API went down").
        sentence = _line_at(answer, m.start())
        if _EXTERNAL_SUBJECT.search(sentence):
            continue
        seen.add(m.span())
        out.append(Violation(
            "meta-plumbing", m.group(0), m.span(), sentence,
            "plumbing vocabulary with the bot as the subject"))

    # DENSITY. The exemptions above are per-sentence, and a dev-framed
    # answer defeats them by naming real vendors while still explaining
    # the fetch path ("live chains come from broker APIs or data vendors
    # via REST/WebSocket endpoints (like Tradier, Polygon...)"). No
    # legitimate trader answer enumerates fetch mechanics this densely,
    # so past a threshold the shape itself is the violation regardless of
    # who the nominal subject is.
    distinct = {m.group(0).lower()
                for m in _DENSITY_RE.finditer(answer or "")}
    if len(distinct) >= 3 and not any(v.rule == "meta-plumbing"
                                      for v in out):
        out.append(Violation(
            "meta-plumbing", ", ".join(sorted(distinct)[:6]), (0, 0),
            (answer or "").strip().splitlines()[0][:120] if answer else "",
            f"{len(distinct)} distinct plumbing terms — the answer is "
            f"explaining the fetch path"))

    out.sort(key=lambda v: v.span[0])
    return out


_CHECKS = {"meta-plumbing": check_meta_plumbing}


# The phrasing the deleted prompt block prescribed for exactly this
# situation. Used only when an answer is plumbing END TO END, so a
# sentence strip has nothing left to keep.
SAFE_REFUSAL = ("→ don't have that one — pull it from your broker or "
                "data vendor.")


def resolve_violations(answer: str, retry_answer: str | None,
                       tool_calls=None, strip_fn=None,
                       fallback: str | None = SAFE_REFUSAL
                       ) -> tuple[str, str]:
    """The guard ladder, as ONE decision both callers share.

    Returns (final_answer, outcome) where outcome is one of:
      clean        nothing was wrong
      regenerated  the retry came back clean and was used
      stripped     the retry still violated; offending sentences excised
      replaced     the answer was plumbing end to end, so there was
                   nothing to keep; the prescribed refusal goes out
                   instead
      shipped      nothing worked and no fallback was given

    Callers do their own regeneration (async in the bot, sync in the
    harness) and hand the result in, so the DECISION lives in one place
    while the I/O stays where it belongs. Two implementations of this
    ladder would drift, and drifting between what production does and
    what the harness measures is the exact failure this project has
    already paid for four times.
    """
    strip_fn = strip_fn or _default_strip
    if not answer or not validate(answer, tool_calls):
        return answer, "clean"
    if retry_answer and not validate(retry_answer, tool_calls):
        return retry_answer, "regenerated"
    bad = violating_sentences(answer, tool_calls)
    stripped = strip_fn(answer, bad)
    if stripped and not validate(stripped, tool_calls):
        return stripped, "stripped"
    # BEYOND the repetition detector's ladder, deliberately. That one
    # ships the original because a glitchy answer still carries the
    # content and something beats blank. Here the violation IS the
    # content, and there is a correct non-blank answer — the refusal the
    # rule prescribes. Shipping known plumbing to satisfy "something
    # beats blank" would defeat the only assertion this class makes.
    if fallback and not validate(fallback, tool_calls):
        return fallback, "replaced"
    return answer, "shipped"


def _default_strip(answer: str, to_remove: list[str]) -> str:
    """Fallback strip. The bot passes its own `_strip_sentences`."""
    out = answer
    for t in to_remove:
        out = out.replace(t, " ")
    return re.sub(r"\s{2,}", " ", out).strip()


def violating_sentences(answer: str, tool_calls=None, only=None) -> list[str]:
    """The exact sentence strings carrying a violation.

    Feeds the strip fallback, which removes whole sentences rather than
    editing inside them — the same shape `_repetition_glitch_sentences`
    uses, and for the same reason: excising a clause mid-sentence mangles
    prose, while a violating sentence is self-contained.
    """
    out, seen = [], set()
    for v in validate(answer, tool_calls, only):
        line = v.line.strip()
        for sent in re.split(r"(?<=[.!?])\s+", line):
            if v.match.lower() in sent.lower():
                if sent not in seen:
                    seen.add(sent)
                    out.append(sent)
                break
        else:
            if line and line not in seen:
                seen.add(line)
                out.append(line)
    return out


def validate(answer: str, tool_calls=None, only=None) -> list[Violation]:
    """Run every enabled check. `only` restricts to named rules."""
    vs: list[Violation] = []
    for name, fn in _CHECKS.items():
        if only and name not in only:
            continue
        vs.extend(fn(answer, tool_calls))
    return vs


# ------------------------------------------------------------ self-test
# VIOLATING answers are verbatim from fixture 07b runs where the prompt
# rule was in force and lost. CLEAN answers include the shapes that must
# NOT fire: an on-topic answer about a company whose product is
# infrastructure, and the correct refusal the rule actually wants.
_BAD = [
    "the API only returns a snapshot, so you'd poll the endpoint daily "
    "and store the snapshot in the backend schema.",
    "If you're building the backend for the bot's tracker, you'll need to "
    "poll the chain daily and store the snapshot to get a reliable trend, "
    "the current feed only gives us the static state",
    "→ I don't cache historical chain data — my feed only gives us the "
    "current static state.",
    "we'd need to ingest that into the database on a cron to answer it.",
]
_GOOD = [
    "→ don't have multi-day chain history. your broker keeps it.",
    "→ I don't have that — pull it from your broker or data vendor.",
    "→ $SNOW's database business is the whole story here: consumption "
    "revenue grew 28% and the API-driven workloads are what analysts "
    "are underwriting.",
    "→ Coinbase's API went down during the selloff, so fills were "
    "delayed across the board.",
    "→ $ORCL at $244.10, up 1.8% on the session.",
    "→ he opened AVGO 450C at 4.5 with no exit posted.",
]


def _self_test() -> int:
    print("=== ask_response_validate self-test ===\n")
    fails = 0
    print("MUST FLAG (recorded 07b violations):")
    for a in _BAD:
        vs = check_meta_plumbing(a)
        ok = bool(vs)
        fails += 0 if ok else 1
        print(f"  {'caught ' if ok else 'MISSED '} {a[:66]!r}")
        if vs:
            print(f"            -> {vs[0].match!r} ({len(vs)} total)")
    print("\nMUST NOT FLAG (correct answers):")
    for a in _GOOD:
        vs = check_meta_plumbing(a)
        ok = not vs
        fails += 0 if ok else 1
        print(f"  {'clean  ' if ok else 'FALSE+ '} {a[:66]!r}")
        if vs:
            print(f"            -> {vs[0]}")
    print("\n" + "-" * 62)
    if fails:
        print(f"{fails} case(s) wrong — validator not usable as enforcement")
        return 1
    print(f"all {len(_BAD)} violations caught, "
          f"{len(_GOOD)} clean answers passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--text", default=None,
                    help="validate one answer given on the command line")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()
    if args.text:
        vs = validate(args.text)
        for v in vs:
            print(v)
        print(f"{len(vs)} violation(s)")
        return 1 if vs else 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
