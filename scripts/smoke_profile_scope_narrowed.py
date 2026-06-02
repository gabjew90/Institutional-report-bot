"""Static smoke test that the narrowed profile-auto-load scope is in effect.

Validates:
  1. The slash command /ask path no longer pipes
     find_users_mentioned_in_text results into profile_ids
  2. The @mention handler path no longer pipes
     find_users_mentioned_in_text results into profile_ids
  3. The reply-parent name-mention scan no longer mutates profile_ids
  4. mentioned_ids extraction is still present (subject-verbatim block
     still gets to use literal name matches)
  5. _answer_with_gemini no longer assembles analyst_block (Task 9)
"""

import inspect
import sys
import re

import discord_bot.bot as bot_mod


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


# Pull the entire bot.py source so we can scan it as text.
SRC = inspect.getsource(bot_mod)


def test_slash_path_no_name_mention_in_profile_ids():
    """The profile_ids assembly should not pull mentioned_ids in."""
    profile_id_assigns = re.findall(
        r"profile_ids\s*=\s*list\(set\([^)]+\)\)", SRC
    )
    assert profile_id_assigns, "no profile_ids = list(set(...)) assignment found"
    for assign in profile_id_assigns:
        # mentioned_ids should NOT appear in any of these unions
        assert "mentioned_ids" not in assign, (
            f"profile_ids assignment still pulls mentioned_ids: {assign!r}"
        )
    _ok("no profile_ids = list(set(...)) assignment pulls mentioned_ids in")


def test_reply_parent_name_scan_no_longer_mutates_profile_ids():
    """The reply-parent ref_content scan should NOT add to profile_ids."""
    # Look for the specific pattern that was removed: a profile_ids =
    # list(set(profile_ids + [uid])) call inside a ref_content loop.
    bad_pattern = re.search(
        r"ref_content[^=]+?find_users_mentioned_in_text\([^)]+\)"
        r"[\s\S]*?profile_ids\s*=\s*list\(set\(profile_ids",
        SRC,
    )
    assert bad_pattern is None, (
        "reply-parent name scan still mutates profile_ids "
        f"(matched: {bad_pattern.group(0)[:200] if bad_pattern else None!r})"
    )
    _ok("reply-parent ref_content scan does NOT propagate into profile_ids")


def test_mentioned_ids_still_extracted():
    """Subject-verbatim should still work - mentioned_ids extraction via
    find_users_mentioned_in_text(question) stays."""
    assert (
        "find_users_mentioned_in_text(question)" in SRC
    ), "find_users_mentioned_in_text(question) call is gone — would break subject-verbatim"
    _ok("find_users_mentioned_in_text(question) still called (for subject-verbatim)")


def test_analyst_block_not_assembled():
    """_answer_with_gemini should no longer build the analyst_block."""
    src = inspect.getsource(bot_mod._answer_with_gemini)
    assert "analyst_blocks: list[str] = []" not in src, (
        "_answer_with_gemini still builds analyst_blocks - should be removed"
    )
    # sections.append should not include analyst_block
    assert "sections.append(analyst_block)" not in src, (
        "_answer_with_gemini still appends analyst_block to sections"
    )
    _ok("_answer_with_gemini no longer assembles analyst_block")


if __name__ == "__main__":
    print("=== profile-scope-narrowed static smoke ===")
    test_slash_path_no_name_mention_in_profile_ids()
    test_reply_parent_name_scan_no_longer_mutates_profile_ids()
    test_mentioned_ids_still_extracted()
    test_analyst_block_not_assembled()
    print("\nALL PROFILE-SCOPE SMOKE TESTS PASS")
