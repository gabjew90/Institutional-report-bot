"""The fantasy league's table as posted lines (2026-11-19, audit run 12
design 3).

`report/league_table.py` computes the table from Sleeper's weekly scores.
This module renders the view a question asks for, and the lines are posted
as written (discord_bot/answer_block.py): the model writes only the
opening. Before it, every league-table question was assembled by the model
from raw payloads, and in one evening it invented the week-6 standings,
answered "top 6 at the end of week 6" with that week's game winners,
answered "toughest schedule over the next 5 weeks" with points against
over the past nine, and told a manager he had the fewest points when he
had the second fewest.

Views, from the asker's words:

- standings (the default): seed, record, points for with its rank, points
  against. "top 6" keeps six rows. A past week ("through week 6") is the
  table as it stood then, with no odds and no schedule.
- schedule: each team's remaining regular-season opponents and what those
  opponents average a week, hardest first. "next 5 weeks" narrows it.
- playoffs: each team's chance of a playoff spot and its expected final
  wins. The odds are the bot's own estimate and the header says so.

A question with none of these words gets no lines: the payload is still
shown to the model, and nothing is posted for it.
"""
from __future__ import annotations

import re

_SCHEDULE_RE = re.compile(
    r"\b(?:schedules?|strength\s+of\s+schedule|sos|remaining\s+(?:games|opponents|matchups)"
    r"|(?:hardest|toughest|easiest|softest|roughest)\s+(?:road|path|slate|run|stretch|remaining))\b", re.I)
# "odds" and "chances" alone are not here: "what are the odds I win this
# week" is about one game.
_PLAYOFF_RE = re.compile(
    r"\b(?:playoffs?|post\s?season|clinch\w*|eliminat\w*|in\s+the\s+hunt|bubble"
    r"|win\s+(?:the\s+)?(?:league|championship|it\s+all|chip|ship))\b", re.I)
# "top 5 rbs" is about players: a count needs a word that says teams.
_STANDINGS_RE = re.compile(
    r"\b(?:standings?|records?|seeds?|seeding|first\s+place|last\s+place|best\s+team|worst\s+team"
    r"|(?:top|bottom)\s+\d{1,2}\s+(?:teams?|seeds?|spots?|managers?|at|after|through|thru|as\s+of"
    r"|in\s+(?:the\s+)?(?:league|standings))"
    r"|who(?:'s|s|\s+is)\s+(?:winning|leading|in\s+first|in\s+last)\s+(?:the\s+)?league"
    r"|how(?:'s|s|\s+is)\s+(?:the|our)\s+league(?:\s+looking)?)\b", re.I)
# Season totals, unless the question is about one week's scoring.
_POINTS_RE = re.compile(r"\b(?:points?\s+(?:for|against)|(?:most|fewest|least)\s+points)\b", re.I)
_ONE_WEEK_RE = re.compile(r"\b(?:this|last)\s+week\b|\btonight\b|\btoday\b|\byesterday\b", re.I)
# The NFL's own standings, schedule and playoffs are not this league's.
_NOT_LEAGUE_RE = re.compile(r"\bnfl\b|\bsuper\s*bowl\b|\bafc\b|\bnfc\b|\bwild\s*card\b", re.I)
_TOP_RE = re.compile(r"\btop\s+(\d{1,2})\b", re.I)
_NEXT_RE = re.compile(r"\bnext\s+(\d{1,2}|two|three|four|five|six)\s+(?:weeks?|games?|matchups?)\b", re.I)
_WORD_N = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def view(question: str) -> str | None:
    """'schedule' | 'playoffs' | 'standings' | None for the asker's words."""
    q = question or ""
    if _NOT_LEAGUE_RE.search(q):
        return None
    if _SCHEDULE_RE.search(q):
        return "schedule"
    if _PLAYOFF_RE.search(q):
        return "playoffs"
    if _STANDINGS_RE.search(q) or (_POINTS_RE.search(q) and not _ONE_WEEK_RE.search(q)):
        return "standings"
    return None


def _record(t: dict) -> str:
    return f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t.get("ties") else "")


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _standings(table: dict, question: str) -> list[str]:
    teams = table["teams"]
    top = _TOP_RE.search(question or "")
    keep = min(len(teams), int(top.group(1))) if top else len(teams)
    spots = int(table.get("playoff_spots") or 0)
    head = (f"→ Standings through week {table['through_week']} of {table['regular_season_weeks']}"
            + (f". Top {spots} make the playoffs." if spots else "."))
    lines = [head]
    for t in teams[:keep]:
        lines.append(f"→ {t['seed']}. **{t['manager']}** {_record(t)}, **{t['points_for']:,.1f}** points for "
                     f"({_ordinal(t['points_for_rank'])}), {t['points_against']:,.1f} against")
    return lines


def _schedule(table: dict, question: str) -> list[str]:
    if not table.get("is_current"):
        return []
    m = _NEXT_RE.search(question or "")
    n = None
    if m:
        n = _WORD_N.get(m.group(1).lower()) or int(m.group(1))
    ppg = {t["manager"]: float(t.get("points_per_game") or 0.0) for t in table["teams"]}
    rows = []
    for t in table["teams"]:
        games = (t.get("remaining") or [])[:n] if n else (t.get("remaining") or [])
        if not games:
            continue
        avg = sum(ppg.get(g["opponent"], 0.0) for g in games) / len(games)
        rows.append((avg, t, games))
    if not rows:
        return []
    rows.sort(key=lambda r: -r[0])
    weeks = sorted({g["week"] for _, _, games in rows for g in games})
    span = f"week {weeks[0]}" if len(weeks) == 1 else f"weeks {weeks[0]} to {weeks[-1]}"
    lines = [f"→ Remaining schedule, {span}, hardest first. The figure is what the opponents have "
             f"averaged a week through week {table['through_week']}."]
    for i, (avg, t, games) in enumerate(rows, 1):
        opp = ", ".join(g["opponent"] for g in games)
        lines.append(f"→ {i}. **{t['manager']}** ({_record(t)}): opponents average **{avg:,.1f}** ({opp})")
    return lines


def _playoffs(table: dict, question: str) -> list[str]:
    teams = [t for t in table["teams"] if t.get("playoff_odds") is not None]
    if not table.get("is_current") or not teams:
        return []
    left = int(table.get("weeks_left") or 0)
    spots = int(table.get("playoff_spots") or 0)
    if not left:
        lines = [f"→ The regular season is over. Top {spots} by record, then points for:"]
        for t in table["teams"][:spots]:
            lines.append(f"→ {t['seed']}. **{t['manager']}** {_record(t)}, {t['points_for']:,.1f} points for")
        return lines
    lines = [f"→ Playoff odds through week {table['through_week']}, {left} "
             f"{'week' if left == 1 else 'weeks'} left, top {spots} make it. This is my own estimate: "
             "10,000 simulated finishes, each team scoring around its average so far."]
    for t in sorted(teams, key=lambda t: (-t["playoff_odds"], t["seed"])):
        odds = t["playoff_odds"] * 100
        shown = "over 99%" if 99 < odds < 100 else "under 1%" if 0 < odds < 1 else f"{odds:.0f}%"
        lines.append(f"→ **{t['manager']}** ({_record(t)}, seed {t['seed']}): **{shown}**, "
                     f"about {t['expected_wins']:.1f} wins expected")
    return lines


def block_lines(result: dict, question: str = "") -> list[str]:
    """The posted lines for a `table` payload, [] when the question asks
    for none of the views or the table has no week played."""
    table = (result or {}).get("table") or {}
    if table.get("status") != "ok" or not table.get("teams"):
        return []
    which = view(question)
    if not which:
        return []
    asked = table.get("asked_week")
    note = []
    if asked and int(asked) > int(table.get("last_played_week") or 0):
        note = [f"→ Week {asked} is not final. The last week scored is week {table['last_played_week']}."]
    if which == "schedule":
        lines = _schedule(table, question)
    elif which == "playoffs":
        lines = _playoffs(table, question)
    else:
        lines = _standings(table, question)
    if not lines and which != "standings":
        # a past week has no schedule and no odds: its standings answer it
        lines = _standings(table, question)
    return [*note, *lines] if lines else []
