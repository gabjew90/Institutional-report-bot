"""Caller corrections to a book the bot just published.

THE INCIDENT (2026-08-26)
=========================
Abe asked the bot for Kyle's open positions. The book listed seven,
including MU 980C 08-28 and AVGO 450C 09-04. Kyle replied "No AVGO" and
"No MU anymore". He was right both times: he had exited, never posted an
exit in his alerts channel, and analyst_trades therefore held an `open`
row with no `close`. The open-position state machine is a fold over the
event chain, so with no close event the position stays live until expiry
-- nine more days for the AVGO contract.

The extractor was not broken. It resolves bare exits like "Sold @ 5.65"
through the Discord reply parent and had recorded fifteen closes for
Kyle. It simply never saw these: both entry messages had zero replies,
and the only statement he was out came in a different channel entirely.
The analyst watcher listens on each caller's own alerts channel, so
anything said elsewhere is invisible to it.

WHY THIS TRIGGER AND NOT A BROADER ONE
======================================
"No AVGO" is not a trade event. It is a pronoun. It means nothing
without the list it refers to, and general chat parsing that tried to
read it as an exit would be guessing. What makes it unambiguous here is
the whole situation: the person who owns the book, in the channel where
it was just published, naming a ticker that is on it, with an explicit
exit cue.

So the antecedent is the trigger. A correction is only read against a
book this bot actually published (bot_book_posts records each one), and
every one of these must hold:

  1. the author is a REGISTERED CALLER, and it is THEIR book
  2. same channel as the book
  3. a direct reply to it, OR inside a short window after it
  4. the message names a ticker that was ON that book
  5. an explicit EXIT CUE governs that ticker

Requirement 3 is why this is not reply-only. Kyle's two corrections had
no reply reference at all -- they were standalone lines twenty and
twenty-four seconds after the book. A reply-only trigger would have
missed the exact case that motivated the feature.

Requirement 5 is the one doing the real work. Two messages in that same
minute name tickers that were on the book and are NOT exits: "^ this
plus the SMCI 39c and I added 1 more SPX" (a confirmation) and "I was so
close to tailing AAOI dang it" (a regret). Both are inert here because
neither carries an exit cue.

WHAT IT WRITES
==============
A `close` row with no price and no gain_pct. That is not a gap, it is
the honest record: the caller said he was out and did not say at what.
The W/L tally already models this as `closed_unscored` -- a posted exit
with no number, counted as closed but not scored -- so these land in an
existing bucket rather than inventing one.

A KNOWN LIMIT, RECORDED DELIBERATELY
====================================
Twenty-seven minutes after "No MU anymore", Kyle wrote "Bruh my MU 980c
ITM" -- that same contract, as his. His own account of his book was
self-contradictory within the half hour. This code believes him at the
time he speaks, which means it would have closed MU 980C and been wrong
by 21:48. That is accepted: being wrong in the direction the caller
himself asserted is a better failure than being wrong in a direction
nobody claimed. A later `open` or `add` on the same contract supersedes
the close through the normal state machine, since it folds by latest
action -- so the re-entry repairs itself if he posts one.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# How long after a book a bare correction is still attributable. Kyle's
# landed at 20 and 24 seconds. Minutes, not hours: the further from the
# book, the less "No AVGO" is anchored to it, and an unanchored exit
# claim is the guessing this design exists to avoid.
WINDOW_MINUTES = 10

# Exit cues, each anchored to the ticker it governs. `T` is substituted
# with the specific ticker at match time -- these NEVER run against a
# generic ticker pattern, only against symbols already known to be on
# the book, which is what keeps stray capitalised words out.
#
# Every pattern requires the cue and the ticker in a fixed relationship.
# A message merely CONTAINING a ticker and, separately, the word "sold"
# does not match: "sold my SPX, holding AVGO" must not close AVGO.
_EXIT_PATTERNS = (
    # "no AVGO", "no more MU", "no MU anymore", "no longer in MU"
    r"\bno\s+(?:more\s+|longer\s+(?:in\s+|holding\s+)?)?{T}\b",
    # "out of AVGO", "I'm out of MU"
    r"\bout\s+of\s+{T}\b",
    # "closed AVGO", "sold AVGO", "dumped MU", "exited MU"
    #
    # VERB BEFORE TICKER, always. The mirrored form -- ticker then
    # predicate, "{T} is closed/gone/dead/flat" -- was in this list
    # until the corpus sweep, where it produced most of the false
    # positives by itself: "Spx closed +0.02%", "MU CLOSED 700", "Spx
    # closed 7413", "MU dead", "Spy flat", "Ooooo even btc dead". Every
    # one is commentary on the PRICE, and in that word order a position
    # statement and a price statement are the same string. Verb-first
    # order carries the distinction, so only it survives. "AVGO is
    # closed" no longer registers: losing a rare shape is far cheaper
    # than closing a live position every time someone notes where SPX
    # settled.
    r"\b(?:closed|sold|dumped|exited|cut|axed)\s+(?:out\s+of\s+)?"
    r"(?:my\s+|the\s+|all\s+(?:my\s+)?)?{T}\b",
    # "AVGO anymore" only when a negation governs it earlier in the line
    r"\bno\b[^.!?\n]*\b{T}\b[^.!?\n]*\banymore\b",
)

# Cues that look like exits but are not, checked BEFORE the exit
# patterns and vetoing the whole message. These are the shapes that
# would otherwise close a position the caller is describing keeping,
# adding to, or wishing they had.
_VETO_PATTERNS = (
    # "still in AVGO", "still holding MU", "still have AVGO"
    r"\bstill\s+(?:in|holding|hold|have|got|long|short)\b",
    # "not selling", "didn't sell", "won't close", "never sold"
    r"\b(?:not|never|didn'?t|don'?t|won'?t|wouldn'?t|ain'?t)\s+"
    r"(?:gonna\s+|going\s+to\s+|about\s+to\s+)?"
    r"(?:sell|selling|sold|close|closing|closed|cut|cutting|exit|exiting)\b",
    # "added 1 more SPX", "bought more MU", "I added"
    r"\b(?:added|adding|bought|buying|grabbed|starting|opened)\b",
    # "should have sold", "so close to tailing", "wish I sold"
    r"\b(?:should'?ve|should\s+have|wish\s+i|almost|nearly|so\s+close\s+to)\b",
    # a question is a question, not a report: "sold AVGO?"
    r"\?\s*$",
    # SOMEONE ELSE'S trade. "He sold MRVL" is not Kyle closing MRVL.
    # Found by the corpus sweep.
    r"\b(?:he|she|they|someone|somebody|everyone|nobody|u|you|your|"
    r"his|her|their)\s+(?:just\s+|already\s+)?"
    r"(?:sold|closed|cut|dumped|exited|axed)\b",
    # DELIBERATION, not a completed exit. Both of these are real
    # messages: "I was thinking to cut my NVDA puts actually" and
    # "Hmmm to cut TSLA 0dtes or not". A trade under consideration is
    # still open, and reading it as an exit closes a position the
    # caller is in the middle of deciding to keep.
    r"\b(?:thinking|thinkin|considering|debating|tempted|"
    r"should\s+i|shall\s+i|do\s+i|might|maybe|probably|"
    r"about\s+to|gonna|going\s+to|planning|plan\s+to|"
    r"or\s+not|if\s+i|when\s+i|need\s+to|have\s+to|want\s+to)\b",
)

_VETO_RE = tuple(re.compile(p, re.I) for p in _VETO_PATTERNS)


def _ticker_re(pattern: str, ticker: str) -> re.Pattern:
    return re.compile(pattern.format(T=re.escape(ticker.upper())), re.I)


def parse_exit_corrections(text: str, listed_tickers: list[str]) -> list[str]:
    """Tickers the message says the caller is OUT of.

    Only symbols in `listed_tickers` (the book as published) are ever
    considered, so this cannot invent a position. Returns [] for
    anything that is not an unambiguous exit.
    """
    msg = (text or "").strip()
    if not msg or not listed_tickers:
        return []

    for veto in _VETO_RE:
        if veto.search(msg):
            log.debug(f"book-reply: veto {veto.pattern!r} on {msg[:60]!r}")
            return []

    hits: list[str] = []
    for raw in listed_tickers:
        t = (raw or "").strip().upper()
        if not t:
            continue
        for pat in _EXIT_PATTERNS:
            if _ticker_re(pat, t).search(msg):
                hits.append(t)
                break
    return hits


def _resolve_caller(settings, author_username: str) -> tuple[str, str] | None:
    """(canonical_name, display) for a registered, ENABLED caller."""
    uname = (author_username or "").strip().lower()
    if not uname:
        return None
    try:
        for c in settings.resolve_analyst_callers():
            if (c.get("username") or "").strip().lower() != uname:
                continue
            if c.get("enabled") is False:
                return None
            return ((c.get("name") or "").strip().lower(),
                    c.get("display") or uname)
    except Exception as e:
        log.warning(f"book-reply: caller registry lookup failed: {e}")
    return None


async def handle_book_correction(bot, message) -> int:
    """Record closes for positions the caller says they are out of.

    Returns the number of close rows written. Zero is the overwhelmingly
    common outcome and is not an error -- almost every message reaching
    this function is ordinary chat.

    Never raises: a correction failing to record must not take down
    message handling. The caller logs.
    """
    import db
    from config import settings

    text = (message.content or "").strip()
    if not text:
        return 0

    who = _resolve_caller(settings, getattr(message.author, "name", ""))
    if not who:
        return 0                      # not a registered caller
    canonical, display = who

    channel_id = getattr(getattr(message, "channel", None), "id", None)
    if channel_id is None:
        return 0

    # Candidate books: the direct reply parent if there is one, else
    # every book published in this channel inside the window. Both paths
    # then require the book to be THIS caller's.
    books: list[dict] = []
    ref = getattr(message, "reference", None)
    ref_id = getattr(ref, "message_id", None) if ref else None
    if ref_id:
        b = db.get_bot_book_post(int(ref_id))
        if b:
            books = [b]
    if not books:
        books = db.find_recent_book_posts(
            channel_id=int(channel_id), within_minutes=WINDOW_MINUTES)

    books = [b for b in books
             if (b.get("caller") or "").lower() == canonical]
    if not books:
        return 0

    listed: list[str] = []
    for b in books:
        for t in b.get("tickers") or []:
            if t not in listed:
                listed.append(t)

    exits = parse_exit_corrections(text, listed)
    if not exits:
        return 0

    # Only close what is actually open right now. The book may be
    # seconds stale, and a ticker the caller mentions that is no longer
    # open needs no correction.
    try:
        open_positions = db.get_current_analyst_positions(
            caller=canonical, tracking_mode="caller")
    except Exception as e:
        log.warning(f"book-reply: open-position read failed: {e}")
        return 0

    posted_at = _posted_at_iso(message)
    written = 0
    for i, pos in enumerate(open_positions):
        tk = (pos.get("ticker") or "").strip().upper()
        if tk not in exits:
            continue
        try:
            db.record_analyst_trade(
                discord_message_id=int(message.id),
                # Synthetic attachment id: caption-only rows use 0, so
                # these take a distinct namespace to avoid colliding
                # with a real caption row on the same message. One
                # message can correct several positions, hence the
                # index.
                discord_attachment_id=900_000 + i,
                author=getattr(message.author, "name", "") or display,
                author_id=getattr(message.author, "id", None),
                posted_at=posted_at,
                image_url=None,
                caption=text[:500],
                is_trade=True,
                gemini_json={
                    "source": "book_correction",
                    "correction_text": text[:500],
                    "book_message_ids": [b["message_id"] for b in books],
                    "note": (
                        "Caller stated an exit against a book this bot "
                        "published. No exit price was given, so gain_pct "
                        "is NULL and the W/L tally counts this as "
                        "closed_unscored."
                    ),
                },
                ticker=tk,
                contract_type=pos.get("contract_type"),
                strike=pos.get("strike"),
                expiry=pos.get("expiry"),
                action="close",
                gain_pct=None,       # no price stated — never guess one
                price=None,
                caller=canonical,
                tracking_mode="caller",
            )
            written += 1
            log.info(
                f"book-reply: recorded CLOSE for {display} {tk} "
                f"{pos.get('strike')} {pos.get('expiry')} from "
                f"{text[:60]!r} (msg={message.id})")
        except Exception as e:
            log.error(f"book-reply: close insert failed for {tk}: {e}",
                      exc_info=True)
    return written


def _posted_at_iso(message) -> str:
    from datetime import datetime, timezone
    ts = getattr(message, "created_at", None)
    if ts is None:
        return datetime.now(timezone.utc).isoformat()
    try:
        return ts.isoformat()
    except Exception:
        return datetime.now(timezone.utc).isoformat()
