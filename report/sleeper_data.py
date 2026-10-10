"""Sleeper fantasy-football API client + payload builder for /ask.

Read-only, no auth, no key (https://docs.sleeper.com). All fetchers are
synchronous urllib like the other report/ data modules — the /ask
executor calls them via asyncio.to_thread.

The module is DB-free by design: player-ID -> name translation comes in
through a `player_name_resolver` callable so the payload builder can be
smoke-tested with a plain dict and the executor can pass the
db-backed cache resolver.

Undocumented endpoints (projections, weekly stats) are used with a
graceful degrade: they return {} on any failure and the payload says the
data was unavailable, so if Sleeper ever drops them the tool loses those
two topics and nothing else.
"""

import json
import logging
import re
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

_BASE = "https://api.sleeper.app/v1"
_TIMEOUT = 12

# Sleeper user_id -> (discord_author_id, discord_username, room name).
# Derived 2026-08-20 from #fantasy-football-yapping chat forensics (BK's
# 8/17 draft-order post + the Ayatollah-beard exchange), NOT from name
# similarity — half the league uses unrelated handles on each platform.
# Room display names churn constantly; author_id is the identity.
SLEEPER_TO_DISCORD: dict[str, tuple[int, str, str]] = {
    "474360404319924224": (618962629548834816, "arcticaces", "Arxfic"),
    "1395568845166497792": (1192771108332650496, "abullish_xyz", "Abe"),
    "1000983853277814784": (423994649317736448, "bankerkyle", "BK"),
    "603677779459895296": (597142631142654002, "cemini23", "Cemini"),
    "1395571693904232448": (959505264615239690, "_themelvin", "Declan"),
    "911113573785550848": (906671292906893322, "dizzydean6", "dizzydean6"),
    "1395638093469454336": (162444150577299456, "f.jamal", "f.jamal"),
    "1395573375782367232": (264777559026171905, "2pale", "2Pale"),
    "1395814374639144960": (757772170863837206, "nft_spaceman", "Ry"),
    # 2026-08-23: TipDrop (1393740565673177088 / tipdropio) dropped out
    # the night before the draft ("some real shitty news"); BK replaced
    # him with StinkyDillPickle on roster 3. Identified via Arxfic's
    # best-team tag during the draft.
    "1268074568744976384": (296014750477844502, "re4lsl1msh4dy",
                            "DeepFried"),
    "1135287187546898432": (704361827290579084, "tulch", "Tulch"),
    "737063774686687232": (811385796295655434, "vincenzo9231", "Vincenzo"),
}
# SV (sv77788, discord 1095941993957437521) is co-owner on cbarone's
# roster — he has no Sleeper user record, so he can't appear in the map
# above. Surfaced as a co-owner tag on that roster instead.
CO_OWNERS: dict[str, str] = {"603677779459895296": "SV (co-owner)"}
# Discord author_id -> the room name _resolve_member matches on. Lets
# /ask answer "how's MY matchup" without asking who you are
# (2026-09-03). SV is a co-owner with no Sleeper user record, so he maps
# to the roster he shares.
DISCORD_TO_MANAGER: dict[int, str] = {
    **{aid: room for (aid, _u, room) in SLEEPER_TO_DISCORD.values()},
    1095941993957437521: "Cemini",
}


def manager_for_discord_id(user_id) -> str:
    """Room name for a Discord author_id, '' when they are not a manager."""
    try:
        return DISCORD_TO_MANAGER.get(int(user_id), "")
    except (TypeError, ValueError):
        return ""


def _get(path: str):
    """GET a Sleeper API path, return parsed JSON. Raises on HTTP/parse
    errors — callers that can degrade catch and continue."""
    url = f"{_BASE}{path}"
    req = urllib.request.Request(
        url, headers={"User-Agent": "MarketPulseBot/1.0"}
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_state() -> dict:
    """League-wide NFL state: current week, season, season_type."""
    return _get("/state/nfl") or {}


def fetch_league(league_id: str) -> dict:
    return _get(f"/league/{league_id}") or {}


def fetch_users(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/users") or []


def fetch_rosters(league_id: str) -> list[dict]:
    return _get(f"/league/{league_id}/rosters") or []


def fetch_matchups(league_id: str, week: int) -> list[dict]:
    return _get(f"/league/{league_id}/matchups/{week}") or []


def fetch_transactions(league_id: str, week: int) -> list[dict]:
    return _get(f"/league/{league_id}/transactions/{week}") or []


def fetch_draft_picks(draft_id: str) -> list[dict]:
    return _get(f"/draft/{draft_id}/picks") or []


def fetch_trending(kind: str = "add", hours: int = 24,
                   limit: int = 12) -> list[dict]:
    """League-wide (all of Sleeper) trending adds/drops."""
    kind = "drop" if kind == "drop" else "add"
    return _get(
        f"/players/nfl/trending/{kind}"
        f"?lookback_hours={hours}&limit={limit}"
    ) or []


def fetch_projections(season: str, week: int) -> dict:
    """UNDOCUMENTED endpoint — {} on any failure, by design."""
    try:
        return _get(f"/projections/nfl/regular/{season}/{week}") or {}
    except Exception as e:
        log.info(f"sleeper projections unavailable (undocumented): {e}")
        return {}


def fetch_weekly_stats(season: str, week: int) -> dict:
    """UNDOCUMENTED endpoint — {} on any failure, by design."""
    try:
        return _get(f"/stats/nfl/regular/{season}/{week}") or {}
    except Exception as e:
        log.info(f"sleeper weekly stats unavailable (undocumented): {e}")
        return {}


_SCHEDULE_URL = "https://api.sleeper.app/schedule/nfl/regular/{season}"
_STATUS_CACHE: dict = {}          # (season, week) -> (fetched_at, {team: state})
_STATUS_TTL_S = 60


def fetch_game_states(season: str, week: int) -> dict[str, str]:
    """NFL team -> 'pre' | 'live' | 'final' for one week, from Sleeper's
    schedule (undocumented, public). {} on any failure.

    2026-10-04: the payload counted a player as done once his game had
    STARTED (weekly stats carry `gp` from kickoff), so during Sunday Night
    Football 2Pale's QB Bryce Young was "played", the tool reported zero
    players left, and the bot told the room 2Pale had "0% chance" with
    "zero players left to play". The schedule is the only Sleeper source
    that says a game is still in progress ('in_game')."""
    import time as _time
    key = (str(season), int(week))
    hit = _STATUS_CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < _STATUS_TTL_S:
        return hit[1]
    try:
        req = urllib.request.Request(_SCHEDULE_URL.format(season=season),
                                     headers={"User-Agent": "MarketPulseBot/1.0"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            games = json.loads(resp.read().decode("utf-8")) or []
    except Exception as e:
        log.info(f"sleeper schedule unavailable (undocumented): {e}")
        return {}
    states: dict[str, str] = {}
    for g in games:
        if not isinstance(g, dict) or g.get("week") != int(week):
            continue
        s = str(g.get("status") or "")
        state = "final" if s == "complete" else "live" if s == "in_game" else "pre"
        for team in (g.get("home"), g.get("away")):
            if team:
                states[str(team)] = state
    _STATUS_CACHE[key] = (_time.monotonic(), states)
    return states


# 'Bryce Young (QB, CAR)' -> 'CAR'. The resolver labels players from the
# sleeper_players cache as name (position, team); a team defense is
# labelled by its abbreviation alone.
_TEAM_IN_LABEL = re.compile(r"\((?:[A-Z0-9/]+,\s*)?([A-Z]{2,3})\)\s*$")


def team_of(label: str) -> str:
    m = _TEAM_IN_LABEL.search(label or "")
    if m:
        return m.group(1)
    bare = (label or "").strip()
    return bare if re.fullmatch(r"[A-Z]{2,3}", bare) else ""


def game_state(label: str, started: bool | None, states: dict[str, str]) -> str | None:
    """'pre', 'live' or 'final' for one starter. Falls back to the stats
    flag when the schedule is down or the team is unknown: a started
    game then counts as 'final' (the old behaviour) and an unstarted one
    as 'pre'. None when neither source knows."""
    s = states.get(team_of(label)) if states else None
    if s:
        return s
    if started is None:
        return None
    return "final" if started else "pre"


# Spread of one player's weekly score around his projection. Sleeper does
# not publish a variance; 0.6 of the projection is the usual rule of thumb
# for fantasy scoring and is what makes a 20-point lead with one QB left
# a likely win, not a certain one.
_PLAYER_SD_FRACTION = 0.6


def win_chance(points_a: float, rem_a: list[float],
               points_b: float, rem_b: list[float]) -> tuple[int, int] | None:
    """(A %, B %) that each side finishes ahead: current points plus the
    projection still to come, with each remaining player's score spread
    around his projection. The bot's own estimate. Sleeper's in-app Win%
    is not in any Sleeper API (checked 2026-10-04: REST and GraphQL)."""
    import math
    mean = (points_a + sum(rem_a)) - (points_b + sum(rem_b))
    sd = math.sqrt(sum((_PLAYER_SD_FRACTION * r) ** 2 for r in rem_a + rem_b))
    if sd < 0.5:
        if abs(mean) < 0.005:
            return 50, 50
        return (100, 0) if mean > 0 else (0, 100)
    pa = 0.5 * (1 + math.erf(mean / (sd * math.sqrt(2))))
    a = max(0, min(100, round(pa * 100)))
    return a, 100 - a


def fetch_players_trimmed() -> list[tuple[str, str, str, str]]:
    """Download the full ~15MB players dump and trim to
    (player_id, name, position, team) rows for the DB cache. Called at
    most daily by the scheduler job / lazy first-use refresh."""
    raw = _get("/players/nfl") or {}
    rows: list[tuple[str, str, str, str]] = []
    for pid, p in raw.items():
        if not isinstance(p, dict):
            continue
        name = (
            p.get("full_name")
            or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            or pid
        )
        rows.append((
            str(pid), name,
            (p.get("position") or "")[:6],
            (p.get("team") or "")[:4],
        ))
    return rows


# ---------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------

TOPICS = (
    "league", "standings", "matchups", "roster",
    "transactions", "draft", "trending", "projections",
    # What players ACTUALLY scored, against what they were projected to.
    # fetch_weekly_stats has existed since the module was written and no
    # topic ever called it, so "what did he actually put up" was
    # unanswerable while "what is he projected for" was not (2026-09-09,
    # owner: "so users can ask for stats ... anything sleeper offers?").
    "stats",
    # One manager's whole week in one payload: roster with projections
    # and slots, this week's matchup with both sides' starters, record
    # and the standings table. The prefetch for any league question
    # from a manager (2026-09-03, owner: "the bot should be presented
    # with all the relevant info from the API").
    "situation",
)


def _owner_label(sleeper_user_id: str, users_by_id: dict) -> str:
    """'BK (bankerkyle)' when mapped, Sleeper display name otherwise.

    A mapped member's Sleeper username follows it: the room names teams
    by it ("how is stinky dill pickle's team doing" is
    StinkyDillPickle, DeeP FRieD's account), and on 2026-10-07 the model,
    seeing only Discord names, answered with 2Pale's team."""
    mapped = SLEEPER_TO_DISCORD.get(str(sleeper_user_id))
    sleeper_name = (
        users_by_id.get(str(sleeper_user_id), {}).get("display_name")
        or str(sleeper_user_id)
    )
    # Team names stay out of the label: one is an anti-Islam joke that the
    # safety filter can block a whole payload over. _resolve_member still
    # matches them when a member is named that way.
    label = (
        f"{mapped[2]} ({mapped[1]}) · Sleeper {sleeper_name}" if mapped
        else f"{sleeper_name} (not on discord)"
    )
    co = CO_OWNERS.get(str(sleeper_user_id))
    return f"{label} + {co}" if co else label


def _resolve_member(member: str, users_by_id: dict) -> str | None:
    """Loose member -> sleeper user_id. Accepts discord username, room
    name, or sleeper display name, case-insensitive substring both ways."""
    m = (member or "").strip().lower().lstrip("@")
    if not m:
        return None
    def _within(name: str) -> bool:
        # a name inside the question counts only as a whole word: "Ry"
        # inside "reverse terry" is not Ry
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(name)}(?:'?s)?(?![a-z0-9])", m))

    for sid, (_aid, uname, room) in SLEEPER_TO_DISCORD.items():
        if m in uname.lower() or _within(uname.lower()) \
                or m in room.lower() or _within(room.lower()):
            return sid
    # Sleeper usernames and team names, compared without spaces or
    # punctuation: "stinky dill pickle" is StinkyDillPickle.
    flat = _flat(m)
    if len(flat) < 3:
        return None
    for sid, u in users_by_id.items():
        for name in (u.get("display_name"), (u.get("metadata") or {}).get("team_name")):
            n = _flat(name or "")
            if len(n) >= 3 and (flat in n or n in flat):
                return sid
    return None


def _flat(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _names(ids: list, resolver) -> list[str]:
    """Translate player IDs via the resolver; unknown IDs pass through
    raw so a stale cache degrades to visible IDs, never silence."""
    ids = [str(i) for i in (ids or []) if i]
    known = resolver(ids) if ids else {}
    return [known.get(i, f"id:{i}") for i in ids]


def _name1(pid, resolver) -> str:
    """One id -> one name, total: a missing/None id returns '?' instead
    of the IndexError `_names([pid])[0]` raises when pid is falsy
    (2026-08-20 review — a malformed trending/draft row would have
    killed the whole payload)."""
    got = _names([pid], resolver)
    return got[0] if got else "?"


def _still_to_come(row: dict) -> float:
    """Projected points a starter has left: the whole projection before
    kickoff, the unscored part while his game is live, none after."""
    pr = row.get("projected") or 0.0
    if row.get("game_state") == "pre":
        return pr
    if row.get("game_state") == "live":
        # A player already past his projection still has game left: keep a
        # quarter of it so the estimate does not treat him as finished.
        return max(pr - (row.get("actual") or 0.0), 0.25 * pr)
    return 0.0


def _matchup_side(side: dict, stats: dict, proj: dict, names: dict,
                  states: dict | None = None) -> dict:
    """One team in a matchup: its starters' points so far, who has not
    played yet, who is playing right now, and what is still projected to
    come."""
    pp = side.get("players_points") or {}
    # "0" is Sleeper's id for an empty starting slot
    starters = [str(p) for p in (side.get("starters") or []) if p and str(p) != "0"]
    rows = []
    for pid in starters:
        st = stats.get(pid) or {}
        started = bool(st.get("gp") or st.get("gms_active")) if stats else None
        actual = pp.get(pid)
        if actual is None and st.get("pts_ppr") is not None:
            actual = st.get("pts_ppr")
        pr = (proj.get(pid) or {}).get("pts_ppr") if proj else None
        label = names.get(pid, pid)
        state = game_state(label, started, states or {})
        if state is None and states:
            state = "pre"        # schedule up, stats down, team unknown: still to come
        rows.append({
            "player": label,
            "actual": round(float(actual), 1) if actual is not None else None,
            "projected": round(float(pr), 1) if pr is not None else None,
            "game_started": state in ("live", "final") if state else started,
            "game_state": state,
        })
    points = round(float(side.get("points") or 0), 2)
    yet = [r for r in rows if r["game_state"] == "pre"]
    live = [r for r in rows if r["game_state"] == "live"]
    known = bool(proj) and (bool(stats) or bool(states))
    rem_each = [round(_still_to_come(r), 1) for r in yet + live]
    remaining = round(sum(rem_each), 1) if known else None
    played = [r for r in rows if r["game_started"] and r["actual"] is not None]
    out = {
        "points": points,
        "yet_to_play": [{"player": r["player"], "projected": r["projected"]}
                        for r in yet],
        # Starters whose game is in progress: still scoring. A side with
        # anyone here is not done, whatever yet_to_play says.
        "playing_now": [{"player": r["player"], "points_so_far": r["actual"],
                         "projected": r["projected"]} for r in live],
        "remaining_projected": remaining,
        "projected_final": (round(points + remaining, 1)
                            if remaining is not None else None),
        "players_played": sum(1 for r in rows if r["game_state"] == "final"),
        "players_left": len(yet) + len(live),
        "_starters": rows,
        "_remaining_each": rem_each if known else None,
    }
    if played:
        top = max(played, key=lambda r: r["actual"])
        out["standout"] = {k: top[k] for k in ("player", "actual", "projected")}
        with_proj = [r for r in played if r["projected"] is not None]
        if with_proj:
            dud = min(with_proj, key=lambda r: r["actual"] - r["projected"])
            if dud["actual"] < dud["projected"]:
                out["dud"] = {k: dud[k] for k in ("player", "actual", "projected")}
    return out


def _game_story(sides: list[dict]) -> dict:
    """Leader, margin and the projected result for a two-team game."""
    if len(sides) != 2:
        return {}
    a, b = sides
    if a["points"] == b["points"]:
        story = {"leader": None, "margin": 0.0}
    else:
        lead, trail = (a, b) if a["points"] > b["points"] else (b, a)
        story = {"leader": lead["manager"],
                 "margin": round(lead["points"] - trail["points"], 2)}
    if a.get("projected_final") is not None and b.get("projected_final") is not None:
        pa, pb = a["projected_final"], b["projected_final"]
        if pa != pb:
            win = a if pa > pb else b
            story["projected_winner"] = win["manager"]
            story["projected_margin"] = round(abs(pa - pb), 1)
            story["comeback_projected"] = bool(
                story["leader"] and win["manager"] != story["leader"])
    if any(s.get("remaining_projected") is None for s in sides):
        story["status"] = "unknown"          # game status endpoint down
    elif all(s["players_left"] == 0 for s in sides):
        story["status"] = "final"
    elif all(s["players_played"] == 0 and not s.get("playing_now") for s in sides):
        story["status"] = "not started"
    else:
        story["status"] = "in progress"
    if all(s.get("_remaining_each") is not None for s in sides):
        ca, cb = win_chance(a["points"], a["_remaining_each"],
                            b["points"], b["_remaining_each"])
        story["win_chance_estimate"] = {a["manager"]: ca, b["manager"]: cb}
    return story


def build_topic_payload(
    league_id: str,
    topic: str,
    *,
    week: int | None = None,
    member: str | None = None,
    player_name_resolver=None,
) -> dict:
    """Compose Sleeper API calls into one compact dict for the model.

    Never raises for a data-shaped problem: unknown topic / member /
    empty week all return a payload that says so. Network errors on the
    PRIMARY endpoint propagate — the executor turns those into a tool
    error result.
    """
    resolver = player_name_resolver or (lambda ids: {})
    topic = (topic or "standings").strip().lower()
    if topic not in TOPICS:
        return {
            "status": "error",
            "error": f"unknown topic {topic!r} — use one of {TOPICS}",
        }

    state = {}
    try:
        state = fetch_state()
    except Exception as e:
        log.info(f"sleeper state fetch failed (non-fatal): {e}")
    season = str(state.get("season") or "")
    current_week = int(state.get("week") or 1) or 1
    wk = int(week) if week else current_week

    league = fetch_league(league_id)
    users_by_id = {
        str(u.get("user_id")): u for u in fetch_users(league_id)
    }
    rosters = fetch_rosters(league_id)
    roster_owner = {
        r.get("roster_id"): str(r.get("owner_id")) for r in rosters
    }

    out: dict = {
        "league": league.get("name"),
        "season": league.get("season"),
        "league_status": league.get("status"),
        "week": wk,
        "topic": topic,
    }

    if topic == "league":
        s = league.get("scoring_settings") or {}
        out["settings"] = {
            "teams": league.get("total_rosters"),
            "ppr": s.get("rec"),
            "pass_td": s.get("pass_td"),
            "playoff_teams": (league.get("settings") or {}).get(
                "playoff_teams"),
            "trade_deadline_week": (league.get("settings") or {}).get(
                "trade_deadline"),
            "waiver_budget": (league.get("settings") or {}).get(
                "waiver_budget"),
        }
        out["managers"] = [
            _owner_label(str(r.get("owner_id")), users_by_id)
            for r in rosters
        ]

    elif topic == "standings":
        rows = []
        for r in rosters:
            st = r.get("settings") or {}
            rows.append({
                "manager": _owner_label(str(r.get("owner_id")), users_by_id),
                "record": f"{st.get('wins', 0)}-{st.get('losses', 0)}"
                          + (f"-{st['ties']}" if st.get("ties") else ""),
                "points_for": st.get("fpts", 0),
                "points_against": st.get("fpts_against", 0),
                "waiver_budget_used": st.get("waiver_budget_used", 0),
            })
        rows.sort(
            key=lambda x: (
                -int(x["record"].split("-")[0]), -float(x["points_for"] or 0)
            )
        )
        out["standings"] = rows

    elif topic == "matchups":
        mus = fetch_matchups(league_id, wk)
        # Player detail per side (2026-09-27, owner: "more entertaining
        # than just the score, like comebacks, player performance, win
        # conditions"). The payload was two totals per game, so answers
        # read as a scoreboard, and the model computed its own margins,
        # which the figure-provenance guard could not find in any source
        # and hedged ("Couldn't verify these specifics"). Margins,
        # remaining projections and projected finals are computed HERE.
        stats, proj = {}, {}
        if season and mus:
            # both endpoints are independent and each can be slow
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=2) as pool:
                f_stats = pool.submit(fetch_weekly_stats, season, wk)
                f_proj = pool.submit(fetch_projections, season, wk)
                stats = f_stats.result() or {}
                proj = f_proj.result() or {}
        by_matchup: dict = {}
        for m in mus:
            by_matchup.setdefault(m.get("matchup_id"), []).append(m)
        ids = sorted({str(p) for m in mus for p in (m.get("starters") or [])
                      if p and str(p) != "0"})
        names = dict(zip(ids, _names(ids, resolver)))
        states = fetch_game_states(season, wk) if season else {}
        games, performances = [], []
        for mid, pair in sorted(by_matchup.items(), key=lambda kv: kv[0] or 0):
            if mid is None:
                continue  # teams without an opponent this week (bye)
            sides = []
            for side in pair:
                owner = roster_owner.get(side.get("roster_id"), "")
                team = _matchup_side(side, stats, proj, names, states)
                team["manager"] = _owner_label(owner, users_by_id)
                for r in team.pop("_starters"):
                    if r["game_started"]:
                        performances.append({
                            "player": r["player"], "actual": r["actual"],
                            "projected": r["projected"],
                            "manager": team["manager"]})
                sides.append(team)
            story = _game_story(sides)
            for team in sides:
                team.pop("_remaining_each", None)
            games.append({"matchup": mid, "teams": sides, **story})
        out["matchups"] = games
        if not games:
            out["note"] = (
                f"No matchups for week {wk} — the season may not have "
                "started. Say so; do NOT invent scores."
            )
        else:
            played = [p for p in performances if p["actual"] is not None]
            out["week_standouts"] = sorted(
                played, key=lambda r: r["actual"], reverse=True)[:3]
            out["week_busts"] = sorted(
                [p for p in played if p["projected"]],
                key=lambda r: r["actual"] - r["projected"])[:3]
            out["note"] = (
                "Tell each game as a story, not a scoreboard: who leads "
                "and by how much (margin), who each side still has to "
                "play and what they are projected for (yet_to_play, "
                "remaining_projected), who is playing right now and still "
                "scoring (playing_now: a side with anyone there is not "
                "done, never say it has no players left), the standout and the dud on each "
                "side, and whether the trailing team is projected to "
                "come back (comeback_projected). win_chance_estimate is "
                "the bot's own estimate from points and projections; call "
                "it that, never Sleeper's or Kalshi's Win%, which no API "
                "exposes. week_standouts and "
                "week_busts are the league's best and worst starter "
                "performances against projection. Quote the margins and "
                "projected finals given here; do not work out your own. "
                "Projections or game status missing (null) means Sleeper's "
                "unofficial endpoint was down: say so rather than guess. A "
                "starter on bye or ruled out never gets a game status, so "
                "he shows in yet_to_play; if yet_to_play names someone who "
                "is out, say the projection counts him."
            )

    elif topic == "roster":
        sid = _resolve_member(member or "", users_by_id)
        if not sid:
            return {
                "status": "empty",
                "note": (
                    f"Could not match {member!r} to a league manager. "
                    "Say so — do NOT guess a roster. Managers: "
                    + ", ".join(
                        _owner_label(s, users_by_id)
                        for s in SLEEPER_TO_DISCORD
                    )
                ),
            }
        r = next(
            (x for x in rosters if str(x.get("owner_id")) == sid), None)
        if not r:
            return {
                "status": "empty",
                "note": "Manager matched but holds no roster — say so.",
            }
        starters = r.get("starters") or []
        bench = [p for p in (r.get("players") or []) if p not in starters]
        out["manager"] = _owner_label(sid, users_by_id)
        out["starters"] = _names(starters, resolver)
        out["bench"] = _names(bench, resolver)
        if not starters and not bench:
            out["note"] = (
                "Roster is empty (pre-draft). Say so; do NOT invent "
                "players."
            )

    elif topic == "situation":
        sid = _resolve_member(member or "", users_by_id)
        if not sid:
            return {
                "status": "empty",
                "note": (
                    f"Could not match {member!r} to a league manager. "
                    "Say so — do NOT guess. Managers: "
                    + ", ".join(
                        _owner_label(s, users_by_id)
                        for s in SLEEPER_TO_DISCORD
                    )
                ),
            }
        proj = {}
        if season:
            try:
                proj = fetch_projections(season, wk) or {}
            except Exception as e:
                log.info(f"sleeper projections failed (non-fatal): {e}")

        def _pts(pid) -> float | None:
            p = proj.get(str(pid)) or {}
            v = p.get("pts_ppr")
            return round(float(v), 1) if v is not None else None

        # Who has actually played (2026-09-13: "how's my team doing" was
        # answered with "depending on who's left on your board" because
        # the payload carried projections and totals but never said
        # which starters had taken the field). Sleeper's weekly stats
        # carry `gp` once a player's game has started; a starter with
        # no entry has not played yet.
        stats = {}
        if season:
            try:
                stats = fetch_weekly_stats(season, wk) or {}
            except Exception as e:
                log.info(f"sleeper weekly stats failed (non-fatal): {e}")

        def _actual(pid) -> float | None:
            s = stats.get(str(pid)) or {}
            v = s.get("pts_ppr")
            return round(float(v), 1) if v is not None else None

        def _started(pid) -> bool:
            s = stats.get(str(pid)) or {}
            return bool(s.get("gp") or s.get("gms_active"))

        states = fetch_game_states(season, wk) if season else {}

        def _row(p, label: str, slot: str) -> dict:
            # no stats at all -> _started is False -> 'pre', the old
            # behaviour: every starter counts as still to play
            state = game_state(label, _started(p), states)
            return {"player": label, "slot": slot, "projected": _pts(p),
                    "actual": _actual(p),
                    "game_started": state in ("live", "final") if state else _started(p),
                    "game_state": state}

        def _lineup(r: dict) -> dict:
            starters = [p for p in (r.get("starters") or []) if p]
            bench = [p for p in (r.get("players") or []) if p not in starters]
            names = dict(zip([str(p) for p in starters + bench],
                             _names(starters + bench, resolver)))
            rows = ([_row(p, names[str(p)], "starter") for p in starters]
                    + [_row(p, names[str(p)], "bench") for p in bench])
            starters_rows = [x for x in rows if x["slot"] == "starter"]
            yet = [x["player"] for x in starters_rows if x["game_state"] == "pre"]
            live = [x["player"] for x in starters_rows if x["game_state"] == "live"]
            rem_each = [round(_still_to_come(x), 1) for x in starters_rows
                        if x["game_state"] in ("pre", "live")]
            # Points already scored plus what is still to come, the same
            # sum win_chance uses. The sum of every starter's pre-game
            # projection ignored points already on the board, so a live
            # week read "projected 129.6 vs 133.0" beside a 77% win
            # estimate for the same side (2026-10-09, BK vs Ry after the
            # Thursday game). Before kickoff the two are equal.
            scored = sum(x["actual"] or 0 for x in starters_rows
                         if x["game_state"] in ("live", "final"))
            unknown = sum(x["projected"] or 0 for x in starters_rows
                          if x["game_state"] is None)
            total = round(scored + sum(rem_each) + unknown, 1)
            return {"players": rows, "projected_total": total,
                    "yet_to_play": yet, "playing_now": live,
                    "remaining_projected": round(sum(rem_each), 1),
                    "_remaining_each": rem_each}

        mine = next((x for x in rosters if str(x.get("owner_id")) == sid), None)
        if not mine:
            return {"status": "empty",
                    "note": "Manager matched but holds no roster — say so."}
        out["manager"] = _owner_label(sid, users_by_id)
        st = mine.get("settings") or {}
        out["record"] = (f"{st.get('wins', 0)}-{st.get('losses', 0)}"
                         + (f"-{st['ties']}" if st.get("ties") else ""))
        out["points_for"] = st.get("fpts", 0)
        out["waiver_budget_used"] = st.get("waiver_budget_used", 0)
        out["roster"] = _lineup(mine)

        # This week's opponent, with their lineup, so a matchup read can
        # be argued player by player rather than total vs total.
        opp = None
        try:
            mus = fetch_matchups(league_id, wk)
            my_m = next((m for m in mus if m.get("roster_id") == mine.get("roster_id")), None)
            if my_m and my_m.get("matchup_id") is not None:
                other = next((m for m in mus
                              if m.get("matchup_id") == my_m.get("matchup_id")
                              and m.get("roster_id") != mine.get("roster_id")), None)
                if other:
                    opp_roster = next((x for x in rosters
                                       if x.get("roster_id") == other.get("roster_id")), None)
                    opp = {
                        "manager": _owner_label(roster_owner.get(other.get("roster_id"), ""),
                                                users_by_id),
                        "points_so_far": other.get("points"),
                        "my_points_so_far": my_m.get("points"),
                        "lineup": _lineup(opp_roster) if opp_roster else None,
                    }
                    if opp["lineup"] and proj:
                        mine_pts = float(my_m.get("points") or 0)
                        opp_pts = float(other.get("points") or 0)
                        mine_pct, opp_pct = win_chance(
                            mine_pts, out["roster"]["_remaining_each"],
                            opp_pts, opp["lineup"]["_remaining_each"])
                        opp["win_chance_estimate"] = {out["manager"]: mine_pct,
                                                      opp["manager"]: opp_pct}
                        # The totals quoted beside the estimate are built
                        # from the same points, Sleeper's league-scored
                        # matchup points, so the two cannot disagree.
                        out["roster"]["projected_total"] = round(
                            mine_pts + sum(out["roster"]["_remaining_each"]), 1)
                        opp["lineup"]["projected_total"] = round(
                            opp_pts + sum(opp["lineup"]["_remaining_each"]), 1)
        except Exception as e:
            log.info(f"sleeper matchups failed (non-fatal): {e}")
        out["matchup"] = opp or {
            "note": f"No matchup for week {wk} — the season may not have started."}
        out["roster"].pop("_remaining_each", None)
        if opp and opp.get("lineup"):
            opp["lineup"].pop("_remaining_each", None)

        rows = []
        for r in rosters:
            s2 = r.get("settings") or {}
            rows.append({
                "manager": _owner_label(str(r.get("owner_id")), users_by_id),
                "record": f"{s2.get('wins', 0)}-{s2.get('losses', 0)}",
                "points_for": s2.get("fpts", 0),
            })
        rows.sort(key=lambda x: (-int(x["record"].split("-")[0]),
                                 -float(x["points_for"] or 0)))
        out["standings"] = rows
        out["note"] = (
            "Everything about this manager's week. Analyse from it: "
            "start/sit is bench vs starter at an eligible slot, the "
            "matchup is lineup vs lineup, the standings give the stakes. "
            "Each player carries projected, actual (points so far) and "
            "game_state (pre, live, final); yet_to_play NAMES the starters "
            "whose game has not started, playing_now the ones whose game is "
            "in progress and still scoring, and remaining_projected is what "
            "is still on the board, for both sides. Use those names: never "
            "say 'depending on who is left' when the list is right here, "
            "and never say a side has no one left while playing_now names "
            "someone. matchup.win_chance_estimate is the bot's own estimate "
            "from points and projections; call it that, never Sleeper's or "
            "Kalshi's Win%, which no API exposes. Projections "
            "missing (null) means the endpoint was down; say so rather "
            "than inventing numbers. Draft, transactions and trending are "
            "separate topics."
        )

    elif topic == "transactions":
        txs = []
        for w in {wk, max(1, wk - 1)}:
            try:
                txs.extend(fetch_transactions(league_id, w))
            except Exception as e:
                log.info(f"sleeper transactions week {w} failed: {e}")
        txs.sort(key=lambda t: t.get("created") or 0, reverse=True)
        rendered = []
        for t in txs[:20]:
            adds = t.get("adds") or {}
            drops = t.get("drops") or {}
            add_names = _names(list(adds.keys()), resolver)
            drop_names = _names(list(drops.keys()), resolver)
            actors = [
                _owner_label(roster_owner.get(rid, ""), users_by_id)
                for rid in (t.get("roster_ids") or [])
            ]
            faab = sum(
                (b.get("amount") or 0)
                for b in (t.get("waiver_budget") or [])
            )
            rendered.append({
                "type": t.get("type"),
                "status": t.get("status"),
                "managers": actors,
                "adds": add_names,
                "drops": drop_names,
                **({"faab_spent": faab} if faab else {}),
            })
        out["transactions"] = rendered
        if not rendered:
            out["note"] = (
                "No transactions found for this window. Say so; do NOT "
                "invent trades or waiver moves."
            )

    elif topic == "draft":
        picks = []
        try:
            draft_id = str(league.get("draft_id") or "")
            picks = fetch_draft_picks(draft_id) if draft_id else []
        except Exception as e:
            log.info(f"sleeper draft picks fetch failed: {e}")
        rendered = []
        for p in picks:
            meta = p.get("metadata") or {}
            nm = (
                f"{meta.get('first_name', '')} "
                f"{meta.get('last_name', '')}".strip()
                or _name1(p.get("player_id"), resolver)
            )
            rendered.append({
                "pick": f"{p.get('round')}.{p.get('pick_no')}",
                "player": f"{nm} ({meta.get('position', '?')})",
                "manager": _owner_label(
                    str(p.get("picked_by") or ""), users_by_id),
            })
        out["picks"] = rendered
        # Grouped view (2026-08-23): report-card asks need rosters, and
        # reassembling 180 flat picks in-context invites cross-team
        # attribution errors. Same data, pre-grouped.
        by_mgr: dict[str, list] = {}
        for r in rendered:
            by_mgr.setdefault(r["manager"], []).append(
                f"{r['pick']} {r['player']}")
        out["rosters_by_manager"] = by_mgr
        if not rendered:
            out["note"] = (
                f"Draft has no picks yet (league status: "
                f"{league.get('status')}). Say so; do NOT invent picks."
            )

    elif topic == "trending":
        out["trending_adds"] = [
            {"player": _name1(t.get("player_id"), resolver),
             "adds": t.get("count")}
            for t in fetch_trending("add")
        ]
        out["trending_drops"] = [
            {"player": _name1(t.get("player_id"), resolver),
             "drops": t.get("count")}
            for t in fetch_trending("drop")
        ]
        out["scope"] = "all of Sleeper (not just this league)"

    elif topic == "stats":
        # Results, and the projection beside them so the answer can say
        # who beat their number rather than just who scored.
        stats = fetch_weekly_stats(season, wk) if season else {}
        if not stats:
            return {
                "status": "empty",
                "note": (
                    "Weekly stats are unavailable (unofficial endpoint). "
                    "Say the data isn't available — do NOT invent points "
                    "scored."
                ),
            }
        proj = fetch_projections(season, wk) if season else {}
        sid = _resolve_member(member or "", users_by_id)
        slot_of: dict[str, str] = {}
        if sid:
            r = next(
                (x for x in rosters if str(x.get("owner_id")) == sid), None)
            starters = (r.get("starters") or []) if r else []
            bench = [p for p in ((r.get("players") or []) if r else [])
                     if p not in starters]
            ids = list(starters) + bench
            slot_of = {**{str(p): "starter" for p in starters},
                       **{str(p): "bench" for p in bench}}
            out["manager"] = _owner_label(sid, users_by_id)
            out["note"] = (
                "actual points scored in week %s, with the projection "
                "beside each so the answer can name who beat or missed "
                "their number. A player who did not play shows 0.0." % wk
            )
        else:
            # No member named: the week's top scorers league-wide, which
            # is what a question about a player in someone else's league
            # (or a screenshot) needs.
            ids = sorted(
                stats, key=lambda k: (stats[k] or {}).get("pts_ppr") or 0,
                reverse=True,
            )[:15]
            out["note"] = (
                "top scorers across the NFL in week %s, not only this "
                "league's rosters." % wk
            )
        names = _names(ids, resolver)
        out["actual_ppr"] = [
            {"player": n,
             "pts": round((stats.get(str(i)) or {}).get("pts_ppr") or 0, 1),
             "projected": round(
                 (proj.get(str(i)) or {}).get("pts_ppr") or 0, 1),
             **({"slot": slot_of[str(i)]} if str(i) in slot_of else {})}
            for i, n in zip(ids, names)
        ]

    elif topic == "projections":
        proj = fetch_projections(season, wk) if season else {}
        if not proj:
            return {
                "status": "empty",
                "note": (
                    "Projections are unavailable (unofficial endpoint). "
                    "Say the data isn't available — do NOT invent "
                    "projected points."
                ),
            }
        sid = _resolve_member(member or "", users_by_id)
        slot_of: dict[str, str] = {}
        if sid:
            r = next(
                (x for x in rosters if str(x.get("owner_id")) == sid), None)
            starters = (r.get("starters") or []) if r else []
            # Bench too, or "who should I start" has nothing to compare
            # and the answer is a recital of the lineup already set
            # (2026-09-03, the screenshot with ten arrows and no words).
            bench = [p for p in ((r.get("players") or []) if r else [])
                     if p not in starters]
            ids = list(starters) + bench
            slot_of = {**{str(p): "starter" for p in starters},
                       **{str(p): "bench" for p in bench}}
            out["manager"] = _owner_label(sid, users_by_id)
            out["note"] = (
                "starters are the lineup as currently set on Sleeper; "
                "bench is everyone else on the roster. A start/sit answer "
                "names the swaps where a bench player projects higher at "
                "a slot he is eligible for, and says so when the lineup "
                "is already optimal."
            )
        else:
            ids = sorted(
                proj, key=lambda k: (proj[k] or {}).get("pts_ppr") or 0,
                reverse=True,
            )[:15]
        names = _names(ids, resolver)
        out["projected_ppr"] = [
            {"player": n,
             "pts": round((proj.get(str(i)) or {}).get("pts_ppr") or 0, 1),
             **({"slot": slot_of[str(i)]} if str(i) in slot_of else {})}
            for i, n in zip(ids, names)
        ]

    out["status"] = "ok"
    return out
