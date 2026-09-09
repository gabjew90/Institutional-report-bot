"""The `stats` topic (2026-09-09).

`fetch_weekly_stats` had existed since the module was written and no
topic ever called it, so "what is he projected for" was answerable and
"what did he actually put up" was not. Owner: "so users can ask for
stats, injuries, projections, anything sleeper offers right?"

Every Sleeper HTTP call is stubbed; these run the real topic branch.
"""
import sys

from report import sleeper_data as SD

_PLAYERS = {"1": "Mahomes", "2": "Kelce", "3": "Etienne", "9": "Bench Guy"}


def _resolver(ids):
    return {i: _PLAYERS.get(i, f"id:{i}") for i in ids}


def _stub(monkey, *, stats, proj):
    """Point every Sleeper fetch at canned data."""
    saved = {}
    fakes = {
        "fetch_state": lambda: {"season": "2026", "week": 2},
        "fetch_league": lambda lid: {"name": "Omnibeta", "season": "2026",
                                     "status": "in_season",
                                     "scoring_settings": {}, "settings": {},
                                     "total_rosters": 12},
        # a display name that is NOT in the real SLEEPER_TO_DISCORD map,
        # so _resolve_member returns this stub id rather than the
        # production id for a room member with the same nickname.
        "fetch_users": lambda lid: [{"user_id": "u1",
                                     "display_name": "zzmgr"}],
        "fetch_rosters": lambda lid: [
            {"roster_id": 1, "owner_id": "u1",
             "starters": ["1", "2"], "players": ["1", "2", "9"],
             "settings": {}}],
        "fetch_weekly_stats": lambda season, wk: stats,
        "fetch_projections": lambda season, wk: proj,
    }
    for k, v in fakes.items():
        saved[k] = getattr(SD, k)
        setattr(SD, k, v)
    monkey.append((saved,))
    return saved


def _restore(saved):
    for k, v in saved.items():
        setattr(SD, k, v)


def _call(**kw):
    return SD.build_topic_payload(
        "L1", kw.pop("topic"), player_name_resolver=_resolver, **kw)


def _run(stats, proj, **kw):
    box = []
    saved = _stub(box, stats=stats, proj=proj)
    try:
        return _call(**kw)
    finally:
        _restore(saved)


def test_stats_is_a_topic_at_all():
    assert "stats" in SD.TOPICS


def test_a_member_gets_actuals_with_the_projection_beside_each():
    out = _run({"1": {"pts_ppr": 31.4}, "2": {"pts_ppr": 8.2},
                "9": {"pts_ppr": 22.0}},
               {"1": {"pts_ppr": 22.1}, "2": {"pts_ppr": 14.0},
                "9": {"pts_ppr": 6.5}},
               topic="stats", member="zzmgr")
    assert out["status"] == "ok"
    rows = {r["player"]: r for r in out["actual_ppr"]}
    assert rows["Mahomes"]["pts"] == 31.4
    assert rows["Mahomes"]["projected"] == 22.1
    assert rows["Mahomes"]["slot"] == "starter"
    # the bench is included, which is the whole point of "who should I
    # have started"
    assert rows["Bench Guy"]["slot"] == "bench"
    assert rows["Bench Guy"]["pts"] == 22.0


def test_without_a_member_it_is_the_week_s_top_scorers_league_wide():
    """A screenshot from another league names players this league has
    never rostered."""
    out = _run({"1": {"pts_ppr": 9.0}, "2": {"pts_ppr": 40.5},
                "3": {"pts_ppr": 25.0}},
               {}, topic="stats")
    names = [r["player"] for r in out["actual_ppr"]]
    assert names == ["Kelce", "Etienne", "Mahomes"], names
    assert "not only this league" in out["note"]


def test_an_unavailable_endpoint_says_so_and_invents_nothing():
    """The endpoint is undocumented and returns {} on any failure. The
    projections topic already refuses to guess; so does this one."""
    out = _run({}, {"1": {"pts_ppr": 22.1}}, topic="stats", member="zzmgr")
    assert out["status"] == "empty"
    assert "do NOT invent" in out["note"]


def test_a_missing_projection_reads_zero_not_a_crash():
    out = _run({"1": {"pts_ppr": 12.0}}, {}, topic="stats", member="zzmgr")
    rows = {r["player"]: r for r in out["actual_ppr"]}
    assert rows["Mahomes"]["pts"] == 12.0
    assert rows["Mahomes"]["projected"] == 0.0


def test_the_tool_advertises_stats_and_separates_it_from_projections():
    from discord_bot.ask_tools import _build_fantasy_league_tool
    d = _build_fantasy_league_tool().function_declarations[0]
    assert "stats" in d.parameters.properties["topic"].description
    assert "'stats'" in d.description
    assert "ACTUALLY scored" in d.description
    from discord_bot.tool_docs import TOOL_DOCS
    doc = TOOL_DOCS["lookup_fantasy_league"]
    assert "projections is the forecast, stats is the result" in doc


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
