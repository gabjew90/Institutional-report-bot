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

A fourth view has its own payload (`strength`, report/roster_strength.py,
2026-11-26): how good each roster is. "Who has the best team if everyone is
healthy" was answered with the three highest season point totals, because
no payload measured a roster. `strength_lines` posts the answer first (who,
by how much), then the teams with what makes each one strong or thin.
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
    r"\b(?:standings?|records?|seeds?|seeding|first\s+place|last\s+place"
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


# ---- roster strength (report/roster_strength.py) ---------------------------

TOP_ROWS = 3
_ALL_RE = re.compile(
    r"\b(?:power\s+rank\w*|rank\s+(?:the|all|every|each|them|us|our)\b|every\s+team|all\s+(?:\d{1,2}|twelve|the)\s+teams?"
    r"|full\s+(?:list|rank\w*)|whole\s+league)", re.I)
_WORST_RE = re.compile(r"\b(?:worst|weakest|thinnest|shallowest)\b", re.I)
_BEST_RE = re.compile(r"\b(?:best|strongest|deepest)\b", re.I)
_SUFFIXES = {"jr.", "jr", "sr.", "sr", "ii", "iii", "iv", "v"}
_PARTICLES = {"st.", "st", "de", "van", "von"}
_GROUP_NAME = {"QB": "quarterback", "RB": "running back", "WR": "receiver", "TE": "tight end"}


def _short(name: str) -> str:
    """'Kenneth Walker III' -> 'Walker', 'Amon-Ra St. Brown' -> 'St. Brown'."""
    parts = (name or "").split()
    while len(parts) > 1 and parts[-1].lower() in _SUFFIXES:
        parts.pop()
    if len(parts) >= 3 and parts[-2].lower() in _PARTICLES:
        return " ".join(parts[-2:])
    return parts[-1] if parts else (name or "")


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


# "my team", "our roster": the asker's own team is one of the two compared.
# "I" alone is not: "who has the best team, I think it's DeepFried".
_MINE_RE = re.compile(r"\b(?:my|mine|our|ours)\b", re.I)


def _named(team: dict, question: str) -> bool:
    """The question names this team: the room's name as a whole word, or a
    word of four letters or more that is the manager's Discord or Sleeper
    username or the end of it ("kyle" is bankerkyle, "jamal" is f.jamal).
    The start of a username counts only when it is not an English word:
    "dizzy" is dizzydean6, "them" is not _themelvin."""
    q = (question or "").lower().replace("’", "'")
    m = (team.get("manager") or "").strip().lower()
    if m and re.search(rf"(?<![a-z0-9]){re.escape(m)}(?:'?s)?(?![a-z0-9])", q):
        return True
    words = {re.sub(r"[^a-z]", "", w[:-2] if w.endswith("'s") else w) for w in re.findall(r"[a-z0-9_.']{4,}", q)}
    words = {w for w in words if len(w) >= 4}
    if not words:
        return False
    from discord_bot.listed_symbols import english_words
    for alias in team.get("aliases") or []:
        a = re.sub(r"[^a-z]", "", (alias or "").lower())
        for w in words:
            if a and (a == w or a.endswith(w) or (a.startswith(w) and w not in english_words())):
                return True
    return False


def _group(t: dict, pos: str) -> str:
    g = (t.get("groups") or {}).get(pos) or {}
    who = ", ".join(f"{_short(p['player'])} {p['avg']:.1f}" for p in g.get("players") or [])
    return f"{_GROUP_NAME.get(pos, pos)} ({who})"


def _strength_row(t: dict) -> str:
    line = f"→ {t['rank']}. **{t['manager']}** ({t['record']}, seed {t['seed']}): **{t['full_health']:.1f}**"
    out = [p["player"] for p in t.get("out_now") or []]
    if out and t["available"] < t["full_health"]:
        line += f", {t['available']:.1f} without {_and(out)}"
    line = line.rstrip(".") + "."           # a name can end in "Jr."
    groups = t.get("groups") or {}
    best, thin = t.get("best_group"), t.get("thinnest_group")
    if not best or not thin or best == thin:
        return line
    # "best" and "thinnest" are against the league's average for the group
    up = (groups[best].get("vs_league_average") or 0) > 0
    down = (groups[thin].get("vs_league_average") or 0) < 0
    if up and down:
        return line + f" Best at {_group(t, best)}, thinnest at {_group(t, thin)}."
    if up:
        return line + f" Above the league's average at every position, most at {_group(t, best)}."
    return line + f" Below the league's average at every position, most at {_group(t, thin)}."


def strength_lines(result: dict, question: str = "") -> list[str]:
    """The posted lines for a `strength` payload: the answer, the teams
    asked about, then what the figures are. [] when it has no table.

    Two teams picked out by the question ("is my team better than Kyle's",
    "compare BK and Declan's teams") are compared with each other. Any
    other question is about the league: the best roster, or the weakest
    when the words say so, with the teams next to it."""
    s = (result or {}).get("strength") or {}
    teams = s.get("teams") or []
    if s.get("status") != "ok" or len(teams) < 2:
        return []
    q = question or ""
    asker = s.get("asker") or ""
    picked = [t for t in teams if _named(t, q) or (t["manager"] == asker and _MINE_RE.search(q))]
    foot = (f"→ Each figure is a team's best lineup from its current roster, every player at his average in "
            f"the games he played through week {s['through_week']}, in league scoring. Out means on IR or "
            "missed his team's last game.")
    if len(picked) == 2:
        a, b = picked                       # stronger first: teams are in rank order
        return [f"→ **{a['manager']}** has the better roster at full health: its best lineup averages "
                f"**{a['full_health']:.1f}** points a week against **{b['manager']}**'s **{b['full_health']:.1f}**.",
                _strength_row(a), _strength_row(b), foot]
    worst = bool(_WORST_RE.search(q)) and not _BEST_RE.search(q)
    top = _TOP_RE.search(q)
    keep = len(teams) if _ALL_RE.search(q) else min(len(teams), int(top.group(1)) if top else TOP_ROWS)
    shown = teams[-keep:][::-1] if worst else teams[:keep]
    shown += [t for t in teams if t not in shown and (t in picked or t["manager"] == asker)]
    if worst:
        last, above = teams[-1], teams[-2]
        lines = [f"→ **{last['manager']}** has the weakest roster at full health: its best lineup averages "
                 f"**{last['full_health']:.1f}** points a week, {above['full_health'] - last['full_health']:.1f} "
                 f"fewer than **{above['manager']}**'s."]
    else:
        lead, second = teams[0], teams[1]
        lines = [f"→ **{lead['manager']}** has the best roster at full health: its best lineup averages "
                 f"**{lead['full_health']:.1f}** points a week, {lead['full_health'] - second['full_health']:.1f} "
                 f"more than **{second['manager']}**'s."]
    lines += [_strength_row(t) for t in shown]
    if not worst:
        now = min(teams, key=lambda t: t.get("available_rank") or 99)
        if now is not teams[0]:
            lines.append(f"→ With the players who are out left off, the best lineup is **{now['manager']}**'s, "
                         f"**{now['available']:.1f}**.")
        one = next((t for t in teams if t.get("seed") == 1), None)
        if one and one["rank"] > 1 and one not in shown:
            lines.append(f"→ The 1 seed, **{one['manager']}** ({one['record']}), ranks {_ordinal(one['rank'])} "
                         f"at full health with **{one['full_health']:.1f}**.")
    return [*lines, foot]


# ---- a trade --------------------------------------------------------------------
# The `trade` topic (report/roster_strength.py :: trade, 2026-11-27): what the
# named players do to each side's best lineup. "should I trade bowers for both
# Derrick Henry and LaPorta" was answered "Take it immediately" from one
# week's projections, in a week one of the asker's backs was on a bye.

def _pts(x: float) -> str:
    return f"{abs(x):.1f}"


def _who(rows: list[dict]) -> str:
    return _and([f"{_short(r['player'])} ({r['avg']:.1f})" for r in rows])


def _value(p: dict, first: bool) -> str:
    """'Bowers **18.7** (TE1 of 23 rostered, 8 games)'."""
    if p.get("avg") is None:
        return f"{_short(p['player'])} no average (under 2 full games played, counted as zero)"
    rank = (f"{p['position']}{p['position_rank']} of {p['position_count']}{' rostered' if first else ''}, "
            if p.get("position_rank") else "")
    return f"{_short(p['player'])} **{p['avg']:.1f}** ({rank}{p['games']} games)"


def _side_row(sd: dict) -> str:
    deal = f"→ **{sd['manager']}** sends {_and([_short(n) for n in sd['gives']])} and gets " \
           f"{_and([_short(n) for n in sd['gets']])}: "
    if sd["after"] == sd["before"] and not sd["comes_in"] and not sd["comes_out"]:
        return deal + f"best lineup stays **{sd['before']:.1f}** points a week, with nobody new starting."
    line = deal + (f"best lineup **{sd['before']:.1f}** to **{sd['after']:.1f}** points a week "
                   f"({sd['change']:+.1f}).")
    if sd["comes_in"]:
        line += f" Into the lineup: {_who(sd['comes_in'])}."
    if sd["comes_out"]:
        line += f" Out of it: {_who(sd['comes_out'])}."
    return line


def _effect(sd: dict, first: bool) -> str:
    c = sd["change"]
    if c > 0:
        return f"helps **{sd['manager']}** by **{_pts(c)}** points a week" if first else \
            f"helps **{sd['manager']}** by {_pts(c)}"
    if c < 0:
        return f"costs **{sd['manager']}** **{_pts(c)}** points a week" if first else \
            f"costs **{sd['manager']}** {_pts(c)}"
    return f"leaves **{sd['manager']}**'s best lineup unchanged"


def trade_lines(result: dict, question: str = "") -> list[str]:
    """The posted lines for a `trade` payload: what the trade does to each
    side's best lineup, the lineup changes, each player's season average,
    then what the figures are. [] when nothing was measured.

    Who sends whom is read from the rosters: a named player moves from the
    team that has him."""
    t = (result or {}).get("trade") or {}
    players = t.get("players") or []
    if t.get("status") != "ok" or not players:
        return []
    sides = t.get("sides") or []
    foot = (f"→ Each figure is a team's best lineup from its roster, every player at his average in the games he "
            f"played through week {t['through_week']}, in league scoring. Injuries, byes and the schedule ahead "
            "are not in it.")
    order = [n for sd in sides for n in sd.get("gives") or []]
    players = sorted(players, key=lambda p: order.index(p["player"]) if p["player"] in order else len(order))
    values = "→ Season averages: " + ", ".join(_value(p, i == 0) for i, p in enumerate(players)) + "."
    if t.get("kind") == "swap" and len(sides) == 2:
        a, b = sides
        lines = [f"→ On season averages the trade {_effect(a, True)} and {_effect(b, False)}.",
                 _side_row(a), _side_row(b), values]
        for sd in sides:
            n = sd["extra_players"]
            if n == 1:
                lines.append(f"→ **{sd['manager']}** gets one more player than it sends, so a roster spot has "
                             "to be cleared.")
            elif n > 1:
                lines.append(f"→ **{sd['manager']}** gets {n} more players than it sends, so {n} roster spots "
                             "have to be cleared.")
        return [*lines, foot]
    if t.get("kind") == "value" and sides:
        own = sides[0]
        who = _and([_short(n) for n in own["gives"]])
        one = players[0]
        if len(players) == 1 and one.get("avg") is not None:
            rank = (f", {one['position']}{one['position_rank']} of the {one['position_count']} rostered"
                    if one.get("position_rank") else "")
            lines = [f"→ **{one['player']}** averages **{one['avg']:.1f}** points a game over {one['games']} "
                     f"games{rank}."]
        else:
            lines = [values]
        if own["change"] < 0:
            lines.append(f"→ Without {who}, **{own['manager']}**'s best lineup goes from **{own['before']:.1f}** to "
                         f"**{own['after']:.1f}** points a week ({own['change']:+.1f})"
                         + (f": {_who(own['comes_in'])} would start." if own["comes_in"]
                            else ": nobody on the roster would take the spot."))
            lines.append(f"→ A return is even for **{own['manager']}** when it puts about **{_pts(own['change'])}** "
                         "points a week back into that lineup.")
        else:
            lines.append(f"→ {who} {'is' if len(own['gives']) == 1 else 'are'} not in **{own['manager']}**'s best "
                         f"lineup, which stays **{own['before']:.1f}** points a week either way.")
        if len(sides) > 1:
            buy = sides[1]
            if buy["change"] > 0:
                lines.append(f"→ With {who}, **{buy['manager']}**'s best lineup goes from **{buy['before']:.1f}** to "
                             f"**{buy['after']:.1f}** ({buy['change']:+.1f})"
                             + (f", in place of {_who(buy['comes_out'])}." if buy["comes_out"] else "."))
            else:
                lines.append(f"→ {who} would not start for **{buy['manager']}**, whose best lineup stays "
                             f"**{buy['before']:.1f}**.")
        return [*lines, foot]
    return [values, foot]
