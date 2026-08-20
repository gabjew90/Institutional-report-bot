"""Smoke test for the Sleeper fantasy /ask tool (2026-08-20).

Validates, with NO network I/O (fetchers patched with fixtures):
  1. standings payload maps owners to discord identities, sorts by record
  2. roster topic resolves loose member names; unknown member -> empty
     + no-fabrication note; empty roster carries the pre-draft note
  3. transactions render adds/drops through the name resolver; empty ->
     note
  4. projections degrade to status=empty + note when the undocumented
     endpoint returns {}
  5. player-id translation falls back to visible raw ids, never silence
  6. executor: unconfigured league id -> error telling the model to say
     the lookup isn't available
  7. tool registration: executor map + conditional tool-list wiring
  8. db cache helpers round-trip on a real in-memory table
"""

import asyncio
import sys
from unittest.mock import patch

import report.sleeper_data as sd


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_LEAGUE = {
    "name": "Omnibeta Degens", "season": "2026", "status": "in_season",
    "total_rosters": 12, "draft_id": "d1",
    "scoring_settings": {"rec": 1.0, "pass_td": 4},
    "settings": {"playoff_teams": 6, "trade_deadline": 11,
                 "waiver_budget": 100},
}
_USERS = [
    {"user_id": "1000983853277814784", "display_name": "bankerkyle"},
    {"user_id": "1135287187546898432", "display_name": "Tulchh"},
    {"user_id": "9999", "display_name": "RandoOutsider"},
]
_ROSTERS = [
    {"roster_id": 1, "owner_id": "1000983853277814784",
     "starters": ["4034", "6794"], "players": ["4034", "6794", "1111"],
     "settings": {"wins": 2, "losses": 0, "fpts": 250.5,
                  "fpts_against": 180.0, "waiver_budget_used": 40}},
    {"roster_id": 8, "owner_id": "1135287187546898432",
     "starters": [], "players": [],
     "settings": {"wins": 1, "losses": 1, "fpts": 200.0,
                  "fpts_against": 210.0, "waiver_budget_used": 0}},
    {"roster_id": 4, "owner_id": "9999", "starters": [], "players": [],
     "settings": {"wins": 0, "losses": 2, "fpts": 150.0,
                  "fpts_against": 260.0, "waiver_budget_used": 100}},
]
_NAMES = {"4034": "Ja'Marr Chase (WR, CIN)", "6794": "Bijan Robinson (RB, ATL)"}


def _resolver(ids):
    return {i: _NAMES[i] for i in ids if i in _NAMES}


def _patched(**over):
    base = {
        "fetch_state": lambda: {"season": "2026", "week": 2},
        "fetch_league": lambda lid: _LEAGUE,
        "fetch_users": lambda lid: _USERS,
        "fetch_rosters": lambda lid: _ROSTERS,
        "fetch_matchups": lambda lid, w: [],
        "fetch_transactions": lambda lid, w: [],
        "fetch_draft_picks": lambda did: [],
        "fetch_trending": lambda kind="add", **kw: [],
        "fetch_projections": lambda s, w: {},
    }
    base.update(over)
    return [patch.object(sd, k, v) for k, v in base.items()]


def _build(topic, **kw):
    patches = _patched(**kw.pop("fetchers", {}))
    for p in patches:
        p.start()
    try:
        return sd.build_topic_payload(
            "L1", topic, player_name_resolver=_resolver, **kw)
    finally:
        for p in patches:
            p.stop()


def test_standings_maps_discord_and_sorts():
    out = _build("standings")
    assert out["status"] == "ok", out
    rows = out["standings"]
    assert rows[0]["manager"].startswith("BK (bankerkyle)"), rows[0]
    assert rows[0]["record"] == "2-0"
    assert "not on discord" in rows[2]["manager"], rows[2]
    _ok("standings: discord mapping + record sort + unmapped tagged")


def test_roster_member_resolution():
    out = _build("roster", member="@BK")
    assert out["manager"].startswith("BK"), out
    assert out["starters"] == [
        "Ja'Marr Chase (WR, CIN)", "Bijan Robinson (RB, ATL)"], out
    assert out["bench"] == ["id:1111"], \
        "unknown player id must degrade to visible raw id"
    # loose room-name match
    out = _build("roster", member="tulch")
    assert out["manager"].startswith("Tulch"), out
    assert "pre-draft" in (out.get("note") or ""), \
        "empty roster must carry the pre-draft note"
    # unknown member
    out = _build("roster", member="monsoon")
    assert out["status"] == "empty" and "do NOT guess" in out["note"], out
    _ok("roster: member resolution, raw-id fallback, empty notes")


def test_transactions_and_empty_note():
    tx = [{
        "type": "waiver", "status": "complete", "created": 5,
        "roster_ids": [1], "adds": {"4034": 1}, "drops": {"1111": 1},
        "waiver_budget": [{"amount": 23}],
    }]
    out = _build("transactions",
                 fetchers={"fetch_transactions": lambda lid, w: tx})
    t = out["transactions"][0]
    assert t["adds"] == ["Ja'Marr Chase (WR, CIN)"] and t["faab_spent"] == 23, t
    assert t["drops"] == ["id:1111"], "drop must degrade to raw id"
    out = _build("transactions")
    assert "do NOT invent" in (out.get("note") or ""), out
    _ok("transactions: name translation + FAAB + empty note")


def test_projections_degrade():
    out = _build("projections")
    assert out["status"] == "empty" and "do NOT invent" in out["note"], out
    proj = {"4034": {"pts_ppr": 21.4}, "6794": {"pts_ppr": 18.2}}
    out = _build("projections", member="bk",
                 fetchers={"fetch_projections": lambda s, w: proj})
    assert out["projected_ppr"][0]["pts"] == 21.4, out
    _ok("projections: unofficial-endpoint degrade + member starters")


def test_executor_unconfigured():
    import discord_bot.bot as bot_mod
    with patch.object(bot_mod.settings, "sleeper_league_id", ""):
        res = asyncio.run(
            bot_mod._execute_fantasy_league({"topic": "standings"}))
    assert res["status"] == "error" and "not configured" in res["error"], res
    _ok("executor: unconfigured league -> honest error")


def test_wiring():
    import inspect
    import discord_bot.bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert '"lookup_fantasy_league": _execute_fantasy_league' in src, \
        "executor map entry missing"
    assert src.count("_build_fantasy_league_tool()") >= 2, \
        "tool must be in both tool lists (main + retry)"
    assert "sleeper_league_id" in src, "registration must be conditional"
    import scheduler.jobs as jobs
    assert hasattr(jobs, "_sleeper_players_refresh_job")
    jsrc = inspect.getsource(jobs.setup_scheduler)
    assert "sleeper_players_refresh" in jsrc, "daily cache job not registered"
    _ok("wiring: executor map, both tool lists, conditional, cron job")


def test_db_cache_roundtrip():
    import sqlite3
    import db as db_mod
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE sleeper_players (
               player_id TEXT PRIMARY KEY, name TEXT NOT NULL,
               position TEXT, team TEXT,
               updated_at TEXT NOT NULL DEFAULT (datetime('now')))"""
    )
    with patch.object(db_mod, "get_connection", return_value=conn),          patch.object(db_mod, "SLEEPER_DUMP_MIN_ROWS", 2):
        # anomaly guard: a tiny dump must NOT wipe the cache
        assert db_mod.upsert_sleeper_players([("1", "One", "QB", "X")]) == 0
        n = db_mod.upsert_sleeper_players(
            [("4034", "Ja'Marr Chase", "WR", "CIN"),
             ("6794", "Bijan Robinson", "RB", "ATL")])
        assert n == 2
        got = db_mod.get_sleeper_player_names(["4034", "missing"])
        assert got == {"4034": "Ja'Marr Chase (WR, CIN)"}, got
        age = db_mod.sleeper_players_cache_age_hours()
        assert age is not None and age < 1, age
    _ok("db cache: anomaly guard + upsert + name render + age")


if __name__ == "__main__":
    print("=== sleeper fantasy tool smoke ===")
    test_standings_maps_discord_and_sorts()
    test_roster_member_resolution()
    test_transactions_and_empty_note()
    test_projections_degrade()
    test_executor_unconfigured()
    test_wiring()
    test_db_cache_roundtrip()
    print("\nALL SLEEPER TOOL SMOKE TESTS PASS")
