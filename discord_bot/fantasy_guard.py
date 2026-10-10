"""Win percentages in fantasy answers come from the league data (2026-10-05).

BK asked "what % chance of winning did Jamal have before this Panthers?".
BK had posted "I went into this game w a 23% chance" and "From 23% chance
to 56% chance LFG" about his own matchup, and the bot answered "23% going
into the matchup" for Jamal. It did so again after the router started
sending the question to the league data: the model reaches for the nearest
percentage in the chat. Sleeper's own Win% is not in any Sleeper API, so
the only win chances the bot has are its own estimates in the payload
(report/sleeper_data.win_chance). A fantasy answer whose win percentage is
not one of those is rewritten once to use them, and the rewrite is kept
only if no stray percentage is left.
"""
from __future__ import annotations

import re

_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")


def win_estimates(payload) -> dict[str, int]:
    """manager label -> estimate %, from a lookup_fantasy_league result
    (matchups topic: every game; situation topic: the asker's game)."""
    out: dict[str, int] = {}
    if not isinstance(payload, dict):
        return out
    for game in payload.get("matchups") or []:
        if isinstance(game, dict):
            out.update(game.get("win_chance_estimate") or {})
    m = payload.get("matchup")
    if isinstance(m, dict):
        out.update(m.get("win_chance_estimate") or {})
    return out


def _asker_words(question: str) -> str:
    """The asker's own message, without quoted blocks (the router's view)."""
    if not question:
        return ""
    from discord_bot.ask_router import _last_line
    return _last_line(question)


_WIN_WORDS_RE = re.compile(r"\b(?:win|wins|winning|won|chances?|odds|probabilit|likely|kalshi)",
                           re.I)
# a sentence ends at . ! ? followed by whitespace, so "41.0" stays whole
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def stray_percents(answer: str, estimates: dict[str, int], question: str = "") -> list[str]:
    """Win percentages in the answer that are not one of the estimates.
    A percentage counts when its sentence is about winning, or when the
    question asked about win chances (2026-10-05: "**23%**, as BK noted in
    chat" answered "what % chance of winning" with no win word of its
    own). Target share, roster rate and snap share are percentages too."""
    allowed = {float(v) for v in estimates.values()}
    # the router's win-chance pattern, not the broad word list: "who won
    # the trade?" does not make a target share a win chance
    from discord_bot.ask_router import _WIN_CHANCE_RE
    asked = bool(_WIN_CHANCE_RE.search(_asker_words(question)))
    out = []
    for sentence in _SENTENCE_SPLIT_RE.split(answer or ""):
        if not asked and not _WIN_WORDS_RE.search(sentence):
            continue
        out += [m.group(0) for m in _PCT_RE.finditer(sentence)
                if float(m.group(1)) not in allowed]
    return out


NO_ESTIMATE_LINE = ("→ I don't have a win chance for that one: Sleeper doesn't publish its "
                    "Win%, and the odds people post in here are about their own games.")


def strip_stray(answer: str, estimates: dict[str, int], question: str = "") -> str:
    """Last resort when the rewrite failed: drop the sentences that carry
    a stray win percentage. If nothing of substance is left, say plainly
    that the bot has no win chance for it."""
    kept = []
    for line in (answer or "").split("\n"):
        parts = _SENTENCE_SPLIT_RE.split(line)
        good = [s for s in parts if not stray_percents(s, estimates, question)]
        if len(good) == len(parts):
            kept.append(line)
        elif any(s.strip(" →*") for s in good):
            kept.append(" ".join(good).rstrip())
    out = "\n".join(kept).strip()
    if not out.strip(" →*\n") or out.strip() in ("→", ""):
        return NO_ESTIMATE_LINE
    return out


REWRITE_PROMPT = (
    "Rewrite the fantasy answer below. It gives a win percentage that is not in the league "
    "data. Percentages members post in chat are about their OWN matchup and never another "
    "member's. The only win chances the bot has are its own estimates from current points and "
    "the projections still to come:\n{estimates}\n"
    "Use the estimate for the manager the question is about and say it is the bot's estimate "
    "(Sleeper's in-app Win% is not available to the bot). If the question asks about a moment "
    "the data does not cover, such as before a game, say the bot only has the estimate as of "
    "now. {empty}Keep every other figure and name as written and keep the arrow format. Output "
    "only the rewritten answer.\n\nQUESTION:\n{question}\n\nANSWER:\n{answer}"
)


def rewrite_prompt(answer: str, question: str, estimates: dict[str, int]) -> str:
    lines = "\n".join(f"- {k}: {v}%" for k, v in sorted(estimates.items())) or "- none"
    empty = ("No estimate is available right now, so give no win percentage at all. "
             if not estimates else "")
    return REWRITE_PROMPT.format(estimates=lines, empty=empty, question=question,
                                 answer=answer)


# Sleeper credited with a win percentage it never published (2026-10-10
# audit: "Sleeper has you holding a 62% win probability"; the figure was
# the bot's own estimate, and the tool note already says never to call it
# Sleeper's). Rewritten in code: the claim names the wrong source.
_SLEEPER_CLAIM_RE = re.compile(
    r"\b(?:Sleeper(?:'s|’s)?(?:\s+app)?|the\s+app)\s+"
    r"(?:has|gives|puts|shows|projects|says|lists|is\s+giving)\s+"
    r"(?P<who>you|him|them|[A-Z][\w']*)\s+(?:holding\s+|sitting\s+)?(?:at\s+)?(?:an?\s+)?"
    r"(?=\**\d{1,3}(?:\.\d+)?\s?%)")
_SLEEPER_POSSESSIVE_RE = re.compile(
    r"\bSleeper(?:'s|’s)\s+(?=(?:win\s+)?(?:probability|odds|chances?|win\s*%))", re.I)


def fix_attribution(answer: str) -> tuple[str, bool]:
    """(answer, changed): a win percentage credited to Sleeper is credited
    to the bot's estimate instead."""
    if not answer or "leeper" not in answer and "the app" not in answer:
        return answer, False
    new = _SLEEPER_CLAIM_RE.sub(lambda m: f"my estimate puts {m.group('who')} at ", answer)
    new = _SLEEPER_POSSESSIVE_RE.sub("my estimated ", new)
    # capital at a sentence or bullet start ("→ My estimate puts you ...")
    new = re.sub(r"(^|\n|→\s*|[.!?]\s+)(\**)my estimat",
                 lambda m: m.group(1) + m.group(2) + "My estimat", new)
    return new, new != answer
