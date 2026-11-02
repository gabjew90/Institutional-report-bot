"""Fantasy playoff odds for a stated league format, computed in code.

2026-10-27 audit F1-F3: BK, under Yahoo's table of playoff odds by record,
asked how the odds change "in a 12-man league and top 8 teams make playoffs".
The model made the figures up (75-80% for 4-3, 45-50% for 3-4), the
win-percent guard cut the answer mid-thought, and the footer cited the Sleeper
league, which the question was not about. This is arithmetic, so it is
answered before the model with no Gemini call.

Every remaining game is a coin flip and the other teams' records are treated
as independent, so the figures describe the format, not a real schedule.
"""
from __future__ import annotations

import re
from math import comb

DEFAULT_GAMES = 14
DEFAULT_PLAYED = 7
_LEAGUE_RE = re.compile(r"\b(\d{1,2})[- ]?(?:man|team|person)\s+(?:league|fantasy|format)\b", re.I)
_SPOTS_RES = (
    re.compile(r"\btop[- ]?(\d{1,2})\b", re.I),
    re.compile(r"\b(\d{1,2})[- ]?teams?\s+(?:make|get|go|qualify|are in)\b", re.I),
    re.compile(r"\b(\d{1,2})[- ]team\s+(?:playoffs?|postseason)\b", re.I),
)
_ASKS_ODDS_RE = re.compile(r"\b(?:odds|chances?|probabilit\w*|percent)\b|%", re.I)
_PLAYOFF_RE = re.compile(r"\b(?:playoffs?|postseason)\b", re.I)
# the season's length, never "after 7 games"
_GAMES_RE = re.compile(r"\b(\d{1,2})[- ]?(?:week|game)s?\s+(?:regular\s+)?(?:season|schedule)\b", re.I)
_BYE_RE = re.compile(r"\bbyes?\b", re.I)
_RECORD_RE = re.compile(r"\b(\d{1,2})-(\d{1,2})\b")
# "7\-0: 98\.4%": a table of records in the post the asker replied to
_TABLE_RE = re.compile(r"\b(\d{1,2})\\?-(\d{1,2})\s*:")


def parse(asker_text: str, question: str = "") -> dict | None:
    """The league format the asker named, or None when this is not a
    question about one (a member's own league stays with the model)."""
    text = asker_text or ""
    if not _PLAYOFF_RE.search(text) or not _ASKS_ODDS_RE.search(text) or _BYE_RE.search(text):
        return None
    league = _LEAGUE_RE.search(text)
    spots = next((m for m in (r.search(text) for r in _SPOTS_RES) if m), None)
    if not league or not spots:
        return None
    teams, playoff = int(league.group(1)), int(spots.group(1))
    if not 2 <= playoff < teams <= 32:
        return None
    games_m = _GAMES_RE.search(text)
    games = int(games_m.group(1)) if games_m else DEFAULT_GAMES
    if not 1 <= games <= 30:
        return None
    records = [(int(w), int(l)) for w, l in _RECORD_RE.findall(text) if int(w) + int(l) <= games]
    if not records:
        table = [(int(w), int(l)) for w, l in _TABLE_RE.findall(question or "")]
        played = table[0][0] + table[0][1] if table else DEFAULT_PLAYED
        if played > games:
            return None
        records = [(w, played - w) for w in range(played, -1, -1)]
    return {"teams": teams, "spots": playoff, "games": games, "records": records}


def _wins_pmf(games: int) -> list[float]:
    return [comb(games, k) / 2 ** games for k in range(games + 1)]


def odds(teams: int, spots: int, games: int, wins: int, losses: int) -> float:
    """The chance a team at wins-losses finishes in the top `spots`, ties
    broken at random. Sums over its final win total, then over how many of
    the other teams finish above it and level with it."""
    left = _wins_pmf(games - wins - losses)
    field = _wins_pmf(games)
    others = teams - 1
    total = 0.0
    for extra, p_final in enumerate(left):
        final = wins + extra
        above = sum(field[final + 1:])
        level = field[final]
        below = max(0.0, 1.0 - above - level)
        for a in range(min(others, spots - 1) + 1):
            for t in range(others - a + 1):
                ways = comb(others, a) * comb(others - a, t)
                p = ways * above ** a * level ** t * below ** (others - a - t)
                total += p_final * p * min(1.0, (spots - a) / (t + 1))
    return total


def _pct(p: float) -> str:
    if p >= 0.995:
        return ">99%"
    if p < 0.005:
        return "<1%"
    return f"{p * 100:.0f}%"


def answer(spec: dict) -> str:
    """The posted answer: the format, one line per record, the assumption."""
    lines = [f"→ {spec['teams']}-team league, top {spec['spots']} make the playoffs, "
             f"{spec['games']}-game regular season:"]
    for w, l in spec["records"]:
        p = odds(spec["teams"], spec["spots"], spec["games"], w, l)
        lines.append(f"→ **{w}-{l}:** {_pct(p)}")
    lines.append("→ Computed in code with every remaining game a coin flip and ties broken "
                 "at random. Real schedules and team strength will move these.")
    return "\n\n".join(lines)
