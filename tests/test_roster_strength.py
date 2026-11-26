"""How good each roster is (report/roster_strength.py and the strength view
of discord_bot/league_lines.py, 2026-11-26).

"Assuming every person on each team's roster is fully healthy, who
currently has the best team?" was answered with the three highest season
point totals: no payload measured a roster."""
from datetime import date

from discord_bot import answer_block
from discord_bot import ask_router as R
from discord_bot import league_lines as L
from report import roster_strength as RS
from report import sleeper_data as SD

SCORING = {"pass_yd": 0.04, "pass_td": 4.0, "rush_yd": 0.1, "rec": 1.0, "rec_yd": 0.1, "rec_td": 6.0,
           "xpm": 1.0, "sack": 1.0, "bonus": 0.0}
SLOTS = ["QB", "RB", "WR", "FLEX", "DEF", "BN", "BN"]
LABELS = {
    "q1": "Quinn One (QB, KC)", "q2": "Quinn Two (QB, SF)",
    "r1": "Rory Star (RB, KC)", "r2": "Rory Depth (RB, SF)", "r3": "Rory Hurt Jr. (RB, SF)",
    "w1": "Wes Alpha (WR, KC)", "w2": "Wes Beta (WR, SF)", "w3": "Wes Gamma (WR, KC)",
    "w4": "Amon St. Delta (WR, SF)", "KC": "Kansas City Chiefs (DEF, KC)", "SF": "San Francisco 49ers (DEF, SF)",
}


def _row(points, snaps=40):
    # rec_yd carries the points so the scoring formula is exercised
    return {"gp": 1, "rec_yd": points * 10, "off_snp": snaps}


def _weeks():
    """Three scored weeks. r3 averaged 30 over weeks 1-2 and missed week 3.
    q2 played three snaps in week 2. w4 played one game."""
    stats = {
        1: {"q1": _row(20), "q2": _row(18), "r1": _row(15), "r2": _row(8), "r3": _row(28), "w1": _row(12),
            "w2": _row(10), "w3": _row(6), "w4": _row(40), "KC": {"gp": 1, "sack": 5}, "SF": {"gp": 1, "sack": 3}},
        2: {"q1": _row(22), "q2": _row(0, snaps=3), "r1": _row(17), "r2": _row(10), "r3": _row(32), "w1": _row(14),
            "w2": _row(12), "w3": _row(8), "KC": {"gp": 1, "sack": 7}, "SF": {"gp": 1, "sack": 5}},
        3: {"q1": _row(24), "q2": _row(22), "r1": _row(16), "r2": _row(9), "w1": _row(13),
            "w2": _row(11), "w3": _row(7), "KC": {"gp": 1, "sack": 6}, "SF": {"gp": 1, "sack": 4},
            "idle": {"gp": 0, "gms_active": 1, "rec_yd": 990}},
    }
    return {w: RS.week_points(s, SCORING) for w, s in stats.items()}


ROSTERS = [
    {"roster_id": 1, "owner_id": "1", "players": ["q1", "r1", "w1", "w3", "KC"],
     "settings": {"wins": 2, "losses": 1, "fpts": 210, "fpts_decimal": 50}},
    {"roster_id": 2, "owner_id": "2", "players": ["q2", "r2", "r3", "w2", "w4", "SF"], "reserve": ["r3"],
     "settings": {"wins": 1, "losses": 2, "fpts": 180}},
]
NAMES = {1: "BK", 2: "Tulch"}


def _strength(**kw):
    return RS.build(ROSTERS, NAMES, LABELS, SLOTS, _weeks(), through_week=3, **kw)


def test_points_are_the_leagues_scoring_and_only_games_played_count():
    wk = _weeks()
    assert wk[1]["q1"] == (20.0, 40.0) and wk[1]["KC"] == (5.0, None)
    assert "idle" not in wk[3]                     # active and never on the field is not a game
    table = RS.season_table(wk, {p: RS.parse_label(l)[1] for p, l in LABELS.items()})
    assert table["q1"] == {"games": 3, "points": 66.0, "last_week": 3, "avg": 22.0}
    # three snaps is an injury exit or a cameo, not a game: 20.0, not 13.3
    assert table["q2"]["games"] == 2 and table["q2"]["avg"] == 20.0
    assert table["r3"]["last_week"] == 2 and table["r3"]["avg"] == 30.0


def test_the_best_lineup_fills_fixed_slots_then_the_flex():
    line = RS.best_lineup([("a", "RB", 10.0), ("b", "RB", 20.0), ("c", "WR", 5.0), ("d", "QB", 30.0),
                           ("e", "QB", 29.0), ("f", "TE", 12.0)], ["QB", "RB", "WR", "FLEX", "BN"])
    assert line == [("QB", "d"), ("RB", "b"), ("WR", "c"), ("FLEX", "f")]   # a second QB cannot flex


def test_full_health_counts_the_injured_and_available_leaves_them_out():
    s = _strength()
    assert s["status"] == "ok" and s["through_week"] == 3
    tulch, bk = s["teams"]                         # strongest first at full health
    assert (tulch["manager"], tulch["rank"], tulch["seed"]) == ("Tulch", 1, 2)
    # q2 20.0, r3 30.0, w2 11.0, flex r2 9.0, SF 4.0
    assert tulch["full_health"] == 74.0
    # without r3 (on IR, missed week 3): r2 moves up and nobody is left to flex
    assert tulch["available"] == 44.0
    assert tulch["out_now"] == [{"player": "Rory Hurt Jr.", "avg": 30.0, "last_played_week": 2, "on_ir": True}]
    assert tulch["uncounted"] == ["Amon St. Delta"]            # one game is not an average
    assert tulch["scored_per_game"] == 60.0 and bk["scored_per_game"] == 70.2
    # q1 22, r1 16, w1 13, flex w3 7, KC 6
    assert (bk["full_health"], bk["available"], bk["out_now"]) == (64.0, 64.0, [])
    assert bk["available_rank"] == 1 and tulch["available_rank"] == 2
    assert tulch["best_group"] == "RB" and tulch["groups"]["RB"]["vs_league_average"] == 7.0
    assert bk["thinnest_group"] == "RB"


def test_a_missing_week_gives_no_ranking_at_all():
    weeks = _weeks()
    del weeks[2]
    s = RS.build(ROSTERS, NAMES, LABELS, SLOTS, weeks, through_week=3)
    assert s["status"] == "unavailable" and "teams" not in s
    assert RS.build(ROSTERS, NAMES, LABELS, SLOTS, {}, through_week=0)["status"] == "empty"


def test_the_reduced_weeks_are_cached_and_an_empty_week_is_not():
    calls = []

    def fetch(season, week):
        calls.append(week)
        return {} if week == 2 else {"q1": _row(10)}
    RS._cache.clear()
    assert sorted(RS.fetch_season("2026", 3, SCORING, fetch=fetch)) == [1, 3]
    assert sorted(RS.fetch_season("2026", 3, SCORING, fetch=fetch)) == [1, 3]
    assert sorted(calls) == [1, 2, 2, 3]            # week 2 is asked for again, the others are not


def _result(**kw):
    s = _strength(**kw)
    for t, aliases in zip(s["teams"], (["tulch", "Tulchh"], ["bankerkyle"])):
        t["aliases"] = aliases
    return {"status": "ok", "topic": "strength", "strength": s}


def test_the_answer_comes_first_then_the_teams_and_what_the_figure_is():
    lines = L.strength_lines(_result(), "who has the best team if everyone is healthy")
    assert lines == [
        "→ **Tulch** has the best roster at full health: its best lineup averages **74.0** points a week, "
        "10.0 more than **BK**'s.",
        "→ 1. **Tulch** (1-2, seed 2): **74.0**, 44.0 without Rory Hurt Jr. Best at running back (Hurt 30.0), "
        "thinnest at quarterback (Two 20.0).",
        "→ 2. **BK** (2-1, seed 1): **64.0**. Best at receiver (Alpha 13.0), thinnest at running back (Star 16.0).",
        "→ With the players who are out left off, the best lineup is **BK**'s, **64.0**.",
        "→ Each figure is a team's best lineup from its current roster, every player at his average in the games "
        "he played through week 3, in league scoring. Out means on IR or missed his team's last game."]
    worst = L.strength_lines(_result(), "who has the worst roster")
    assert worst[0].startswith("→ **BK** has the weakest roster at full health: its best lineup averages **64.0**")
    assert worst[1].startswith("→ 2. **BK**") and len(worst) == 4


def test_two_teams_named_are_compared_with_each_other():
    """2026-09-03: "Statistically, how much better is my team than Kyle's"."""
    lines = L.strength_lines(_result(asker="Tulch"), "Statistically, how much better is my team than Kyle’s")
    assert lines[0] == ("→ **Tulch** has the better roster at full health: its best lineup averages **74.0** "
                        "points a week against **BK**'s **64.0**.")
    assert len(lines) == 4 and lines[1].startswith("→ 1. **Tulch**") and lines[2].startswith("→ 2. **BK**")
    # a word that is not a name picks nobody
    ry = {"manager": "Ry", "aliases": ["nft_spaceman", "rybryethguy"]}
    assert L._named(ry, "who has the best team") is False
    assert L._named(ry, "is ry's team any good") is True and L._named(ry, "spaceman's roster") is True
    declan = {"manager": "Declan", "aliases": ["_themelvin", "DihtrickCohones"]}
    assert L._named(declan, "is my team better than them") is False        # "them" is not _themelvin
    assert L._named(declan, "is melvin's team any good") is True
    assert L._named({"manager": "dizzydean6", "aliases": ["dizzydean6"]}, "how good is dizzy's roster") is True
    # "I" alone does not make the asker one of two teams compared
    league = L.strength_lines(_result(asker="Tulch"), "who has the best team, I think it's Kyle")
    assert league[0].startswith("→ **Tulch** has the best roster at full health")


def test_the_lines_are_posted_as_an_answer_block():
    blk = answer_block.build("lookup_fantasy_league", _result(), date(2026, 11, 26), question="who has the best team")
    assert blk and blk["lines"][0].startswith("→ **Tulch** has the best roster at full health")
    # The first line is the answer, so the model's sentence above it could
    # only say it again (2026-11-26 harness): the lines are the whole reply.
    assert blk["whole"] and "whole reply" in answer_block.note(blk)
    text, kept = answer_block.compose("Tulch leads the league in roster strength, powered by his backs.", [blk])
    assert not kept and text.startswith("→ **Tulch** has the best roster at full health")
    bad = {"status": "ok", "topic": "strength", "strength": {"status": "unavailable"}}
    assert answer_block.build("lookup_fantasy_league", bad, date(2026, 11, 26), question="best team") is None


def test_the_router_sends_a_roster_question_to_the_strength_view():
    fc = "🏈-fantasy-football-yapping-🏈"

    def route(q, mgr="BK"):
        return R.classify(q, fantasy_enabled=True, channel_name=fc, asker_manager=mgr)
    want = [(R.T_FANTASY, {"topic": "strength", "member": "BK"})]
    for q in ("assuming every person on each team’s roster is fully healthy, who currently has the best team?",
              "who looks like they currently have the best team to win the whole thing?",
              "Statistically, how much better is my team than Kyle’s",
              "who has the worst roster", "power rankings", "compare bk and declan teams",
              "is my squad better than his"):
        r = route(q)
        assert r.shape == R.FANTASY and r.prefetch == want, (q, r.prefetch)
    assert route("who has the best team", mgr="").prefetch == [(R.T_FANTASY, {"topic": "strength"})]
    # not a roster question: one player's health, the standings, one week
    for q in ("is CMC fully healthy", "show me standings", "who's winning the league", "hows my matchup",
              "who won the draft", "who has the best team this week"):
        assert not any(a.get("topic") == "strength" for _, a in route(q).prefetch), q


def test_the_tool_builds_the_view_from_sleepers_stats(monkeypatch):
    league = {"name": "Omnibeta", "season": "2026", "status": "in_season", "roster_positions": SLOTS,
              "scoring_settings": SCORING, "settings": {"last_scored_leg": 3}}
    users = [{"user_id": "1", "display_name": "bankerkyle"}, {"user_id": "2", "display_name": "Tulchh"}]
    raw = {
        1: {"q1": _row(20), "r1": _row(15), "w1": _row(12), "w3": _row(6), "KC": {"gp": 1, "sack": 5},
            "q2": _row(18), "r2": _row(8), "w2": _row(10), "SF": {"gp": 1, "sack": 3}},
    }
    monkeypatch.setattr(SD, "fetch_state", lambda: {"season": "2026", "week": 4})
    monkeypatch.setattr(SD, "fetch_league", lambda lid: league)
    monkeypatch.setattr(SD, "fetch_users", lambda lid: users)
    monkeypatch.setattr(SD, "fetch_rosters", lambda lid: ROSTERS)
    monkeypatch.setattr(SD, "fetch_weekly_stats", lambda season, week: raw[1])
    RS._cache.clear()
    out = SD.build_topic_payload("L1", "strength", member="Tulchh",
                                 player_name_resolver=lambda ids: {i: LABELS[i] for i in ids if i in LABELS})
    assert out["status"] == "ok" and out["week"] == 3
    s = out["strength"]
    assert s["asker"] == "Tulchh" and [t["manager"] for t in s["teams"]] == ["bankerkyle", "Tulchh"]
    assert s["teams"][0]["aliases"] == ["bankerkyle"]
    # the stats endpoint down: no ranking, and the payload says so
    monkeypatch.setattr(SD, "fetch_weekly_stats", lambda season, week: {})
    RS._cache.clear()
    down = SD.build_topic_payload("L1", "strength", player_name_resolver=lambda ids: {})
    assert down["status"] == "unavailable" and "strength" not in down
