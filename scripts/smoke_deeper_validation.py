"""Deeper validation than the unit smokes.

Catches the kinds of issues that mocked tests miss:

  1. Each new tool's FunctionDeclaration round-trips through the Gemini
     SDK's serialization. If the schema is malformed (e.g. unsupported
     type, recursive ref), the SDK errors here — not at runtime when
     a /ask call goes out.

  2. _ASK_SYSTEM_INSTRUCTION compiles cleanly and the runtime
     instruction builder runs without error.

  3. Tool-call dispatch handles each new tool name without
     missing-case fallthrough.

  4. Each executor runs with realistic args and returns the documented
     response shape (NOT just 'is dict?' — actually has the keys
     the model needs).
"""

import asyncio
import sys
from unittest.mock import patch


def _ok(msg):
    print(f"PASS {msg}")


def _fail(msg):
    print(f"FAIL {msg}")
    sys.exit(1)


def test_tool_schemas_serialize():
    """Tool defs must serialize through the SDK without error."""
    from discord_bot import bot as bot_mod
    from google.genai import types

    for builder_name in (
        "_build_chat_search_tool",
        "_build_user_profile_tool",
        "_build_trade_log_tool",
        "_build_market_price_tool",
    ):
        builder = getattr(bot_mod, builder_name)
        tool = builder()
        # Force the SDK to walk the schema. model_dump is the pydantic
        # path the SDK uses internally; if it raises, the API call
        # would fail too.
        try:
            payload = tool.model_dump(mode="json", exclude_none=True)
        except Exception as e:
            _fail(f"{builder_name}: schema serialization failed: {type(e).__name__}: {e}")
        # Must have function_declarations populated
        if not payload.get("function_declarations"):
            _fail(f"{builder_name}: serialized payload missing function_declarations")
        decl = payload["function_declarations"][0]
        if not decl.get("name") or not decl.get("description"):
            _fail(f"{builder_name}: decl missing name or description")
        # Schema must declare type=OBJECT for parameters
        params = decl.get("parameters") or {}
        if params.get("type") not in ("OBJECT", "object"):
            _fail(f"{builder_name}: parameters.type is {params.get('type')!r}, not OBJECT")
        print(f"  serialized {builder_name}: {len(str(payload))} chars, "
              f"params={list((params.get('properties') or {}).keys())}")
    _ok("all 4 tool schemas serialize cleanly via SDK model_dump")


def test_system_instruction_builds():
    """Runtime prompt-builder should produce a non-empty string."""
    from discord_bot import bot as bot_mod
    instr = bot_mod._build_runtime_system_instruction()
    assert isinstance(instr, str), f"instruction not str: {type(instr)}"
    assert len(instr) > 1000, f"instruction implausibly short: {len(instr)} chars"
    # Must mention each new tool name
    for tool_name in (
        "lookup_user_profile",
        "lookup_trade_log",
        "lookup_market_price",
        "search_chat_messages",
    ):
        assert tool_name in instr, f"instruction missing reference to {tool_name!r}"
    # Must NOT mention the dead tool name
    assert "lookup_user_ranks" not in instr, (
        "instruction still references the dead tool lookup_user_ranks"
    )
    print(f"  instruction: {len(instr)} chars; all 4 tool names present; "
          f"no dead-tool refs")
    _ok("_build_runtime_system_instruction() produces clean prompt")


def test_executor_end_to_end_shapes():
    """Each executor returns the documented response shape."""
    import discord_bot.bot as bot_mod

    # lookup_user_profile — username mode with mock DB
    with patch("db.lookup_user_ranks", return_value={
        "users": [{
            "user_id": 12345, "username": "bk", "display_name": "BK",
            "trader_rank": 1, "trader_total": 53,
            "racism_rank": 1, "racism_total": 3,
            "trader_rationale": "loud", "racism_rationale": "loud",
        }],
        "count": 1,
        "mode": "by_username",
    }):
        result = asyncio.run(bot_mod._execute_user_profile({"username": "bk"}))
    assert "users" in result and result["users"], result
    assert "user_id" in result["users"][0], result["users"][0]
    print(f"  user_profile result keys: {list(result.keys())}")
    _ok("user_profile executor returns shape with users + count")

    # lookup_trade_log — caller anchor with mock DB
    with (
        patch("db.format_analyst_trades_for_context", return_value="trades data"),
        patch("config.Settings.resolve_analyst_callers", return_value=[
            {"name": "abe", "display": "Abe"},
        ]),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({
            "caller": "abe", "kind": "open",
        }))
    for k in ("anchor", "kind", "data_quality", "trades_text"):
        assert k in result, f"trade_log missing key {k!r}: {result}"
    print(f"  trade_log caller result keys: {list(result.keys())}")

    # lookup_trade_log — username anchor
    with (
        patch("db.resolve_username_to_user_id", return_value=12345),
        patch("db.get_user_profile_recent_trades_section", return_value="snippet"),
    ):
        result = asyncio.run(bot_mod._execute_trade_log({"username": "x"}))
    for k in ("anchor", "kind", "data_quality", "profile_recent_trades"):
        assert k in result, f"trade_log username missing key {k!r}: {result}"
    print(f"  trade_log username result keys: {list(result.keys())}")
    _ok("trade_log executor returns documented shape for both anchors")

    # lookup_market_price — stock + crypto + unknown
    with (
        patch(
            "report.market_data._fetch_finnhub_quote",
            side_effect=lambda sym: (
                {"price": 100.0, "prev_close": 99.0, "change_pct": 1.0}
                if sym == "SPY" else None
            ),
        ),
        patch(
            "report.market_data._fetch_binance_24h",
            return_value={"price": 50000.0, "change_24h_rolling": -2.0},
        ),
        patch("report.market_data._session_label", return_value=("OPEN", "note")),
    ):
        result = asyncio.run(bot_mod._execute_market_price({
            "symbols": ["SPY", "BTC", "ASDF"],
        }))
    for k in ("session", "timestamp", "quotes"):
        assert k in result, f"market_price missing key {k!r}: {result}"
    assert len(result["quotes"]) == 3
    print(f"  market_price result keys: {list(result.keys())}, "
          f"quotes={len(result['quotes'])}")
    _ok("market_price executor returns documented shape with session+quotes")

    # search_chat_messages Shape C — username only
    with patch("db.search_chat_messages_for_ask", return_value=[
        {"posted_at": "2026-06-01T15:00", "content": "test"},
    ]):
        result = asyncio.run(bot_mod._execute_chat_search({
            "username": "bk", "days": 1,
        }))
    assert "matches" in result, f"chat_search missing matches: {result}"
    print(f"  chat_search Shape C result keys: {list(result.keys())}")
    _ok("chat_search Shape C executor returns matches list")


def test_dispatch_handles_all_new_tools():
    """The dispatch must wire each tool. Refactored 2026-06-10 from an
    if/elif chain to a guarded `_tool_executors` map — assert the map
    entry exists (a missing entry routes to 'unknown tool' error)."""
    import inspect
    from discord_bot import bot as bot_mod
    src = inspect.getsource(bot_mod._answer_with_gemini)
    for tool_name in (
        "search_chat_messages",
        "lookup_user_profile",
        "lookup_trade_log",
        "lookup_market_price",
    ):
        anchored = (
            f'"{tool_name}":' in src
        )
        assert anchored, (
            f"dispatch executor map missing entry for {tool_name!r} — "
            f"calls would fall through to 'unknown tool' error"
        )
    _ok("dispatch executor map has an entry for every tool in the tools list")


def test_no_orphan_references():
    """After Task 4, _build_user_ranks_tool / _execute_user_ranks should
    be entirely gone. After Task 9, analyst_block reference (other than
    a comment) should be gone."""
    from discord_bot import bot as bot_mod
    import inspect
    src = inspect.getsource(bot_mod)
    assert "def _build_user_ranks_tool" not in src
    assert "async def _execute_user_ranks" not in src
    # analyst_block: only allowed inside a comment line
    for line in src.splitlines():
        stripped = line.lstrip()
        if "analyst_block" in line and not stripped.startswith("#"):
            _fail(
                f"non-comment line still references analyst_block: {line.strip()!r}"
            )
    _ok("no orphan references to dead symbols (analyst_block, _build_user_ranks_tool)")


if __name__ == "__main__":
    print("=== Deeper end-to-end validation ===")
    test_tool_schemas_serialize()
    test_system_instruction_builds()
    test_executor_end_to_end_shapes()
    test_dispatch_handles_all_new_tools()
    test_no_orphan_references()
    print("\nALL DEEPER VALIDATION TESTS PASS")
