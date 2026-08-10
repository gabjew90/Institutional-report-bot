"""Smoke: profile depth is decided at prompt assembly, not after a block.

Six filter fixes before this one all lived on the recovery path — the
prompt went out maximal and got trimmed only once Gemini rejected it.
This tests the premise change: the payload is chosen up front from what
the question actually needs.

The fixtures are the real ones. Every question below is copied verbatim
from an ask that shipped the "Gemini bounced this one" wrapper to a user
(2026-08-07 and 2026-08-09 ask-logs), and the profile fixture reproduces
the exact three-container shape db.format_user_profiles_for_context
renders: racism-signal metric + rationale in the header line, a regex
slur-examples block, and **Voice.** verbatim samples.

Unlike the ladder smokes (which assert that certain strings exist in the
source of _answer_with_gemini), these drive the real decision functions
and assert on the payload. The ungrounded-ladder regression found on
2026-08-10 shipped precisely because no test could observe behaviour.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Real rendered shape, slurs included — this is what actually rides the
# prompt today.
PROFILES = (
    '- **Tulch** (tulch, <@704361827290579084>) — _racism signal '
    '(humor:52/100, slurs:26) — The messages show regular casual use of '
    'ethnic and racial tropes as punchlines, pivoting straight from '
    'earnings talk into stereotyped references and slurs. · trader-rank '
    '#5/56 (Chat reads frantic and hyperactive, driven by earnings lotto '
    'tickets on high-beta tech.)_:\n'
    '  recent slur usage (regex fallback):\n'
    '    · where the cons nigga\n'
    '    · my nigga sold puts again\n'
    '**Tulch (tulch, <@704361827290579084>) — 3321 msgs**\n'
    '**Personality and style.**\n'
    'Tulch is a hyperactive software-sales scalper who treats the discord '
    'as his primary trading terminal.\n'
    '**Voice.**\n'
    '- "Where the cons nigga" — [demanding contract fills]\n'
    '- "I’m still green fuckasses" — [flexing his P&L]\n'
    '**Retarded takes.**\n'
    '- Sold puts at the bottom on SPX.\n'
    '**Recent trades.**\n'
    '- NVDA calls, closed red.\n'
)

# Verbatim from the ask-logs. All four shipped the failure wrapper.
BLOCKED_QUESTIONS = [
    "cna you summarize the last 12 hours of chat for me today",
    "explain to these peasants what the ticker COHR is, and when earnings is",
    "how can I long platinum and palladium via public equity options?",
    "reason for high volume in money markets are because of boomer "
    "population not wanting part in stock market?",
]

# Shapes where the voice material IS the product.
PERSON_QUESTIONS = [
    "roast tulch",
    "who is the worst trader in here",
    "what does tulch's profile say about him",
    "rank the room by pnl",
    "<@704361827290579084> explain yourself",
    "[MESSAGE BEING REPLIED TO — from omniwiz — user_id 1422761344322502807]\n"
    "you're wrong about that",
]

SLUR_TOKENS = ["nigga", "fuckasses"]


def test_blocked_questions_now_go_lean():
    import discord_bot.bot as bot
    for q in BLOCKED_QUESTIONS:
        needs, reason = bot._question_needs_person_material(q, PROFILES)
        if needs:
            _fail(f"still sends FULL profiles for {q[:50]!r} (reason={reason}) "
                  f"— this is one of the asks that got filter-blocked")
    _ok(f"all {len(BLOCKED_QUESTIONS)} previously-blocked asks now assemble LEAN")


def test_person_questions_keep_full_profiles():
    import discord_bot.bot as bot
    for q in PERSON_QUESTIONS:
        needs, reason = bot._question_needs_person_material(q, PROFILES)
        if not needs:
            _fail(f"would strip voice material for {q[:50]!r} — roasts and "
                  f"person questions must keep it")
    _ok(f"all {len(PERSON_QUESTIONS)} person-directed asks keep FULL profiles")


def test_lean_removes_all_three_bait_containers():
    import discord_bot.bot as bot
    lean = bot._lean_profiles_for_prompt(PROFILES)

    for tok in SLUR_TOKENS:
        if tok in lean.lower():
            _fail(f"LEAN profile still contains {tok!r} — a bait container "
                  f"survived the trim")
    if "racism signal" in lean.lower() or "humor:52" in lean:
        _fail("racism-signal metric survived — this container sits OUTSIDE "
              "**Voice.** and no ladder rung ever reached it")
    if "ethnic and racial tropes" in lean:
        _fail("racism rationale prose survived")
    if "recent slur usage" in lean.lower():
        _fail("regex slur-examples block survived")
    _ok("LEAN drops voice samples, racism signal + rationale, slur examples")


def test_lean_keeps_the_analytical_context():
    """The point of LEAN over question-only: the bot still knows who it
    is talking to."""
    import discord_bot.bot as bot
    lean = bot._lean_profiles_for_prompt(PROFILES)
    for keep in ("**Tulch**", "trader-rank #5/56",
                 "**Personality and style.**", "hyperactive software-sales",
                 "**Retarded takes.**", "**Recent trades.**"):
        if keep not in lean:
            _fail(f"LEAN dropped {keep!r} — that is analytical context, "
                  f"not filter bait")
    if "**Voice.**" not in lean:
        _fail("Voice header stub removed — the schema should stay intact")
    _ok("LEAN keeps identity, ranks, personality, takes and trades")


def test_lean_is_strictly_smaller():
    import discord_bot.bot as bot
    lean = bot._lean_profiles_for_prompt(PROFILES)
    if len(lean) >= len(PROFILES):
        _fail(f"LEAN is not smaller ({len(lean)} vs {len(PROFILES)})")
    _ok(f"LEAN is {len(PROFILES) - len(lean)} chars smaller than FULL")


def test_empty_profiles_are_safe():
    import discord_bot.bot as bot
    if bot._lean_profiles_for_prompt("") != "":
        _fail("empty profiles_block did not round-trip")
    needs, _ = bot._question_needs_person_material("roast me", "")
    if not needs:
        _fail("person shape must still register with no profiles loaded")
    _ok("empty profile block is handled")


def test_ladder_rungs_only_ever_shrink():
    """A retry must never re-add material assembly withheld. Both rungs
    that rebuild the prompt must source it from profiles_for_prompt."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    i = src.find("if safety_blocked or prompt_block:")
    j = src.find('_ask_meta["filter_retry"] = "failed"', i)
    ladder = src[i:j]
    rebuilds = [ln for ln in ladder.splitlines()
                if "_strip_voice_sections(" in ln or "_mask_slur_tokens(voice" in ln]
    if not rebuilds:
        _fail("no ladder rung rebuilds the prompt — test is stale")
    if "_strip_voice_sections(profiles_block)" in ladder:
        _fail("a ladder rung rebuilds from the FULL profiles_block — it "
              "would re-add voice/racism material that assembly withheld, "
              "escalating the payload on retry")
    if "profiles_for_prompt" not in ladder:
        _fail("ladder does not reference profiles_for_prompt")
    _ok("ladder rungs rebuild from the sent payload, never the full block")


if __name__ == '__main__':
    test_blocked_questions_now_go_lean()
    test_person_questions_keep_full_profiles()
    test_lean_removes_all_three_bait_containers()
    test_lean_keeps_the_analytical_context()
    test_lean_is_strictly_smaller()
    test_empty_profiles_are_safe()
    test_ladder_rungs_only_ever_shrink()
    print("\nAll profile-depth smoke tests passed.")
