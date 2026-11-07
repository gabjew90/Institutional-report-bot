"""Profile bullets rest on the member's own messages (run 8, 2026-11-05).

The audit's cases: terbear given Monsoon's classmates from a single-quoted
line that answered Monsoon (M1); Recent bullets with no quote carrying
facts from June (M2); a quote stitched from two messages never traced
(M14); tag dates in UTC (M16); empty headers left behind (M17); a habit
read from one message (M19); framing that rules a claim false (M5); a room
reaction in the Personality paragraph and the rationales (M4, M21).
"""
import itertools
import json

import db
from scripts import profile_guards as G

_ids = itertools.count(980_000_000)
ME, MONSOON = 930001, 930002


def _say(author, text, at="datetime('now')", channel=7):
    conn = db.get_connection()
    cur = conn.execute(
        "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
        "author_username, author_display, content, posted_at) "
        f"VALUES (?, ?, 'general', ?, ?, ?, ?, {at})",
        (next(_ids), channel, author, f"u{author}", f"Name{author}", text))
    conn.commit()
    return cur.lastrowid


def setup_module(_):
    conn = db.get_connection()
    conn.execute("DELETE FROM chat_messages WHERE author_id IN (?, ?)", (ME, MONSOON))
    conn.execute("INSERT OR REPLACE INTO user_profiles (user_id, username, display_name, profile_text) "
                 "VALUES (?, 'monsoonx', 'Monsoon', 'x')", (MONSOON,))
    conn.commit()


def teardown_module(_):
    # other tests count profiled members and their messages (the race board)
    conn = db.get_connection()
    conn.execute("DELETE FROM chat_messages WHERE author_id IN (?, ?)", (ME, MONSOON))
    conn.execute("DELETE FROM user_profiles WHERE user_id IN (?, ?)", (ME, MONSOON))
    conn.commit()


def test_a_single_quoted_line_answering_someone_about_their_classmates_goes():
    _say(MONSOON, "monsoon here, you gonna get my classmates", channel=11)
    mid = _say(ME, "Is that ur classmate", channel=11)
    profile = ("**Recent personal life.**\n"
               f"- Currently in school, rating 'Is that ur classmate' + [rates them all] {{m{mid}}}\n")
    text, dropped = G.apply(profile, ME)
    assert "classmate" not in text
    assert any("someone else" in d for d in dropped)


def test_a_recent_bullet_with_no_message_behind_it_goes():
    profile = "**Recent personal life.**\n- Works the Subway counter + [sandwich artist]\n"
    text, dropped = G.apply(profile, ME)
    assert "Subway" not in text
    assert any("no source message" in d for d in dropped)


def test_a_cited_quote_is_kept_with_its_ids_and_comes_back_on_the_next_refresh():
    mid = _say(ME, "booked the ecuador airbnb for the 23rd lets go")
    profile = ("**Recent personal life.**\n"
               f'- Ecuador trip: "booked the ecuador airbnb for the 23rd" + [packing already] {{m{mid}}}\n')
    sources: dict = {}
    text, dropped = G.apply(profile, ME, sources_out=sources)
    assert dropped == []
    assert "{m" not in text and "(said " in text
    assert list(sources.values()) == [[mid]]
    again = G.with_citations(text, json.dumps(sources))
    assert f"{{m{mid}}}" in again and "(said " not in again


def test_a_quote_joined_across_two_messages_is_traced_piece_by_piece():
    _say(ME, "I'm not even black")
    _say(ME, "I'm brown actually")
    ok = '**Retarded takes.**\n- Race check: "I\'m not even black... I\'m brown actually" + [the reveal]\n'
    text, dropped = G.apply(ok, ME)
    assert "Race check" in text, dropped
    bad = '**Retarded takes.**\n- Race check: "I\'m not even black... I am purple today" + [the reveal]\n'
    text, dropped = G.apply(bad, ME)
    assert "Race check" not in text


def test_the_tag_date_is_new_york_time():
    msg = {"posted_at": "2026-11-05T01:23:00", "reply_parent_id": None, "channel_id": 1, "id": 1}
    assert G._tag(msg, ME).startswith(" (said Nov 4")


def test_a_habit_from_one_message_loses_its_frequency():
    _say(ME, "I used to deliver pizza for dominos lol")
    profile = ('**Recent personal life.**\n'
               '- Ex pizza guy: "I used to deliver pizza for dominos" + [brings it up whenever someone asks]\n')
    text, _ = G.apply(profile, ME)
    assert "Ex pizza guy" in text and "whenever" not in text


def test_framing_that_calls_a_real_plan_fictional_goes():
    _say(ME, "flying out to quito next month finally")
    profile = ('**Recent personal life.**\n'
               '- Travel: "flying out to quito next month" + [more fictional travel plans]\n')
    text, dropped = G.apply(profile, ME)
    assert "quito" not in text
    assert any("claim was true" in d for d in dropped)


def test_an_emptied_section_loses_its_header():
    profile = ("**Voice.**\n- \"a line nobody ever wrote here\"\n\n"
               "**Recent personal life.**\n- Works the Subway counter + [sandwich artist]\n\n"
               "**Recent trades.**\n- none\n")
    text, _ = G.apply(profile, ME)
    assert "**Voice.**" not in text and "Recent personal life" not in text
    assert "**Recent trades.**" in text


def test_personality_sentences_with_a_room_reaction_go():
    profile = ("**Personality and style.** Trades small caps. When he asked for help "
               "the room immediately pivots to mocking him. Posts late at night.\n")
    text, dropped = G.apply(profile, ME)
    assert "pivots" not in text and "Trades small caps." in text and "Posts late at night." in text
    assert dropped


def test_a_rationale_quoting_a_word_the_member_never_wrote_loses_that_sentence():
    text, dropped = G.check_rationale(
        "Big talker. Known for 'Pajeet' coin-shilling in every thread. Loves calls.", ME)
    assert "Pajeet" not in text and "Big talker." in text
    assert dropped


def test_a_quote_typed_as_two_quick_messages_is_found():
    _say(ME, "I dont hold.", channel=12)
    _say(ME, "I scalp. I hold cash lol", channel=12)
    profile = '**Voice.**\n- "I don\'t hold. I scalp. I hold cash lol" — [his defence]\n'
    text, dropped = G.apply(profile, ME)
    assert "I scalp" in text, dropped


def test_the_sweep_drops_a_stale_bullet_from_a_stored_profile():
    _say(ME, "my dog ate my homework again today")
    conn = db.get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO user_profiles (user_id, username, display_name, profile_text, "
        "racism_rationale) VALUES (?, 'me', 'Me', ?, ?)",
        (ME, "**Recent personal life.**\n- Dog trouble: \"my dog ate my homework again\" + [classic]\n"
             "- Works the Subway counter + [sandwich artist]\n",
         "Quiet. The room immediately pivots to mocking him."))
    conn.commit()
    out = G.sweep_stored()
    assert out["changed"] >= 1
    text, racism, sources = conn.execute(
        "SELECT profile_text, racism_rationale, profile_sources FROM user_profiles WHERE user_id = ?",
        (ME,)).fetchone()
    assert "Dog trouble" in text and "Subway" not in text and "(said " in text
    assert "pivots" not in racism and "Quiet." in racism
    assert json.loads(sources)
    assert G.sweep_stored()["changed"] == 0          # a second pass finds nothing new


def test_personality_bullets_need_no_quote():
    profile = "**Personality and style.**\n- Trades small caps all day\n- Posts late at night\n"
    text, dropped = G.apply(profile, ME)
    assert "Trades small caps" in text and "Posts late" in text, dropped


def test_a_time_written_with_dots_supports_the_hour():
    _say(ME, "up at 4 a.m. again for the open")
    profile = '**Recent personal life.**\n- Early riser: "up at 4 a.m. again" + [wakes at 4 AM]\n'
    text, dropped = G.apply(profile, ME)
    assert "Early riser" in text, dropped
