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
import re

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


def _post(author, text, channel, reply_to=None, days_ago=0, name=None):
    mid = next(_ids)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO chat_messages (discord_message_id, channel_id, channel_name, author_id, "
        "author_username, author_display, content, posted_at, reply_parent_id) "
        "VALUES (?, ?, 'stonks', ?, ?, ?, ?, datetime('now', ?), ?)",
        (mid, channel, author, f"u{author}", name or f"U{author}", text, f"-{days_ago} day", reply_to))
    conn.commit()
    return mid


def test_run7_a_line_answering_someone_about_someone_else(monkeypatch):
    """Run 7 M1/M5/M6: a joke about Grand, a reply about the other person's
    cat, both written up as the member's own life."""
    monkeypatch.setattr(db, "member_aliases", lambda: {"grand": OTHER})
    _post(OTHER, "why did grand leave", 501)
    _post(ME, "Got goth chick pregnant", 501)
    parent = _post(OTHER, "my cat weighs 30 pounds", 502)
    _post(ME, "your cat is fatter than the fed balance sheet", 502, reply_to=parent)
    _post(OTHER, "what are you up to", 503)
    _post(ME, "Gym then picking up the kids", 503)
    text, dropped = G.apply(
        "**Recent personal life.**\n"
        "- Dad now: \"Got goth chick pregnant\" — [a family man]\n"
        "- Fat cat: \"your cat is fatter than the fed balance sheet\"\n"
        "- Gym dad: \"Gym then picking up the kids\"\n", ME, [])
    assert "goth" not in text and "Fat cat" not in text
    assert "Gym dad" in text and "answers someone about someone else" in " ".join(dropped)


def test_run7_verdicts_frequency_repeats_and_one_word_voice():
    """M2/M14 (the writer ruling a claim false), M17 ("constantly" from
    one message), M18 (a quote used twice), M19 (a one-word voice line)."""
    _post(ME, "SPACEX IS PUBLIC NOW BOYS", 510)
    _post(ME, "buying the dip on everything", 510)
    text, dropped = G.apply(
        "**Voice.**\n- \"retard\"\n- \"buying the dip on everything\"\n\n"
        "**Retarded takes.**\n"
        "- SpaceX: \"SPACEX IS PUBLIC NOW BOYS\" — [which was incorrect]\n"
        "- Dip addict: constantly \"buying the dip on everything\"\n", ME, [])
    reasons = " ".join(dropped)
    assert "SpaceX" not in text and "rules on whether a claim was true" in reasons
    assert "one-word voice line" in reasons
    assert "Dip addict" not in text and "repeats a quote" in reasons
    text, _ = G.apply("**Retarded takes.**\n- Dip addict: constantly \"buying the dip on everything\"\n", ME, [])
    assert "constantly" not in text and "Dip addict:" in text


def test_run7_kept_bullets_carry_their_date_and_who_they_replied_to():
    parent = _post(OTHER, "who is buying this", 520, name="Wock")
    _post(ME, "I bought the whole float of this garbage", 520, reply_to=parent)
    text, _ = G.apply("**Retarded takes.**\n- Bagholder: \"I bought the whole float of this garbage\"\n", ME, [])
    assert re.search(r"\(said [A-Z][a-z]{2} \d{1,2}, replying to Wock\)$", text)
    # a second pass replaces the tag, it does not stack one on another
    again, _ = G.apply(text, ME, [])
    assert again == text


def test_run7_old_two_word_quotes_acronyms_and_unit_figures():
    """M4 (a short quote months old as recent life), M10 (acronyms the
    member never wrote), M11 ("$16k" from a stray 16, "4 AM" from "at 4")."""
    _post(ME, "ski season", 530, days_ago=120)
    text, dropped = G.apply("**Recent personal life.**\n- Skier: \"ski season\"\n", ME, [])
    assert "Skier" not in text and "older than 60 days" in " ".join(dropped)
    hay = G._norm("lost 16 bucks, see you at 4")
    assert G.unsupported_terms("- Job — works in IT", hay, set()) == ["IT"]
    assert G.unsupported_terms("- Spent $16k on it", hay, set()) == ["$16k"]
    assert G.unsupported_terms("- Up at 4 AM", hay, set()) == ["4"]
    assert G.unsupported_terms("- Spent $16k on it", G._norm("dropped 16k on a watch"), set()) == []


def test_run7_a_pasta_with_a_double_space_is_still_a_pasta():
    """M3: an exact LIKE missed a meme pasted with a double space."""
    meme = ("WE ARE ALL GONNA MAKE IT  THE CANDLES WILL BE GREEN AND THE BEARS WILL BE BROKE, "
            "HOLD THE LINE BROTHERS AND SISTERS, THE MOON IS CLOSER THAN IT LOOKS TONIGHT OK, "
            "DIAMOND HANDS ONLY, PAPER HANDS GET LEFT AT THE STATION")
    _post(ME, meme, 540)
    assert G.is_copypasta("WE ARE ALL GONNA MAKE IT THE CANDLES", ME)
