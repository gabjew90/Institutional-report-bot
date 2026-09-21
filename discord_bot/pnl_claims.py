"""Claims about a member's trading, checked against the documented ledger.

Two incidents, one shape. 2026-09-18: the bot told the room that
bankerkyle's "entire existence is posting unhinged property arbitrage
fantasies between blowing up accounts on weekly options" on a day his
caller log carried QQQ closes at +234% and +208% and a 58-trade average
of +136%. 2026-09-19: "holding MSTR puts into a freight train, sunny",
where the MSTR puts were BK's and Monsoon's and sunny has never had an
MSTR row. Both times `analyst_trades` had the answer.

The /ask prompt bound the first of these from 2026-08-20 ("Touch P&L
angles only when the ledger hands you a specific fresh receipt") and
the model skipped it, so the rule is code and that prompt sentence is
gone (CLAUDE.md enforcement policy 1).

SHAPE (2026-09-19 review). Detection is pure and does no I/O:
`claim_candidates` returns who-claims-what, the caller fetches those
members' ledgers once off the event loop, and `judge_candidates`
decides. The first draft called into SQLite from inside the detector,
once per member, on the Discord event loop.

SCOPE. A claim is only attributed to a member the sentence or the
answer actually names, or to the turn's explicit subject. The first
draft fell back to the whole chat window, so a sentence naming nobody
("everyone chasing the squeeze in NVDA calls got hosed") flagged every
member mentioned anywhere in the last 4000 characters of room chat.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# The bags-exits-bleeding-account family. Deliberately literal: these
# assert a LOSING RECORD, not a risky style. "0DTE degen" and
# "full-ports into lottery tickets" describe how someone trades and
# stay legal, because the room's own register is not the target.
#
# Bare idioms are NOT in here (2026-09-19 review): "down bad" is how
# the room says someone is infatuated, "blowing up" is what a phone
# does and what a stock does when it rips. Each needs its P&L object.
_LOSS_CLAIM_RE = re.compile(
    r"(?:blow|blew|blown|blowing|blows)\s+(?:up\s+)?"
    r"(?:his\s+|her\s+|their\s+|the\s+|another\s+|\w+'s\s+)?"
    r"(?:account|portfolio|book|port)s?"
    r"|torch(?:ed|ing)\s+(?:his|her|their|the|another)?\s*account"
    r"|(?:account|portfolio|pnl|p&l|log|book)\s+(?:is\s+|looks\s+|reads\s+)?"
    r"(?:like\s+)?(?:a\s+)?(?:crime scene|disaster|graveyard|bloodbath|wreck)"
    r"|bleeding\s+(?:out|money|premium|an?\s+account)"
    r"|(?:holding|stuck\s+with|sitting\s+on|averaging\s+down\s+on)\s+"
    r"(?:his\s+|her\s+|their\s+)?bags"
    r"|bag(?:holding|holder)"
    r"|down\s+bad\s+(?:on|in)\b"
    r"|(?:never|can't|cannot|doesn't|does\s+not)\s+"
    r"(?:wins?|prints?|clos(?:e|es|ing)\s+green)"
    r"|(?:losing|loser|underwater|red)\s+(?:streak|every|on\s+everything)"
    r"|vaporiz(?:ed|ing)\s+(?:his|her|their)\s+(?:account|book)",
    re.IGNORECASE,
)

# "holding MSTR puts", "long NVDA calls", "sitting on TSLA shares". The
# ticker is the checkable part. `in` is NOT a verb here: "the squeeze in
# NVDA calls" is a market observation, not an attribution.
_POSITION_RE = re.compile(
    r"(?:holding|hold|held|sitting\s+on|stuck\s+(?:in|with)|riding|"
    r"bought|buying|owns?|long|short|averaging\s+down\s+on)\s+"
    r"(?:his\s+|her\s+|their\s+|those\s+|the\s+|some\s+|a\s+bunch\s+of\s+)?"
    r"\$?([A-Z]{1,6})\b"
    r"(?=\s+(?:put|call|share|stock|position|bag|weekl|lotto|contract|\d))",
)
_NOT_A_TICKER = {
    "A", "I", "AI", "THE", "AND", "FOR", "NOT", "ALL", "ATM", "OTM", "ITM",
    "CEO", "CFO", "IPO", "ETF", "FOMC", "FED", "CPI", "PCE", "GDP", "US",
    "USA", "UK", "EU", "OK", "DD", "TA", "IV", "OI", "DTE", "PM", "AM", "ET",
    "LOL", "IMO", "YOLO", "EOD", "YTD", "QQ", "OP", "IT",
}

# Enough of a record to call a losing claim false. One lucky screenshot
# is not a defence; a stream of documented closes is.
MIN_WINS_TO_CONTRADICT = 3
# A sentence that names half the room is not making a claim about any
# one of them.
MAX_SUBJECTS_PER_SENTENCE = 2

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def loss_claim_sentences(answer: str) -> list[str]:
    """Sentences that assert someone is losing money."""
    return [s.strip() for s in _SENT_SPLIT.split(answer or "")
            if s.strip() and _LOSS_CLAIM_RE.search(s)]


def _named_members(text: str, members: dict[str, int]) -> list[tuple[str, int]]:
    """(surface, user_id) for every known member named in `text`."""
    hits: list[tuple[str, int]] = []
    low = (text or "").lower()
    for surface, uid in (members or {}).items():
        s = (surface or "").strip().lower()
        # two characters, because "BK" is a real handle; the word
        # boundaries below are what keep it from matching prose
        if len(s) < 2 or uid is None:
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", low):
            pair = ((surface or "").strip(), uid)
            if pair not in hits:
                hits.append(pair)
    return hits


def _subjects(sentence: str, answer: str, members: dict[str, int],
              subject_surfaces) -> list[tuple[str, int]]:
    """Who this sentence is about, narrowest source first.

    The sentence, then the answer, then the turn's declared subject
    (the person being replied to or roasted). Never the raw chat
    window: a sentence naming nobody must not pick up every member the
    room happened to mention (2026-09-19 review).
    """
    who = _named_members(sentence, members) or _named_members(answer, members)
    if not who and subject_surfaces:
        who = [(s, members[s]) for s in subject_surfaces if s in members]
    return who[:MAX_SUBJECTS_PER_SENTENCE]


def claim_candidates(answer: str, members: dict[str, int],
                     subject_surfaces=()) -> list[dict]:
    """Every checkable claim in the answer, with no I/O.

    Each entry: {kind: 'loss'|'position', surface, user_id, sentence,
    ticker (position only)}. The caller fetches ledgers for
    `{c['user_id'] for c in candidates}` and passes them to
    `judge_candidates`.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for sent in _SENT_SPLIT.split(answer or ""):
        sent = sent.strip()
        if not sent:
            continue
        is_loss = bool(_LOSS_CLAIM_RE.search(sent))
        tickers = sorted({t for t in _POSITION_RE.findall(sent)
                          if t not in _NOT_A_TICKER})
        if not is_loss and not tickers:
            continue
        for surface, uid in _subjects(sent, answer, members, subject_surfaces):
            if is_loss and ("loss", surface, uid) not in seen:
                seen.add(("loss", surface, uid))
                out.append({"kind": "loss", "surface": surface,
                            "user_id": uid, "sentence": sent[:300]})
            for tick in tickers:
                key = ("position", surface, uid, tick)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"kind": "position", "surface": surface,
                            "user_id": uid, "ticker": tick,
                            "sentence": sent[:300]})
    return out


def judge_candidates(candidates: list[dict],
                     stats: dict[int, dict]) -> tuple[list[dict], list[dict]]:
    """(loss claims the ledger contradicts, positions it does not show).

    `stats[user_id]` is {"wins": int, "losses": int, "tickers": set[str]}.
    A member missing from `stats`, or with no logged trades at all, is
    skipped: absence of a ledger is not evidence a claim is wrong, and
    plenty of the room never posts a screenshot.
    """
    bad_loss: list[dict] = []
    bad_pos: list[dict] = []
    for c in candidates or []:
        st = (stats or {}).get(c["user_id"])
        if not st:
            continue
        if c["kind"] == "loss":
            wins, losses = int(st.get("wins") or 0), int(st.get("losses") or 0)
            if wins >= MIN_WINS_TO_CONTRADICT and wins > losses:
                bad_loss.append(dict(c, wins=wins, losses=losses))
        else:
            owned = {str(t).upper() for t in (st.get("tickers") or set())}
            if owned and c["ticker"] not in owned:
                bad_pos.append(dict(c))
    return bad_loss, bad_pos


def correction_note(bad_loss: list[dict], bad_pos: list[dict],
                    protected: bool = False) -> str:
    """The directive appended to a regeneration.

    States the receipts and points at the material the prompt already
    prefers. Never names a replacement target: the model's instinct on
    being told a position is not X's is to move it to Y's.

    `protected` switches to a subtractive directive for a protected
    asker — drop the claim, put nothing in its place.
    """
    if not bad_loss and not bad_pos:
        return ""
    lines = ["[LEDGER CHECK] Your draft states something about a member's "
             "trading that the documented log contradicts. The log wins."]
    for c in bad_loss:
        lines.append(
            f"- **{c['surface']}** is not losing: {c['wins']} documented "
            f"win(s) against {c['losses']} loss(es) in the last 21 days."
        )
    for b in bad_pos:
        lines.append(
            f"- **{b['surface']}** has no logged {b['ticker']} trade. "
            f"That position is not theirs."
        )
    if protected:
        # A protected asker is protected from having personal material
        # turned on them, which is what the ordinary note points the
        # model at. Leaving the claim standing is not the alternative:
        # it publishes something untrue about the person the rule exists
        # to protect (2026-09-19 review). So: drop it, replace it with
        # nothing.
        lines.append(
            "Rewrite so no sentence makes these claims. Do not move a "
            "position onto a different name and do not invent a "
            "replacement trade. Do not substitute another jab or any "
            "personal material about them — drop the claim and say "
            "nothing in its place. Keep the rest of the answer as it is."
        )
        return "\n".join(lines)
    lines.append(
        "Rewrite so no sentence claims these people are losing money, "
        "blowing up accounts, holding bags, or holding a position they do "
        "not have. Do not move a position onto a different name to fix it, "
        "and do not invent a replacement trade. Their own takes, habits and "
        "messages are the stronger material anyway. Keep the register and "
        "the length; change the claim."
    )
    return "\n".join(lines)
