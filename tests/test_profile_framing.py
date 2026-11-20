"""The writer's words around a real quote (scripts/profile_guards.py).

2026-11-19, audit run 13: every quote in 22 profiles traced to its member,
and the failures were the framing. About 28 bullets called one message a
habit ("his go-to excuse", "Obsessed with protein intake"), and about 22
lines ruled on a quote or claimed the room's reaction ("a baseless
claim", "though the room remains skeptical"). The framings below are the
writer's, with the quotes replaced.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import profile_guards as G  # noqa: E402


def test_a_verdict_adjective_goes_and_the_quote_stays():
    assert G.soften('- "x y" — [a baseless, racially-charged take on AI market dynamics]') == (
        '- "x y" — [a racially-charged take on AI market dynamics]')
    assert G.soften('- "x y" — [his delusional claim to explain a market move]') == (
        '- "x y" — [his claim to explain a market move]')
    assert G.soften('- "x y" — [his immediate, transparent deflection]') == (
        '- "x y" — [his immediate deflection]')
    # the article is fitted to the word that now follows it
    assert G.soften('- "x y" — [an absurd, baseless idea]') == '- "x y" — [an idea]'
    assert G.soften('- "x y" — [an absurd take on rates]') == '- "x y" — [a take on rates]'
    # the member's own words are never touched, and "transparent" is a
    # verdict only in front of what it rules on
    line = '- "his baseless absurd take" — [a framing of the transparent glass]'
    assert G.soften(line) == line


def test_a_tail_about_the_rooms_reaction_goes():
    assert G.soften('- Claims access to firms — "a b" + [drops names to sound like a mogul, though the '
                    'room remains skeptical]') == (
        '- Claims access to firms — "a b" + [drops names to sound like a mogul]')
    assert G.soften('- "a b" + [posts skyline photos to prove he is there, despite the room questioning '
                    'his actual whereabouts]') == '- "a b" + [posts skyline photos to prove he is there]'
    assert G.soften('- "a b" — [his attempt at humor that just made the room cringe]') == (
        '- "a b" — [his attempt at humor]')
    # what the member did toward the room is his own conduct
    line = '- "a b" — [asks the room for real estate advice while complaining about the cost of living]'
    assert G.soften(line) == line


def test_one_message_is_not_a_habit():
    assert G._drop_habit('- "a b" — his go-to excuse for poor trading decisions.') == (
        '- "a b" — his excuse for poor trading decisions.')
    assert G._drop_habit('- "a b" — [his standard reaction to a losing trade]') == (
        '- "a b" — [his reaction to a losing trade]')
    assert G._drop_habit('- "a b" — his usual, go-to excuse.') == '- "a b" — his excuse.'
    assert G._drop_habit('- "a b" — [a classic contradiction where he dismisses a setup]') == (
        '- "a b" — [a contradiction where he dismisses a setup]')
    # one message shows that he posted about it, not that he is obsessed
    assert G._drop_habit('- Obsessed with protein intake — "a b" + [mocks meal prep]') == (
        '- Posted about protein intake — "a b" + [mocks meal prep]')
    assert G._drop_habit('- Obsessively tracks every move — "a b"') == '- Tracks every move — "a b"'
    # inside the member's quote nothing changes
    line = '- "my go-to is the standard obsessed with tacos"'
    assert G._drop_habit(line) == line
