"""The league table as posted lines (discord_bot/league_lines.py, 2026-11-19,
audit run 12 design 3): standings for any week played, remaining schedules,
playoff odds. Fixtures are the four-team league of test_league_table."""
from datetime import date

from discord_bot import answer_block
from discord_bot import league_lines as L
from report import sleeper_data as SD
from tests.test_league_table import NAMES, WEEKS, _build


def _result(**kw):
    return {"status": "ok", "topic": "table", "table": _build(**kw)}


def test_the_view_follows_the_askers_words():
    assert L.view("league standings") == "standings"
    assert L.view("who has the fewest points") == "standings"
    assert L.view("top 6 at the end of week 6") == "standings"
    assert L.view("what are my playoff chances") == "playoffs"
    assert L.view("who is gonna win the league") == "playoffs"
    assert L.view("who has the toughest remaining schedule over the next 5 weeks") == "schedule"
    # a question with none of the words gets the payload and no posted lines
    assert L.view("any injury news on CMC") is None
    # about players, one game, one week's scoring, or the NFL itself: not the league table
    for q in ("top 5 rbs this week", "what are the odds i win this week", "who scored the most points this week",
              "who makes the nfl playoffs", "whats the nfl schedule thursday"):
        assert L.view(q) is None, q
    assert L.view("who has the most points") == "standings"
    assert L.block_lines(_result(), "any injury news on CMC") == []


def test_standings_are_the_table_with_its_week_stated():
    lines = L.block_lines(_result(), "league standings")
    assert lines == [
        "→ Standings through week 3 of 5. Top 2 make the playoffs.",
        "→ 1. **BK** 2-1, **370.8** points for (1st), 330.0 against",
        "→ 2. **Ry** 1-1-1, **321.8** points for (2nd), 306.5 against",
        "→ 3. **Abe** 1-1-1, **306.8** points for (3rd), 301.5 against",
        "→ 4. **Tulch** 1-2, **274.5** points for (4th), 335.8 against"]
    assert len(L.block_lines(_result(), "top 2 teams right now")) == 3


def test_a_past_week_is_the_table_as_it_stood():
    """2026-11-16: the standings tool returned the current table labelled
    "week 6", and the week-6 standings were invented from it."""
    lines = L.block_lines(_result(through=1), "standings after week 1")
    assert lines[0].startswith("→ Standings through week 1 of 5.")
    assert lines[1] == "→ 1. **BK** 1-0, **120.5** points for (1st), 100.2 against"
    # no odds and no schedule for a week that is over: its standings answer it
    assert L.block_lines(_result(through=1), "playoff odds after week 1") == lines
    # a week not yet scored says so, and gives the last week that is
    late = L.block_lines(_result(through=5), "standings through week 5")
    assert late[0] == "→ Week 5 is not final. The last week scored is week 3."
    assert late[1].startswith("→ Standings through week 3 of 5.")


def test_the_schedule_is_the_remaining_opponents_and_what_they_average():
    """2026-11-16: "toughest schedule over the next 5 weeks" was answered
    with points against over the past nine."""
    lines = L.block_lines(_result(), "who has the toughest remaining schedule")
    assert lines[0] == ("→ Remaining schedule, weeks 4 to 5, hardest first. The figure is what the "
                        "opponents have averaged a week through week 3.")
    # Ry plays Tulch (91.5 a week) and BK (123.6): the hardest pair left
    assert lines[1] == "→ 1. **Ry** (1-1-1): opponents average **107.5** (Tulch, BK)"
    # BK plays Abe (102.2) and Ry (107.2)
    assert lines[3] == "→ 3. **BK** (2-1): opponents average **104.7** (Abe, Ry)"
    one = L.block_lines(_result(), "toughest schedule next 1 weeks")
    assert one[0].startswith("→ Remaining schedule, week 4, hardest first.")
    assert any("(Abe)" in ln for ln in one)


def test_playoff_odds_are_labelled_the_bots_own_estimate():
    lines = L.block_lines(_result(), "what are my playoff chances")
    assert lines[0] == ("→ Playoff odds through week 3, 2 weeks left, top 2 make it. This is my own "
                        "estimate: 10,000 simulated finishes, each team scoring around its average so far.")
    assert len(lines) == 5 and lines[1].startswith("→ **BK** (2-1, seed 1): **")
    assert all("wins expected" in ln for ln in lines[1:])
    # the same question gets the same figures until another week is played
    assert L.block_lines(_result(), "what are my playoff chances") == lines


def test_the_lines_are_posted_as_an_answer_block():
    blk = answer_block.build("lookup_fantasy_league", _result(), date(2026, 11, 19), question="league standings")
    assert blk and blk["lines"][0].startswith("→ Standings through week 3 of 5.")
    assert answer_block.build("lookup_fantasy_league", _result(), date(2026, 11, 19),
                              question="any injury news") is None
    # another topic of the same tool has no block
    assert answer_block.build("lookup_fantasy_league", {"status": "ok", "topic": "situation"},
                              date(2026, 11, 19), question="league standings") is None


def test_the_tool_builds_the_table_and_past_standings_from_the_weekly_scores(monkeypatch):
    league = {"name": "Omnibeta", "season": "2026", "status": "in_season",
              "settings": {"playoff_week_start": 6, "playoff_teams": 2, "last_scored_leg": 3}}
    users = [{"user_id": str(i), "display_name": n} for i, n in NAMES.items()]
    rosters = [{"roster_id": i, "owner_id": str(i), "settings": {"wins": 9, "losses": 9, "fpts": 999}}
               for i in NAMES]
    monkeypatch.setattr(SD, "fetch_state", lambda: {"season": "2026", "week": 4})
    monkeypatch.setattr(SD, "fetch_league", lambda lid: league)
    monkeypatch.setattr(SD, "fetch_users", lambda lid: users)
    monkeypatch.setattr(SD, "fetch_rosters", lambda lid: rosters)
    monkeypatch.setattr(SD, "fetch_matchups", lambda lid, w: WEEKS.get(w, []))
    from report import league_table
    league_table._week_cache.clear()
    out = SD.build_topic_payload("L1", "table")
    assert out["status"] == "ok" and out["table"]["through_week"] == 3 and out["records_through_week"] == 3
    assert [t["manager"] for t in out["table"]["teams"]] == ["BK", "Ry", "Abe", "Tulch"]
    # `standings` with a week is that week's table, not the rosters' current records
    past = SD.build_topic_payload("L1", "standings", week=1)
    assert past["records_through_week"] == 1 and past["week"] == 1
    assert past["standings"][0] == {"manager": "BK", "record": "1-0", "points_for": 120.5,
                                    "points_against": 100.25}
    # with no week it is still the current table from the rosters
    now = SD.build_topic_payload("L1", "standings")
    assert now["standings"][0]["record"] == "9-9"
