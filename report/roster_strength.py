"""How good each roster is, computed in code (2026-11-26).

"Assuming every person on each team's roster is fully healthy, who
currently has the best team?" was answered on 2026-11-25 with the three
highest season point totals. No payload carried a measure of a roster, so
the model recited the numbers it had, and the room's reply was "this bot
can't think for itself, it just spews numbers". Season points are what a
team has scored with the lineups it happened to start. They say nothing
about a player who was traded for last week, one who has sat on IR since
week 4, or one left on the bench.

The measure here is each team's best legal lineup from the players on its
roster today, with every player valued at his average in the games he
played this season, in the league's own scoring:

- Sleeper's weekly stats times the league's scoring settings reproduce the
  league's own `players_points` exactly (checked 2026-11-26 over weeks 1 to
  11: 2,073 player-weeks, no mismatch).
- A game counts when the player played it (`gp`). For a quarterback, back,
  receiver or tight end a game of fewer than MIN_SNAPS offensive snaps does
  not: it is an injury exit or a cameo, and counting it puts a healthy
  Lamar Jackson at 16.0 a game instead of 20.0.
- A player with fewer than MIN_GAMES counted games has no average and is
  left out of every lineup (`uncounted` in the payload).

Two lineups per team: `full_health` uses everyone, and `available` leaves
out the players who are out now. A player is out when he sits in the
roster's reserve (IR) slot or did not play in his NFL team's last game.
That is a fact from the feed. Sleeper's injury tag is not in the player
cache, and its weekly projection is no test: it carried 16.3 points for a
receiver listed Out.

Pure functions over Sleeper's JSON, plus one cached fetcher. No model, no
database.
"""
from __future__ import annotations

import logging
import re
import time

log = logging.getLogger(__name__)

MIN_GAMES = 2
MIN_SNAPS = 10
_SKILL = ("QB", "RB", "WR", "TE")
# a flexible slot and the positions it takes, narrowest first
_FLEX = {"REC_FLEX": ("WR", "TE"), "WRRB_FLEX": ("RB", "WR"), "FLEX": ("RB", "WR", "TE"),
         "SUPER_FLEX": ("QB", "RB", "WR", "TE")}
_NOT_A_SLOT = ("BN", "IR", "TAXI")
_TTL_S = 24 * 3600
_cache: dict = {}

_LABEL_RE = re.compile(r"^(?P<name>.*?)\s*\((?P<pos>[A-Z/]+)(?:,\s*(?P<team>[A-Z]{2,3}))?\)\s*$")


def parse_label(label: str) -> tuple[str, str, str]:
    """'Bryce Young (QB, CAR)' -> ('Bryce Young', 'QB', 'CAR'). A label
    with no position gives ('<label>', '', '')."""
    m = _LABEL_RE.match(label or "")
    if not m:
        return (label or "").strip(), "", ""
    return m.group("name"), m.group("pos"), m.group("team") or ""


def week_points(stats: dict, scoring: dict) -> dict[str, tuple[float, float | None]]:
    """player id -> (points in the league's scoring, offensive snaps or
    None) for everyone who played that week."""
    weights = []
    for k, v in (scoring or {}).items():
        try:
            if float(v):
                weights.append((k, float(v)))
        except (TypeError, ValueError):
            continue
    out: dict[str, tuple[float, float | None]] = {}
    for pid, st in (stats or {}).items():
        if not isinstance(st, dict) or not st.get("gp"):
            continue
        pts = 0.0
        for k, w in weights:
            try:
                pts += float(st.get(k) or 0) * w
            except (TypeError, ValueError):
                continue
        snaps = st.get("off_snp")
        try:
            snaps = float(snaps) if snaps is not None else None
        except (TypeError, ValueError):
            snaps = None
        out[str(pid)] = (round(pts, 2), snaps)
    return out


def fetch_season(season: str, last_week: int, scoring: dict, fetch=None) -> dict[int, dict]:
    """week -> week_points for weeks 1..last_week, four requests at a time.
    Only the reduced table is kept (a week of raw stats is 600 KB of JSON),
    for a day: Sleeper corrects stats after the games. A week that comes
    back empty is not cached and is missing from the result."""
    from concurrent.futures import ThreadPoolExecutor
    if fetch is None:
        from report.sleeper_data import fetch_weekly_stats as fetch
    sig = hash(tuple(sorted((str(k), str(v)) for k, v in (scoring or {}).items())))

    def one(week: int) -> dict:
        key = (fetch, str(season), int(week), sig)
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < _TTL_S:
            return hit[1]
        pts = week_points(fetch(season, week) or {}, scoring)
        if pts:
            _cache[key] = (time.time(), pts)
        return pts

    weeks = list(range(1, int(last_week) + 1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(one, weeks))
    return {w: g for w, g in zip(weeks, got) if g}


def season_table(weeks: dict[int, dict], positions: dict[str, str]) -> dict[str, dict]:
    """player id -> {games, points, avg, last_week} over the counted games."""
    out: dict[str, dict] = {}
    for w in sorted(weeks):
        for pid, (pts, snaps) in weeks[w].items():
            if positions.get(pid) in _SKILL and snaps is not None and snaps < MIN_SNAPS:
                continue
            row = out.setdefault(pid, {"games": 0, "points": 0.0, "last_week": 0})
            row["games"] += 1
            row["points"] += pts
            row["last_week"] = w
    for row in out.values():
        row["points"] = round(row["points"], 2)
        row["avg"] = round(row["points"] / row["games"], 1)
    return out


def best_lineup(cands: list[tuple[str, str, float]], slots: list[str]) -> list[tuple[str, str]]:
    """[(slot, player id)] for the highest-scoring legal lineup. `cands` is
    (player id, position, average). Fixed slots take the best at their
    position, then each flexible slot the best player left that it allows."""
    left = sorted(cands, key=lambda c: (-c[2], c[0]))
    used: set[str] = set()
    line: list[tuple[str, str]] = []
    real = [s for s in (slots or []) if s not in _NOT_A_SLOT]
    order = [s for s in real if s not in _FLEX] + sorted(
        (s for s in real if s in _FLEX), key=lambda s: list(_FLEX).index(s))
    for slot in order:
        allowed = _FLEX.get(slot) or (slot,)
        pick = next((c for c in left if c[0] not in used and c[1] in allowed), None)
        if pick:
            used.add(pick[0])
            line.append((slot, pick[0]))
    return line


def _record(st: dict) -> str:
    return f"{int(st.get('wins') or 0)}-{int(st.get('losses') or 0)}" + (
        f"-{int(st['ties'])}" if st.get("ties") else "")


def build(rosters: list[dict], names: dict, labels: dict[str, str], slots: list[str],
          weeks: dict[int, dict], *, through_week: int, asker: str = "") -> dict:
    """Every team's best lineup at full health and with who is playing now.

    `names` maps a roster id to the room's name for its manager, `labels` a
    player id to 'Name (POS, TEAM)'. Teams come back strongest first at
    full health. status is 'empty' before any week is scored and
    'unavailable' when a scored week's stats are missing (the averages
    would be wrong, so none are given)."""
    through_week = int(through_week or 0)
    if through_week < 1:
        return {"status": "empty",
                "note": "No week has been scored yet, so no player has an average and the rosters cannot be "
                        "ranked this way. Say so. For a read on the rosters before the season, call "
                        "lookup_fantasy_league topic=draft."}
    missing = [w for w in range(1, through_week + 1) if w not in weeks]
    if missing:
        return {"status": "unavailable",
                "note": f"Sleeper's weekly stats did not come back for week(s) {missing}. "
                        "Say the roster comparison is unavailable right now. Do not rank the rosters yourself."}
    parsed = {pid: parse_label(lbl) for pid, lbl in labels.items()}
    positions = {pid: p[1] for pid, p in parsed.items()}
    table = season_table({w: weeks[w] for w in weeks if w <= through_week}, positions)
    # the last week each NFL team played, from its defense's row
    team_last: dict[str, int] = {}
    for w in sorted(weeks):
        if w > through_week:
            break
        for pid in weeks[w]:
            if pid.isalpha():
                team_last[pid] = w

    def out_now(pid: str, reserve: set) -> bool:
        if pid in reserve:
            return True
        if positions.get(pid) == "DEF":
            return False
        last = team_last.get(parsed.get(pid, ("", "", ""))[2], through_week)
        return table[pid]["last_week"] < last

    seeds = sorted(rosters, key=lambda r: (
        -(float((r.get("settings") or {}).get("wins") or 0) + 0.5 * float((r.get("settings") or {}).get("ties") or 0)),
        -(float((r.get("settings") or {}).get("fpts") or 0)
          + float((r.get("settings") or {}).get("fpts_decimal") or 0) / 100)))
    seed_of = {r.get("roster_id"): i for i, r in enumerate(seeds, 1)}

    teams = []
    for r in rosters:
        st = r.get("settings") or {}
        pids = [str(p) for p in (r.get("players") or []) if p]
        reserve = {str(p) for p in (r.get("reserve") or []) if p}
        counted = [p for p in pids if table.get(p, {}).get("games", 0) >= MIN_GAMES and positions.get(p)]
        cands = [(p, positions[p], table[p]["avg"]) for p in counted]
        full = best_lineup(cands, slots)
        away = {p for p in counted if out_now(p, reserve)}
        avail = best_lineup([c for c in cands if c[0] not in away], slots)

        def rows(line):
            return [{"slot": s, "player": parsed[p][0], "position": positions[p], "avg": table[p]["avg"],
                     "games": table[p]["games"]} for s, p in line]

        groups = {}
        for pos in _SKILL:
            mine = [p for s, p in full if s == pos]
            if mine:
                groups[pos] = {"points": round(sum(table[p]["avg"] for p in mine), 1),
                               "players": [{"player": parsed[p][0], "avg": table[p]["avg"]} for p in mine]}
        games = int(st.get("wins") or 0) + int(st.get("losses") or 0) + int(st.get("ties") or 0)
        scored = float(st.get("fpts") or 0) + float(st.get("fpts_decimal") or 0) / 100
        teams.append({
            "manager": names.get(r.get("roster_id")) or f"roster {r.get('roster_id')}",
            "roster_id": r.get("roster_id"), "record": _record(st), "seed": seed_of.get(r.get("roster_id")),
            "full_health": round(sum(table[p]["avg"] for _, p in full), 1),
            "available": round(sum(table[p]["avg"] for _, p in avail), 1),
            "scored_per_game": round(scored / games, 1) if games else None,
            "lineup": rows(full),
            "out_now": [{"player": parsed[p][0], "avg": table[p]["avg"], "last_played_week": table[p]["last_week"],
                         "on_ir": p in reserve} for _, p in full if p in away],
            "groups": groups,
            "uncounted": [parsed.get(p, (p, "", ""))[0] for p in pids
                          if p not in counted and (p in reserve or positions.get(p) in _SKILL)
                          and table.get(p, {}).get("games", 0) < MIN_GAMES],
        })
    if not teams:
        return {"status": "empty", "note": "The league has no rosters."}
    league = {pos: round(sum(t["groups"][pos]["points"] for t in teams if pos in t["groups"])
                         / max(1, sum(1 for t in teams if pos in t["groups"])), 1) for pos in _SKILL}
    for t in teams:
        gaps = {pos: round(g["points"] - league[pos], 1) for pos, g in t["groups"].items()}
        for pos, gap in gaps.items():
            t["groups"][pos]["vs_league_average"] = gap
        if gaps:
            t["best_group"] = max(gaps, key=lambda p: (gaps[p], p))
            t["thinnest_group"] = min(gaps, key=lambda p: (gaps[p], p))
    teams.sort(key=lambda t: (-t["full_health"], t["seed"] or 99))
    for i, t in enumerate(teams, 1):
        t["rank"] = i
    now = sorted(teams, key=lambda t: (-t["available"], t["rank"]))
    for i, t in enumerate(now, 1):
        t["available_rank"] = i
    return {
        "status": "ok", "through_week": through_week, "asker": asker or "",
        "measure": ("full_health is the team's best legal lineup from its current roster, each player at his "
                    f"average in the games he played this season (weeks 1-{through_week}, league scoring, a game "
                    f"of under {MIN_SNAPS} snaps not counted, {MIN_GAMES}+ games needed). available is the same "
                    "lineup without the players in out_now: on IR, or did not play in their NFL team's last "
                    "game. Neither is a projection."),
        "league_average_group_points": league,
        "teams": teams,
    }
