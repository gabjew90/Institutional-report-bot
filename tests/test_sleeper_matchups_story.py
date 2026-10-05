"""The `matchups` topic tells the game, not just the score (2026-09-27).

Owner: "make the football answers more entertaining than just the score,
like comebacks, player performance, win conditions". The payload was two
totals per game; the model worked out margins itself and the
figure-provenance guard, finding no source for them, hedged the answer
("Couldn't verify these specifics", 9/28 "what are all the matchups
that are still close?"). Every Sleeper call is stubbed.
"""
from report import sleeper_data as SD

NAMES = {"1": "Mahomes", "2": "Kelce", "3": "Swift", "4": "Hurts",
         "5": "Brown", "6": "Bijan"}


def _resolver(ids):
    return {i: NAMES.get(i, f"id:{i}") for i in ids}


def _run(monkeypatch, *, matchups, stats, proj, states=None):
    monkeypatch.setattr(SD, "fetch_state", lambda: {"season": "2026", "week": 3})
    monkeypatch.setattr(SD, "fetch_league", lambda lid: {
        "name": "Omnibeta", "season": "2026", "status": "in_season"})
    monkeypatch.setattr(SD, "fetch_users", lambda lid: [
        {"user_id": "uA", "display_name": "zzalpha"},
        {"user_id": "uB", "display_name": "zzbravo"}])
    monkeypatch.setattr(SD, "fetch_rosters", lambda lid: [
        {"roster_id": 1, "owner_id": "uA", "settings": {}},
        {"roster_id": 2, "owner_id": "uB", "settings": {}}])
    monkeypatch.setattr(SD, "fetch_matchups", lambda lid, wk: matchups)
    monkeypatch.setattr(SD, "fetch_weekly_stats", lambda s, wk: stats)
    monkeypatch.setattr(SD, "fetch_projections", lambda s, wk: proj)
    monkeypatch.setattr(SD, "fetch_game_states", lambda s, wk: states or {})
    return SD.build_topic_payload("L1", "matchups", player_name_resolver=_resolver)


def _names(out):
    return {t["manager"]: t for t in out["matchups"][0]["teams"]}


LIVE = [
    {"matchup_id": 1, "roster_id": 1, "points": 120.0, "starters": ["1", "2", "3"],
     "players_points": {"1": 38.2, "2": 4.1, "3": 0}},
    {"matchup_id": 1, "roster_id": 2, "points": 110.5, "starters": ["4", "5", "6"],
     "players_points": {"4": 30.0, "5": 19.0, "6": 0}},
]
STATS_LIVE = {"1": {"gp": 1}, "2": {"gp": 1}, "4": {"gp": 1}, "5": {"gp": 1}}
PROJ = {"1": {"pts_ppr": 24.0}, "2": {"pts_ppr": 14.5}, "3": {"pts_ppr": 9.0},
        "4": {"pts_ppr": 22.0}, "5": {"pts_ppr": 15.0}, "6": {"pts_ppr": 21.0}}


def test_a_live_game_carries_margin_remaining_and_a_projected_comeback(monkeypatch):
    out = _run(monkeypatch, matchups=LIVE, stats=STATS_LIVE, proj=PROJ)
    g = out["matchups"][0]
    assert g["leader"] and g["margin"] == 9.5 and g["status"] == "in progress"
    t = _names(out)
    lead = t[g["leader"]]
    trail = next(v for k, v in t.items() if k != g["leader"])
    assert lead["yet_to_play"] == [{"player": "Swift", "projected": 9.0}]
    assert trail["yet_to_play"] == [{"player": "Bijan", "projected": 21.0}]
    assert lead["projected_final"] == 129.0 and trail["projected_final"] == 131.5
    assert g["comeback_projected"] is True and g["projected_margin"] == 2.5


def test_each_side_names_its_standout_and_its_dud(monkeypatch):
    out = _run(monkeypatch, matchups=LIVE, stats=STATS_LIVE, proj=PROJ)
    lead = _names(out)[out["matchups"][0]["leader"]]
    assert lead["standout"] == {"player": "Mahomes", "actual": 38.2, "projected": 24.0}
    assert lead["dud"] == {"player": "Kelce", "actual": 4.1, "projected": 14.5}


def test_the_week_standouts_and_busts_are_league_wide(monkeypatch):
    out = _run(monkeypatch, matchups=LIVE, stats=STATS_LIVE, proj=PROJ)
    assert out["week_standouts"][0]["player"] == "Mahomes"
    assert out["week_busts"][0]["player"] == "Kelce"
    assert "yet_to_play" not in out["week_standouts"][0]
    assert "_starters" not in _names(out)[out["matchups"][0]["leader"]]


def test_a_finished_game_is_final(monkeypatch):
    stats = {p: {"gp": 1} for p in ("1", "2", "3", "4", "5", "6")}
    out = _run(monkeypatch, matchups=LIVE, stats=stats, proj=PROJ)
    g = out["matchups"][0]
    assert g["status"] == "final" and "comeback_projected" in g


def test_status_endpoint_down_says_unknown_rather_than_guessing(monkeypatch):
    out = _run(monkeypatch, matchups=LIVE, stats={}, proj=PROJ)
    g = out["matchups"][0]
    assert g["status"] == "unknown" and "projected_winner" not in g
    assert g["margin"] == 9.5, "the score itself still comes from Sleeper"


def test_the_note_asks_for_the_story_and_forbids_own_arithmetic(monkeypatch):
    out = _run(monkeypatch, matchups=LIVE, stats=STATS_LIVE, proj=PROJ)
    assert "story" in out["note"] and "do not work out your own" in out["note"]


def test_no_games_still_says_the_season_has_not_started(monkeypatch):
    out = _run(monkeypatch, matchups=[], stats={}, proj={})
    assert out["matchups"] == [] and "do NOT invent scores" in out["note"]


def test_standout_rows_carry_only_what_the_answer_quotes(monkeypatch):
    out = _run(monkeypatch, matchups=LIVE, stats=STATS_LIVE, proj=PROJ)
    assert set(out["week_standouts"][0]) == {"player", "actual", "projected", "manager"}


def test_an_empty_lineup_slot_is_not_a_player_left_to_play(monkeypatch):
    mus = [dict(LIVE[0], starters=["1", "2", "3", "0"]), LIVE[1]]
    out = _run(monkeypatch, matchups=mus, stats=STATS_LIVE, proj=PROJ)
    lead = _names(out)[out["matchups"][0]["leader"]]
    assert [p["player"] for p in lead["yet_to_play"]] == ["Swift"]


def test_teams_without_an_opponent_are_not_a_game(monkeypatch):
    bye = {"matchup_id": None, "roster_id": 3, "points": 0, "starters": []}
    out = _run(monkeypatch, matchups=LIVE + [bye], stats=STATS_LIVE, proj=PROJ)
    assert len(out["matchups"]) == 1
