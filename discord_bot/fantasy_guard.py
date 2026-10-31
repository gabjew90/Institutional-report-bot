"""Win percentages in fantasy answers come from the league data (2026-10-05).

BK asked "what % chance of winning did Jamal have before this Panthers?".
BK had posted "I went into this game w a 23% chance" and "From 23% chance
to 56% chance LFG" about his own matchup, and the bot answered "23% going
into the matchup" for Jamal. It did so again after the router started
sending the question to the league data: the model reaches for the nearest
percentage in the chat. Sleeper's own Win% is not in any Sleeper API, so
the only win chances the bot has are its own estimates in the payload
(report/sleeper_data.win_chance). A fantasy answer whose win percentage is
not one of those loses the sentence that carries it (`strip_stray`);
the rewrite that ran first until 2026-10-30 is gone (audit design note 2).
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


def _bot_parent(question: str) -> str:
    """The text of the bot's own message the asker replied to, else ''."""
    m = re.search(r"\[MESSAGE BEING REPLIED TO — from omniwiz[^\]]*\]\s*\n(.*?)(?=\n\[[^\]\n]*message to you\]|\Z)",
                  question or "", re.S)
    return m.group(1) if m else ""


def stray_percents(answer: str, estimates: dict[str, int], question: str = "") -> list[str]:
    """Win percentages in the answer that are not one of the estimates.
    A percentage counts when its sentence is about winning, or when the
    question asked about win chances (2026-10-05: "**23%**, as BK noted in
    chat" answered "what % chance of winning" with no win word of its
    own). Target share, roster rate and snap share are percentages too."""
    allowed = {float(v) for v in estimates.values()}
    # The bot's own figure in the answer being replied to was an estimate
    # when it was given: BK's "He's cooked" under 2Pale's 11% stripped the
    # whole reply (2026-10-26 audit F2).
    allowed |= {float(m.group(1)) for m in _PCT_RE.finditer(_bot_parent(question))}
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
    """Drop the sentences that carry
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
    # a footer alone ("Data: Sleeper league") is not an answer (2026-10-26 F2)
    body = "\n".join(ln for ln in out.split("\n")
                     if not re.match(r"\s*(?:Data|Sources?):", ln)).strip(" →*\n")
    if not body:
        return NO_ESTIMATE_LINE
    return out


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


# The asker's side and the opponent's, from the situation payload
# (2026-10-11 audit: "you hold a 52% win probability with Josh Allen,
# Chuba Hubbard, Kyle Pitts, and the Denver Defense still to play" was
# said to 2Pale; all four were Abe's, and so most likely was the 52%).
_YOU_RE = re.compile(r"\b(?:you|your|you're|youre|yours|ya)\b", re.I)


def _player_names(rows) -> list[str]:
    out = []
    for r in rows or []:
        label = r.get("player") if isinstance(r, dict) else r
        if label:
            out.append(str(label).split(" (")[0].strip())
    return out


def sides(payload) -> dict:
    """{you_manager, opp_manager, you_players, opp_players, you_pct,
    opp_pct} from a situation payload, or {} for any other shape."""
    if not isinstance(payload, dict):
        return {}
    m = payload.get("matchup")
    if not (isinstance(m, dict) and isinstance(m.get("you"), dict)
            and isinstance(m.get("opponent"), dict)):
        return {}
    you, opp = m["you"], m["opponent"]
    roster = (payload.get("roster") or {}).get("players") or []
    return {"you_manager": you.get("manager") or "", "opp_manager": opp.get("manager") or "",
            "you_players": _player_names(roster),
            "opp_players": _player_names(opp.get("players")),
            "you_pct": you.get("win_chance_pct"), "opp_pct": opp.get("win_chance_pct")}


def _manager_words(label: str) -> list[str]:
    """'Abe (abullish_xyz) · Sleeper ayatollahabdullah' -> its names."""
    words = re.findall(r"[A-Za-z0-9_.]{3,}", label or "")
    return [w for w in words if w.lower() != "sleeper"]


def _mentions(sentence: str, name: str, shared_last: frozenset = frozenset()) -> bool:
    """A player named by full name, last name (unless the other side has a
    player with that last name too), or a defense by its city."""
    low = sentence.lower()
    parts = name.split()
    forms = {name.lower()}
    if len(parts) >= 2 and len(parts[-1]) >= 4 and parts[-1].lower() not in shared_last:
        forms.add(parts[-1].lower())
    if len(parts) >= 2 and parts[-1].lower() in _NFL_NICKNAMES:
        forms = {name.lower(), f"{parts[0].lower()} defense", f"{parts[0].lower()} d/st",
                 f"{parts[0].lower()} dst", f"{parts[-1].lower()} defense"}
    return any(re.search(r"(?<![a-z])" + re.escape(f) + r"(?![a-z])", low) for f in forms)


_NFL_NICKNAMES = frozenset({
    "cardinals", "falcons", "ravens", "bills", "panthers", "bears", "bengals", "browns",
    "cowboys", "broncos", "lions", "packers", "texans", "colts", "jaguars", "chiefs",
    "raiders", "chargers", "rams", "dolphins", "vikings", "patriots", "saints", "giants",
    "jets", "eagles", "steelers", "49ers", "seahawks", "buccaneers", "titans", "commanders"})


def wrong_side(answer: str, s: dict) -> list[str]:
    """Sentences addressed to the asker that give them the opponent's
    players or the opponent's win chance, without naming the opponent."""
    if not answer or not s:
        return []
    you_set = set(s["you_players"])
    opp_only = [p for p in s["opp_players"] if p not in you_set]
    opp_words = _manager_words(s["opp_manager"])
    you_last = frozenset(p.split()[-1].lower() for p in s["you_players"] if p.split())
    bad = []
    for sent in _SENTENCE_SPLIT_RE.split(answer):
        if not sent.strip() or not _YOU_RE.search(sent):
            continue
        if any(re.search(r"(?<![A-Za-z0-9_])" + re.escape(w) + r"(?![A-Za-z0-9_])", sent, re.I)
               for w in opp_words):
            continue        # the sentence names the opponent: his players are his
        if any(_mentions(sent, p, you_last) for p in opp_only):
            bad.append(sent.strip())
            continue
        pcts = {round(float(v)) for v in _PCT_RE.findall(sent)}
        if (s.get("opp_pct") is not None and s.get("you_pct") != s.get("opp_pct")
                and s["opp_pct"] in pcts and s.get("you_pct") not in pcts):
            bad.append(sent.strip())
    return bad
