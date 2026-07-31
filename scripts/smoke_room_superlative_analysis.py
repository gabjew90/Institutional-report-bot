"""Smoke: "who's the most X in the chat" must fire the PYTHON directive.

2026-07-30 — BK asked "who are the happiest people in the chat? How
about the angriest" and got a one-line joke about himself: no names, no
data, no code. A prompt rule saying "room superlatives are Type 1" does
not help, because the thing that actually forces Python is
_is_analysis_request() -> _ASK_ANALYSIS_DIRECTIVE, and that gate keys on
explicit keywords (analyze / compare / correlate / win rate / chart the).
A superlative question about the room carries none of them, so the
directive never attached and the model was free to banter.

Ranking the room by a trait IS analysis — pull the messages, score the
trait per author, rank, chart it. That is exactly what the code sandbox
was enabled for.

Guard against over-firing too: the shape has to be a question about
PEOPLE IN THIS ROOM, not any sentence containing a superlative.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


FIRES = [
    "who are the happiest people in the chat? How about the angriest",
    "who's the most bearish in the room",
    "who posts the most",
    "who is the angriest here",
    "who在 the chat talks the most about crypto".replace("在", " in "),
    "rank everyone in the chat by how much they swear",
    "who's the funniest member",
    "top 5 angriest people in the server",
]

# Must NOT fire: superlatives that aren't about ranking this room.
QUIET = [
    "who's the best CEO in tech",
    "what's the best entry here",
    "who is BK",
    "what's the highest SPY has been this year",
    "who won the game last night",
    "best whiskey under $50",
    "who's going to win the election",
]


def test_room_superlatives_fire():
    import discord_bot.bot as bot
    missed = [q for q in FIRES if not bot._is_analysis_request(q)]
    if missed:
        lines = "\n".join(f"  - {q!r}" for q in missed)
        _fail(
            f"{len(missed)} room-superlative question(s) did NOT trigger "
            f"the Python directive:\n{lines}"
        )
    _ok(f"all {len(FIRES)} room-superlative questions fire the directive")


def test_non_room_superlatives_stay_quiet():
    import discord_bot.bot as bot
    fired = [q for q in QUIET if bot._is_analysis_request(q)]
    if fired:
        lines = "\n".join(f"  - {q!r}" for q in fired)
        _fail(
            f"{len(fired)} question(s) wrongly triggered analysis — a "
            f"superlative alone is not a ranking of this room:\n{lines}"
        )
    _ok(f"all {len(QUIET)} non-room questions stay quiet")


def test_existing_keywords_still_fire():
    """Don't regress the original detector."""
    import discord_bot.bot as bot
    for q in ("analyze the trade log",
              "compare BK and abe win rates",
              "chart the correlation"):
        assert bot._is_analysis_request(q), q
    _ok("original analysis keywords still fire")


def test_superlative_followup_is_sticky():
    """The SECOND message was "How about the happiest" — a refinement of
    a ranking that had just been posted. It carries no superlative
    subject of its own, so it must stick via the reply context."""
    import discord_bot.bot as bot
    q = (
        "[MESSAGE BEING REPLIED TO - from omniwiz]\n"
        "→ **The angriest:** SV, 41 of his last 200 messages are "
        "complaints about fills\n\n"
        "[bearishkyle's message to you]\n"
        "How about the happiest"
    )
    assert bot._is_analysis_request(q), (
        "a follow-up refining a just-posted ranking must stay in "
        "analysis mode — otherwise half the exchange gets code and "
        "charts and the other half gets banter"
    )
    _ok("superlative follow-up stays in analysis mode")


if __name__ == "__main__":
    print("=== room-superlative analysis-directive smoke ===")
    test_room_superlatives_fire()
    test_non_room_superlatives_stay_quiet()
    test_existing_keywords_still_fire()
    test_superlative_followup_is_sticky()
    print("\nALL ROOM-SUPERLATIVE SMOKE TESTS PASS")
