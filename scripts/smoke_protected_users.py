"""Smoke: protected-members list — never insulted, always defended.

User request 2026-08-05: a special list of members the bot must never
insult, clap back at, or be sarcastic toward, and must defend and
praise instead.

Contract:
- List lives in the PROTECTED_USER_IDS env var, comma-separated Discord
  author IDs. Keyed by author_id, never display name — this room
  renames constantly and name-keying split one member into three on the
  trade scoreboard.
- When a protected member is the asker, is @-mentioned in the question,
  or is among the loaded profile subjects, a binding directive is
  appended to the system instruction (recency beats buried rules on
  flash-tier models — same mechanism as the FACT/ANALYSIS directives).
- The directive requires grounded praise only: material that exists in
  the dossier, no invented achievements (anchor-receipts discipline).
- The roast-rewrite guards (roast-recycle, pnl-monotone) are skipped
  outright when the asker is protected: those rewrites INJECT jabs
  (the 08-03 Boeing incident) and must never run on a protected asker.
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


def test_config_parses_ids():
    from config import Settings
    s = Settings(protected_user_ids=" 423994649317736448, 111 ,,bad, 222 ")
    assert s.protected_user_id_set == {423994649317736448, 111, 222}, (
        s.protected_user_id_set
    )
    s2 = Settings(protected_user_ids="")
    assert s2.protected_user_id_set == set()
    _ok("config parses PROTECTED_USER_IDS into an int set, ignores junk")


def test_scope_detection():
    import discord_bot.bot as bot
    prot = {111, 222}
    # asker protected
    assert bot._protected_in_scope(111, "what's SPY doing", [], prot) == {111}
    # mentioned user protected
    got = bot._protected_in_scope(
        999, "roast <@222> for me", [], prot)
    assert got == {222}, got
    # profile subject protected (replied-to author path)
    assert bot._protected_in_scope(999, "what a take", [222, 999], prot) \
        == {222}
    # nobody protected -> empty
    assert bot._protected_in_scope(999, "roast <@333>", [333], prot) == set()
    _ok("scope: asker, @-mention, and profile subjects detected by id")


def test_directive_content():
    import discord_bot.bot as bot
    d = bot._build_protected_directive({222}, asker_id=999,
                                       asker_display_name="BK")
    low = d.lower()
    for needle in ("never insult", "sarcas", "clap", "defend",
                   "roast me", "never invent"):
        assert needle in low, f"directive missing {needle!r}:\n{d}"
    assert "<@222>" in d, "directive must name the protected member by id"
    # asker-protected variant addresses the asker
    d2 = bot._build_protected_directive({999}, asker_id=999,
                                        asker_display_name="BK")
    assert "the asker" in d2.lower() or "BK" in d2, d2
    _ok("directive bans insult/clapback/sarcasm, requires grounded defense")


def test_roast_guards_skip_protected_asker():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    for marker, name in (("_prior_bot_answer_texts):", "roast-recycle"),
                         ("_roast_is_pnl_monotone(answer, profiles_block)):",
                          "P&L-monotone")):
        i = src.find(marker)
        assert i != -1, f"{name} guard condition not found"
        cond = src[max(0, i - 640):i + len(marker)]
        assert "_asker_protected" in cond, (
            f"the {name} rewrite injects jabs and must be skipped for a "
            f"protected asker"
        )
    _ok("roast rewrites cannot run on a protected asker's answer")


def test_directive_wired_into_prompt_extra():
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    assert "_protected_extra" in src, "directive never assembled"
    i = src.find("_prompt_extra = _fact_extra + _analysis_extra")
    assert i != -1 and "_protected_extra" in src[i:i + 120], (
        "_protected_extra must ride _prompt_extra so every retry that "
        "preserves directives (smoke_retry_keeps_directives) carries it"
    )
    assert '"protected-member"' in src, "guard stamp missing from ask_meta"
    _ok("directive rides _prompt_extra; retries inherit it; meta stamped")


if __name__ == "__main__":
    print("=== protected-users smoke ===")
    test_config_parses_ids()
    test_scope_detection()
    test_directive_content()
    test_roast_guards_skip_protected_asker()
    test_directive_wired_into_prompt_extra()
    print("\nALL PROTECTED-USERS SMOKE TESTS PASS")
