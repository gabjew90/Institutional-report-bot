"""Smoke test: /ask prompt diet — behavior anchors survive, bloat doesn't.

Context (2026-07-27 prompt review): the system prompt had grown to
~95K chars (~24K tokens) by incident accretion — every dated failure
got a narrative paragraph. The 07-08 diagnosis in bot.py already tied
prompt weight to grounding skips ("the model answers from that context
and its priors instead"). The diet keeps every RULE and moves the
incident stories to code comments.

Two guards:
  1. CONCEPT ANCHORS — one distinctive substring per rule family that
     must survive the diet (complements smoke_ask_prompt_contract.py,
     which freezes the 25 data-correctness anchors verbatim).
  2. SIZE CEILING — the prompt must stay under the ceiling so it can't
     silently re-bloat one incident paragraph at a time. New rules are
     fine; they must displace narrative, not stack on it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


_SIZE_CEILING = 65_000  # chars (~16K tokens). Was 94,795 pre-diet.

# (anchor substring, rule family it proves survived)
_CONCEPT_ANCHORS = [
    # framing
    ("ghost writer", "ghost-writer framing (filter-critical — see memory)"),
    ("options-alert service", "customer-respect framing"),
    ("NEVER META-NARRATE", "no plumbing talk"),
    # types + format
    ("TYPE 1", "type taxonomy"),
    ("TYPE 2", "type taxonomy"),
    ("TYPE 3", "type taxonomy"),
    ("Quick read", "depth tiers"),
    ("Full DD", "depth tiers"),
    ("→", "literal arrow-bullet format"),
    ("No emojis", "emoji ban"),
    ("THE VOICE", "voice register spec"),
    ("default down", "type-3 trigger discipline"),
    ("personal color beats P&L", "roast material hierarchy"),
    ("fresh material", "anti-recycling"),
    ("Closure messages get closure replies", "closure handling"),
    # secrecy + presentation
    ("Never narrate them", "instruction secrecy"),
    ("Don't acknowledge being a bot", "no bot self-reference"),
    ("No apologizing", "no apologies"),
    ("NEVER cite your context blocks", "no context-block citations"),
    # data-adjacent behavior
    ("Company filings", "source-quality hierarchy"),
    ("Recency is its own trigger", "recency search trigger"),
    ("no clean consensus", "uncertainty handling"),
    ("Bad-faith framing", "bad-faith questions still get answers"),
    # 2026-07-30: "@omniwiz how much did citadel make today" came back
    # as a Type 2 banter paragraph instead of Type 1 arrows. Citadel
    # doesn't publish daily P&L, so the question matched Type 2's old
    # trigger wording ("no clean factual answer") literally. Undisclosed
    # != subjective.
    ("Undisclosed isn't subjective", "unavailable data stays Type 1"),
    ("QUOTATION", "verbatim quotation rule"),
    ("CHART COMMANDS", "fc-command lexicon"),
    ("Match by username", "username-keyed attribution"),
    ("top 5", "leaderboard cap"),
    ("how do I climb", "rank-climb exception"),
    ("Multi-position list format", "caller book format"),
    ("PRIORITY ORDER", "conflict priority list"),
]


def test_concept_anchors_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    missing = [(a, n) for a, n in _CONCEPT_ANCHORS if a not in ins]
    if missing:
        lines = "\n".join(f"  - {n}   (anchor: {a!r})" for a, n in missing)
        _fail(f"{len(missing)} rule famil(ies) dropped by the diet:\n{lines}")
    _ok(f"all {len(_CONCEPT_ANCHORS)} concept anchors present")


def test_size_ceiling():
    import discord_bot.bot as bot_mod
    n = len(bot_mod._ASK_SYSTEM_INSTRUCTION)
    assert n <= _SIZE_CEILING, (
        f"/ask system prompt is {n} chars (ceiling {_SIZE_CEILING}). "
        f"New rules must displace narrative, not stack on it — move "
        f"incident stories to the code-comment ledger in ask_prompt.py."
    )
    _ok(f"prompt size {n} chars <= {_SIZE_CEILING} ceiling")


if __name__ == "__main__":
    print("=== /ask prompt diet smoke ===")
    test_concept_anchors_present()
    test_size_ceiling()
    print("\nALL /ASK PROMPT DIET SMOKE TESTS PASS")
