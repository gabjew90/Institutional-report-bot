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


def test_a_year_in_the_framing_is_not_checked():
    hay = G._norm("been doing this 5 years")
    assert G.unsupported_terms("- Trader since 2021 — [been at it since 2021]", hay, set()) == []


def test_audit_2026_10_26_profile_guards():
    """Run-6 M2 (a three-word pasta quote, a one-poster all-caps meme), M7
    (new room verbs), M8 (one-digit figures with a unit)."""
    assert G.is_copypasta("my kids somehow", ME)          # three words of the 10/14 pasta
    meme = ("BUY THE DIP SELL THE RIP THE BLOCKCHAIN NEVER SLEEPS AND NEITHER DO WE, "
            "WAGMI WAGMI WAGMI DIAMOND HANDS FOREVER, NGMI IF YOU SELL, TO THE MOON WE GO TONIGHT, "
            "HODL HODL HODL UNTIL THE LAMBO ARRIVES AT MY DOOR")
    _say(ME, meme)
    assert G.is_copypasta("BUY THE DIP SELL THE RIP", ME)
    text, dropped = G.apply("**Recent personal life.**\n"
                            "- Kids: \"my kids somehow\" — [a lot]\n"
                            "- Cook: \"I was a chef back in the day\" — [the room treats him as the chef]\n"
                            "- Chef: \"I was a chef back in the day\" — [said he cooked]\n", ME,
                            ["I was a chef back in the day"])
    assert "Kids" not in text and "the room treats" not in text and "said he cooked" in text
    hay = G._norm("bought a jacket today, wake up early")
    assert G.unsupported_terms("- Style — [dropped $4k on a jacket]", hay, set()) == ["$4k"]
    assert G.unsupported_terms("- Early — [wakes at 4 AM]", hay, set()) == ["4"]
    assert G.unsupported_terms("- Style — [dropped $4k on a jacket]", G._norm("4k jacket lol"), set()) == []


def test_a_recent_bullet_needs_a_recent_quote_2026_10_26():
    """Run-6 M6: recent life built from a message months old."""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
        "author_username, author_display, content, posted_at) "
        "VALUES (?, 1, 'stonks', ?, 'uME', 'UME', ?, datetime('now', '-120 day'))",
        (next(_ids), ME, "moving to denver next month for the new job"))
    conn.commit()
    text, dropped = G.apply("**Recent personal life.**\n"
                            "- Moving: \"moving to denver next month for the new job\"\n"
                            "- Cook: \"I was a chef back in the day\"\n", ME,
                            ["I was a chef back in the day"])
    assert "Moving" not in text and "Cook" in text and "older than 60 days" in " ".join(dropped)


def test_a_gif_link_is_not_a_quote():
    text, dropped = G.apply("**Voice.**\n- Reacts: \"https://tenor.com/view/pout-kiss-4\"\n", ME, [])
    assert "tenor" not in text and "GIF" in " ".join(dropped)
