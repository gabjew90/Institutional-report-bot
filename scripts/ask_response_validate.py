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
    # `index` and `table` were in this list and should never have
    # been: "the index hugs all-time highs" is a MARKET index, and a
    # table is something an answer legitimately shows. Both produced
    # false positives in the recorded-corpus sweep.
    r"query the|rate[- ]limit|plumbing"
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
    # `index` is gone: "the index hugs all-time highs" is a MARKET
    # index. `plumbing` is gone too — it moved to the windowed detector,
    # because "essential reading on market plumbing" is about markets
    # while "I'm looking at an order book, not the plumbing" is about
    # the bot, and only a nearby self-reference separates them.
    r"\b(?:the|my|our) (?:feeds?|back[- ]?ends?|pipelines?|"
    r"trackers?|databases?|caches?|data layers?|endpoints?|"
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


# The answer the rule PRESCRIBES. "I only have the current snapshot --
# no historical log" is the correct response to a history question, and
# the first cut of this validator flagged it on the word `snapshot`,
# then stripped it. A validator that deletes the sentence its own rule
# asks for is worse than no validator.
_PRESCRIBED_REFUSAL = re.compile(
    r"(?i)\b(?:don'?t|do not|doesn'?t)\s+have\b"
    r"|\bonly\s+have\b|\bno\s+(?:historical|multi-?day|history|live)\b"
    r"|\bpull\s+(?:it\s+|that\s+)?(?:from|off)\b"
    r"|\byour\s+broker\b|\bdata vendor\b"
    r"|\bnot?\s+(?:chart|indicator)\s+(?:view|feed)\b"
)


def check_meta_plumbing(answer: str, tool_calls=None,
                        **_) -> list[Violation]:
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
            sent = _line_at(answer, m.start())
            if _PRESCRIBED_REFUSAL.search(sent):
                continue
            out.append(Violation(
                "meta-plumbing", m.group(0), m.span(), sent,
                "describes the bot's own data plumbing"))

    for m in _BOT_NOUNS.finditer(answer or ""):
        if m.span() in seen:
            continue
        sentence = _line_at(answer, m.start())
        if (_EXTERNAL_SUBJECT.search(sentence)
                or _PRESCRIBED_REFUSAL.search(sentence)):
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
        if (_EXTERNAL_SUBJECT.search(sentence)
                or _PRESCRIBED_REFUSAL.search(sentence)):
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
                for m in _DENSITY_RE.finditer(answer or "")
                if not _PRESCRIBED_REFUSAL.search(
                    _line_at(answer or "", m.start()))}
    if len(distinct) >= 3 and not any(v.rule == "meta-plumbing"
                                      for v in out):
        out.append(Violation(
            "meta-plumbing", ", ".join(sorted(distinct)[:6]), (0, 0),
            (answer or "").strip().splitlines()[0][:120] if answer else "",
            f"{len(distinct)} distinct plumbing terms — the answer is "
            f"explaining the fetch path"))

    out.sort(key=lambda v: v.span[0])
    return out


# ------------------------------------------------- class 2: macro prints
# A macro print figure is a number only the calendar tool can source.
# Stated without that tool firing this turn, it came from memory or a
# Google snippet — the 2026-06-05 / 06-08 shape, where /ask said NFP 172k
# while the same day's pulse said 120k, then recycled the stale 172k days
# later.
_MACRO_SERIES = re.compile(
    r"\b(?:core\s+)?(?:CPI|PCE|PPI|GDP|NFP|ISM|"
    r"nonfarm(?:\s+payrolls?)?|payrolls?|"
    r"(?:initial\s+|continuing\s+)?(?:jobless\s+)?claims|"  # noqa
    r"unemployment(?:\s+rate)?|retail\s+sales|"
    r"consumer\s+price(?:\s+index)?|"
    r"personal\s+consumption(?:\s+expenditures)?)\b",
    re.I,
)

# A PRINTED VALUE, not a schedule. Times ("8:30") and plain dates are
# deliberately unmatched: "CPI lands Wednesday at 8:30" is a calendar
# statement, not a figure, and flagging it would punish a correct answer.
_MACRO_FIGURE = re.compile(
    r"-?\d+(?:\.\d+)?\s?%"
    r"|\b\d{2,4}\s?[Kk]\b"
    r"|\b\d{1,3}(?:,\d{3})+\b"
    r"|\b\d\.\d\b"
)

_MACRO_WINDOW = 110
_CALENDAR_TOOL = "lookup_economic_calendar"


def check_macro_unsourced(answer: str, tool_calls=None,
                          **_) -> list[Violation]:
    """Flag a macro print FIGURE stated without the calendar tool firing.

    Deliberately keyed on the tool-call log rather than on wording: the
    prompt already said ALWAYS call the tool FIRST, in bold, twice, with
    both incident dates attached, and the model still answered from
    memory. Whether the tool fired is a fact about the turn, so it is
    checkable, so it belongs here rather than in prose.
    """
    if _CALENDAR_TOOL in (tool_calls or []):
        return []
    out: list[Violation] = []
    text = answer or ""
    for m in _MACRO_SERIES.finditer(text):
        lo = max(0, m.start() - _MACRO_WINDOW)
        hi = min(len(text), m.end() + _MACRO_WINDOW)
        fig = _MACRO_FIGURE.search(text[lo:hi])
        if not fig:
            continue
        out.append(Violation(
            "macro-unsourced", f"{m.group(0)} … {fig.group(0)}",
            m.span(), _line_at(text, m.start()),
            f"macro print figure with no {_CALENDAR_TOOL} call this turn"))
        break   # one per answer is enough to trigger the ladder
    return out



# ------------------------------------------- class 3: blocked-URL guess
# bot.py refuses to fetch x.com / twitter.com / t.co because X serves a
# login wall to unauthenticated scrapers. _maybe_fetch_user_urls returns
# "" for those hosts, so the model sees a bare link and NO content, and
# nothing in the turn tells it the content is missing. On 2026-08-26 it
# filled the gap: asked to verify a tweet, it ran google_search and
# produced three confident bullets about 30Y yields and Treasury
# buybacks with four citations, describing a post it never read.
#
# The citations are what make this the worst shape in the suite. A
# visibly wrong answer gets corrected by the room. A confabulation
# wearing four sources reads as verified.
#
# The intended fallback — ask for the pasted text — was never written
# into the prompt at all. It existed only as a code comment at bot.py:67
# describing what someone should tell users. So there was no prose to
# fail; there was nothing.
_BLOCKED_HOSTS = ("x.com", "twitter.com", "t.co")
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+")

# The answer claiming to know what the link contains.
#
# Case-insensitivity goes in the FLAGS argument, never as an inline
# `(?i)` in the middle of a pattern. Python 3.10 only warns about a
# mid-pattern global flag; Python 3.12 — which is what the Railway
# container runs — raises re.error at import. That took the bot down
# on 2026-08-26. The DeprecationWarning was visible in harness output
# for hours beforehand and went unread.
_LINK_CLAIM = re.compile(
    r"\bthe (?:tweet|post|article|link|thread)\s+"
    r"(?:says|said|claims?|argues?|points? out|shows?|notes?|reads)"
    r"|\baccording to the (?:tweet|post|link|thread)\b"
    r"|\bhe'?s (?:right|wrong) (?:that|about)\b"
    r"|\bthat'?s (?:basically )?(?:it|correct|right|true)\b"
    r"|\bknowing \w+,? it'?s\b",
    re.I,
)

# The answer doing the correct thing instead.
_ASKS_FOR_TEXT = re.compile(
    r"\bpaste\b|\bcopy .{0,20}(?:the )?(?:text|tweet|post)\b"
    r"|\bdrop the text\b|\bwhat does it say\b"
    r"|\bcan'?t (?:open|see|read|access)\b|\bdon'?t have access\b"
    r"|\bscreenshot\b",
    re.I,
)


def _unretrieved_urls(question: str, fetched: str | None) -> list[str]:
    """URLs in the question whose contents nothing retrieved.

    NOT keyed on the blocked-host list. ANY url counts, because nothing
    in this system can fetch an arbitrary page: no registered tool takes
    a URL, google_search grounds on question text rather than browsing,
    code execution has no network, and the SDK's url_context tool is not
    registered (and returned URL_RETRIEVAL_STATUS_ERROR when tested on
    both a normal host and x.com). The ONLY retrieval path is
    _maybe_fetch_user_urls, whose output arrives as `fetched`. Absent
    from `fetched` means nobody read it.
    """
    out = []
    for u in _URL_RE.findall(question or ""):
        if fetched and u in fetched:
            continue
        out.append(u)
    return out


def _has_pasted_body(question: str) -> bool:
    """Did the asker paste the content alongside the link?

    Heuristic and deliberately generous: quote marks, a markdown quote
    block, or a substantial run of words once the URLs are stripped. Being
    generous here is the safe direction — a false 'yes' just means the
    guard stays out of the way on a turn where the model does have
    material to work from.
    """
    if not question:
        return False
    bare = _URL_RE.sub(" ", question)
    if '"' in bare or "\u201c" in bare or ">" in bare:
        return True
    words = [w for w in re.split(r"\s+", bare) if w.strip("@:,.?!")]
    return len(words) >= 14


def check_unfetchable_link_claim(answer: str, tool_calls=None,
                                 question=None, fetched=None,
                                 **_) -> list[Violation]:
    """Flag an answer that describes a link the turn could not fetch.

    Fires only when all three hold: the question carries a blocked-host
    URL, the asker pasted no body, and nothing was fetched for it. Then
    an answer that asserts what the link contains — or that fails to ask
    for the text — is unfounded by construction.
    """
    if not question:
        return []
    blocked = _unretrieved_urls(question, fetched)
    if not blocked:
        return []
    if _has_pasted_body(question):
        return []

    claim = _LINK_CLAIM.search(answer or "")
    if claim:
        return [Violation(
            "unfetchable-link-claim", claim.group(0), claim.span(),
            _line_at(answer or "", claim.start()),
            f"asserts the contents of {blocked[0]}, which was never "
            f"fetched (login-walled host)")]
    if not _ASKS_FOR_TEXT.search(answer or ""):
        return [Violation(
            "unfetchable-link-claim", blocked[0], (0, 0),
            (answer or "").strip().splitlines()[0][:120]
            if answer else "",
            "answered about a login-walled link without the text and "
            "without asking for it")]
    return []



# ----------------------------------------- class 4: repetition glitch
# NOT a new detector. This wraps the one production already runs
# (_has_repetition_glitch / _repetition_glitch_sentences in bot.py) so
# the harness scores the answer AFTER the same retry-then-strip ladder
# the user's answer goes through. Fixture 24 asserts a user never sees a
# repetition loop; scoring raw model output measured a stage where that
# rule does not live, exactly as 07b did before the plumbing class was
# wired. This was the eighth production/harness divergence.
#
# Imported lazily: bot.py imports THIS module at load time, so a
# module-level import here would be a cycle.
def _is_in_tail(answer: str, sentence: str, frac: float = 0.4) -> bool:
    """Is this sentence in the last `frac` of the answer?"""
    i = answer.find(sentence)
    return i >= 0 and i >= int(len(answer) * (1 - frac))


def _is_quotation(answer: str, sentence: str) -> bool:
    """Quoted material repeats on purpose. Lyrics are the clear case."""
    t = sentence.strip()
    if t.startswith(">") or t.startswith("> "):
        return True
    i = answer.find(sentence)
    if i < 0:
        return False
    line = _line_at(answer, i)
    return line.strip().startswith(">") or '"' in line


def check_repetition(answer: str, tool_calls=None, **_) -> list[Violation]:
    if not answer:
        return []
    try:
        from discord_bot.bot import (
            _has_repetition_glitch, _repetition_glitch_sentences)
    except Exception as e:
        return [Violation("repetition-glitch", "", (0, 0), "",
                          f"detector unavailable: {e}")]
    if not _has_repetition_glitch(answer):
        return []

    # SCOPE: end-of-generation only, which is the signature the detector
    # documents. A corpus sweep found 23 false positives from applying it
    # to the whole answer, all of them legitimate structure rather than a
    # loop:
    #   quoted lyrics   "Swerve, swerve, swerve, swerve, deeper now"
    #                   -- fixture 19 EXISTS to quote lyrics verbatim
    #   parallel data   "May revised down from +129K; June revised down
    #                    from +57K"
    #   chain listings  "total call open interest at 1.2M, put open
    #                    interest at 1.5M"
    # Mid-answer clause restatement is a DIFFERENT failure with a
    # different fix (validator class 6); it does not belong to this
    # detector, and widening this one to reach it is what produced the
    # false positives.
    sents = [t for t in (_repetition_glitch_sentences(answer) or [])
             if _is_in_tail(answer, t) and not _is_quotation(answer, t)]
    if not sents:
        return []
    if not sents:
        return [Violation("repetition-glitch", "<whole answer>", (0, 0),
                          answer.strip().splitlines()[0][:120],
                          "answer loops but no single sentence isolates it")]
    return [Violation("repetition-glitch", t[:60], (0, 0), t,
                      "end-of-generation repetition loop")
            for t in sents]



# ------------------------------------- class 3: unforced price levels
# An absolute price level for a ticker, index or crypto must come from a
# lookup_market_price call in the SAME turn. Not memory, not the chat
# block, not a Google snippet -- snippets return wrong-symbol index
# numbers, which is how an NDX question once volunteered "near its
# 52-week highs around $30,500", a level no index called NDX trades at.
#
# Keyed on the tool-call log because that is a fact about the turn.
# Whether a number is "from memory" is not checkable; whether the tool
# fired is.
_PRICE_TOOL = "lookup_market_price"

# A ticker-ish token: $CASHTAG, or a bare 2-5 letter symbol, or one of
# the index/crypto names the room actually uses.
_TICKERISH = re.compile(
    r"\$[A-Za-z]{1,5}\b"
    r"|\b(?:SPX|SPY|NDX|QQQ|DJI|RUT|VIX|ES|NQ|BTC|ETH|SOL|SUI|PEPE|"
    r"DXY|GLD|SLV|USO|TLT|IWM)\b"
    r"|\b(?:bitcoin|ethereum|solana|nasdaq|the dow|s&p|russell)\b",
    re.I,
)

# An ABSOLUTE level, not a change. "$244.10", "244.10", "5,900", "$4.5k".
# Percentages are deliberately excluded: "up 1.8%" is a change, and the
# rule is about levels. Small bare integers are excluded too -- strike
# counts, contract counts and "8 planets" are not price levels.
_PRICE_LEVEL = re.compile(
    r"\$\s?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|\$\s?\d+(?:\.\d+)?\s?[kK]?"
    r"|\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"
    r"|\b\d{3,}\.\d+\b"
)

# Shapes that are NOT a spot level even though they look numeric.
# Every entry here came from a FALSE POSITIVE found by scanning 548
# recorded answers: the first cut flagged "$3 courthouse parking
# receipts", a company's $490M revenue, an 840,000 employee-ish figure,
# and 1,200,000 of OPEN INTEREST -- which is class 4's job, not this
# one. A price validator that strips a revenue sentence is deleting
# correct content, and the suite pass rate hides it.
_NOT_A_LEVEL = re.compile(
    r"\b\d{1,5}\s*[cp]\b"                       # 450c / 190p strikes
    r"|\bstrike\b|\bexpir|\b\d{1,2}/\d{1,2}\b"
    r"|\b(?:FDIC|insured|covers?)\b"             # the $250k explainer
    r"|\bmarket cap\b|\bvaluation\b|\bAUM\b"
    r"|\brevenue\b|\bannual\b|\bgenerat|\bprofit|\bearnings\b"
    r"|\bemployees?\b|\bheadcount\b|\bstaff\b|\busers?\b"
    r"|\bopen interest\b|\bOI\b|\bvolume\b|\bcontracts?\b"
    r"|\bimplied vol|\bIV\b|\bput-?call\b"
    r"|\bpayrolls?\b|\bCPI\b|\bPCE\b|\bGDP\b|\bclaims\b"
    r"|\breceipts?\b|\bparking\b|\bstipend\b|\bjury\b"
    r"|\bIPO\b|\braised\b|\bfunding\b|\bfloat\b|\bunlock"
    r"|\bfollowers?\b|\bmessages?\b|\bmsgs?\b",
    re.I,
)

# A price CLAIM needs price language, not just a number near a ticker.
# Without this, any figure in a sentence that happens to mention a
# ticker was treated as a quote.
_PRICE_CUE = re.compile(
    r"\b(?:at|around|near|sits?|sitting|trading|trades?|traded|price[ds]?|"
    r"level|spot|close[ds]?|closing|open(?:ed|ing)?|high|low|print(?:ed|s)?|"
    r"quote[ds]?|last|bid|ask|hit|touch(?:ed|es)?|holding|above|below|"
    r"support|resistance|handle)\b",
    re.I,
)

# A number immediately followed by a unit is a QUANTITY, not a level:
# "accumulating 840,000 BTC" is a holdings count. Found by scanning
# recorded answers -- it was four of the five surviving false positives.
_QUANTITY_UNIT = re.compile(
    r"^[\s*_,)\]+~-]*(?:BTC|ETH|SOL|SUI|coins?|shares?|units?|tokens?|"
    r"contracts?|"
    r"bitcoin|ether)\b", re.I)

_PRICE_WINDOW = 70


def check_unforced_price(answer: str, tool_calls=None,
                         **_) -> list[Violation]:
    """Flag an absolute price level stated without the price tool firing.

    The prompt carried this rule in 719 chars, in bold, with the incident
    quoted. It is a tool-call assertion, so it is checkable, so per
    CLAUDE.md rule 1 it belongs here instead.
    """
    if _PRICE_TOOL in (tool_calls or []):
        return []
    text = answer or ""
    for m in _PRICE_LEVEL.finditer(text):
        lo = max(0, m.start() - _PRICE_WINDOW)
        hi = min(len(text), m.end() + _PRICE_WINDOW)
        window = text[lo:hi]
        if not _TICKERISH.search(window):
            continue
        sentence = _line_at(text, m.start())
        # Strikes, expirations, market caps, macro prints and the FDIC
        # explainer are all numbers near a ticker that are NOT spot.
        if _NOT_A_LEVEL.search(sentence):
            continue
        # A number beside a ticker is not automatically a quote.
        if not _PRICE_CUE.search(sentence):
            continue
        if _QUANTITY_UNIT.match(text[m.end():m.end() + 20]):
            continue
        return [Violation(
            "unforced-price", m.group(0).strip(), m.span(), sentence,
            f"absolute price level with no {_PRICE_TOOL} call this turn")]
    return []



# ------------------------------- class 4: unforced market-data stats
# Open interest, options volume, implied volatility and put-call ratios
# come from lookup_options_chain. Stated without it, they are pattern
# matched from a Google snippet or memory -- the 2026-06-06 shape, where
# "SPY June OI 248,553 / IV 10.3% / put-call 1.28" shipped with no live
# source behind any number.
_CHAIN_TOOL = "lookup_options_chain"

_CHAIN_STAT = re.compile(
    r"\b(?:open interest|OI)\b"
    r"|\bput[-/ ]?call(?:\s+ratio)?\b"
    r"|\bimplied vol(?:atility)?\b|\bIV\b"
    r"|\boptions?\s+volume\b|\bcall\s+volume\b|\bput\s+volume\b"
    r"|\bgamma exposure\b|\bGEX\b|\bdealer positioning\b",
    re.I,
)

# A NUMBER attached to that stat. Without one it is commentary, not an
# assertion: "IV is elevated" states nothing checkable, "IV at 12.1%"
# does.
_CHAIN_FIGURE = re.compile(
    r"\d+(?:\.\d+)?\s?%"
    r"|\b\d{1,3}(?:,\d{3})+\b"
    r"|\b\d+(?:\.\d+)?\s?[KkMm]\b"
    r"|\b\d+\.\d+\b"
)

_CHAIN_WINDOW = 90


def check_unforced_market_data(answer: str, tool_calls=None,
                               **_) -> list[Violation]:
    """Flag a chain statistic with a figure, absent the chain tool."""
    if _CHAIN_TOOL in (tool_calls or []):
        return []
    text = answer or ""
    for m in _CHAIN_STAT.finditer(text):
        lo = max(0, m.start() - _CHAIN_WINDOW)
        hi = min(len(text), m.end() + _CHAIN_WINDOW)
        if not _CHAIN_FIGURE.search(text[lo:hi]):
            continue
        sentence = _line_at(text, m.start())
        # Saying you DON'T have it is the prescribed answer, not a claim.
        if re.search(r"(?i)\b(?:don'?t|do not|no|not)\s+have\b"
                     r"|\bno (?:live )?(?:feed|data|source)\b"
                     r"|\bbroker\b", sentence):
            continue
        return [Violation(
            "unforced-market-data", m.group(0), m.span(), sentence,
            f"chain statistic with a figure and no {_CHAIN_TOOL} call")]
    return []


# --------------------------------- class 5: unforced time-series claims
# lookup_market_price and lookup_options_chain both return a SNAPSHOT --
# one moment. A trend, a delta, a multi-day change or a "highest since"
# cannot be derived from one number. The 2026-06-07 shape: a correct SPY
# OI snapshot dressed with a "~2% over 5 days" trend no source returned.
_SNAPSHOT_TOOLS = ("lookup_market_price", "lookup_options_chain")
_HISTORY_TOOL = "lookup_price_history"

_TREND_CLAIM = re.compile(
    # `\**` throughout: answers arrive with markdown bold, and
    # "over the past **5 days**" must match the same as the plain form.
    r"\b(?:over|in|during)\s+the\s+(?:last|past)\s+\**\s*\d+\s*\**\s*"
    r"(?:d|day|days|w|week|weeks|month|months|session|sessions)\b"
    r"|\b(?:week|month|day)[- ]over[- ](?:week|month|day)\b"
    r"|\bhighest\s+(?:since|in)\b|\blowest\s+(?:since|in)\b"
    r"|\bhas\s+been\s+(?:climbing|rising|falling|trending|building|"
    r"bleeding|grinding)\b"
    r"|\btrend(?:ing|ed)?\s+(?:up|down|higher|lower)\b"
    r"|\b\d+(?:\.\d+)?\s?%\s+(?:over|across)\s+\d+\b"
    r"|\b(?:up|down)\s+[-+]?\d+(?:\.\d+)?\s?%\s+(?:this|over the)\s+"
    r"(?:week|month)\b",
    re.I,
)


def check_unforced_time_series(answer: str, tool_calls=None,
                               **_) -> list[Violation]:
    """Flag a trend claim built on snapshot-only tools.

    Clean when lookup_price_history fired -- that tool DOES return a
    series, so a trend from it is sourced.
    """
    calls = tool_calls or []
    if _HISTORY_TOOL in calls:
        return []
    text = answer or ""
    m = _TREND_CLAIM.search(text)
    if not m:
        return []
    sentence = _line_at(text, m.start())
    # The prescribed refusal names the absence; it is not a claim.
    if re.search(r"(?i)\bno\s+(?:historical|multi-?day|history)\b"
                 r"|\bonly\s+(?:have\s+)?the\s+current\b"
                 r"|\bsnapshot\s+only\b|\bdon'?t\s+have\b"
                 r"|\bcan'?t\s+derive\b|\bbroker\b", sentence):
        return []
    return [Violation(
        "unforced-time-series", m.group(0), m.span(), sentence,
        "trend or change-over-time claim from snapshot-only tools "
        f"(calls: {calls or 'none'})")]


_CHECKS = {
    "meta-plumbing": check_meta_plumbing,
    "macro-unsourced": check_macro_unsourced,
    "unfetchable-link-claim": check_unfetchable_link_claim,
    "repetition-glitch": check_repetition,
    "unforced-price": check_unforced_price,
    "unforced-market-data": check_unforced_market_data,
    "unforced-time-series": check_unforced_time_series,
}


# The phrasing the deleted prompt block prescribed for exactly this
# situation. Used only when an answer is plumbing END TO END, so a
# sentence strip has nothing left to keep.
SAFE_REFUSAL = ("→ don't have that one — pull it from your broker or "
                "data vendor.")

# Class 3's replacement is different: nothing about the answer is
# salvageable, and the correct move is the request the code comment at
# bot.py:67 always intended.
PASTE_REQUEST = ("→ can't open x/twitter links — they serve a login wall. "
                 "paste the text and I'll take a look.")


def resolve_violations(answer: str, retry_answer: str | None,
                       tool_calls=None, strip_fn=None,
                       fallback: str | None = None,
                       **ctx) -> tuple[str, str]:
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
    vs = validate(answer, tool_calls, **ctx) if answer else []
    if not vs:
        return answer, "clean"
    if retry_answer and not validate(retry_answer, tool_calls, **ctx):
        return retry_answer, "regenerated"

    rules = {v.rule for v in vs}
    if fallback is None:
        if "unfetchable-link-claim" in rules:
            fallback = PASTE_REQUEST
        elif rules == {"repetition-glitch"}:
            # Production ships the ORIGINAL when the strip fails: a
            # glitchy answer still carries the content, so something
            # beats blank. Replacing it with a refusal would delete a
            # correct answer over a duplicated clause.
            fallback = None
        else:
            fallback = SAFE_REFUSAL

    # A blocked-URL answer is unfounded END TO END — it describes a page
    # nobody read. Excising sentences would leave the same fabrication
    # with fewer words, so this class skips the strip rung entirely.
    if "unfetchable-link-claim" in rules:
        if fallback and not validate(fallback, tool_calls, **ctx):
            return fallback, "replaced"
        return answer, "shipped"

    bad = violating_sentences(answer, tool_calls, **ctx)
    stripped = strip_fn(answer, bad)
    if stripped and not validate(stripped, tool_calls, **ctx):
        return stripped, "stripped"
    # BEYOND the repetition detector's ladder, deliberately. That one
    # ships the original because a glitchy answer still carries the
    # content and something beats blank. Here the violation IS the
    # content, and there is a correct non-blank answer — the refusal the
    # rule prescribes. Shipping known plumbing to satisfy "something
    # beats blank" would defeat the only assertion this class makes.
    if fallback and not validate(fallback, tool_calls, **ctx):
        return fallback, "replaced"
    return answer, "shipped"


def _default_strip(answer: str, to_remove: list[str]) -> str:
    """Fallback strip. The bot passes its own `_strip_sentences`."""
    out = answer
    for t in to_remove:
        out = out.replace(t, " ")
    return re.sub(r"\s{2,}", " ", out).strip()


def violating_sentences(answer: str, tool_calls=None, only=None,
                        **ctx) -> list[str]:
    """The exact sentence strings carrying a violation.

    Feeds the strip fallback, which removes whole sentences rather than
    editing inside them — the same shape `_repetition_glitch_sentences`
    uses, and for the same reason: excising a clause mid-sentence mangles
    prose, while a violating sentence is self-contained.
    """
    out, seen = [], set()
    for v in validate(answer, tool_calls, only, **ctx):
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


def validate(answer: str, tool_calls=None, only=None,
             **ctx) -> list[Violation]:
    """Run every enabled check. `only` restricts to named rules."""
    vs: list[Violation] = []
    for name, fn in _CHECKS.items():
        if only and name not in only:
            continue
        vs.extend(fn(answer, tool_calls, **ctx))
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


# class 2 cases. Each is (answer, tool_calls) — the tool log is half the
# signal, so a case without it would prove nothing.
_MACRO_BAD = [
    ("-> payrolls printed 172K against 160K expected.", []),
    ("-> core PCE came in at 2.6% year over year.", []),
    ("-> initial claims were 221,000 last week.", []),
    ("-> unemployment ticked up to 4.3% on the last print.", []),
    ("-> CPI ran 3.1% headline, hotter than the 2.9% consensus.",
     ["lookup_market_price"]),
]
_MACRO_GOOD = [
    # the same figure, with the tool that sources it
    ("-> payrolls printed 172K against 160K expected.",
     ["lookup_economic_calendar"]),
    # schedule talk is not a printed figure
    ("-> core PCE lands Wednesday at 8:30 ET.", []),
    ("-> CPI is the print that matters this week.", []),
    ("-> Warsh speaks Friday August 28 at 10:00 ET.", []),
    # numbers that belong to other nouns entirely
    ("-> $ORCL at $244.10, up 1.8% on the session.", []),
    ("-> he opened AVGO 450C at 4.5 with no exit posted.", []),
    ("-> open interest has been trending up 2% over the last 5 days.", []),
]


_PRICE_BAD = [
    ("-> $ORCL at $244.10, up 1.8% on the session.", []),
    ("-> NDX is holding near its 52-week highs around $30,500.", []),
    ("-> bitcoin is sitting at 94,300 right now.", []),
    ("-> SPX around 5,912 into the close.", ["search_chat_messages"]),
]
_PRICE_GOOD = [
    # the tool fired
    ("-> $ORCL at $244.10, up 1.8% on the session.",
     ["lookup_market_price"]),
    # changes are not levels
    ("-> $NVDA down 1.1% on the session.", []),
    # strikes are not spot
    ("-> he opened AVGO 450C at 4.5 with no exit posted.", []),
    # the FDIC explainer, a standing false-positive risk
    ("-> FDIC covers 250k per depositor, per insured bank.", []),
    # market cap is class 4 territory, not a price level
    ("-> $GEO market cap sits near $4.0B on the latest count.", []),
    # macro prints belong to class 2
    ("-> payrolls printed 172K against 160K expected.",
     ["lookup_economic_calendar"]),
    # no ticker anywhere near the number
    ("-> 200 messages in the window, mostly chop complaints.", []),
]


_CHAIN_BAD = [
    ("-> SPY June OI 248,553, IV 10.3%, put-call 1.28.", []),
    ("-> call open interest sits at 1.2M against 1.5M puts.", []),
    ("-> ATM implied volatility is 12.1% for that expiry.",
     ["lookup_market_price"]),
]
_CHAIN_GOOD = [
    ("-> SPY June OI 248,553, IV 10.3%, put-call 1.28.",
     ["lookup_options_chain"]),
    # the prescribed refusal
    ("-> I don't have a live feed for gamma exposure — pull it from "
     "your broker.", []),
    # commentary with no figure asserts nothing checkable
    ("-> IV is elevated into the print.", []),
    # other numbers, no chain stat
    ("-> $ORCL at $244.10, up 1.8%.", ["lookup_market_price"]),
]

_SERIES_BAD = [
    ("-> open interest has been trending up 2% over the last 5 days.",
     ["lookup_options_chain"]),
    ("-> $NVDA has been climbing all week.", ["lookup_market_price"]),
    ("-> volume is the highest since March.", ["lookup_options_chain"]),
]
_SERIES_GOOD = [
    # the history tool DOES return a series
    ("-> $NVDA has been climbing all week.", ["lookup_price_history"]),
    # the prescribed refusal
    ("-> I only have the current snapshot — no historical log to derive "
     "a 5-day trend.", ["lookup_options_chain"]),
    ("-> snapshot only: call OI 1.2M. no multi-day history available.",
     ["lookup_options_chain"]),
    # a same-session move is not a multi-day trend
    ("-> $ORCL at $244.10, up 1.8% on the session.",
     ["lookup_market_price"]),
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
    for label, bad, good, fn in (
            ("CLASS 4 (chain stat, no chain tool)",
             _CHAIN_BAD, _CHAIN_GOOD, check_unforced_market_data),
            ("CLASS 5 (trend from snapshot tools)",
             _SERIES_BAD, _SERIES_GOOD, check_unforced_time_series)):
        print(f"\n{label} — MUST FLAG:")
        for a, tc in bad:
            vs = fn(a, tc)
            ok = bool(vs)
            fails += 0 if ok else 1
            print(f"  {'caught ' if ok else 'MISSED '} {a[:62]!r}")
        print(f"{label} — MUST NOT FLAG:")
        for a, tc in good:
            vs = fn(a, tc)
            ok = not vs
            fails += 0 if ok else 1
            print(f"  {'clean  ' if ok else 'FALSE+ '} {a[:62]!r}")
            if vs:
                print(f"            -> {vs[0]}")

    print("\nCLASS 3 — MUST FLAG (price level, no price tool):")
    for a, tc in _PRICE_BAD:
        vs = check_unforced_price(a, tc)
        ok = bool(vs)
        fails += 0 if ok else 1
        print(f"  {'caught ' if ok else 'MISSED '} {a[:64]!r}")
    print("\nCLASS 3 — MUST NOT FLAG:")
    for a, tc in _PRICE_GOOD:
        vs = check_unforced_price(a, tc)
        ok = not vs
        fails += 0 if ok else 1
        print(f"  {'clean  ' if ok else 'FALSE+ '} {a[:64]!r}")
        if vs:
            print(f"            -> {vs[0]}")

    print("\nCLASS 2 — MUST FLAG (macro figure, no calendar tool):")
    for a, tc in _MACRO_BAD:
        vs = check_macro_unsourced(a, tc)
        ok = bool(vs)
        fails += 0 if ok else 1
        print(f"  {'caught ' if ok else 'MISSED '} {a[:64]!r}")
    print("\nCLASS 2 — MUST NOT FLAG:")
    for a, tc in _MACRO_GOOD:
        vs = check_macro_unsourced(a, tc)
        ok = not vs
        fails += 0 if ok else 1
        print(f"  {'clean  ' if ok else 'FALSE+ '} {a[:64]!r}")
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
