"""Subject resolution for short handles (2026-08-27, review session 1).

The >=3-char floor exists because two-letter handles collide with
ordinary words ("bk" inside "back" would be a match without word
boundaries, and even bounded, short tokens fire on noise). But the
floor as originally written also blocked EXPLICIT @-mentions, so
members named BK or Ry could never be a question's subject and their
questions fell back to asker-scoped receipts — the Tulch misscoping,
made permanent for every short-handle member. This is the same >=3
assumption that turned out to be the fixture-27 harness bug; the
production copy was never revisited until this review.

The fix: an @-mention matches at any length (mention resolution
already ran upstream, so "@BK" is a deliberate reference), while
bare-text matching keeps the >=3 floor.
"""
import sys

from discord_bot.bot import _roast_subjects

PROFILES = (
    "- **Sam** (theorb_18574, <@318466418301730816>) — rank 21\n"
    "stuff about sam\n"
    "- **BK** (bankerkyle, <@423994649317736448>) — rank 4\n"
    "stuff about bk\n"
    "- **Tulch** (tulch, <@111>) — rank 30\n"
    "stuff about tulch\n"
)


def _subjects(q, asker_uname="theorb_18574", asker_disp="Sam"):
    return _roast_subjects(q, PROFILES, asker_uname, asker_disp)


def test_short_handle_at_mention_matches():
    """THE FIX: '@BK' resolves BK as the subject."""
    assert ("BK", "bankerkyle") in _subjects("is @BK cooked or what")


def test_short_handle_bare_text_still_does_not_match():
    """The >=3 floor stays for bare text — 'bk' with no @ is too
    collision-prone to treat as a reference."""
    assert _subjects("is bk cooked or what") == []


def test_short_handle_inside_word_does_not_match():
    assert _subjects("checking my @BKX index chart") == []


def test_long_handle_bare_text_matches():
    """Unchanged behaviour: >=3-char handles match without the @."""
    assert ("Tulch", "tulch") in _subjects("is tulch still fading everything")


def test_long_username_matches():
    assert ("BK", "bankerkyle") in _subjects("what is bankerkyle holding")


def test_asker_is_never_their_own_subject():
    assert _subjects("is @Sam cooked", asker_uname="theorb_18574",
                     asker_disp="Sam") == []


def test_order_follows_the_question():
    """First-named is primary — the 2026-08-12 two-subject fix must
    survive the short-handle change."""
    got = _subjects("is tulch worse than @BK")
    assert got[0] == ("Tulch", "tulch")
    assert got[1] == ("BK", "bankerkyle")
    got = _subjects("is @BK worse than tulch")
    assert got[0] == ("BK", "bankerkyle")


if __name__ == "__main__":
    sys.exit("run via: py -3.12 tests/run_tests.py")
