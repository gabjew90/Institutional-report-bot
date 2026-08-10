"""Smoke: the filter-block ladder drops function tools but KEEPS google_search.

2026-08-07 (commit 07faeb86) fixed a real bug — the ladder was resending
with the full tools list, the model replied with a function_call it could
never execute, `.text` came back empty, and the ladder read that as another
block. The fix nulled `config.tools`.

It nulled too much. google_search rides in the SAME tools list as the
function declarations, so every ladder tier went out ungrounded. The asks
that hit the ladder over 2026-08-07..09 were "when is COHR earnings",
"how can I long platinum and palladium via public equity options", and
"reason for high volume in money markets" — factual questions where search
IS the answer. Recovering the ask while silently dropping grounding just
trades one failure mode for another.

google_search is safe to keep: it resolves server-side and returns text,
never a function_call, so it cannot cause the empty-.text failure the strip
exists to prevent.
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


def test_ladder_config_keeps_search_drops_functions():
    """Behavioural check against the real SDK types: apply the ladder's
    own filter to a config shaped like the live one."""
    from google.genai import types
    import discord_bot.bot as bot

    config = types.GenerateContentConfig(
        tools=[
            types.Tool(google_search=types.GoogleSearch()),
            bot._build_chat_search_tool(),
            bot._build_user_profile_tool(),
            bot._build_market_price_tool(),
        ],
        tool_config=types.ToolConfig(
            include_server_side_tool_invocations=True,
        ),
    )

    ladder_tools = [
        t for t in (config.tools or [])
        if getattr(t, "google_search", None) is not None
    ] or None

    if not ladder_tools:
        _fail("google_search was dropped from the ladder config — every "
              "ladder tier will answer ungrounded")
    if len(ladder_tools) != 1:
        _fail(f"expected exactly 1 search tool, kept {len(ladder_tools)}")
    for t in ladder_tools:
        if getattr(t, "function_declarations", None):
            _fail("a function-declaring tool survived into the ladder config "
                  "— this is the empty-.text failure 07faeb86 fixed")
    _ok("ladder config keeps google_search, drops all function tools")


def test_ladder_source_does_not_null_tools():
    """Guard the regression directly: `"tools": None` in the ladder config
    is the exact line that cost grounding."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    i = src.find("_ladder_config = config.model_copy")
    if i == -1:
        _fail("ladder config construction not found")
    window = src[max(0, i - 400): i + 500]
    if '"tools": None' in window:
        _fail('ladder config sets "tools": None — google_search goes with '
              'the function tools and every tier answers ungrounded')
    if 'google_search' not in window:
        _fail("ladder config does not mention google_search — it must "
              "explicitly preserve the search tool")
    _ok("ladder config does not null the tools list")


def test_tool_config_survives_with_search():
    """include_server_side_tool_invocations is what surfaces grounding
    records; it must ride along whenever search is offered."""
    import discord_bot.bot as bot
    src = inspect.getsource(bot._answer_with_gemini)
    i = src.find("_ladder_config = config.model_copy")
    window = src[i: i + 600]
    if 'config.tool_config' not in window:
        _fail("tool_config is not carried over — grounding metadata from "
              "the ladder's search calls will not be reported")
    _ok("tool_config carried over while search is offered")


if __name__ == '__main__':
    test_ladder_config_keeps_search_drops_functions()
    test_ladder_source_does_not_null_tools()
    test_tool_config_survives_with_search()
    print("\nAll filter-ladder grounding smoke tests passed.")
