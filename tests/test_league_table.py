"""The fantasy league's table, computed in code (report/league_table.py,
2026-11-17, audit run 12 F1-F7). A four-team league over three played weeks
of a five-week regular season, in Sleeper's matchup-row shape."""
from report import league_table as T

NAMES = {1: "BK", 2: "Abe", 3: "Ry", 4: "Tulch"}


def _week(*games):
    rows = []
    for i, (a, pa, b, pb) in enumerate(games, 1):
        rows += [{"roster_id": a, "matchup_id": i, "points": pa}, {"roster_id": b, "matchup_id": i, "points": pb}]
    return rows


WEEKS = {
    1: _week((1, 120.5, 2, 100.25), (3, 90.0, 4, 95.5)),
    2: _week((1, 110.0, 3, 130.75), (2, 105.5, 4, 80.0)),
    3: _week((1, 140.25, 4, 99.0), (2, 101.0, 3, 101.0)),      # a tie
    4: _week((1, 0.0, 2, 0.0), (3, 0.0, 4, 0.0)),              # not played: pairings only
    5: _week((1, 0.0, 3, 0.0), (2, 0.0, 4, 0.0)),
    6: _week((1, 0.0, 4, 0.0), (2, 0.0, 3, 0.0)),              # the playoffs start here
}


def _build(**kw):
    return T.build(WEEKS, NAMES, playoff_start=6, spots=2, season="2026", **kw)


def _team(table, name):
    return next(t for t in table["teams"] if t["manager"] == name)


def test_records_and_points_come_from_the_weekly_scores():
    t = _build()
    assert (t["through_week"], t["last_played_week"], t["regular_season_weeks"], t["weeks_left"]) == (3, 3, 5, 2)
    assert [(x["manager"], x["wins"], x["losses"], x["ties"]) for x in t["teams"]] == [
        ("BK", 2, 1, 0), ("Ry", 1, 1, 1), ("Abe", 1, 1, 1), ("Tulch", 1, 2, 0)]
    bk = _team(t, "BK")
    assert (bk["points_for"], bk["points_against"]) == (370.75, 330.0)      # decimals kept
    # level on wins and ties, Ry is ahead of Abe on points for
    assert _team(t, "Ry")["points_for"] == 321.75 and _team(t, "Abe")["points_for"] == 306.75
    assert [x["points_for_rank"] for x in t["teams"]] == [1, 2, 3, 4]


def test_the_table_through_an_earlier_week_is_that_weeks_table():
    """Audit F1: asked for week 6, the tool returned today's table labelled week 6."""
    t = _build(through=1)
    assert t["through_week"] == 1 and not t["is_current"]
    assert [(x["manager"], x["wins"], x["losses"], x["points_for"]) for x in t["teams"]] == [
        ("BK", 1, 0, 120.5), ("Tulch", 1, 0, 95.5), ("Abe", 0, 1, 100.25), ("Ry", 0, 1, 90.0)]
    # a past table has no remaining schedule and no odds: those belong to today
    assert all(x["remaining"] == [] and x["playoff_odds"] is None for x in t["teams"])
    # a week that has not been played is answered with the last one that has
    late = _build(through=5)
    assert late["through_week"] == 3 and late["asked_week"] == 5 and late["is_current"]


def test_the_remaining_schedule_is_the_regular_season_only():
    t = _build()
    assert _team(t, "BK")["remaining"] == [{"week": 4, "opponent": "Abe"}, {"week": 5, "opponent": "Ry"}]
    # Abe still plays BK (123.6 a week) and Tulch (91.5); Tulch plays Ry (107.25) and Abe (102.25)
    assert _team(t, "Abe")["remaining_opponents_ppg"] == 107.5 and _team(t, "Abe")["schedule_rank"] in (1, 2)
    assert _team(t, "Tulch")["remaining_opponents_ppg"] == 104.8 and _team(t, "Tulch")["schedule_rank"] in (3, 4)
    assert sorted(x["schedule_rank"] for x in t["teams"]) == [1, 2, 3, 4]


def test_the_odds_are_stable_and_ordered_by_strength():
    a, b = _build(), _build()
    assert [x["playoff_odds"] for x in a["teams"]] == [x["playoff_odds"] for x in b["teams"]], "same draw each time"
    odds = {x["manager"]: x["playoff_odds"] for x in a["teams"]}
    assert abs(sum(odds.values()) - 2.0) < 1e-6, "two spots"
    assert odds["BK"] > odds["Ry"] > odds["Tulch"]
    assert all(x["wins"] + 0.5 * x["ties"] <= x["expected_wins"] <= x["wins"] + 0.5 * x["ties"] + 2
               for x in a["teams"])


def test_a_finished_regular_season_has_certain_odds():
    done = {**WEEKS, 4: _week((1, 100.0, 2, 90.0), (3, 80.0, 4, 70.0)),
            5: _week((1, 100.0, 3, 90.0), (2, 80.0, 4, 70.0))}
    t = T.build(done, NAMES, playoff_start=6, spots=2, season="2026")
    assert t["weeks_left"] == 0
    assert [x["playoff_odds"] for x in t["teams"]] == [1.0, 1.0, 0.0, 0.0]


def test_before_the_season_there_is_no_table():
    empty = {w: _week((1, 0.0, 2, 0.0), (3, 0.0, 4, 0.0)) for w in range(1, 6)}
    t = T.build(empty, NAMES, playoff_start=6, spots=2)
    assert t["status"] == "empty" and t["through_week"] == 0
    assert [x["manager"] for x in t["teams"]] == ["BK", "Abe", "Ry", "Tulch"]


def test_weeks_are_fetched_once_and_final_weeks_are_kept():
    calls = []

    def fetch(league_id, week):
        calls.append(week)
        return WEEKS[week]
    got = T.fetch_weeks("L1", current_week=4, last_regular=5, fetch=fetch)
    assert sorted(got) == [1, 2, 3, 4, 5] and sorted(calls) == [1, 2, 3, 4, 5]
    T.fetch_weeks("L1", current_week=4, last_regular=5, fetch=fetch)
    assert sorted(calls) == [1, 2, 3, 4, 5], "nothing is read again inside the cache window"


def test_a_week_in_progress_is_not_counted():
    live = {**WEEKS, 4: _week((1, 55.5, 2, 20.0), (3, 0.0, 4, 0.0))}       # one game under way
    t = T.build(live, NAMES, playoff_start=6, spots=2, season="2026", final_week=3)
    assert t["through_week"] == 3 and _team(t, "BK")["wins"] == 2 and _team(t, "BK")["points_for"] == 370.75
    assert _team(t, "BK")["remaining"][0] == {"week": 4, "opponent": "Abe"}
