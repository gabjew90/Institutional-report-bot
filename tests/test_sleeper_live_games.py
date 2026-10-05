"""Live games and the win estimate in the Sleeper payload (2026-10-04).

During Sunday Night Football 2Pale's QB Bryce Young (CAR) was mid-game,
the payload counted him as played, and the bot said 2Pale had "0% chance"
with "zero players left to play". Every Sleeper call is stubbed.
"""
import io
import json

from report import sleeper_data as SD

NAMES = {"1": "Bryce Young (QB, CAR)", "2": "Carnell Tate (WR, TEN)",
         "3": "Chris Olave (WR, NO)", "4": "Jonathan Taylor (RB, IND)",
         "5": "PIT"}
STATES = {"CAR": "live", "TEN": "final", "IND": "final", "NO": "pre", "PIT": "final"}
PROJ = {"1": {"pts_ppr": 22.0}, "2": {"pts_ppr": 14.0}, "3": {"pts_ppr": 15.0},
        "4": {"pts_ppr": 18.0}, "5": {"pts_ppr": 8.0}}
STATS = {p: {"gp": 1} for p in ("1", "2", "4", "5")}      # Olave's game not started
GAME = [
    {"matchup_id": 4, "roster_id": 1, "points": 82.54, "starters": ["1", "2", "5"],
     "players_points": {"1": 10.0, "2": 21.5, "5": 8.0}},
    {"matchup_id": 4, "roster_id": 2, "points": 115.92, "starters": ["3", "4"],
     "players_points": {"3": 0.0, "4": 22.7}},
]


def _resolver(ids):
    return {i: NAMES.get(i, f"id:{i}") for i in ids}


def _stub(monkeypatch, states=STATES):
    monkeypatch.setattr(SD, "fetch_state", lambda: {"season": "2026", "week": 4})
    monkeypatch.setattr(SD, "fetch_league", lambda lid: {"name": "L", "season": "2026"})
    monkeypatch.setattr(SD, "fetch_users", lambda lid: [
        {"user_id": "uA", "display_name": "Lord2Pale"},
        {"user_id": "uB", "display_name": "vincenzo31"}])
    monkeypatch.setattr(SD, "fetch_rosters", lambda lid: [
        {"roster_id": 1, "owner_id": "uA", "starters": ["1", "2", "5"],
         "players": ["1", "2", "5"], "settings": {"wins": 0, "losses": 3, "fpts": 270}},
        {"roster_id": 2, "owner_id": "uB", "starters": ["3", "4"],
         "players": ["3", "4"], "settings": {"wins": 2, "losses": 1, "fpts": 437}}])
    monkeypatch.setattr(SD, "fetch_matchups", lambda lid, wk: GAME)
    monkeypatch.setattr(SD, "fetch_weekly_stats", lambda s, wk: STATS)
    monkeypatch.setattr(SD, "fetch_projections", lambda s, wk: PROJ)
    monkeypatch.setattr(SD, "fetch_game_states", lambda s, wk: states)


def test_team_is_read_from_the_player_label():
    assert SD.team_of("Bryce Young (QB, CAR)") == "CAR"
    assert SD.team_of("PIT") == "PIT"
    assert SD.team_of("id:9228") == ""


def test_a_player_in_a_live_game_is_still_left(monkeypatch):
    _stub(monkeypatch)
    out = SD.build_topic_payload("L1", "matchups", player_name_resolver=_resolver)
    game = out["matchups"][0]
    pale = next(t for t in game["teams"] if "Lord2Pale" in t["manager"])
    assert [p["player"] for p in pale["playing_now"]] == ["Bryce Young (QB, CAR)"]
    assert pale["players_left"] == 1, "a QB mid-game is not 'zero players left'"
    assert pale["remaining_projected"] == 12.0          # 22 projected, 10 scored
    assert game["status"] == "in progress"
    assert "_remaining_each" not in pale


def test_the_win_estimate_is_never_zero_while_someone_is_playing(monkeypatch):
    _stub(monkeypatch)
    game = SD.build_topic_payload("L1", "matchups", player_name_resolver=_resolver)["matchups"][0]
    est = game["win_chance_estimate"]
    pale = next(v for k, v in est.items() if "Lord2Pale" in k)
    assert sum(est.values()) == 100
    assert 0 <= pale < 10, est      # down 33 with ~12 to come against Olave's 15: unlikely, not settled


def test_without_the_schedule_a_started_game_counts_as_played(monkeypatch):
    _stub(monkeypatch, states={})
    pale = next(t for t in SD.build_topic_payload(
        "L1", "matchups", player_name_resolver=_resolver)["matchups"][0]["teams"]
        if "Lord2Pale" in t["manager"])
    assert pale["playing_now"] == [] and pale["players_left"] == 0


def test_situation_names_who_is_playing_and_estimates_the_matchup(monkeypatch):
    _stub(monkeypatch)
    monkeypatch.setattr(SD, "_resolve_member", lambda m, u: "uA")
    out = SD.build_topic_payload("L1", "situation", member="2pale",
                                 player_name_resolver=_resolver)
    assert out["roster"]["playing_now"] == ["Bryce Young (QB, CAR)"]
    assert out["roster"]["yet_to_play"] == []
    est = out["matchup"]["win_chance_estimate"]
    assert sum(est.values()) == 100 and "_remaining_each" not in out["roster"]


def test_win_chance_edges():
    assert SD.win_chance(100, [], 90, []) == (100, 0)
    assert SD.win_chance(90, [], 90, []) == (50, 50)
    a, b = SD.win_chance(100, [10.0], 100, [10.0])
    assert a == b == 50


def test_schedule_states(monkeypatch):
    rows = [{"week": 4, "home": "CAR", "away": "DET", "status": "in_game"},
            {"week": 4, "home": "NO", "away": "ATL", "status": "pre_game"},
            {"week": 4, "home": "BAL", "away": "TEN", "status": "complete"},
            {"week": 5, "home": "KC", "away": "LV", "status": "pre_game"}]

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(SD.urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(json.dumps(rows).encode()))
    SD._STATUS_CACHE.clear()
    st = SD.fetch_game_states("2026", 4)
    assert st == {"CAR": "live", "DET": "live", "NO": "pre", "ATL": "pre",
                  "BAL": "final", "TEN": "final"}
    SD._STATUS_CACHE.clear()
