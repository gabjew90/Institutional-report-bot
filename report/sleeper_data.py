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
    "1393740565673177088": (883873127359201300, "tipdropio", "TipDrop"),
    "1135287187546898432": (704361827290579084, "tulch", "Tulch"),
    "737063774686687232": (811385796295655434, "vincenzo9231", "Vincenzo"),
}
# SV (sv77788, discord 1095941993957437521) is co-owner on cbarone's
# roster — he has no Sleeper user record, so he can't appear in the map
# above. Surfaced as a co-owner tag on that roster instead.
CO_OWNERS: dict[str, str] = {"603677779459895296": "SV (co-owner)"}


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
)


def _owner_label(sleeper_user_id: str, users_by_id: dict) -> str:
    """'BK (bankerkyle)' when mapped, Sleeper display name otherwise."""
    mapped = SLEEPER_TO_DISCORD.get(str(sleeper_user_id))
    sleeper_name = (
        users_by_id.get(str(sleeper_user_id), {}).get("display_name")
        or str(sleeper_user_id)
    )
    label = (
        f"{mapped[2]} ({mapped[1]})" if mapped
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
    for sid, (_aid, uname, room) in SLEEPER_TO_DISCORD.items():
        if m in uname.lower() or uname.lower() in m \
                or m in room.lower() or room.lower() in m:
            return sid
    for sid, u in users_by_id.items():
        dn = (u.get("display_name") or "").lower()
        if dn and (m in dn or dn in m):
            return sid
    return None


def _names(ids: list, resolver) -> list[str]:
    """Translate player IDs via the resolver; unknown IDs pass through
    raw so a stale cache degrades to visible IDs, never silence."""
    ids = [str(i) for i in (ids or []) if i]
    known = resolver(ids) if ids else {}
    return [known.get(i, f"id:{i}") for i in ids]


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
        by_matchup: dict = {}
        for m in mus:
            by_matchup.setdefault(m.get("matchup_id"), []).append(m)
        games = []
        for mid, pair in sorted(by_matchup.items(), key=lambda kv: kv[0] or 0):
            sides = []
            for side in pair:
                owner = roster_owner.get(side.get("roster_id"), "")
                sides.append({
                    "manager": _owner_label(owner, users_by_id),
                    "points": side.get("points"),
                })
            games.append({"matchup": mid, "teams": sides})
        out["matchups"] = games
        if not games:
            out["note"] = (
                f"No matchups for week {wk} — the season may not have "
                "started. Say so; do NOT invent scores."
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
                or _names([p.get("player_id")], resolver)[0]
            )
            rendered.append({
                "pick": f"{p.get('round')}.{p.get('pick_no')}",
                "player": f"{nm} ({meta.get('position', '?')})",
                "manager": _owner_label(
                    str(p.get("picked_by") or ""), users_by_id),
            })
        out["picks"] = rendered
        if not rendered:
            out["note"] = (
                f"Draft has no picks yet (league status: "
                f"{league.get('status')}). Say so; do NOT invent picks."
            )

    elif topic == "trending":
        out["trending_adds"] = [
            {"player": _names([t.get("player_id")], resolver)[0],
             "adds": t.get("count")}
            for t in fetch_trending("add")
        ]
        out["trending_drops"] = [
            {"player": _names([t.get("player_id")], resolver)[0],
             "drops": t.get("count")}
            for t in fetch_trending("drop")
        ]
        out["scope"] = "all of Sleeper (not just this league)"

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
        if sid:
            r = next(
                (x for x in rosters if str(x.get("owner_id")) == sid), None)
            ids = (r.get("starters") or []) if r else []
            out["manager"] = _owner_label(sid, users_by_id)
        else:
            ids = sorted(
                proj, key=lambda k: (proj[k] or {}).get("pts_ppr") or 0,
                reverse=True,
            )[:15]
        names = _names(ids, resolver)
        out["projected_ppr"] = [
            {"player": n,
             "pts": round((proj.get(str(i)) or {}).get("pts_ppr") or 0, 1)}
            for i, n in zip(ids, names)
        ]

    out["status"] = "ok"
    return out
