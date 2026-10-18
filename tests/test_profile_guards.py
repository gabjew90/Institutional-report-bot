"""Deterministic profile checks before save (scripts/profile_guards.py).

2026-10-18 audit: the quotes were right, the framing around them was not.
Cases are the audit's own: a copypasta read as a life story (M1), a surf
trip placed in Costa Rica when the link was wacosurf.com (M2, checked in
the bracketed framing only, since names in a bullet's head paraphrase its
quote), an invented
single-quoted anchor (M5), a room-reaction claim (M8), and a trader
rationale saying "no documented receipts" beside a +150% close (M7).
"""
import itertools

import db
from scripts import profile_guards as G

_ids = itertools.count(970_000_000)
ME, OTHER = 920001, 920002


def _say(author, text, ocr=None):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
        "author_username, author_display, content, posted_at, image_ocr_text) "
        "VALUES (?, 1, 'stonks', ?, ?, ?, ?, datetime('now'), ?)",
        (next(_ids), author, f"u{author}", f"U{author}", text, ocr))
    conn.commit()


def setup_module(_):
    db.get_connection().execute("DELETE FROM chat_messages WHERE author_id IN (?, ?)", (ME, OTHER))
    db.get_connection().commit()
    pasta = ("my kids somehow went from toddlers to teenagers while I was locking in gains, "
             "I missed every birthday and every recital, I am officially done with Omnibeta Group")
    _say(OTHER, pasta)
    _say(ME, pasta)
    _say(ME, "booked the trip, wacosurf.com here I come")
    _say(ME, "I was a chef back in the day")
    _say(ME, "NVDA calls up 150% today")


PROFILE = """**Recent personal life.**
- Missed his kids growing up: "my kids somehow went from toddlers to teenagers while I was locking in gains"
- Surf trip booked: "booked the trip, wacosurf.com here I come" — [planning his trip to Costa Rica]
- Former cook: "I was a chef back in the day"
- Runs marathons: 'only started running 4 months ago'
- The room constantly accuses him of lying about his job

**Recent trades.**
- NVDA 200C — [closed +150.00% per the log]
"""


def test_failing_bullets_are_dropped_and_good_ones_kept():
    text, dropped = G.apply(PROFILE, ME, ["I was a chef back in the day"])
    assert "Former cook" in text
    assert "NVDA 200C" in text                       # trades are checked elsewhere
    for gone in ("Missed his kids", "Costa Rica", "Runs marathons", "accuses him"):
        assert gone not in text, gone
    reasons = " ".join(dropped)
    assert "copypasta" in reasons and "Costa" in reasons and "single-quoted" in reasons
    assert "room-reaction" in reasons


def test_a_no_receipts_line_beside_a_winning_close_goes():
    r = "Talks big. There are no documented receipts in the window. Loves calls."
    assert G.fix_receipts_claim(r, PROFILE) == "Talks big. Loves calls."
    assert G.fix_receipts_claim(r, "**Recent trades.**\n- none\n") == r


def test_a_catchphrase_others_use_is_not_copypasta():
    _say(OTHER, "fuck it we ball")
    _say(ME, "fuck it we ball")
    assert not G.is_copypasta("fuck it we ball", ME)


def test_an_abbreviation_supports_the_full_name():
    hay = G._norm("been in cali for a week, then aus")
    assert G.unsupported_terms("- Family stuff — [back in California, then Australia]", hay, set()) == []


def teardown_module(_):
    # the suite shares one DB; other tests count chat messages in a window
    db.get_connection().execute("DELETE FROM chat_messages WHERE author_id IN (?, ?)", (ME, OTHER))
    db.get_connection().commit()
