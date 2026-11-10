"""Which members a question names. Since 2026-11-09 there is one matcher,
db.members_named_in_text over the alias map, and
db.find_users_mentioned_in_text is @-mentions plus that. The profile-table
matcher it replaced had no stopwords ("oracle" loaded The Oracle), and its
prefix pass matched "report" to reportfirst1112 (2026-10-08 audit)."""
import db
from db_parts import chat as C

G, ORACLE, WOCK, SV, RY, BK, MONSOON, NAGUS = 1, 2, 3, 4, 5, 6, 7, 8
ROWS = [
    (G, "G", 3545), (ORACLE, "The Oracle", 900), (WOCK, "Wock", 1834),
    (WOCK, "Fundamental Wock", 434), (SV, "SV", 12181), (RY, "Ry_bry", 4943),
    (RY, "Ry_spaceman", 5514), (BK, "BK", 53605), (BK, "bearishkyle", 2649),
    (MONSOON, "Monsoon", 15334), (NAGUS, "A Bullish Grand Nagus", 608),
    (NAGUS, "Grand Nagus Yeezy", 12238),
]
PROFILES = [(MONSOON, "reportufirst", "Monsoon"), (WOCK, "sleep04025", "Fundamental Wock"),
            (G, "biggestg55", "G")]
AMAP = C.build_member_aliases(ROWS, PROFILES)


def named(text):
    return set(db.members_named_in_text(text, AMAP))


def test_short_names_the_room_types_resolve():
    """SV had 12,181 messages and no alias: the map dropped names under 3 letters."""
    assert named("SV unmentioned is offensive") == {SV}
    assert named("99 seems low, Sv shouldve done 50") == {SV}
    assert named("Is ry gay") == {RY}
    assert named("what did bk say") == {BK}


def test_a_profile_username_resolves():
    assert named("what does sleep04025 hold") == {WOCK}


def test_ordinary_words_do_not_resolve_a_member():
    assert named("what are the fundamentals for CRWD") == set()
    assert named("what time apld report earnings") == set()
    assert named("is this chat bullish or bearish into tomorrow") == set()
    assert named("did oracle report") == set()


def test_a_one_letter_display_name_matches_as_a_standalone_capital():
    """2026-10-10 audit: "what G and wock we're talking about" never
    loaded G (display name "G"), and the answer put BK in his place."""
    assert named("what G and wock we're talking about") == {G, WOCK}
    assert named("G's take on nbis") == {G}
    for text in ("5G rollout and g force", "going to the gym", "🤝 my G",
                 'replace every "G" with "B"'):
        assert G not in named(text), text


def test_a_capital_letter_inside_a_longer_name_is_not_an_alias():
    assert named("A good day for semis") == set()


def test_a_name_used_as_a_verb_finds_the_member_2026_10_18():
    """Audit A21: "I'm done wocking" got a cough-syrup definition; Wock is a member."""
    assert named("I'm done wocking") == {WOCK}
    assert named("wockhardt pharma") == set()


def test_a_slur_never_matches_a_member():
    from scripts.slur_patterns import count_racial_slurs
    word = "nigga"
    assert count_racial_slurs(word) > 0
    amap = C.build_member_aliases([(9, "Niggahjo", 500)])
    assert db.members_named_in_text(f"LMAO I wish a {word} would", amap) == {}


def test_find_users_is_mentions_then_the_alias_map():
    C._alias_cache["map"] = AMAP
    try:
        assert db.find_users_mentioned_in_text("<@123> and SV") == [123, SV]
        assert db.find_users_mentioned_in_text("did oracle report") == []
    finally:
        C._alias_cache["map"] = None


def test_a_name_with_an_ending_reports_the_word_written():
    """chat_count drops a member whose every surface is the counted word."""
    assert db.members_named_in_text("how often did abe say wocking", AMAP)[WOCK] == ["wocking"]
