"""Teams named by Sleeper username or team name (2026-10-07: "how is
stinky dill pickle's team doing" was answered with 2Pale's team)."""
from report import sleeper_data as S

USERS = {
    "1": {"display_name": "StinkyDillPickle", "metadata": {}},
    "2": {"display_name": "vincenzo31", "metadata": {"team_name": "Put It In Reverse Terry"}},
    "3": {"display_name": "RandoOutsider", "metadata": {"team_name": "Rando FC"}},
}


def test_label_carries_sleeper_username_and_team_name():
    sid = next(s for s, (_a, u, _r) in S.SLEEPER_TO_DISCORD.items() if u == "vincenzo9231")
    users = {sid: USERS["2"]}
    label = S._owner_label(sid, users)
    assert label.startswith("Vincenzo") or "(vincenzo9231)" in label
    assert "Sleeper vincenzo31" in label and "Reverse Terry" not in label
    assert S._owner_label("3", USERS) == "RandoOutsider (not on discord)"


def test_spaced_sleeper_names_and_team_names_resolve():
    users = {"1": USERS["1"], "2": USERS["2"]}
    assert S._resolve_member("stinky dill pickle", users) == "1"
    assert S._resolve_member("put it in reverse terry", users) == "2"
    assert S._resolve_member("monsoon", users) is None
    declan = next(s for s, (_a, u, _r) in S.SLEEPER_TO_DISCORD.items() if u == "_themelvin")
    assert S._resolve_member("declans", {}) == declan
