"""Smoke test: /ask prompt CONTRACT — the load-bearing (b) rules survive.

This is the guard for a voice overhaul. The /ask system prompt mixes
voice (free to rewrite) with data-correctness / tool / no-fabrication
rules — and a chunk of the latter is buried INSIDE the voice sections
wearing a voice-rule costume (see ask_prompt_voice_triage.md). A naive
"delete the voice walls" rewrite silently drops those and breaks the
numbers or makes the bot fabricate.

Each anchor below is a frozen (b) rule. If a rewrite drops one, this
fails LOUDLY and names exactly which rule went missing. When you rewrite
voice, keep these verbatim (they're the contract); everything not
anchored here is yours to rework.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# (anchor substring that must remain verbatim, human name of the rule).
_CONTRACT = [
    # --- the 8 tools (names must match the code-defined tools) ---
    ("lookup_market_price", "tool: lookup_market_price"),
    ("lookup_options_chain", "tool: lookup_options_chain"),
    ("lookup_economic_calendar", "tool: lookup_economic_calendar"),
    ("lookup_earnings_date", "tool: lookup_earnings_date"),
    ("lookup_trade_log", "tool: lookup_trade_log"),
    ("lookup_user_profile", "tool: lookup_user_profile"),
    ("search_chat_messages", "tool: search_chat_messages"),
    ("Google Search", "tool: Google Search"),
    # --- context + routing scaffolding ---
    ("ALWAYS-ON CONTEXT", "the 4 always-on context streams"),
    ("HARD ROUTING RULES", "price->tool hard routing"),
    # --- no-fabrication guards (read like voice, are FACTS) ---
    ("ZERO UNFORCED PRICE ASSERTIONS", "no unforced price levels"),
    ("ZERO UNFORCED MARKET-DATA ASSERTIONS", "no unforced OI/IV/macro stats"),
    ("ZERO UNFORCED TRADE-OUTCOME ASSERTIONS", "no fabricated trade outcomes"),
    ("ZERO UNFORCED TIME-SERIES CLAIMS", "no fabricated trends from snapshots"),
    ("NO SELF-GENERATED TECHNICAL ANALYSIS", "no invented RSI / chart levels"),
    ("DO NOT CONFABULATE SPECIFICS", "no invented unlock/float/ticker specifics"),
    # --- data-reading correctness ---
    ("CONV-SCOPED rank", "conv-scoped rank != global leaderboard"),
    ("chat_stated_trades", "trade-log: chat-stated trades / never 'you did nothing'"),
    ("Never invent positions", "callers: never invent positions/words"),
    ("Scores (0-100): hidden", "metrics: ranks shareable, raw scores hidden"),
    ("authoritative source for who you", "asker identity = the separator line"),
    ("distinct from the ASKER", "subject of the question != asker"),
    ("tied to the user who actually said it", "attribute material to the right user"),
    # --- conflict-priority anti-fabrication ---
    ("Don't fabricate", "priority #1: don't fabricate"),
    ("add new specifics under pressure", "don't invent new specifics under challenge"),
]

# Tombstones: anchors DELIBERATELY removed from the prompt by the repo
# owner. Recorded here rather than silently deleted, so the history stays
# legible and the guard stays green-when-intact — a permanently-red guard
# masks real regressions (exactly what it must catch during a rewrite).
# If a tombstoned rule is ever re-added to the prompt, move it back into
# _CONTRACT.
_TOMBSTONES = [
    # Removed by owner 2026-06-25 (commit f450537f, "Refactor bot
    # guidelines for clarity"): the no-racial/protected-class-slurs-in-
    # bot-voice line from "Match the room's register." The removal was
    # the owner's explicit, repeated decision.
    ("protected-class slurs", "bot's own voice never deploys racial/protected-class slurs"),
]


def test_contract_rules_present():
    import discord_bot.bot as bot_mod
    ins = bot_mod._ASK_SYSTEM_INSTRUCTION
    missing = [(anchor, name) for anchor, name in _CONTRACT if anchor not in ins]
    if missing:
        lines = "\n".join(f"  - {name}   (anchor: {anchor!r})" for anchor, name in missing)
        _fail(
            f"{len(missing)} load-bearing /ask rule(s) dropped from the prompt:\n"
            f"{lines}\n"
            f"These are the (b) contract — keep them verbatim through a voice "
            f"rewrite. See ask_prompt_voice_triage.md."
        )
    _ok(f"/ask prompt contract intact — all {len(_CONTRACT)} load-bearing "
        f"rules present")


def test_contract_count_guard():
    # If the contract list itself shrinks, that's a deliberate change — make
    # it visible so nobody quietly removes an anchor from the guard too.
    # 25 required anchors + 1 documented tombstone (see _TOMBSTONES).
    assert len(_CONTRACT) >= 25, (
        f"contract anchor list shrank to {len(_CONTRACT)} — removing an "
        f"anchor weakens the guard; do it deliberately, not silently"
    )
    assert len(_TOMBSTONES) >= 1, "tombstone history must not be deleted"
    _ok(f"contract guard covers {len(_CONTRACT)} anchors (>=25) "
        f"+ {len(_TOMBSTONES)} documented tombstone(s)")


if __name__ == "__main__":
    print("=== /ask prompt contract smoke ===")
    test_contract_rules_present()
    test_contract_count_guard()
    print("\nALL /ASK PROMPT CONTRACT SMOKE TESTS PASS")
