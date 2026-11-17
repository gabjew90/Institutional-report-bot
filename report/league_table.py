"""The fantasy league's table, computed in code (2026-11-17, audit run 12).

Every league-table question had been assembled by the model from raw
payloads, and in one evening it invented the week-6 standings (the tool
returned the current table labelled "week 6"), answered "top 6 at the end
of week 6" with that week's game winners, answered "toughest schedule over
the next 5 weeks" with points against over the past nine, gave the sixth
seed to a team "on points tiebreakers and schedule favorability" that
Sleeper's own numbers contradict, and told a manager he had the fewest
points when he had the second fewest.

Sleeper's public API carries every input: each week's pairings and scores
(`matchups/<week>`, future weeks included, with zero points), the number of
playoff teams and the week the playoffs start. So the table is built here,
for any week that has been played:

- records and points for and against through a week, from the weekly scores
- each team's remaining regular-season opponents, and how many points a
  week those opponents have averaged
- playoff odds and expected final wins, by simulating the remaining games

The odds are the bot's own estimate. Each remaining game draws both teams'
scores from a normal distribution around the team's average weekly score so
far, with the league's week-to-week spread for every team (ten weeks is too
few to trust one team's own spread). Seeding is Sleeper's default: wins,
then points for. The draw is seeded by the season and the week, so the same
question gets the same figures until another week is played.

Pure functions over the API's JSON, plus one fetcher. No model, no database.
"""
from __future__ import annotations

import logging
import random
import statistics
import time

log = logging.getLogger(__name__)

SIM_RUNS = 10_000
# A league-wide spread below this is treated as this (week 1 or 2).
_MIN_SD = 15.0
_FINAL_TTL_S = 24 * 3600
_OPEN_TTL_S = 300
_week_cache: dict = {}


def _points(row: dict) -> float:
    v = row.get("custom_points")
    if v is None:
        v = row.get("points")
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def games_of(week_rows: list[dict]) -> list[tuple[int, float, int, float]]:
    """One (roster, points, roster, points) per game of a week. A team with
    no opponent that week (a bye in the bracket) has no game."""
    by_id: dict = {}
    for r in week_rows or []:
        if r.get("matchup_id") is not None and r.get("roster_id") is not None:
            by_id.setdefault(r["matchup_id"], []).append(r)
    out = []
    for _, pair in sorted(by_id.items()):
        if len(pair) == 2:
            a, b = sorted(pair, key=lambda r: r["roster_id"])
            out.append((int(a["roster_id"]), _points(a), int(b["roster_id"]), _points(b)))
    return out


def played(week_rows: list[dict]) -> bool:
    """A week counts once a score is in. Before it starts every score is 0."""
    return any(pa > 0 or pb > 0 for _, pa, _, pb in games_of(week_rows))


def table_through(weeks: dict[int, list[dict]], through: int) -> dict[int, dict]:
    """roster_id -> {wins, losses, ties, pf, pa, scores} over weeks 1..through."""
    rows: dict[int, dict] = {}

    def row(rid: int) -> dict:
        return rows.setdefault(rid, {"wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "pa": 0.0, "scores": []})

    for w in sorted(weeks):
        if w > through:
            break
        for a, pa, b, pb in games_of(weeks[w]):
            for me, mine, theirs in ((a, pa, pb), (b, pb, pa)):
                r = row(me)
                r["pf"] += mine
                r["pa"] += theirs
                r["scores"].append(mine)
                r["wins" if mine > theirs else "losses" if mine < theirs else "ties"] += 1
    for r in rows.values():
        r["pf"], r["pa"] = round(r["pf"], 2), round(r["pa"], 2)
    return rows


def seed_order(rows: dict[int, dict]) -> list[int]:
    """Roster ids in standing order: wins (a tie is half), then points for."""
    return sorted(rows, key=lambda rid: (-(rows[rid]["wins"] + 0.5 * rows[rid]["ties"]),
                                         -rows[rid]["pf"], rid))


def remaining_games(weeks: dict[int, list[dict]], after: int, last_regular: int) -> dict[int, list[tuple[int, int]]]:
    """roster_id -> [(week, opponent)] for weeks after..last_regular."""
    out: dict[int, list[tuple[int, int]]] = {}
    for w in sorted(weeks):
        if not after < w <= last_regular:
            continue
        for a, _, b, _ in games_of(weeks[w]):
            out.setdefault(a, []).append((w, b))
            out.setdefault(b, []).append((w, a))
    return out


def simulate(rows: dict[int, dict], weeks: dict[int, list[dict]], after: int, last_regular: int,
             spots: int, seed: str, runs: int = SIM_RUNS) -> dict[int, dict]:
    """roster_id -> {"odds": share of runs in the top `spots`, "wins":
    average final wins}. With no games left the odds are 1 or 0."""
    ids = sorted(rows)
    means = {rid: (statistics.fmean(rows[rid]["scores"]) if rows[rid]["scores"] else 0.0) for rid in ids}
    every = [s for rid in ids for s in rows[rid]["scores"]]
    # the spread of a team's weeks around its own average, pooled over the league
    resid = [s - means[rid] for rid in ids for s in rows[rid]["scores"]]
    sd = max(statistics.pstdev(resid) if len(resid) > 1 else 0.0, _MIN_SD) if every else _MIN_SD
    fixtures = [(a, b) for w in sorted(weeks) if after < w <= last_regular
                for a, _, b, _ in games_of(weeks[w]) if a in rows and b in rows]
    made = dict.fromkeys(ids, 0)
    total_wins = dict.fromkeys(ids, 0.0)
    rng = random.Random(seed)
    base = {rid: (rows[rid]["wins"] + 0.5 * rows[rid]["ties"], rows[rid]["pf"]) for rid in ids}
    for _ in range(runs if fixtures else 1):
        wins = {rid: base[rid][0] for rid in ids}
        pf = {rid: base[rid][1] for rid in ids}
        for a, b in fixtures:
            sa, sb = rng.gauss(means[a], sd), rng.gauss(means[b], sd)
            pf[a] += sa
            pf[b] += sb
            wins[a if sa >= sb else b] += 1
        order = sorted(ids, key=lambda rid: (-wins[rid], -pf[rid], rid))
        for rid in order[:spots]:
            made[rid] += 1
        for rid in ids:
            total_wins[rid] += wins[rid]
    n = runs if fixtures else 1
    return {rid: {"odds": made[rid] / n, "wins": total_wins[rid] / n} for rid in ids}


def build(weeks: dict[int, list[dict]], names: dict[int, str], *, playoff_start: int, spots: int,
          season: str = "", through: int | None = None, final_week: int | None = None) -> dict:
    """The whole table as plain data.

    `weeks` maps a week to Sleeper's matchup rows for it, `names` a roster
    id to the name the room uses. `through` is the week asked about; left
    out, or past the last week played, it is the last week played.
    `final_week` is the last week Sleeper has scored (the league's
    `last_scored_leg`): a week in progress has scores and is not a result,
    and counting it gave one team two records within a minute (audit F11)."""
    last_regular = max(1, int(playoff_start or 15) - 1)
    done = [w for w in sorted(weeks) if w <= last_regular and played(weeks[w])
            and (final_week is None or w <= int(final_week))]
    last_played = done[-1] if done else 0
    asked = through
    if through is None or through > last_played:
        through = last_played
    through = max(0, int(through))
    rows = table_through(weeks, through)
    for rid in names:                      # a team with no game yet still has a row
        rows.setdefault(rid, {"wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "pa": 0.0, "scores": []})
    order = seed_order(rows)
    current = through == last_played
    left = remaining_games(weeks, through, last_regular) if current else {}
    ppg = {rid: (rows[rid]["pf"] / len(rows[rid]["scores"]) if rows[rid]["scores"] else 0.0) for rid in rows}
    sim = (simulate(rows, weeks, through, last_regular, spots, f"{season}:{through}")
           if current and through else {})
    pf_rank = {rid: i for i, rid in enumerate(sorted(rows, key=lambda r: (-rows[r]["pf"], r)), 1)}
    teams = []
    for seed, rid in enumerate(order, 1):
        r = rows[rid]
        opps = left.get(rid) or []
        teams.append({
            "seed": seed, "roster_id": rid, "manager": names.get(rid) or f"roster {rid}",
            "wins": r["wins"], "losses": r["losses"], "ties": r["ties"],
            "points_for": r["pf"], "points_against": r["pa"], "points_for_rank": pf_rank[rid],
            "remaining": [{"week": w, "opponent": names.get(o) or f"roster {o}"} for w, o in opps],
            "remaining_opponents_ppg": (round(statistics.fmean(ppg[o] for _, o in opps), 1) if opps else None),
            "playoff_odds": (round(sim[rid]["odds"], 4) if sim else None),
            "expected_wins": (round(sim[rid]["wins"], 1) if sim else None),
        })
    hardest = sorted((t for t in teams if t["remaining_opponents_ppg"] is not None),
                     key=lambda t: -t["remaining_opponents_ppg"])
    for i, t in enumerate(hardest, 1):
        t["schedule_rank"] = i             # 1 = the hardest remaining schedule
    return {"status": "ok" if through else "empty", "season": season,
            "through_week": through, "asked_week": asked, "last_played_week": last_played,
            "regular_season_weeks": last_regular, "playoff_spots": spots,
            "weeks_left": max(0, last_regular - through) if current else None,
            "is_current": current, "teams": teams}


def _week_rows(league_id: str, week: int, final: bool, fetch) -> list[dict]:
    key = (fetch, str(league_id), int(week))
    hit = _week_cache.get(key)
    if hit and time.time() - hit[0] < (_FINAL_TTL_S if hit[2] else _OPEN_TTL_S):
        return hit[1]
    rows = fetch(league_id, week) or []
    # a past week is kept for a day only once its scores are in
    _week_cache[key] = (time.time(), rows, final and played(rows))
    return rows


def fetch_weeks(league_id: str, current_week: int, last_regular: int, fetch=None) -> dict[int, list[dict]]:
    """Sleeper's matchup rows for weeks 1..last_regular, four requests at a
    time. A week before the current one is final and cached for a day."""
    from concurrent.futures import ThreadPoolExecutor
    if fetch is None:
        from report.sleeper_data import fetch_matchups as fetch
    weeks = list(range(1, int(last_regular) + 1))
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(lambda w: _week_rows(league_id, w, w < int(current_week), fetch), weeks))
    return dict(zip(weeks, got))
